import os

import pytest

from iosql import Database, Partition, Segment
from iosql.exception import BadArgument, UnsafeOperation
from iosql.partition import RangePartition


def _uri(tmp_path, name="partitioned.iosql"):
    return f"sqlite://{tmp_path}/{name}"


def test_range_partition_contract_has_no_time_key_and_prunes(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "metrics",
        pk=["seq", "symbol"],
        columns={"seq": "integer", "symbol": "text", "value": "real"},
        partition=Partition.range("seq", every=1000),
    )

    result = table.insert_all([
        {"seq": 10, "symbol": "A", "value": 1.0},
        {"seq": 1010, "symbol": "A", "value": 2.0},
        {"seq": 2010, "symbol": "A", "value": 3.0},
    ])

    assert result.rows_seen == 3
    assert table.table_parts() == [0, 1000, 2000]
    assert "time" not in table.get_table_meta()["table_contract"]

    rows = table.select(
        "*",
        where={"field": "seq", "op": "between_and", "value": [1000, 1999]},
        order_by="seq",
        to_dict=True,
    )
    assert rows == [{"seq": 1010, "symbol": "A", "value": 2.0}]

    projected = table.select(
        ["symbol", "value"],
        where=None,
        order_by="seq desc",
        to_dict=True,
    )
    assert projected == [
        {"symbol": "A", "value": 3.0},
        {"symbol": "A", "value": 2.0},
        {"symbol": "A", "value": 1.0},
    ]
    assert "seq" not in projected[0]

    plan = table.explain_select(
        where={"field": "seq", "op": "between_and", "value": [1000, 1999]},
    ).to_dict()
    assert plan["candidate_partitions"] == [1000]
    assert plan["scanned_partitions"] == [1000]
    assert plan["skipped_partitions"] == [0, 2000]
    assert [item["logical_part"] for item in plan["candidate_segments"]] == [1000]

    db.close()


def test_insert_all_computes_partition_key_once_per_row(tmp_path):
    class CountingRangePartition(RangePartition):
        def __init__(self):
            super().__init__(width=1000, column="seq")
            self.calls = 0

        def get_key(self, row):
            self.calls += 1
            return super().get_key(row)

    db = Database(_uri(tmp_path))
    table = db["metrics"]
    partition = CountingRangePartition()
    rows = [
        {"seq": 10, "value": 1.0},
        {"seq": 1010, "value": 2.0},
        {"seq": 2010, "value": 3.0},
    ]

    result = table.insert_all(
        rows,
        pk=["seq"],
        partition=partition,
        column_type={"seq": "integer", "value": "real"},
    )

    assert partition.calls == len(rows)
    assert result.partitions_touched == [0, 1000, 2000]
    db.close()


def test_row_segment_delete_does_not_reopen_sealed_segment(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=2),
        order_key="seq",
    )

    table.write([
        {"event_id": 1, "seq": 1, "kind": "base"},
        {"event_id": 2, "seq": 2, "kind": "base"},
    ])
    assert table.segments()[0]["sealed"] is True

    deleted = table.delete({"field": "event_id", "op": "==", "value": 1})
    assert deleted == 1
    assert table.segments()[0]["row_count"] == 1
    assert table.segments()[0]["sealed"] is True

    table.write({"event_id": 99, "seq": 0, "kind": "late"})
    segments = table.segments()
    assert [item["row_count"] for item in segments] == [1, 1]
    assert [item["sealed"] for item in segments] == [True, False]

    db.close()


def test_row_segment_rejects_upsert_and_replace_until_pk_index(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        segment=Segment.rows(max_rows=2),
        order_key="seq",
    )

    table.write({"event_id": 1, "seq": 1})
    with pytest.raises(BadArgument, match="Segment.rows"):
        table.write({"event_id": 1, "seq": 2}, mode="upsert")
    with pytest.raises(BadArgument, match="Segment.rows"):
        table.write({"event_id": 1, "seq": 3}, mode="replace")
    with pytest.raises(BadArgument, match="Segment.rows"):
        table.insert_all({"event_id": 1, "seq": 4}, replace=True)

    assert table.select("*", where=None, to_dict=True) == [
        {"event_id": 1, "seq": 1},
    ]

    db.close()


def test_calendar_partition_contract_without_time_prunes_equality(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "schedule",
        pk=["event_date", "title"],
        columns={"event_date": "text", "title": "text", "room": "text"},
        partition=Partition.calendar("event_date", every="1day", timezone="UTC"),
    )

    table.insert_all([
        {
            "event_date": "2026-01-01T09:00:00+00:00",
            "title": "check-in 1",
            "room": "A",
        },
        {
            "event_date": "2026-01-03T09:00:00+00:00",
            "title": "check-in 3",
            "room": "B",
        },
    ])

    rows = table.select(
        "*",
        where={
            "field": "event_date",
            "op": "==",
            "value": "2026-01-03T09:00:00+00:00",
        },
        to_dict=True,
    )
    assert rows == [{
        "event_date": "2026-01-03T09:00:00+00:00",
        "title": "check-in 3",
        "room": "B",
    }]

    plan = table.explain_select(
        where={
            "field": "event_date",
            "op": "==",
            "value": "2026-01-03T09:00:00+00:00",
        },
    ).to_dict()
    assert plan["candidate_partitions"] == ["20260103"]
    assert plan["scanned_partitions"] == ["20260103"]
    assert plan["skipped_partitions"] == ["20260101"]
    assert table.get_table_meta()["table_contract"]["time"] == {
        "name": "event_date",
        "unit": "date",
        "timezone": "UTC",
    }

    db.close()


def test_row_segment_late_data_scans_overlapping_segments_and_skips_others(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=3),
        order_key="seq",
    )

    table.insert_all([
        {"event_id": index, "seq": index, "kind": "base"}
        for index in range(6)
    ])
    table.insert_all([
        {"event_id": 99, "seq": 1, "kind": "late"},
    ])

    assert table.table_parts() == ["__all__"]
    segments = table.segments()
    assert [item["row_count"] for item in segments] == [3, 3, 1]
    assert [item["stats"]["seq"] for item in segments] == [
        {"min": 0, "max": 2},
        {"min": 3, "max": 5},
        {"min": 1, "max": 1},
    ]

    rows = table.select(
        "*",
        where={"field": "seq", "op": "between_and", "value": [1, 1]},
        order_by="event_id",
        to_dict=True,
    )
    assert rows == [
        {"event_id": 1, "seq": 1, "kind": "base"},
        {"event_id": 99, "seq": 1, "kind": "late"},
    ]

    plan = table.explain_select(
        where={"field": "seq", "op": "between_and", "value": [1, 1]},
        order_by="seq",
    ).to_dict()
    assert plan["candidate_partitions"] == ["__all__"]
    assert plan["scanned_partitions"] == ["__all__"]
    assert {
        item["segment_id"] for item in plan["scanned_segments"]
    } == {0, 2}
    assert {
        item["segment_id"] for item in plan["skipped_segments"]
    } == {1}
    assert plan["segment_skip_reasons"] == {
        "__all____000001": "pruned_by_segment_stats",
    }
    assert plan["requires_global_sort"] is True

    db.close()


def test_row_segment_compact_preserves_duplicate_primary_keys(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=4),
        order_key="event_id",
    )

    table.write([
        {"event_id": 1, "seq": 1, "kind": "base"},
        {"event_id": 2, "seq": 2, "kind": "base"},
        {"event_id": 3, "seq": 3, "kind": "base"},
        {"event_id": 4, "seq": 4, "kind": "base"},
    ])
    table.write({"event_id": 1, "seq": 5, "kind": "duplicate"})

    before = table.select("*", where=None, order_by="seq", to_dict=True)
    result = table.compact(target_rows=4)

    assert result.rows_read == 5
    assert result.rows_written == 5
    assert result.old_segments == 2
    assert result.new_segments == 2
    assert result.obsolete_segments == 2
    assert table.select("*", where=None, order_by="seq", to_dict=True) == before

    visible = table.segments()
    assert len(visible) == 2
    assert all(item["state"] == "sealed" for item in visible)
    all_segments = table.segments(include_obsolete=True)
    assert [item["state"] for item in all_segments].count("obsolete") == 2
    assert [item["state"] for item in all_segments].count("sealed") == 2

    db.close()


def test_row_segment_validate_and_cleanup_obsolete_files(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": 1, "seq": 1, "kind": "base"},
        {"event_id": 2, "seq": 2, "kind": "base"},
        {"event_id": 3, "seq": 3, "kind": "base"},
        {"event_id": 4, "seq": 4, "kind": "base"},
    ])

    before = table.select("*", where=None, order_by="event_id", to_dict=True)
    table.compact()

    validation = table.validate(deep=True)
    assert validation.ok, validation.to_dict()
    assert validation.checked_segments == 2

    preview = table.cleanup_obsolete()
    assert preview.dry_run is True
    assert preview.candidate_segments == 2
    assert preview.files_deleted == 0
    assert preview.bytes_reclaimable > 0
    assert all(os.path.exists(path) for path in preview.files)

    result = table.cleanup_obsolete(dry_run=False)
    assert result.dry_run is False
    assert result.candidate_segments == 2
    assert result.files_deleted >= 2
    assert result.manifest_rows_deleted == 2
    assert result.bytes_deleted > 0
    assert all(not os.path.exists(path) for path in preview.files)
    assert table.select("*", where=None, order_by="event_id", to_dict=True) == before
    assert [item["state"] for item in table.segments(include_obsolete=True)] == [
        "sealed",
        "sealed",
    ]

    db.close()


def test_deep_validate_detects_manifest_row_count_mismatch(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": 1, "seq": 1},
        {"event_id": 2, "seq": 2},
    ])
    segment = table.segments()[0]
    table.table_meta_db["_iosql_segments"].execute_params(
        (
            "update _iosql_segments set row_count = ? "
            "where table_name = ? and logical_part = ? and segment_id = ?"
        ),
        [999, "events", segment["logical_part"], segment["segment_id"]],
        commit=True,
    )

    validation = table.validate(deep=True)

    assert not validation.ok
    assert [issue.code for issue in validation.errors] == ["row_count_mismatch"]
    assert validation.errors[0].expected == 999
    assert validation.errors[0].actual == 2

    db.close()


def test_cleanup_obsolete_rejects_when_visible_segment_invalid(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": 1, "seq": 1},
        {"event_id": 2, "seq": 2},
        {"event_id": 3, "seq": 3},
        {"event_id": 4, "seq": 4},
    ])
    table.compact()
    preview = table.cleanup_obsolete()
    visible = table.segments()[0]
    table.table_meta_db["_iosql_segments"].execute_params(
        (
            "update _iosql_segments set row_count = ? "
            "where table_name = ? and logical_part = ? and segment_id = ?"
        ),
        [999, "events", visible["logical_part"], visible["segment_id"]],
        commit=True,
    )

    with pytest.raises(UnsafeOperation, match="validation failed"):
        table.cleanup_obsolete(dry_run=False)

    assert all(os.path.exists(path) for path in preview.files)
    assert [item["state"] for item in table.segments(include_obsolete=True)].count(
        "obsolete"
    ) == 2

    db.close()


def test_iter_select_batches_streams_rows_and_honors_limit(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": idx, "seq": idx, "kind": "trade"}
        for idx in range(1, 7)
    ])

    where = {"field": "seq", "op": ">=", "value": 2}
    batches = list(table.iter_select_batches(
        "event_id,seq",
        where=where,
        batch_size=2,
        limit=3,
        to_dict=True,
    ))
    rows = [row for batch in batches for row in batch]

    assert [len(batch) for batch in batches] == [2, 1]
    assert rows == table.select(
        "event_id,seq",
        where=where,
        limit=3,
        to_dict=True,
    )
    assert list(table.iter_select_batches(
        "*",
        where=where,
        batch_size=2,
        limit=0,
        to_dict=True,
    )) == []

    ordered_rows = list(table.iter_select(
        "*",
        where=None,
        order_by="seq desc",
        batch_size=2,
        limit=4,
        to_dict=True,
    ))
    assert ordered_rows == table.select(
        "*",
        where=None,
        order_by="seq desc",
        limit=4,
        to_dict=True,
    )

    db.close()


def test_iter_select_batches_order_by_streams_without_full_select(tmp_path, monkeypatch):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": idx, "seq": idx, "kind": "trade"}
        for idx in range(1, 7)
    ])

    def _must_not_eager_select(*args, **kwargs):
        raise AssertionError(
            'iter_select_batches(order_by=...) 不允许走 select() 全量物化路径'
        )

    monkeypatch.setattr(type(table), 'select', _must_not_eager_select)

    rows = [
        row
        for batch in table.iter_select_batches(
            "*",
            order_by="seq desc",
            batch_size=2,
            limit=3,
            to_dict=True,
        )
        for row in batch
    ]

    assert [row["seq"] for row in rows] == [6, 5, 4]

    db.close()


def test_iter_select_batches_order_by_matches_select_multi_part(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        partition=Partition.range("seq", every=10),
    )
    # seq 跨多个分区（0..70），乱序写入
    seqs = [3, 45, 12, 9, 71, 50, 27, 64]
    table.write([
        {"event_id": idx + 1, "seq": seq, "kind": f"k{idx % 3}"}
        for idx, seq in enumerate(seqs)
    ])
    assert len(table.table_parts()) > 1

    for order_by in ("seq desc", "kind asc, seq desc", "seq"):
        got = [
            row
            for batch in table.iter_select_batches(
                "*", order_by=order_by, batch_size=2, to_dict=True,
            )
            for row in batch
        ]
        assert got == table.select("*", order_by=order_by, to_dict=True)

    db.close()


def test_iter_select_batches_order_by_limit_project(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer", "kind": "text"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": idx, "seq": idx, "kind": f"kind{idx % 2}"}
        for idx in range(1, 7)
    ])
    # NULL 排序值：归并路径与 select() 一致按 SQLite 语义（ASC 最小 / DESC 最大）
    table.write([{"event_id": 100, "kind": "kind9"}])

    order_by = "seq desc, event_id desc"
    batches = list(table.iter_select_batches(
        "event_id,kind",
        order_by=order_by,
        batch_size=2,
        limit=3,
        to_dict=False,
    ))
    rows = [row for batch in batches for row in batch]

    assert [len(batch) for batch in batches] == [2, 1]
    expected = table.select(
        "event_id,kind",
        order_by=order_by,
        limit=3,
        to_dict=False,
    )
    assert rows == expected
    # 排序列 seq 不在投影中：输出行只含投影列，但全局仍按 seq 有序
    assert len(expected[0]) == 2

    got_nulls_last = [
        row
        for batch in table.iter_select_batches(
            "event_id,seq", order_by="seq desc", batch_size=2, to_dict=True,
        )
        for row in batch
    ]
    assert got_nulls_last[-1]["event_id"] == 100
    assert got_nulls_last == table.select(
        "event_id,seq", order_by="seq desc", to_dict=True,
    )

    db.close()


def test_compact_rejects_non_segment_table(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "text"},
    )
    table.write({"id": 1, "value": "a"})

    with pytest.raises(BadArgument, match="Segment.rows"):
        table.compact()

    db.close()


def test_write_hot_path_does_not_scan_legacy_file_stats(tmp_path, monkeypatch):
    db = Database(_uri(tmp_path))
    table = db.table(
        "daily",
        pk=["symbol", "date"],
        columns={
            "symbol": "text",
            "date": "text",
            "close": "real",
        },
        partition=Partition.calendar("date", every="1year", timezone="UTC"),
    )
    table.write({"symbol": "A", "date": "2026-01-01", "close": 1.0})

    def fail_if_scanned(*args, **kwargs):
        raise AssertionError("写入热路径不应扫描整个数据文件")

    monkeypatch.setattr(table, "_data_file_column_stats", fail_if_scanned)
    result = table.write(
        {"symbol": "B", "date": "2026-01-01", "close": 2.0}
    )

    assert result.rows_seen == 1
    assert result.layout_warnings == []
    part_table = next(iter(table._db_parts.values()))["data"]
    assert list(part_table.default_values()) == ["symbol", "date", "close"]
    db.close()


def test_segment_append_uses_incremental_manifest_stats(tmp_path, monkeypatch):
    db = Database(_uri(tmp_path))
    table = db.table(
        "orderbook",
        pk=["event_id"],
        columns={
            "event_id": "integer",
            "ts": "integer",
            "bid": "real",
        },
        segment=Segment.rows(max_rows=10),
        order_key="ts",
    )

    def fail_if_refreshed(*args, **kwargs):
        raise AssertionError("append-only segment 不应全量刷新统计")

    monkeypatch.setattr(table, "_refresh_segment_info", fail_if_refreshed)
    table.write([
        {"event_id": 1, "ts": 10, "bid": 1.1},
        {"event_id": 2, "ts": 20, "bid": 1.2},
    ])
    table.write([
        {"event_id": 3, "ts": 5, "bid": 1.0},
    ])

    segment = table.segments()[0]
    assert segment["row_count"] == 3
    assert segment["stats"]["ts"] == {"min": 5, "max": 20}
    assert segment["stats"]["bid"] == {"min": 1.0, "max": 1.2}
    db.close()


def test_segment_stats_columns_limit_optional_stats_and_keep_pruning_keys(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "orderbook",
        pk=["event_id"],
        columns={
            "event_id": "integer",
            "symbol": "text",
            "ts": "integer",
            "sequence": "integer",
            "price": "real",
            "size": "integer",
        },
        partition="duration(ts,every=1day)",
        segment=Segment.rows(
            max_rows=2,
            stats_columns=["sequence"],
        ),
        order_key="ts",
    )

    table.write([
        {
            "event_id": index,
            "symbol": "A",
            "ts": 1_767_225_600_000 + index,
            "sequence": index,
            "price": 10.0 + index,
            "size": 100 + index,
        }
        for index in range(4)
    ])

    segments = table.segments()
    assert set(segments[0]["stats"]) == {"ts", "sequence"}
    plan = table.explain_select(
        where={
            "field": "sequence",
            "op": "between_and",
            "value": [2, 3],
        },
    ).to_dict()
    assert {item["segment_id"] for item in plan["scanned_segments"]} == {1}
    assert {item["segment_id"] for item in plan["skipped_segments"]} == {0}

    price_plan = table.explain_select(
        where={"field": "price", "op": ">", "value": 1000},
    ).to_dict()
    assert {
        item["segment_id"] for item in price_plan["scanned_segments"]
    } == {0, 1}
    db.close()


def test_segment_mixed_type_stats_remain_conservative(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "mixed",
        pk=["event_id"],
        columns={"event_id": "integer", "value": "text"},
        segment=Segment.rows(max_rows=10),
    )

    table.write({"event_id": 1, "value": 10})
    table.write({"event_id": 2, "value": "text"})
    table.write({"event_id": 3, "value": 20})

    assert table.segments()[0]["stats"]["value"] == {
        "min": None,
        "max": None,
        "complete": False,
    }
    db.close()


def test_sqlite_write_pragmas_default_and_override(tmp_path):
    default_db = Database(_uri(tmp_path, "default.iosql"))
    default_table = default_db.table(
        "rows",
        pk=["id"],
        columns={"id": "integer"},
    )
    default_table.write({"id": 1})

    for physical_db in [
        default_table.table_meta_db,
        next(iter(default_table._db_parts.values())),
    ]:
        with physical_db.get_connection() as conn:
            assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
            assert conn.execute("pragma synchronous").fetchone()[0] == 1
    default_db.close()

    full_db = Database(
        _uri(tmp_path, "full.iosql"),
        pragma_dict={"synchronous": "full"},
    )
    full_table = full_db.table(
        "rows",
        pk=["id"],
        columns={"id": "integer"},
    )
    full_table.write({"id": 1})
    with full_table.table_meta_db.get_connection() as conn:
        assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert conn.execute("pragma synchronous").fetchone()[0] == 2
    full_db.close()
