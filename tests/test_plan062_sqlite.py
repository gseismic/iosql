"""PLAN-062 SQLite 引擎回归测试。

覆盖：
- P0-1 并发读不得回滚 delete（显式 commit）
- P0-2 compact 与 delete 互斥（已删行不复活）
- P0-3 分区文件路径 containment
- S1-1 partition setter 守卫
- S1-2 select 家族业务/未知 kwargs 拒绝
- S1-3 manifest 引用文件缺失时读路径统一报错且不重建
- S1-4 deep validate FD 不泄漏
- S1-5 select(to_dict=False)+投影返回 tuple
- S1-6 spec= 声明契约的行为绑定在实例上生效
- query_util 空 AND 组恒真、字符串 fields 注入拒绝
- P2：缺主键、create_index order、limit bool、Where 边界、
  iter_rows/iter_batches、close 语义、孤儿 segment 文件、delete_all
- ORDER BY + LIMIT 下推与不下推结果一致
"""

import os
import sqlite3
import threading
import time

import pytest

from iosql import Database, Partition, Segment
from iosql.exception import BadArgument, TableSpecConflict, UnsafeOperation
from iosql.spec import TableContract
import iosql.engines.sqlite.table as table_module
from iosql.engines.sqlite.query_util import build_or_cases_subquery


DAY = 86_400_000


def _uri(tmp_path, name="plan062.iosql"):
    return f"sqlite://{tmp_path / name}"


def _wait(event, timeout=5.0):
    assert event.wait(timeout), "worker thread did not reach the synchronization point"


# ---------------------------------------------------------------- P0-1

def test_concurrent_read_does_not_rollback_delete(tmp_path, monkeypatch):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write([{"id": idx, "value": idx} for idx in range(1, 101)])
    part_file = table._get_part_dbfile(table.table_parts()[0])

    about_to_delete = threading.Event()
    reader_done = threading.Event()
    errors = []
    original_build = table_module.build_delete_query

    def hooked_build_delete_query(*args, **kwargs):
        # 在执行 DELETE 之前，让读线程把共享 auto_commit 翻回 False
        about_to_delete.set()
        _wait(reader_done)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(table_module, "build_delete_query", hooked_build_delete_query)

    def reader():
        try:
            _wait(about_to_delete)
            for _ in range(5):
                with table._part_db(part_file, auto_commit=False) as part_db:
                    part_db["data"].query("select 1 from data limit 1")
        except Exception as exc:  # pragma: no cover - 诊断用
            errors.append(exc)
        finally:
            reader_done.set()

    thread = threading.Thread(target=reader)
    thread.start()
    deleted = table.delete({"field": "id", "op": "<=", "value": 10})
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert deleted == 10

    # 释放连接池后再次读取：旧实现会在归还连接时 rollback，导致删除丢失
    table.close()
    reopened = Database(_uri(tmp_path))
    try:
        rows = reopened["items"].select("id", to_dict=True)
        assert len(rows) == 90
        assert min(row["id"] for row in rows) == 11
    finally:
        reopened.close()
    db.close()


# ---------------------------------------------------------------- P0-2

def test_compact_and_delete_do_not_resurrect_rows(tmp_path, monkeypatch):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="id",
    )
    table.write([{"id": idx, "value": idx} for idx in range(1, 7)])

    writing = threading.Event()
    allow_write = threading.Event()
    errors = []
    original_write_chunks = table_module.Table._write_compact_chunks

    def hooked_write_chunks(self, *args, **kwargs):
        writing.set()
        _wait(allow_write)
        return original_write_chunks(self, *args, **kwargs)

    monkeypatch.setattr(table_module.Table, "_write_compact_chunks", hooked_write_chunks)

    def compactor():
        try:
            table.compact(target_rows=2)
        except Exception as exc:  # pragma: no cover - 诊断用
            errors.append(exc)

    compact_thread = threading.Thread(target=compactor)
    compact_thread.start()
    _wait(writing)

    delete_result = {}
    delete_started = threading.Event()

    def deleter():
        delete_started.set()
        delete_result["deleted"] = table.delete(
            {"field": "id", "op": "==", "value": 5}
        )

    delete_thread = threading.Thread(target=deleter)
    delete_thread.start()
    _wait(delete_started)
    # compact 持锁期间 delete 必须阻塞；给旧实现足够窗口执行并复活行
    time.sleep(0.3)
    allow_write.set()
    compact_thread.join(timeout=20)
    delete_thread.join(timeout=20)
    assert not compact_thread.is_alive()
    assert not delete_thread.is_alive()
    assert not errors
    assert delete_result["deleted"] == 1

    remaining = table.select("id", to_dict=True)
    assert 5 not in [row["id"] for row in remaining]
    assert len(remaining) == 5

    # compact 后的新 segment 也必须保持删除结果
    table.compact(target_rows=2)
    remaining = table.select("id", to_dict=True)
    assert 5 not in [row["id"] for row in remaining]
    assert len(remaining) == 5
    table.close()
    db.close()


# ---------------------------------------------------------------- P0-3

def test_part_dbfile_containment(tmp_path):
    db = Database(_uri(tmp_path))
    table = db["guard"]
    for bad_key in ["../../escape", "../x", "../../../../tmp/escape"]:
        with pytest.raises(UnsafeOperation, match="unsafe partition file path"):
            table._get_part_dbfile(bad_key)
    # 正常 key 不受影响
    assert os.path.basename(table._get_part_dbfile(1700000000000)) == (
        "1700000000000.sqlite3"
    )
    db.close()


# ---------------------------------------------------------------- S1-1

def test_partition_setter_guard_on_non_empty_table(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "ticks",
        pk=["ts"],
        columns={"ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
    )
    table.write({"ts": 0, "price": 1.0})

    # 相同配置允许
    table.partition = Partition.duration("ts", every="1day")

    with pytest.raises(TableSpecConflict, match="修复"):
        table.partition = Partition.duration("ts", every="2day")

    # 空表允许更换分区配置
    empty = db.table(
        "empty",
        pk=["ts"],
        columns={"ts": "integer"},
        partition=Partition.duration("ts", every="1day"),
    )
    empty.partition = Partition.duration("ts", every="2day")
    assert empty.partition.interval_ms == 2 * DAY
    db.close()


# ---------------------------------------------------------------- S1-2

def test_select_rejects_business_and_unknown_kwargs(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "ticks",
        pk=["ts"],
        columns={"ts": "integer", "price": "float"},
        time="ts",
        symbol_column="ts",
    )
    table.write({"ts": 1, "price": 1.0})

    for func in (table.select, table.iter_select, table.iter_select_batches):
        with pytest.raises(BadArgument, match="query"):
            list(func(symbols=["a"]))
        with pytest.raises(BadArgument, match="query"):
            list(func(start=1))
        with pytest.raises(BadArgument, match="未知参数"):
            list(func(not_a_param=1))

    with pytest.raises(BadArgument, match="query"):
        table.select(date="2026-01-01", end=2, session="x")
    db.close()


# ---------------------------------------------------------------- S1-3

def test_missing_segment_file_raises_and_is_not_recreated(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="id",
    )
    table.write([{"id": idx, "value": idx} for idx in range(1, 5)])
    segment = table.segments()[0]
    missing_path = segment["path"]
    os.remove(missing_path)

    with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
        table.select("*", to_dict=True)
    assert not os.path.exists(missing_path)

    with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
        list(table.iter_select_batches("*", batch_size=1, to_dict=True))
    assert not os.path.exists(missing_path)

    with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
        list(table.iter_select("*", batch_size=1, to_dict=True))
    assert not os.path.exists(missing_path)

    with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
        table.count_rows()
    assert not os.path.exists(missing_path)

    with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
        list(table.iter_batches(batch_size=1))
    assert not os.path.exists(missing_path)

    with pytest.raises(UnsafeOperation, match="分区数据文件缺失"):
        table.compact()
    assert not os.path.exists(missing_path)
    db.close()


# ---------------------------------------------------------------- S1-4

@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="需要 /proc/self/fd")
def test_deep_validate_does_not_leak_fds(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "daily",
        pk=["ts"],
        columns={"ts": "integer", "value": "integer"},
        partition=Partition.duration("ts", every="1day"),
    )
    table.write([
        {"ts": day * DAY, "value": day} for day in range(8)
    ])

    def fd_count():
        return len(os.listdir("/proc/self/fd"))

    table.validate(deep=True)
    baseline = fd_count()
    for _ in range(6):
        result = table.validate(deep=True)
        assert result.ok
    after = fd_count()
    assert after <= baseline + 2, f"fd leaked: {baseline} -> {after}"
    db.close()


# ---------------------------------------------------------------- S1-5

def test_select_to_dict_false_with_projection_returns_tuples(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "name": "text", "score": "integer"},
    )
    table.write([
        {"id": 1, "name": "a", "score": 2},
        {"id": 2, "name": "b", "score": 1},
    ])

    rows = table.select(["name", "id"], order_by="score", to_dict=False)
    assert [tuple(row) for row in rows] == [("b", 2), ("a", 1)]

    batches = list(table.iter_select_batches(
        ["name", "id"], order_by="score", to_dict=False, batch_size=1
    ))
    assert [tuple(row) for batch in batches for row in batch] == [
        ("b", 2),
        ("a", 1),
    ]
    db.close()


# ---------------------------------------------------------------- S1-6

def test_declared_contract_bindings_applied_to_existing_table(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "ticks",
        pk=["id"],
        columns={"id": "integer", "price": "float"},
        conflict="append",
    )
    table.write({"id": 1, "price": 1.0})

    bound = db.table(
        "ticks",
        spec=TableContract(
            primary_key=["id"],
            columns={"id": "integer", "price": "float"},
            conflict="ignore",
        ),
    )
    assert bound.spec().conflict == "ignore"

    result = bound.write({"id": 1, "price": 9.9})
    assert result.rows_seen == 1
    rows = bound.select(to_dict=True)
    assert len(rows) == 1
    assert rows[0]["price"] == 1.0
    db.close()


# ---------------------------------------------------------------- query_util

def test_empty_and_group_is_always_true(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write([{"id": idx, "value": idx} for idx in range(1, 5)])

    case = {"field": "value", "op": "==", "value": 2}
    assert len(table.select("*", where=[[], [case]], to_dict=True)) == 4
    assert len(table.select("*", where=[[case]], to_dict=True)) == 1
    assert len(table.select("*", where=[[]], to_dict=True)) == 4

    # 单组 OR 编译路径
    where = build_or_cases_subquery([[], [case]], {"value": "integer"})
    assert where.sql == ""
    db.close()


def test_string_fields_rejects_sql_syntax(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "name": "text"},
    )
    table.write({"id": 1, "name": "a"})

    rows = table.select("id, name", to_dict=True)
    assert rows == [{"id": 1, "name": "a"}]
    assert table.select('"id"', to_dict=True) == [{"id": 1}]
    # list 形式保持原行为
    assert table.select(["id", "name"], to_dict=True) == [{"id": 1, "name": "a"}]

    for bad in [
        "id; DROP TABLE items",
        "id from items",
        "count(*)",
        "id, (select 1)",
        "id,,name",
        "id, *",
    ]:
        with pytest.raises(BadArgument, match="fields"):
            table.select(bad, to_dict=True)
    db.close()


# ---------------------------------------------------------------- P2

def test_missing_pk_field_raises_in_write_and_conflict_paths(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )

    with pytest.raises(BadArgument, match="主键"):
        table.write([{"value": 1}])
    with pytest.raises(BadArgument, match="主键"):
        table.write([{"value": 1}], mode="ignore")
    with pytest.raises(BadArgument, match="主键"):
        table.write([{"value": 1}], mode="upsert")
    with pytest.raises(BadArgument, match="主键"):
        table.upsert_all([{"value": 1}], pk=["id"])
    assert table.select(to_dict=True) == []

    segmented = db.table(
        "seg",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="id",
    )
    segmented.write({"id": 1, "value": 1})
    with pytest.raises(BadArgument, match="主键"):
        segmented.write([{"value": 2}], mode="ignore")
    db.close()


def test_create_index_invalid_order_and_limit_bool(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write({"id": 1, "value": 1})

    with pytest.raises(BadArgument, match="order"):
        table.create_index("id", order="bogus")
    table.create_index("id", order="desc")

    with pytest.raises(BadArgument, match="limit"):
        table.select(limit=True)
    with pytest.raises(BadArgument, match="limit"):
        list(table.iter_select_batches(limit=False))
    with pytest.raises(BadArgument, match="limit"):
        list(table.iter_select(limit=False))
    with pytest.raises(BadArgument, match="limit"):
        table.select(limit=False)
    db.close()


def test_where_edge_cases_raise_bad_argument(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write({"id": 1, "value": 1})

    with pytest.raises(BadArgument):
        table.select(where={})
    with pytest.raises(BadArgument):
        table.delete({})
    db.close()


def test_iter_rows_is_row_level_and_iter_batches_is_batch_level(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write([{"id": idx, "value": idx} for idx in range(1, 4)])

    rows = list(table.iter_rows(batch_size=2))
    assert rows == [{"id": 1, "value": 1}, {"id": 2, "value": 2}, {"id": 3, "value": 3}]

    batches = list(table.iter_batches(batch_size=2))
    assert batches == [
        [{"id": 1, "value": 1}, {"id": 2, "value": 2}],
        [{"id": 3, "value": 3}],
    ]
    db.close()


def test_close_semantics_do_not_deadlock_or_reopen(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write({"id": 1, "value": 1})
    part_file = table._get_part_dbfile(table.table_parts()[0])

    with table._part_db(part_file, auto_commit=False) as part_db:
        part_db["data"].query("select 1 from data limit 1")
        with pytest.raises(UnsafeOperation, match="租约"):
            table.close()

    table.close()
    with pytest.raises(UnsafeOperation, match="已关闭"):
        table.select(to_dict=True)
    with pytest.raises(UnsafeOperation, match="已关闭"):
        with table._part_db(part_file, auto_commit=True):
            pass
    db.close()


def test_orphan_segment_file_reported_and_compact_repairs(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "events",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="id",
    )
    table.write([{"id": idx, "value": idx} for idx in range(1, 5)])

    result = table.validate()
    assert "orphan_segment_file" not in [item.code for item in result.warnings]

    orphan_path = table._get_part_dbfile(
        table._segment_part_key("__all__", table._next_segment_id("__all__"))
    )
    conn = sqlite3.connect(orphan_path)
    conn.close()
    assert os.path.exists(orphan_path)

    result = table.validate()
    assert "orphan_segment_file" in [item.code for item in result.warnings]

    table.compact(target_rows=2)
    assert len(table.select(to_dict=True)) == 4
    # 孤儿文件占用的目标名被 compact 安全复用为真实 segment
    assert "orphan_segment_file" not in [
        item.code for item in table.validate().warnings
    ]
    assert any(
        segment["path"] == orphan_path for segment in table.segments()
    )
    db.close()


def test_delete_all_explicit_and_delete_none_deprecated(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "value": "integer"},
    )
    table.write([{"id": 1, "value": 1}, {"id": 2, "value": 2}])

    with pytest.warns(DeprecationWarning):
        deleted = table.delete(None)
    assert deleted == 2
    assert table.select(to_dict=True) == []

    table.write([{"id": 3, "value": 3}])
    assert table.delete_all() == 1
    assert table.select(to_dict=True) == []
    db.close()


# ---------------------------------------------------------------- ORDER BY 下推

def _segmented_rows_table(tmp_path, name="pushdown.iosql"):
    db = Database(_uri(tmp_path, name))
    table = db.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "score": "integer"},
        segment=Segment.rows(max_rows=3, target_rows=3, min_rows=1),
        order_key="ts",
    )
    table.write([
        {"symbol": "A", "ts": 1, "score": 5},
        {"symbol": "B", "ts": 2, "score": None},
        {"symbol": "A", "ts": 3, "score": 2},
        {"symbol": "B", "ts": 4, "score": 7},
        {"symbol": "A", "ts": 5, "score": None},
        {"symbol": "B", "ts": 6, "score": 1},
        {"symbol": "C", "ts": 7, "score": 4},
        {"symbol": "A", "ts": 8, "score": 3},
        {"symbol": "C", "ts": 9, "score": 6},
    ])
    return db, table


@pytest.mark.parametrize(
    "order_by",
    [
        "score",
        "score desc",
        "symbol asc, score desc",
        "score desc, ts asc",
        "symbol desc, score, ts",
    ],
)
@pytest.mark.parametrize("limit", [1, 2, 4, 9, 20])
def test_order_limit_pushdown_matches_full_sort(tmp_path, order_by, limit):
    db, table = _segmented_rows_table(tmp_path)
    pushed = table.select("*", where=None, order_by=order_by, limit=limit, to_dict=True)
    full = table.select("*", where=None, order_by=order_by, to_dict=True)
    assert pushed == full[:limit]
    db.close()


def test_order_limit_pushdown_issues_per_segment_limit(tmp_path, monkeypatch):
    db, table = _segmented_rows_table(tmp_path, name="pushdown_sql.iosql")
    captured = []
    original = table_module.build_select_cases_query

    def spy(*args, **kwargs):
        query = original(*args, **kwargs)
        captured.append((query.sql, list(query.params), kwargs))
        return query

    monkeypatch.setattr(table_module, "build_select_cases_query", spy)
    rows = table.select(
        "*", where=None, order_by="score desc", limit=2, to_dict=True
    )
    assert len(rows) == 2
    assert len(captured) >= 3  # 每个 segment 一条下推 SQL
    for sql, params, kwargs in captured:
        assert "order by" in sql
        assert sql.rstrip().endswith("limit ?;")
        assert kwargs["limit"] == 2
        assert params[-1] == 2
    db.close()


def test_order_limit_pushdown_with_keyset_where(tmp_path):
    db, table = _segmented_rows_table(tmp_path, name="pushdown_keyset.iosql")
    where = [{"field": "ts", "op": ">", "value": 2}]
    for order_by in ["score", "score desc, ts", "symbol, ts desc"]:
        for limit in [1, 3, 100]:
            pushed = table.select(
                "*", where=where, order_by=order_by, limit=limit, to_dict=True
            )
            full = table.select("*", where=where, order_by=order_by, to_dict=True)
            assert pushed == full[:limit]
    # 投影 + to_dict=False 时排序字段不在结果中
    pushed = table.select(
        ["symbol"], where=None, order_by="score desc", limit=3, to_dict=False
    )
    full = table.select(["symbol"], where=None, order_by="score desc", to_dict=False)
    assert [tuple(row) for row in pushed] == [tuple(row) for row in full[:3]]

    # UI keyset 形态的 CaseExpression where 也必须走下推且结果一致
    from iosql.core.query import Case

    expr = Case("ts", ">", 2) * Case("score", "!=", 1)
    pushed = table.select(
        "*", where=expr, order_by="score desc, ts asc", limit=2, to_dict=True
    )
    full = table.select(
        "*", where=expr, order_by="score desc, ts asc", to_dict=True
    )
    assert pushed == full[:2]
    db.close()
