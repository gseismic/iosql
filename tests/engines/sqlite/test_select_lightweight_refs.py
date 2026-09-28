"""PLAN-069 回归：无 where 的 select 不再触发 legacy 列统计扫描。

背景：legacy 布局的 _legacy_segment_info 每次查询对每段做 24 列 MIN/MAX
全段扫描（仅服务于 where 分区裁剪）；无过滤的排序查询白白付出该成本，
418 万行真实库上首屏 14s。修复后无 where 走轻量引用（0 次统计扫描），
有 where 仍计算（裁剪需要），且统计按文件 mtime 缓存。
"""

import time

from iosql import Database, Partition


DAY_MS = 86_400_000
BASE_TS = 1_735_689_600_000


def _make_db(db_dir):
    db = Database(f"sqlite://{db_dir}")
    table = db.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
    )
    rows = []
    for day in range(3):
        for i in range(10):
            rows.append({
                "symbol": ["AAPL", "MSFT"][i % 2],
                "ts": BASE_TS + day * DAY_MS + i * 1000,
                "price": 100.0 + day + i * 0.1,
            })
    table.write(rows)
    return db, table


def test_no_where_select_skips_column_stats(tmp_path):
    db, table = _make_db(tmp_path / "demo.iosql")
    calls = []
    original = table._data_file_column_stats

    def spy(part_key):
        calls.append(part_key)
        return original(part_key)

    table._data_file_column_stats = spy
    try:
        rows = table.select("*", order_by="ts desc, symbol desc", limit=5,
                            to_dict=True)
        assert len(rows) == 5
        assert calls == [], "无 where 查询不应触发列统计扫描（PLAN-069 P2）"
    finally:
        table._data_file_column_stats = original
    db.close()


def test_where_select_still_uses_stats_for_pruning(tmp_path):
    db, table = _make_db(tmp_path / "demo.iosql")
    table._column_stats_cache.clear()
    calls = []
    original = table._data_file_column_stats

    def spy(part_key):
        calls.append(part_key)
        return original(part_key)

    table._data_file_column_stats = spy
    try:
        rows = table.select(
            "*", where={"field": "price", "op": ">=", "value": 100},
            order_by="ts", limit=100, to_dict=True,
        )
        assert len(rows) == 30
        assert len(calls) >= 1, "有 where 时裁剪统计仍应被计算"
    finally:
        table._data_file_column_stats = original
    db.close()


def test_stats_cache_hits_on_repeated_query(tmp_path):
    db, table = _make_db(tmp_path / "demo.iosql")
    where = {"field": "price", "op": ">=", "value": 100}

    # 第一次查询填充 mtime 缓存
    table.select("*", where=where, order_by="ts", limit=100, to_dict=True)
    type(table)._column_stats_cache_hits = 0
    type(table)._column_stats_cache_misses = 0

    rows = table.select("*", where=where, order_by="ts", limit=100,
                        to_dict=True)
    assert len(rows) == 30
    assert type(table)._column_stats_cache_hits >= 1, \
        "文件未变化时重复查询应命中 mtime 统计缓存（PLAN-069 P3）"
    assert type(table)._column_stats_cache_misses == 0


def test_lightweight_results_identical_to_full_refs(tmp_path):
    db, table = _make_db(tmp_path / "demo.iosql")
    lightweight = table.select("*", order_by="ts desc", limit=7, to_dict=True)

    # 模拟旧行为：强制重算完整 refs（有 where 才会带统计），比对结果
    full = table.select(
        "*", where={"field": "price", "op": ">=", "value": 0},
        order_by="ts desc", limit=7, to_dict=True,
    )
    assert [r["ts"] for r in lightweight] == sorted(
        (r["ts"] for r in lightweight), reverse=True
    )
    assert [r["ts"] for r in full] == [r["ts"] for r in lightweight]
    db.close()
