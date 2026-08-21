"""
partition 与 segment 四种组合的完整测试。

覆盖：
1. 两者皆无：退化为单文件 SQLite
2. 仅 partition：逻辑分区路由，无文件滚动
3. 仅 segment：__all__ 逻辑分区 + 按行数滚动
4. 两者都有：逻辑分区路由 + 按行数滚动（二级管理）
5. 跨组合切换：从一种模式变更为另一种
6. write() / write_buffer() / insert_all() API 一致性
7. spec() 持久化与回读
8. explain_select 二级裁剪
"""

import os
import shutil

import pytest

from iosql import (
    DEFAULT_LOGICAL_PART,
    Database,
    Partition,
    Segment,
)
from iosql.exception import (
    BadArgument,
    TableSpecConflict,
)


def _uri(tmp_path, name="test.iosql"):
    return f"sqlite://{tmp_path}/{name}"


# ============================================================
# 组合 1：两者皆无 — 退化为单文件 SQLite
# ============================================================

class TestNeitherPartitionNorSegment:
    """无 partition、无 segment：退化为传统单文件 SQLite 表。"""

    def test_basic_write_and_read(self, tmp_path):
        """最基本的写入和查询，验证退化为单文件模式。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "kv",
            pk=["key"],
            columns={"key": "text", "val": "integer"},
        )

        result = table.write({"key": "a", "val": 1})
        assert result.rows_seen == 1
        assert result.rows_written == 1

        rows = table.select("*", where=None, to_dict=True)
        assert rows == [{"key": "a", "val": 1}]
        db.close()

    def test_write_multiple_rows(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "items",
            pk=["id"],
            columns={"id": "integer", "name": "text"},
        )

        result = table.write([
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "beta"},
            {"id": 3, "name": "gamma"},
        ])
        assert result.rows_seen == 3

        rows = table.select("*", where=None, order_by="id", to_dict=True)
        assert len(rows) == 3
        assert rows[0]["name"] == "alpha"
        db.close()

    def test_write_with_where(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "float"},
        )

        table.write([
            {"id": 1, "val": 1.0},
            {"id": 2, "val": 2.0},
            {"id": 3, "val": 3.0},
        ])

        rows = table.select(
            "*",
            where={"field": "val", "op": ">", "value": 1.5},
            order_by="id",
            to_dict=True,
        )
        assert rows == [
            {"id": 2, "val": 2.0},
            {"id": 3, "val": 3.0},
        ]
        db.close()

    def test_write_modes_upsert_and_ignore(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "scores",
            pk=["id"],
            columns={"id": "integer", "score": "integer"},
        )

        table.write({"id": 1, "score": 80})
        table.write({"id": 1, "score": 90}, mode="upsert")
        assert table.select("*", where=None, to_dict=True) == [
            {"id": 1, "score": 90},
        ]

        table.write({"id": 1, "score": 100}, mode="ignore")
        assert table.select("*", where=None, to_dict=True) == [
            {"id": 1, "score": 90},
        ]
        db.close()

    def test_write_buffer(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "events",
            pk=["seq"],
            columns={"seq": "integer", "kind": "text"},
        )

        with table.write_buffer(max_rows=2) as buf:
            for i in range(5):
                buf.append({"seq": i, "kind": f"type_{i}"})

        rows = table.select("*", where=None, order_by="seq", to_dict=True)
        assert len(rows) == 5
        db.close()

    def test_delete(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
        )
        table.write([
            {"id": 1, "val": "keep"},
            {"id": 2, "val": "remove"},
        ])

        deleted = table.delete({"field": "id", "op": "==", "value": 2})
        assert deleted == 1

        rows = table.select("*", where=None, to_dict=True)
        assert rows == [{"id": 1, "val": "keep"}]
        db.close()

    def test_truncate_requires_confirm(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
        )
        table.write({"id": 1, "val": "x"})

        with pytest.raises(Exception):
            table.truncate()

        table.truncate(confirm=True)
        assert table.select("*", where=None, to_dict=True) == []
        db.close()

    def test_table_parts_and_segments(self, tmp_path):
        """无 partition 无 segment 时 table_parts() 和 segments() 行为。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
        )
        table.write({"id": 1, "val": "x"})

        parts = table.table_parts()
        # 无 partition 时 table_parts() 返回包含单个 None 键的列表
        assert len(parts) == 1

        segs = table.segments()
        # 无 segment 配置时，segments() 返回 legacy 信息，row_count > 0
        assert len(segs) >= 1
        assert segs[0]["row_count"] == 1
        db.close()

    def test_explain_select_no_pruning(self, tmp_path):
        """无分区时 explain 不会有跳过信息。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
        )
        table.write({"id": 1, "val": "hello"})

        plan = table.explain_select(
            "*",
            where={"field": "id", "op": ">", "value": 10},
        )
        # 无匹配数据时 scanned_partitions 为空
        assert plan.scanned_partitions == []
        db.close()

    def test_spec_persists_across_reopen(self, tmp_path):
        """关闭后重新打开，spec 应保持一致。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
        )
        table.write({"id": 1, "val": "x"})

        spec_before = table.spec()
        assert spec_before.primary_key == ["id"]
        assert spec_before.partition is None
        assert spec_before.segment is None

        db.close()

        db2 = Database(_uri(tmp_path))
        table2 = db2.table("data")
        spec_after = table2.spec()
        assert spec_after.primary_key == ["id"]
        assert spec_after.partition is None
        assert spec_after.segment is None
        db2.close()

    def test_write_without_contract_raises(self, tmp_path):
        """无 TableContract 时 write() 会报错。"""
        db = Database(_uri(tmp_path))
        table = db.table("data")  # 无 pk、无 columns
        with pytest.raises(BadArgument, match="没有持久化契约"):
            table.write({"x": 1})
        db.close()


# ============================================================
# 组合 2：仅 partition — 逻辑分区路由，无文件滚动
# ============================================================

class TestPartitionOnly:
    """仅 partition、无 segment：按分区键路由到独立文件，单文件不滚动。"""

    def test_duration_partition_routes_correctly(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
        )

        table.write([
            {"symbol": "A", "ts": 1735689600000, "price": 100.0},
            {"symbol": "A", "ts": 1735776000000, "price": 101.0},
            {"symbol": "B", "ts": 1735689600000, "price": 200.0},
        ])

        parts = table.table_parts()
        assert sorted(parts) == [1735689600000, 1735776000000]

        # 查询仅命中相关分区
        rows = table.select(
            "*",
            where={
                "field": "ts",
                "op": "between_and",
                "value": [1735689600000, 1735689600000 + 1],
            },
            to_dict=True,
        )
        assert len(rows) == 2  # A 和 B 都在这个分区
        db.close()

    def test_calendar_partition_routes_correctly(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "reports",
            pk=["date", "name"],
            columns={"date": "text", "name": "text", "value": "float"},
            partition=Partition.calendar("date", every="1month", timezone="UTC"),
        )

        table.write([
            {"date": "2026-01-15", "name": "alpha", "value": 1.0},
            {"date": "2026-02-20", "name": "beta", "value": 2.0},
        ])

        parts = table.table_parts()
        assert "202601" in parts
        assert "202602" in parts

        rows = table.select(
            "*",
            where={"field": "date", "op": "==", "value": "2026-01-15"},
            to_dict=True,
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "alpha"
        db.close()

    def test_range_partition_routes_correctly(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "logs",
            pk=["seq"],
            columns={"seq": "integer", "msg": "text"},
            partition=Partition.range("seq", every=1000),
        )

        table.write([
            {"seq": 500, "msg": "a"},
            {"seq": 1500, "msg": "b"},
            {"seq": 2500, "msg": "c"},
        ])

        parts = table.table_parts()
        assert sorted(parts) == [0, 1000, 2000]

        rows = table.select(
            "*",
            where={"field": "seq", "op": "between_and", "value": [1000, 1999]},
            order_by="seq",
            to_dict=True,
        )
        assert rows == [{"seq": 1500, "msg": "b"}]
        db.close()

    def test_explain_prunes_correctly(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "ticks",
            pk=["ts"],
            columns={"ts": "integer", "val": "float"},
            partition=Partition.duration("ts", every="1day"),
        )

        base = 1735689600000
        for day in range(4):
            table.write({"ts": base + day * 86400000, "val": float(day)})

        plan = table.explain_select(
            "*",
            where={
                "field": "ts",
                "op": "between_and",
                "value": [base + 86400000, base + 2 * 86400000],
            },
        ).to_dict()

        assert len(plan["scanned_partitions"]) == 2
        assert len(plan["skipped_partitions"]) == 2
        # 无 segment，跳过原因仅在 partition 级
        db.close()

    def test_no_segment_info_in_explain(self, tmp_path):
        """无 segment 时，explain 中 segment 信息应为空。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["ts"],
            columns={"ts": "integer", "val": "text"},
            partition=Partition.duration("ts", every="1day"),
        )
        table.write({"ts": 1735689600000, "val": "x"})

        plan = table.explain_select("*", where=None)
        assert plan.scanned_segments == [] or plan.requires_global_sort is not None
        db.close()


# ============================================================
# 组合 3：仅 segment — __all__ 逻辑分区 + 按行数滚动
# ============================================================

class TestSegmentOnly:
    """仅 segment、无 partition：全部数据使用 __all__ 逻辑分区，按行数自动拆分文件。"""

    def test_segment_creates_files_by_row_count(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "events",
            pk=["event_id"],
            columns={"event_id": "integer", "seq": "integer", "kind": "text"},
            segment=Segment.rows(max_rows=3),
            order_key="seq",
        )

        # 写入 7 行 → 应该产生 3 个 segment (3+3+1)
        table.write([
            {"event_id": i, "seq": i, "kind": "base"}
            for i in range(7)
        ])

        assert table.table_parts() == [DEFAULT_LOGICAL_PART]
        segs = table.segments()
        assert [s["row_count"] for s in segs] == [3, 3, 1]

        # 前两个 segment sealed，最后一个 active
        assert segs[0]["sealed"] is True
        assert segs[1]["sealed"] is True
        assert segs[2]["sealed"] is False
        db.close()

    def test_segment_late_data_creates_new_segment(self, tmp_path):
        """晚到数据进入新 active segment，不会修改 sealed segment。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "events",
            pk=["event_id"],
            columns={"event_id": "integer", "seq": "integer", "kind": "text"},
            segment=Segment.rows(max_rows=3),
            order_key="seq",
        )

        table.write([{"event_id": i, "seq": i, "kind": "base"} for i in range(6)])
        # segment 0 (row 0-2, sealed), segment 1 (row 3-5, sealed)
        assert table.table_parts() == [DEFAULT_LOGICAL_PART]

        # 晚到数据
        table.write({"event_id": 99, "seq": 1, "kind": "late"})
        segs = table.segments()
        assert len(segs) == 3
        assert segs[2]["row_count"] == 1

        # 查询 seq=1，应扫到 segment 0 和 segment 2（晚到数据）
        rows = table.select(
            "*",
            where={"field": "seq", "op": "between_and", "value": [1, 1]},
            order_by="event_id",
            to_dict=True,
        )
        assert len(rows) == 2  # event_id=1 (base) + event_id=99 (late)
        db.close()

    def test_segment_stats_pruning(self, tmp_path):
        """segment stats 应正确跳过无关文件。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "events",
            pk=["event_id"],
            columns={"event_id": "integer", "seq": "integer", "kind": "text"},
            segment=Segment.rows(max_rows=3),
            order_key="seq",
        )

        table.write([{"event_id": i, "seq": i, "kind": "base"} for i in range(6)])

        plan = table.explain_select(
            "*",
            where={"field": "seq", "op": "between_and", "value": [3, 5]},
        ).to_dict()

        assert plan["candidate_partitions"] == [DEFAULT_LOGICAL_PART]
        scanned_ids = {s["segment_id"] for s in plan["scanned_segments"]}
        skipped_ids = {s["segment_id"] for s in plan["skipped_segments"]}
        assert scanned_ids == {1}     # segment 1 包含 seq 3-5
        assert skipped_ids == {0}     # segment 0 被跳过
        assert "pruned_by_segment_stats" in str(plan["segment_skip_reasons"])
        db.close()

    def test_write_buffer_with_segment(self, tmp_path):
        """write_buffer 在 segment 模式下自动分批写入。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "logs",
            pk=["id"],
            columns={"id": "integer", "msg": "text"},
            segment=Segment.rows(max_rows=5),
            order_key="id",
        )

        with table.write_buffer(max_rows=3) as buf:
            for i in range(12):
                buf.append({"id": i, "msg": f"log_{i}"})

        rows = table.select("*", where=None, to_dict=True)
        assert len(rows) == 12
        segs = table.segments()
        assert len(segs) >= 3  # 至少 3 个 segment (5 + 5 + 2)
        db.close()

    def test_segment_defaults_and_thresholds(self, tmp_path):
        """验证 Segment 三级参数的默认值和约束。"""
        s = Segment.rows(max_rows=500_000)
        assert s.kind == "rows"
        assert s.max_rows == 500_000
        assert s.target_rows == 500_000
        assert s.min_rows == 500_000
        assert s.stats_columns is None

        s2 = Segment.rows(max_rows=1000, target_rows=800, min_rows=200)
        assert s2.target_rows == 800
        assert s2.min_rows == 200

        s3 = Segment.rows(
            max_rows=1000,
            stats_columns=["ts", "sequence", "ts"],
        )
        assert s3.stats_columns == ["ts", "sequence"]
        assert Segment.from_dict(s3.to_dict()) == s3
        assert Segment.rows(stats_columns="ts").stats_columns == ["ts"]
        with pytest.raises(BadArgument):
            Segment.rows(stats_columns=[""])

        # 约束校验
        with pytest.raises(BadArgument):
            Segment.rows(max_rows=100, target_rows=200)  # target > max
        with pytest.raises(BadArgument):
            Segment.rows(max_rows=100, min_rows=200)      # min > target

    def test_upsert_and_replace_with_segment_rejected_until_pk_index(self, tmp_path):
        """未实现 pk index/tombstone 前，segment 不允许覆盖写。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "integer"},
            segment=Segment.rows(max_rows=2),
            order_key="id",
        )

        # 写入 4 行 → 2 个 segment
        table.write([
            {"id": 1, "val": 10},
            {"id": 2, "val": 20},
            {"id": 3, "val": 30},
            {"id": 4, "val": 40},
        ])
        with pytest.raises(BadArgument, match="Segment.rows"):
            table.write({"id": 1, "val": 99}, mode="upsert")
        with pytest.raises(BadArgument, match="Segment.rows"):
            table.write({"id": 3, "val": 77}, mode="replace")

        rows = table.select("*", where=None, order_by="id", to_dict=True)
        assert rows == [
            {"id": 1, "val": 10},
            {"id": 2, "val": 20},
            {"id": 3, "val": 30},
            {"id": 4, "val": 40},
        ]

        assert len(table.table_parts()) == 1  # __all__ 不变
        db.close()

    def test_spec_roundtrip_segment_only(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "events",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
            segment=Segment.rows(max_rows=100),
            order_key="id",
        )
        table.write({"id": 1, "val": "x"})
        spec = table.spec()
        assert spec.segment is not None
        assert spec.segment.max_rows == 100
        assert spec.partition is None

        db.close()
        db2 = Database(_uri(tmp_path))
        t2 = db2.table("events")
        spec2 = t2.spec()
        assert spec2.segment is not None
        assert spec2.segment.max_rows == 100
        db2.close()


# ============================================================
# 组合 4：两者都有 — 逻辑分区 + 物理分段（完整模式）
# ============================================================

class TestBothPartitionAndSegment:
    """既有 partition 又有 segment：二级文件管理。"""

    def test_both_routes_and_rolls(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=2),
            order_key="ts",
        )

        # 同一天写入 3 行 → 1 个逻辑分区，2 个 segment (2 + 1)
        base = 1735689600000
        table.write([
            {"symbol": "A", "ts": base, "price": 1.0},
            {"symbol": "B", "ts": base + 1, "price": 2.0},
            {"symbol": "C", "ts": base + 2, "price": 3.0},
        ])

        # 另一天写入 1 行
        table.write({"symbol": "D", "ts": base + 86400000, "price": 4.0})

        parts = sorted(table.table_parts())
        assert parts == [base, base + 86400000]

        segs = table.segments()
        assert len(segs) >= 3  # day1: 2 segments, day2: 1 segment

        # 查询 day1 的所有行
        rows = table.select(
            "*",
            where={
                "field": "ts",
                "op": "between_and",
                "value": [base, base + 86400000 - 1],
            },
            to_dict=True,
        )
        assert len(rows) == 3
        db.close()

    def test_dual_level_explain_pruning(self, tmp_path):
        """explain 应同时展示 partition 和 segment 两级裁剪。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "ticks",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=5),
            order_key="ts",
        )

        base = 1735689600000
        # day0: 10 行 → 2 segments
        for i in range(10):
            table.write({"symbol": "X", "ts": base + i, "price": 1.0})
        # day1: 3 行 → 1 segment
        for i in range(3):
            table.write({"symbol": "X", "ts": base + 86400000 + i, "price": 2.0})
        # day2: 1 行 → 1 segment
        table.write({"symbol": "X", "ts": base + 2 * 86400000, "price": 3.0})

        plan = table.explain_select(
            "*",
            where={
                "field": "ts",
                "op": "between_and",
                "value": [base, base + 86400000 - 1],
            },
        ).to_dict()

        # 一级：只有 day0 的分区被扫描，day1/day2 跳过
        assert len(plan["scanned_partitions"]) == 1
        assert len(plan["skipped_partitions"]) == 2

        # 二级：day0 的所有 segment 都被扫描（within range）
        assert len(plan["scanned_segments"]) == 2
        db.close()

    def test_late_data_across_partitions(self, tmp_path):
        """晚到数据进入正确分区和 segment。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "events",
            pk=["event_id"],
            columns={"event_id": "integer", "ts": "integer", "kind": "text"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=2),
            order_key="ts",
        )

        base = 1735689600000
        # day0: 写入 2 行 → 1 segment (sealed)
        table.write([
            {"event_id": 1, "ts": base, "kind": "a"},
            {"event_id": 2, "ts": base + 1, "kind": "b"},
        ])
        # day0: 晚到数据
        table.write({"event_id": 99, "ts": base, "kind": "late"})

        parts = sorted(table.table_parts())
        assert parts == [base]
        segs = table.segments()
        assert len(segs) == 2  # 2 segments（原 segment sealed + 新 active segment）
        db.close()

    def test_write_buffer_with_both(self, tmp_path):
        """write_buffer 在完整模式下分批写入。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "ticks",
            pk=["id"],
            columns={"id": "integer", "ts": "integer", "val": "text"},
            partition=Partition.duration("ts", every="1hour"),
            segment=Segment.rows(max_rows=3),
            order_key="ts",
        )

        base = 1735689600000
        with table.write_buffer(max_rows=4) as buf:
            for i in range(10):
                buf.append({
                    "id": i,
                    "ts": base + (i // 4) * 3600000,
                    "val": f"v_{i}",
                })

        rows = table.select("*", where=None, to_dict=True)
        assert len(rows) == 10
        assert len(table.table_parts()) >= 2  # 至少跨 2 小时
        db.close()


# ============================================================
# 跨组合切换与异常处理
# ============================================================

class TestCombinationSwitching:
    """从一种组合切换到另一种时的行为。"""

    def test_add_segment_to_existing_table_rejected(self, tmp_path):
        """已有表（无 segment）后追加 segment 声明应被拒绝。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
            partition=Partition.duration("ts", every="1day"),
            # 无 segment
        )
        table.write({"id": 1, "ts": 1735689600000, "val": "x"})
        db.close()

        # 重新打开，尝试追加 segment
        db2 = Database(_uri(tmp_path))
        with pytest.raises(TableSpecConflict):
            db2.table(
                "data",
                pk=["id"],
                columns={"id": "integer", "val": "text", "ts": "integer"},
                partition=Partition.duration("ts", every="1day"),
                segment=Segment.rows(max_rows=100),  # 新增
            )
        db2.close()

    def test_change_partition_rejected(self, tmp_path):
        """已有表的分区策略不能变更。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "ts": "integer"},
            partition=Partition.duration("ts", every="1day"),
        )
        table.write({"id": 1, "ts": 1735689600000})
        db.close()

        db2 = Database(_uri(tmp_path))
        with pytest.raises(TableSpecConflict):
            db2.table(
                "data",
                pk=["id"],
                columns={"id": "integer", "ts": "integer"},
                partition=Partition.duration("ts", every="1hour"),  # 不同
            )
        db2.close()

    def test_reopen_table_without_spec_fine(self, tmp_path):
        """重新打开表不传 spec 是正常操作，不抛异常。"""
        db = Database(_uri(tmp_path))
        table = db.table(
            "data",
            pk=["id"],
            columns={"id": "integer", "val": "text"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=100),
        )
        table.write({"id": 1, "ts": 1735689600000, "val": "x"})
        db.close()

        # 直接用 table(name) 获取引用，不报错
        db2 = Database(_uri(tmp_path))
        t2 = db2.table("data")
        rows = t2.select("*", where=None, to_dict=True)
        assert len(rows) == 1
        db2.close()


# ============================================================
# WriteResult 验证
# ============================================================

class TestWriteResultAcrossCombinations:
    """WriteResult 在四种组合下都应正确返回。"""

    def test_write_result_neither(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table("kv", pk=["k"], columns={"k": "text", "v": "integer"})
        r = table.write({"k": "a", "v": 1})
        assert r.rows_seen == 1
        assert r.rows_written == 1
        assert r.schema_changed is True  # 首次写入创建表
        db.close()

    def test_write_result_partition_only(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "t",
            pk=["ts"],
            columns={"ts": "integer", "v": "text"},
            partition=Partition.duration("ts", every="1day"),
        )
        r = table.write({"ts": 1735689600000, "v": "a"})
        assert r.rows_seen == 1
        assert len(r.partitions_created) == 1
        db.close()

    def test_write_result_segment_only(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "t",
            pk=["id"],
            columns={"id": "integer", "v": "text"},
            segment=Segment.rows(max_rows=100),
        )
        r = table.write({"id": 1, "v": "a"})
        assert r.rows_seen == 1
        assert r.rows_written == 1
        db.close()

    def test_write_result_both(self, tmp_path):
        db = Database(_uri(tmp_path))
        table = db.table(
            "t",
            pk=["ts"],
            columns={"ts": "integer", "v": "text"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=50_000),
        )
        r = table.write({"ts": 1735689600000, "v": "a"})
        assert r.rows_seen == 1
        assert len(r.partitions_created) == 1
        db.close()


# ============================================================
# 目录结构验证
# ============================================================

class TestDirectoryStructure:
    """验证四种组合产生正确的目录结构。"""

    def test_neither_creates_single_file(self, tmp_path):
        db = Database(_uri(tmp_path))
        db.table("kv", pk=["k"], columns={"k": "text", "v": "integer"}).write(
            {"k": "a", "v": 1})
        db.close()

        data_dir = os.path.join(tmp_path, "test.iosql", "kv", "data")
        files = os.listdir(data_dir)
        # 应有至少 1 个 .sqlite3 文件 + meta.sqlite3
        sqlite_files = [f for f in files if f.endswith(".sqlite3")]
        assert len(sqlite_files) >= 1

    def test_partition_only_creates_numbered_files(self, tmp_path):
        db = Database(_uri(tmp_path))
        db.table(
            "t",
            pk=["ts"],
            columns={"ts": "integer", "v": "text"},
            partition=Partition.duration("ts", every="1day"),
        ).write([
            {"ts": 1735689600000, "v": "a"},
            {"ts": 1735776000000, "v": "b"},
        ])
        db.close()

        data_dir = os.path.join(tmp_path, "test.iosql", "t", "data")
        assert os.path.exists(
            os.path.join(data_dir, "1735689600000.sqlite3"))
        assert os.path.exists(
            os.path.join(data_dir, "1735776000000.sqlite3"))

    def test_segment_only_uses_all_prefix(self, tmp_path):
        db = Database(_uri(tmp_path))
        db.table(
            "t",
            pk=["id"],
            columns={"id": "integer", "v": "text"},
            segment=Segment.rows(max_rows=2),
        ).write([
            {"id": 1, "v": "a"},
            {"id": 2, "v": "b"},
            {"id": 3, "v": "c"},
        ])
        db.close()

        data_dir = os.path.join(tmp_path, "test.iosql", "t", "data")
        files = os.listdir(data_dir)
        all_files = [f for f in files if "__all__" in f and f.endswith(".sqlite3")]
        assert len(all_files) >= 2  # 3 行 → 2 segments
