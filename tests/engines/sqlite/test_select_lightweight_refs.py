"""普通查询只读取分区引用；完整统计留给明确请求的诊断操作。"""

from pathlib import Path

import pytest

from iosql import Database, Partition, Segment
from iosql.exception import UnsafeOperation


DAY_MS = 86_400_000
BASE_TS = 1_735_689_600_000


def _make_db(directory):
    db = Database(f"sqlite://{directory}")
    table = db.table(
        "ticks", pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"), symbol_column="symbol",
    )
    table.write([
        {"symbol": ["AAPL", "MSFT"][i % 2],
         "ts": BASE_TS + day * DAY_MS + i * 1000,
         "price": 100.0 + day + i * 0.1}
        for day in range(3) for i in range(10)
    ])
    return db, table


@pytest.fixture
def ticks(tmp_path):
    db, table = _make_db(tmp_path / "demo.iosql")
    try:
        yield table
    finally:
        db.close()


def _forbid_data_statistics(table, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("普通查询不应预扫描分区的 MIN/MAX、COUNT")

    for name in (
        "_data_file_column_stats", "_data_file_time_range",
        "_data_file_session_range", "_data_file_row_count",
    ):
        monkeypatch.setattr(table, name, fail)


@pytest.mark.parametrize("reader", [
    "select", "iter_select", "iter_select_batches", "query", "query_iter_rows",
    "query_iter_batches", "ordered_iter_select", "latest",
])
def test_where_reads_skip_legacy_statistics(ticks, monkeypatch, reader):
    _forbid_data_statistics(ticks, monkeypatch)
    where = [
        {"field": "symbol", "op": "=", "value": "AAPL"},
        {"field": "ts", "op": "between", "value": [BASE_TS, BASE_TS + 9999]},
        {"field": "price", "op": ">", "value": 100.1},
    ]
    expected = [BASE_TS + i * 1000 for i in (2, 4, 6, 8)]
    if reader == "select":
        rows = ticks.select(where=where, order_by="ts")
    elif reader in ("iter_select", "ordered_iter_select"):
        rows = list(ticks.iter_select(
            where=where, batch_size=2,
            order_by="ts" if reader == "ordered_iter_select" else None,
        ))
    elif reader == "iter_select_batches":
        rows = [row for batch in ticks.iter_select_batches(where=where, batch_size=2)
                for row in batch]
    elif reader == "latest":
        rows = ticks.latest(symbols="AAPL", as_of=BASE_TS + 9999).to_list()
        expected = [BASE_TS + 8000]
    else:
        query = ticks.query(
            symbols="AAPL", start=BASE_TS, end=BASE_TS + 9999,
            where="price > 100.1", order_by="ts",
        )
        if reader == "query":
            rows = query.to_list()
        elif reader == "query_iter_rows":
            rows = list(query.iter_rows(batch_size=2))
        else:
            rows = [row for batch in query.iter_batches(batch_size=2) for row in batch]
    assert sorted(row["ts"] for row in rows) == expected
    assert {row["symbol"] for row in rows} == {"AAPL"}


@pytest.mark.parametrize("reader", ["select", "iter_batches", "iter_rows", "count_rows"])
def test_unfiltered_reads_and_count_skip_unrequested_statistics(ticks, monkeypatch, reader):
    _forbid_data_statistics(ticks, monkeypatch)
    if reader == "count_rows":
        assert ticks.count_rows() == 30
    elif reader == "iter_batches":
        assert sum(len(batch) for batch in ticks.iter_batches(batch_size=4)) == 30
    elif reader == "iter_rows":
        assert len(list(ticks.iter_rows(batch_size=4))) == 30
    else:
        rows = ticks.select(order_by="ts desc, symbol desc", limit=5)
        assert [row["ts"] for row in rows] == [
            BASE_TS + 2 * DAY_MS + i * 1000 for i in (9, 8, 7, 6, 5)
        ]


def test_time_pruning_uses_saved_bounds_without_opening_other_partitions(ticks, monkeypatch):
    _forbid_data_statistics(ticks, monkeypatch)
    acquire = ticks._acquire_part_db
    opened = []

    def track(path, auto_commit):
        opened.append(Path(path).stem)
        return acquire(path, auto_commit)

    monkeypatch.setattr(ticks, "_acquire_part_db", track)
    rows = ticks.query(start=BASE_TS, end=BASE_TS + DAY_MS - 1).to_list()
    assert len(rows) == 10
    assert set(opened) == {str(BASE_TS)}


def test_or_conditions_keep_filtering_and_do_not_duplicate_rows(ticks, monkeypatch):
    _forbid_data_statistics(ticks, monkeypatch)
    where = [[{"field": "ts", "op": "=", "value": BASE_TS}],
             [{"field": "price", "op": ">=", "value": 102.8}]]
    rows = ticks.select(where=where, order_by="ts")
    assert [row["ts"] for row in rows] == [
        BASE_TS, BASE_TS + 2 * DAY_MS + 8000, BASE_TS + 2 * DAY_MS + 9000,
    ]


def test_legacy_explain_uses_same_pruning_as_queries_and_reports_statistics(ticks):
    plan = ticks.explain_select(where="price > 101.5")
    # 显式诊断可计算价格范围，但普通执行只按已保存的分区边界裁剪。
    assert plan.scanned_partitions == ticks.table_parts()
    assert plan.skipped_partitions == []
    assert len(ticks.select(where="price > 101.5")) == 14
    assert sum(info["row_count"] for info in plan.scanned_segments) == 30
    first = ticks.segments()[0]
    assert first["row_count"] == 10
    assert first["time_min"] == BASE_TS
    assert first["time_max"] == BASE_TS + 9000
    assert first["stats"]["price"] == {"min": 100.0, "max": 100.9}


def test_wal_append_and_delete_do_not_trust_legacy_stats_cache(ticks, monkeypatch):
    # 显式诊断可留下写入前的统计，普通查询不能据此丢弃 WAL 中的新行。
    ticks.segments()
    new = {"symbol": "AAPL", "ts": BASE_TS + 15000, "price": 1000.0}
    ticks.write(new)
    _forbid_data_statistics(ticks, monkeypatch)
    assert ticks.select(where="price = 1000") == [new]
    assert ticks.delete(where="price = 1000") == 1
    assert ticks.select(where="price = 1000") == []
    assert ticks.count_rows() == 30


def test_row_segments_keep_manifest_pruning_without_scanning_data(tmp_path, monkeypatch):
    with Database(f"sqlite://{tmp_path / 'segments.iosql'}") as db:
        table = db.table(
            "ticks", pk=["ts"], columns={"ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=2),
        )
        table.write([{"ts": BASE_TS + i, "price": float(i)} for i in range(4)])
        plan = table.explain_select(where="price >= 2")
        assert len(plan.scanned_segments) == len(plan.skipped_segments) == 1
        assert plan.skipped_segments[0]["segment_id"] == 0
        _forbid_data_statistics(table, monkeypatch)
        assert table.select(where="price >= 2", order_by="ts") == [
            {"ts": BASE_TS + 2, "price": 2.0}, {"ts": BASE_TS + 3, "price": 3.0},
        ]


@pytest.mark.parametrize("operation", [
    "select", "iter_select", "iter_select_batches", "iter_batches", "iter_rows",
    "count_rows", "delete", "create_index", "drop_index", "schema_evolution",
])
def test_missing_registered_legacy_file_is_not_hidden_or_recreated(tmp_path, operation):
    directory = tmp_path / "missing.iosql"
    db, table = _make_db(directory)
    missing = Path(table._get_part_dbfile(BASE_TS))
    db.close()
    missing.unlink()
    with Database(f"sqlite://{directory}") as reopened:
        table = reopened.table("ticks")
        with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
            if operation == "select":
                table.select(where="price > 0")
            elif operation == "iter_select":
                list(table.iter_select(where="price > 0", order_by="ts"))
            elif operation == "iter_select_batches":
                list(table.iter_select_batches(where="price > 0"))
            elif operation == "iter_batches":
                list(table.iter_batches())
            elif operation == "iter_rows":
                list(table.iter_rows())
            elif operation == "count_rows":
                table.count_rows()
            elif operation == "delete":
                table.delete(where="price > 0")
            elif operation == "create_index":
                table.create_index("price")
            elif operation == "drop_index":
                table.drop_index("price")
            else:
                table.write({"symbol": "AAPL", "ts": BASE_TS + DAY_MS + 15000,
                             "price": 200.0, "new_column": "new"})
        assert not missing.exists()
        validation = table.validate(deep=True)
        assert not validation.ok
        assert any(issue.code == "segment_file_missing" for issue in validation.errors)
