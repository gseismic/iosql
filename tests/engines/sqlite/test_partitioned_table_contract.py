import os
import sqlite3

import pytest

from iosql import Database, Partition, Segment
from iosql.exception import BadArgument, UnsafeOperation
from iosql.engines.sqlite import table as sqlite_table_module
from iosql.engines.sqlite.table import Table as SQLiteTable
from iosql.partition import RangePartition
from iosql.utils.mysqlite.connection_pool import (
    get_process_connection_budget_status,
)


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


def test_query_iter_rows_order_by_streams_without_full_select(tmp_path, monkeypatch):
    """query(..., order_by=...).iter_rows() 必须保持有序流式路径。"""
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "open_time": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([
        {"event_id": idx, "open_time": open_time}
        for idx, open_time in enumerate([30, 10, 20], start=1)
    ])

    def _must_not_eager_select(*args, **kwargs):
        raise AssertionError(
            "query(..., order_by=...).iter_rows() 不允许走 select() 全量路径"
        )

    monkeypatch.setattr(type(table), "select", _must_not_eager_select)

    observed = []
    original_part_ordered_rows = type(table)._part_ordered_rows

    def _track_part_ordered_rows(self, part_dbfile, query, chunk_size):
        for row in original_part_ordered_rows(
            self, part_dbfile, query, chunk_size
        ):
            observed.append(row["open_time"])
            yield row

    monkeypatch.setattr(
        type(table), "_part_ordered_rows", _track_part_ordered_rows
    )

    query = table.query(order_by="open_time")
    iterator = query.iter_rows(batch_size=1)
    first = next(iterator)
    assert first["open_time"] == 10
    assert len(observed) < 3
    rows = [first, *iterator]

    assert [row["open_time"] for row in rows] == [10, 20, 30]

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


def test_iter_select_batches_order_by_concurrent_write_snapshot(tmp_path):
    """迭代期间并发写入：语句级快照保证不重不漏，新行不可见（评审复现场景）。"""
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
        for idx in range(1, 9)
    ])
    snapshot = table.select("*", order_by="seq", to_dict=True)

    got = []
    for i, batch in enumerate(table.iter_select_batches(
        "*", order_by="seq", batch_size=2, to_dict=True,
    )):
        ids = [row["event_id"] for row in batch]
        assert len(ids) == len(set(ids)), '批内 event_id 重复'
        if i == 1:
            # 模拟并发写：更小排序值（评审复现 -1 场景）+ 更大值
            table.insert_all([
                {"event_id": 100, "seq": -1, "kind": "late"},
                {"event_id": 101, "seq": 99, "kind": "late"},
            ])
        got.extend(ids)
    assert len(got) == len(set(got)), '跨批 event_id 重复（OFFSET 重读）'
    assert [row["event_id"] for row in snapshot] == got
    assert 100 not in got and 101 not in got, '迭代中新行可见 → 非快照读'

    db.close()


def test_iter_select_batches_order_by_limit_pushdown_many_parts(tmp_path):
    """limit 下推 per-part LIMIT：多分区 limit=1 只归并出 1 行且与 select() 一致。"""
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        partition=Partition.range("seq", every=10),
    )
    seqs = [3, 45, 12, 9, 71, 50, 27, 64, 18, 33]
    table.write([
        {"event_id": idx + 1, "seq": seq}
        for idx, seq in enumerate(seqs)
    ])
    assert len(table.table_parts()) >= 8

    # 结构回归：limit 生成 per-part LIMIT ?（top-N 排序器，排序内存 O(limit)）
    ref, query = next(table._select_queries_for_where(
        "*",
        table._build_where(None),
        table.get_table_meta()["column_info"],
        order_by_sql=table._order_by_sql("seq desc", "*", {}),
        limit=1,
    ))
    assert query.sql.rstrip().rstrip(';').endswith('limit ?')
    assert query.params[-1] == 1

    rows = [
        row
        for batch in table.iter_select_batches(
            "*", order_by="seq desc", batch_size=2, limit=1, to_dict=True,
        )
        for row in batch
    ]
    assert len(rows) == 1
    assert rows == table.select(
        "*", order_by="seq desc", limit=1, to_dict=True,
    )
    assert rows[0]["seq"] == 71

    db.close()


def test_iter_select_batches_order_by_part_conn_failure_fails(
    tmp_path, monkeypatch
):
    """单分区连接失败必须让查询失败，不允许静默缺数据（评审 P1-1）。"""
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        partition=Partition.range("seq", every=10),
    )
    table.write([
        {"event_id": idx + 1, "seq": seq}
        for idx, seq in enumerate([3, 15, 27])
    ])
    parts = table.table_parts()
    assert len(parts) >= 2
    victim = table._get_part_dbfile(parts[-1])

    original_connect = sqlite_table_module.sqlite3.connect

    def fail_victim_connect(database, *args, **kwargs):
        if kwargs.get('uri') and os.path.basename(victim) in str(database):
            raise sqlite3.OperationalError('injected read-only open failure')
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(
        sqlite_table_module.sqlite3,
        'connect',
        fail_victim_connect,
    )
    with pytest.raises(UnsafeOperation) as exc_info:
        list(table.iter_select_batches(
            "*", order_by="seq", batch_size=2, to_dict=True,
        ))
    # 错误信息必须能定位到出问题的分区文件
    assert str(victim) in str(exc_info.value)

    db.close()


def test_iter_select_batches_order_by_missing_part_table_fails(tmp_path):
    """已登记分区缺少 data 表时不得静默返回空结果。"""
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        partition=Partition.range("seq", every=10),
    )
    table.write([
        {"event_id": idx + 1, "seq": seq}
        for idx, seq in enumerate([3, 15, 27])
    ])
    victim = table._get_part_dbfile(table.table_parts()[-1])

    corruptor = sqlite3.connect(victim)
    try:
        corruptor.execute("DROP TABLE data")
        corruptor.commit()
    finally:
        corruptor.close()

    with pytest.raises(UnsafeOperation, match=os.path.basename(victim)):
        list(table.iter_select_batches(
            "*", order_by="seq", batch_size=2, to_dict=True,
        ))

    db.close()


def test_iter_select_batches_order_by_over_conn_cap_fails(tmp_path):
    """有序归并连接数超过 max_ordered_read_connections 时快速失败（评审 P1-2）。"""
    db = Database(
        _uri(tmp_path),
        max_ordered_read_connections=2,
    )
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        partition=Partition.range("seq", every=10),
    )
    table.write([
        {"event_id": idx + 1, "seq": seq}
        for idx, seq in enumerate([3, 15, 27])
    ])
    assert len(table.table_parts()) >= 3

    with pytest.raises(BadArgument, match="max_ordered_read_connections"):
        list(table.iter_select_batches(
            "*", order_by="seq", batch_size=2, to_dict=True,
        ))

    # 无 order_by 的流式路径不受该上限影响
    rows = [
        row
        for batch in table.iter_select_batches("*", batch_size=2, to_dict=True)
        for row in batch
    ]
    assert len(rows) == 3

    db.close()


def test_iter_select_batches_order_by_respects_process_connection_budget(
    tmp_path,
):
    """raw 有序连接不能绕过 max_process_connections。"""
    db = Database(
        _uri(tmp_path),
        max_process_connections=2,
        max_ordered_read_connections=3,
    )
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        partition=Partition.range("seq", every=10),
    )
    table.write([
        {"event_id": idx + 1, "seq": seq}
        for idx, seq in enumerate([3, 15, 27])
    ])

    before = get_process_connection_budget_status()["open_process_connections"]
    with pytest.raises(BadArgument, match="max_process_connections"):
        list(table.iter_select_batches(
            "*", order_by="seq", batch_size=2, to_dict=True,
        ))
    after = get_process_connection_budget_status()["open_process_connections"]
    assert after == before

    db.close()


def test_ordered_read_budget_fails_fast_for_same_thread_pool_lease(tmp_path):
    """同线程持有池连接时预算不足必须失败，不能永久等待。"""
    db = Database(
        _uri(tmp_path),
        max_process_connections=1,
        max_ordered_read_connections=1,
    )
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2),
        order_key="event_id",
    )
    table.write([{"event_id": 1, "seq": 3}])

    try:
        with table.table_meta_db.get_connection():
            with pytest.raises(BadArgument, match="max_process_connections"):
                list(table.iter_select_batches(
                    "*", order_by="seq", batch_size=1, to_dict=True,
                ))
    finally:
        db.close()


def test_ordered_merge_chunk_keeps_memory_bound_for_large_part_count():
    """part 数增大时不能由固定 chunk 下限突破驻留预算。"""
    assert SQLiteTable._merge_stream_chunk(1000, 2000) == 50
    assert SQLiteTable._merge_stream_chunk(1000, 100_000) == 1
    assert 2000 * SQLiteTable._merge_stream_chunk(1000, 2000) <= 100_000


def test_iter_select_batches_order_by_close_releases_process_reservation(
    tmp_path, monkeypatch,
):
    """提前关闭有序迭代器必须释放 raw 连接预留。"""
    db = Database(_uri(tmp_path), max_ordered_read_connections=10)
    table = db.table(
        "events",
        pk=["event_id"],
        columns={"event_id": "integer", "seq": "integer"},
        partition=Partition.range("seq", every=10),
    )
    table.write([
        {"event_id": idx + 1, "seq": seq}
        for idx, seq in enumerate([3, 15, 27])
    ])

    reserved = []
    released = []
    original_reserve = sqlite_table_module.reserve_process_connections
    original_release = sqlite_table_module.release_process_connections

    def track_reserve(*args, **kwargs):
        token = original_reserve(*args, **kwargs)
        reserved.append(token)
        return token

    def track_release(token):
        released.append(token)
        return original_release(token)

    monkeypatch.setattr(
        sqlite_table_module,
        "reserve_process_connections",
        track_reserve,
    )
    monkeypatch.setattr(
        sqlite_table_module,
        "release_process_connections",
        track_release,
    )

    iterator = table.iter_select_batches(
        "*", order_by="seq", batch_size=1, to_dict=True,
    )
    try:
        assert len(next(iterator)) == 1
        assert len(reserved) == 1
        assert released == []
    finally:
        iterator.close()
        db.close()

    assert released == reserved


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
