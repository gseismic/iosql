"""PLAN-062 核心元数据层回归测试。

覆盖 review 报告 P0-3 与核心元数据 P1/P2：
- calendar fmt 白名单（路径穿越/多级目录）
- Partition.calendar every 透传
- 浮点分区键归一（range/duration）
- migrate_to(dry_run) 纯度与选项校验
- Database.rename 身份校验/根家目录/符号链接/回滚
- 4month/4year 往返
- TimeColumn(unit='date') 时区
- spec=/force_partition 绑定透传
- P2 批次：Segment/PhysicalLayout/PartitionSpec/parse_interval_ms/
  manifest 缺 key/rename_table 回滚/_tables 并发锁/delete(None) 警告
"""

import os
import sqlite3
import threading

import pytest

import iosql.database as database_module
from iosql import (
    Database,
    Partition,
    PartitionSpec,
    Segment,
    TableContract,
    TimeColumn,
    format_partition_spec,
    parse_partition_spec,
)
from iosql.exception import (
    BadArgument,
    PartitionSpecError,
    TableSpecConflict,
    UnsafeOperation,
)
from iosql.partition import CalendarPartition, DurationPartition, RangePartition
from iosql.partition.duration import parse_interval_ms
from iosql.spec import PhysicalLayout


def _uri(path):
    return f"sqlite://{path}"


def _make_kv_db(path, table_name="kv"):
    db = Database(_uri(path))
    db.table(
        table_name, pk=["k"], columns={"k": "text", "v": "integer"},
    ).write({"k": "a", "v": 1})
    db.close()
    return path


# ---------------------------------------------------------------
# P0-3 calendar fmt 白名单
# ---------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_fmt",
    ["../../../pwn", "YYYY/MM/DD", "YYYY.MM.DD", "YYYYMMDD;--", " ../pwn", ""],
)
def test_calendar_fmt_whitelist_rejects_path_like_values(bad_fmt):
    with pytest.raises(PartitionSpecError):
        CalendarPartition(fmt=bad_fmt, tz="UTC", ts_col="d")


def test_calendar_fmt_rejected_from_spec_and_meta():
    with pytest.raises(PartitionSpecError):
        parse_partition_spec("calendar(d,every=1day,fmt=../../../pwn)")
    with pytest.raises(PartitionSpecError):
        parse_partition_spec("calendar(d,every=1day,fmt=YYYY/MM/DD)")
    with pytest.raises(PartitionSpecError):
        CalendarPartition.from_meta({
            "type": "calendar", "ts_col": "d", "fmt": "YYYY/MM/DD", "tz": "UTC",
        })
    with pytest.raises(PartitionSpecError, match="缺少字段"):
        CalendarPartition.from_meta({"type": "calendar", "ts_col": "d"})


def test_calendar_fmt_traversal_never_writes_files(tmp_path):
    db_path = tmp_path / "db.iosql"
    db = Database(_uri(db_path))
    try:
        with pytest.raises((PartitionSpecError, BadArgument)):
            db.table(
                "t", pk=["d"], columns={"d": "text"},
                partition="calendar(d,every=1day,fmt=../../../pwn)",
            )
    finally:
        db.close()
    assert not list(tmp_path.rglob("pwn.sqlite3"))
    assert not (db_path / "t" / "YYYY").exists()


# ---------------------------------------------------------------
# C1-1 Partition.calendar 透传 every
# ---------------------------------------------------------------

def test_partition_calendar_keeps_every_bucket_and_meta():
    week = Partition.calendar("d", every="1week")
    assert week.every == "1week"
    assert week.to_meta()["every"] == "1week"
    # 2025-01-05（周日）归属 2024-12-30 起始的周桶
    assert week.get_key({"d": "2025-01-05"}) == "20241230"

    four_day = Partition.calendar("d", every="4day")
    assert four_day.every == "4day"
    assert four_day.get_key({"d": "2025-01-03"}) == "20250101"
    assert four_day.get_key({"d": "2025-01-05"}) == "20250105"

    restored = CalendarPartition.from_meta(four_day.to_meta())
    assert restored.every == "4day"
    assert restored.get_key({"d": "2025-01-05"}) == "20250105"


def test_calendar_every_write_and_reopen(tmp_path):
    db_path = tmp_path / "cal.iosql"
    db = Database(_uri(db_path))
    try:
        table = db.table(
            "events", pk=["d", "name"],
            columns={"d": "text", "name": "text"},
            partition=Partition.calendar("d", every="4day", timezone="UTC"),
        )
        table.write([
            {"d": "2025-01-03", "name": "a"},
            {"d": "2025-01-05", "name": "b"},
        ])
        assert table.table_parts() == ["20250101", "20250105"]
    finally:
        db.close()
    reopened = Database(_uri(db_path))
    try:
        spec = reopened.table("events").spec()
        assert spec.partition.every == "4day"
        assert spec.partition.to_partition().to_meta()["every"] == "4day"
    finally:
        reopened.close()


# ---------------------------------------------------------------
# C1-2 浮点分区键归一
# ---------------------------------------------------------------

def test_range_and_duration_float_keys_normalize_to_int():
    rng = RangePartition(width=5, column="id")
    assert rng.get_key({"id": 0.0}) == 0
    assert isinstance(rng.get_key({"id": 0.0}), int)
    assert rng.get_key({"id": 7.5}) == 5

    dur = DurationPartition(interval_ms=86_400_000, ts_col="ts")
    assert dur.get_key({"ts": 0.0}) == 0
    assert isinstance(dur.get_key({"ts": 0.5}), int)
    assert dur.get_key({"ts": 86_400_000.0}) == 86_400_000

    with pytest.raises(PartitionSpecError):
        RangePartition(width=2.5, column="id")
    with pytest.raises(PartitionSpecError):
        RangePartition(width=True, column="id")
    with pytest.raises(PartitionSpecError):
        RangePartition(width=0, column="id")


def test_select_equality_hits_float_written_rows(tmp_path):
    db = Database(_uri(tmp_path / "float.iosql"))
    try:
        rng = db.table(
            "r", pk=["id"], columns={"id": "float", "v": "integer"},
            partition=Partition.range("id", every=5),
        )
        rng.write([{"id": 0.0, "v": 1}, {"id": 7.5, "v": 2}])
        assert rng.table_parts() == [0, 5]
        assert rng.select(
            where={"field": "id", "op": "==", "value": 0}, to_dict=True,
        ) == [{"id": 0.0, "v": 1}]

        dur = db.table(
            "d", pk=["ts"], columns={"ts": "float", "v": "integer"},
            partition=Partition.duration("ts", every="1day"),
        )
        dur.write([{"ts": 0.0, "v": 1}])
        assert dur.table_parts() == [0]
        assert len(dur.select(
            where={"field": "ts", "op": "==", "value": 0}, to_dict=True,
        )) == 1
    finally:
        db.close()


# ---------------------------------------------------------------
# C1-3 migrate_to(dry_run) 纯度与选项校验
# ---------------------------------------------------------------

def test_migrate_to_dry_run_validates_options_and_writes_nothing(tmp_path):
    db_path = tmp_path / "src.iosql"
    db = Database(_uri(db_path))
    target = tmp_path / "target.iosql"
    try:
        db.table(
            "kv", pk=["k"], columns={"k": "text", "v": "integer"},
        ).write({"k": "a", "v": 1})

        for kwargs in [
            {"tables": []},
            {"tables": 123},
            {"tables": ["missing"]},
            {"mode": "bogus"},
        ]:
            with pytest.raises(BadArgument):
                db.migrate_to(_uri(target), dry_run=True, **kwargs)
        with pytest.raises(BadArgument):
            db.migrate_to("mysql://u:p@h:1/db", tables=["kv"], dry_run=True)
        with pytest.raises(BadArgument):
            db.migrate_to("", tables=["kv"], dry_run=True)

        assert not target.exists()
        assert not (db_path / "missing").exists()

        result = db.migrate_to(_uri(target), dry_run=True)
        assert result.dry_run is True
        assert result.tables[0].source_rows == 1
        assert not target.exists()
    finally:
        db.close()


# ---------------------------------------------------------------
# C1-4 Database.rename 身份校验与回滚
# ---------------------------------------------------------------

def test_rename_rejects_non_iosql_source_and_target(tmp_path):
    source = tmp_path / "src.iosql"
    _make_kv_db(source)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep")

    db = Database(_uri(source))
    try:
        with pytest.raises(UnsafeOperation):
            db.rename(str(victim), overwrite=True)
        assert (victim / "important.txt").read_text() == "keep"
    finally:
        db.close()

    notdb = tmp_path / "notdb"
    notdb.mkdir()
    (notdb / "x.txt").write_text("x")
    with pytest.raises(UnsafeOperation):
        Database(_uri(notdb)).rename(str(tmp_path / "dst.iosql"))


def test_rename_rejects_root_home_and_symlink_target(tmp_path):
    source = tmp_path / "src.iosql"
    _make_kv_db(source)
    db = Database(_uri(source))
    try:
        for forbidden in ["/", os.path.expanduser("~")]:
            with pytest.raises(UnsafeOperation):
                db.rename(forbidden, overwrite=True)
        link = tmp_path / "link.iosql"
        link.symlink_to(tmp_path / "elsewhere.iosql")
        with pytest.raises(UnsafeOperation, match="符号链接"):
            db.rename(str(link), overwrite=True)
    finally:
        db.close()


def test_rename_overwrite_failure_rolls_back(tmp_path, monkeypatch):
    source = tmp_path / "src.iosql"
    target = tmp_path / "tgt.iosql"
    _make_kv_db(source, "kv")
    _make_kv_db(target, "old")

    real_replace = os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        if str(src).endswith("src.iosql") and str(dst).endswith("tgt.iosql"):
            raise OSError(18, "EXDEV: cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(database_module.os, "replace", flaky_replace)
    db = Database(_uri(source))
    try:
        with pytest.raises(BadArgument, match="重命名数据库失败"):
            db.rename(str(target), overwrite=True)
        assert (target / "old" / "meta.sqlite3").exists()
        assert (source / "kv" / "meta.sqlite3").exists()
        assert db.table("kv").select(to_dict=True) == [{"k": "a", "v": 1}]
        assert not list(tmp_path.glob("*rename_tmp*"))
    finally:
        db.close()


# ---------------------------------------------------------------
# C1-5 4month/4year 往返
# ---------------------------------------------------------------

@pytest.mark.parametrize(
    "every_ms",
    [
        604_800_000,
        2_592_000_000,
        5_184_000_000,
        7_776_000_000,
        10_368_000_000,
        31_536_000_000,
        63_072_000_000,
        126_144_000_000,
        157_680_000_000,
        315_360_000_000,
    ],
)
def test_duration_every_format_parse_roundtrip(every_ms):
    text = format_partition_spec(
        PartitionSpec(column="ts", every=every_ms, kind="duration")
    )
    restored = parse_partition_spec(text)
    assert restored.to_partition().interval_ms == every_ms
    assert parse_interval_ms(
        text[len("duration(ts,every="):-1]
    ) == every_ms


def test_format_duration_every_emits_parseable_month_year_units():
    assert format_partition_spec(
        PartitionSpec(column="ts", every=10_368_000_000, kind="duration")
    ) == "duration(ts,every=4month)"
    assert format_partition_spec(
        PartitionSpec(column="ts", every=126_144_000_000, kind="duration")
    ) == "duration(ts,every=4year)"
    assert format_partition_spec(
        PartitionSpec(column="ts", every=3_600_000.0, kind="duration")
    ) == "duration(ts,every=1hour)"


# ---------------------------------------------------------------
# C1-6 TimeColumn date 时区
# ---------------------------------------------------------------

def test_timecolumn_date_normalize_respects_timezone():
    evening = "2026-01-05T20:00:00+00:00"
    assert TimeColumn(
        "d", unit="date", timezone="Asia/Shanghai",
    ).normalize_value(evening) == "2026-01-06"
    assert TimeColumn(
        "d", unit="date", timezone="UTC",
    ).normalize_value(evening) == "2026-01-05"


# ---------------------------------------------------------------
# C1-7 spec= 绑定与 force_partition
# ---------------------------------------------------------------

def test_spec_declared_conflict_applied_on_existing_table(tmp_path):
    db = Database(_uri(tmp_path / "spec.iosql"))
    try:
        table = db.table(
            "t", pk=["k"], columns={"k": "text", "v": "integer"},
        )
        table.write({"k": "a", "v": 1})
        declared = TableContract(
            primary_key=["k"],
            columns={"k": "text", "v": "integer"},
            conflict="ignore",
        )
        bound = db.table("t", spec=declared)
        assert bound.spec().conflict == "ignore"
        bound.write({"k": "a", "v": 2})
        assert bound.select(to_dict=True) == [{"k": "a", "v": 1}]

        override = TableContract(
            primary_key=["k"], columns={"k": "text", "v": "integer"},
        )
        rebound = db.table("t", spec=override, conflict="ignore")
        assert rebound.spec().conflict == "ignore"
    finally:
        db.close()


def test_spec_order_key_conflict_raises_with_hint(tmp_path):
    db = Database(_uri(tmp_path / "spec2.iosql"))
    try:
        db.table(
            "t", pk=["k"], columns={"k": "text", "v": "integer"},
            order_key="k",
        ).write({"k": "a", "v": 1})
        declared = TableContract(
            primary_key=["k"], columns={"k": "text", "v": "integer"},
            order_key="v",
        )
        with pytest.raises(TableSpecConflict, match="修复"):
            db.table("t", spec=declared)
    finally:
        db.close()


def test_force_partition_shortcut_rejects_existing_partition_change(tmp_path):
    db_path = tmp_path / "force.iosql"
    db = Database(_uri(db_path))
    try:
        table = db.table(
            "t", pk=["ts"], columns={"ts": "integer", "v": "integer"},
            partition=Partition.duration("ts", every="1day"),
        )
        table.write({"ts": 1000, "v": 1})
        assert table.table_parts() == [0]

        with pytest.raises(TableSpecConflict, match="repartition_to"):
            db.table(
                "t", pk=["ts"], columns={"ts": "integer", "v": "integer"},
                partition=Partition.duration("ts", every="1hour"),
                force_partition=True,
            )
        assert table.spec().partition.to_partition().interval_ms == 86_400_000
        assert table.select() == [{"ts": 1000, "v": 1}]
    finally:
        db.close()

    reopened = Database(_uri(db_path))
    try:
        spec = reopened.table("t").spec()
        assert spec.partition.to_partition().interval_ms == 86_400_000
    finally:
        reopened.close()


# ---------------------------------------------------------------
# P2：migrate/ timeseries / 表达式 / 校验
# ---------------------------------------------------------------

def test_timeseries_timecolumn_does_not_pollute_order_key(tmp_path):
    db = Database(_uri(tmp_path / "ts.iosql"))
    try:
        table = db.timeseries(
            "t",
            TimeColumn("ts", unit="ms", timezone="UTC"),
            pk=["ts"],
            columns={"ts": "integer", "v": "integer"},
        )
        assert table.spec().order_key == "ts"
        assert table.spec().time.name == "ts"
        table.write({"ts": 1000, "v": 1})
        assert table.query(start=0, end=2000).to_list() == [
            {"ts": 1000, "v": 1},
        ]
    finally:
        db.close()


def test_format_partition_spec_quoted_column_roundtrip_or_reject():
    assert format_partition_spec(
        parse_partition_spec("duration('my col',every=1day)")
    ) == "duration('my col',every=1day)"
    with pytest.raises(PartitionSpecError):
        format_partition_spec(PartitionSpec(column="a'b", every="1day"))
    with pytest.raises(PartitionSpecError):
        parse_partition_spec("duration(a'b,every=1day)")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PartitionSpec("x", kind="bogus"),
        lambda: PartitionSpec("", every="1day"),
        lambda: PartitionSpec("x", kind="range", every=0),
        lambda: PartitionSpec("x", kind="range", every=2.5),
        lambda: PartitionSpec("x", kind="range", every=True),
        lambda: PartitionSpec("x", kind="calendar", every="bogus"),
        lambda: PartitionSpec("x", kind="calendar", fmt="YYYY/MM/DD"),
        lambda: PartitionSpec("x", kind="duration", every=True),
        lambda: PartitionSpec.from_dict({}),
        lambda: PartitionSpec.from_dict({"column": "x", "kind": "bogus"}),
    ],
)
def test_partition_spec_invalid_inputs_raise_typed(factory):
    with pytest.raises((PartitionSpecError, BadArgument)):
        factory()


def test_parse_interval_ms_numeric_forms_and_bool_rejection():
    assert parse_interval_ms(3_600_000) == 3_600_000
    assert parse_interval_ms(3_600_000.0) == 3_600_000
    assert parse_interval_ms("3600000") == 3_600_000
    assert parse_interval_ms("4month") == 10_368_000_000
    assert parse_interval_ms("4year") == 126_144_000_000
    with pytest.raises(BadArgument):
        parse_interval_ms(True)
    with pytest.raises(PartitionSpecError):
        parse_interval_ms(1.5)
    with pytest.raises(PartitionSpecError):
        parse_interval_ms(0)


def test_corrupt_partition_meta_raises_typed_with_hint(tmp_path):
    with pytest.raises(PartitionSpecError, match="缺少字段"):
        DurationPartition.from_meta({"type": "duration", "ts_col": "k"})
    with pytest.raises(PartitionSpecError, match="缺少字段"):
        RangePartition.from_meta({"type": "range", "ts_col": "k"})
    with pytest.raises(PartitionSpecError, match="缺少"):
        PartitionSpec.from_partition_meta({"type": "duration", "ts_col": "k"})
    with pytest.raises(PartitionSpecError):
        PartitionSpec.from_partition_meta({"type": "bogus", "ts_col": "k"})

    db_path = tmp_path / "corrupt.iosql"
    db = Database(_uri(db_path))
    try:
        table = db.table(
            "kv", pk=["k"], columns={"k": "integer", "v": "integer"},
            partition=Partition.duration("k", every="1day"),
        )
        table.write({"k": 123456, "v": 1})
    finally:
        db.close()

    meta_file = db_path / "kv" / "meta.sqlite3"
    con = sqlite3.connect(str(meta_file))
    try:
        import json
        row = con.execute(
            "select meta from meta where table_name = ?", ("kv",),
        ).fetchone()
        meta = json.loads(row[0])
        del meta["partition"]["interval_ms"]
        con.execute(
            "update meta set meta = ? where table_name = ?",
            (json.dumps(meta), "kv"),
        )
        con.commit()
    finally:
        con.close()

    reopened = Database(_uri(db_path))
    try:
        with pytest.raises(PartitionSpecError, match="缺少字段"):
            reopened.table("kv").spec()
    finally:
        reopened.close()


def test_segment_and_physical_layout_explicit_validation():
    with pytest.raises(BadArgument):
        Segment(max_rows=100, target_rows=0)
    with pytest.raises(BadArgument):
        Segment(max_rows=100, min_rows=0)
    with pytest.raises(BadArgument):
        Segment(max_rows=0)
    with pytest.raises(BadArgument):
        PhysicalLayout(target_rows_per_file=None)
    with pytest.raises(BadArgument):
        PhysicalLayout(target_rows_per_file=0)
    layout = PhysicalLayout(
        target_rows_per_file=10, min_rows_per_file=5, max_rows_per_file=20,
    )
    assert PhysicalLayout.from_dict(layout.to_dict()).to_dict() == layout.to_dict()
    contract = TableContract(
        primary_key=["k"], columns={"k": "text"},
        physical_layout=layout,
    )
    assert TableContract.from_dict(contract.to_dict()).physical_layout.to_dict() \
        == layout.to_dict()


def test_rename_table_failure_rolls_back_and_reopens(tmp_path, monkeypatch):
    db = Database(_uri(tmp_path / "rt.iosql"))
    try:
        db.table(
            "source", pk=["id"], columns={"id": "integer", "name": "text"},
        ).write({"id": 1, "name": "source"})
        db.table(
            "target", pk=["id"], columns={"id": "integer", "name": "text"},
        ).write({"id": 2, "name": "target"})

        real_replace = os.replace

        def flaky_replace(src, dst, *args, **kwargs):
            if (
                os.path.basename(str(src)) == "source"
                and os.path.basename(str(dst)) == "target"
            ):
                raise OSError(13, "simulated failure")
            return real_replace(src, dst)

        monkeypatch.setattr(database_module.os, "replace", flaky_replace)
        with pytest.raises(BadArgument, match="重命名表失败"):
            db.rename_table("source", "target", overwrite=True)
        monkeypatch.undo()

        assert db.table_names() == ["source", "target"]
        assert db.table("source").select(to_dict=True) == [
            {"id": 1, "name": "source"},
        ]
        assert db.table("target").select(to_dict=True) == [
            {"id": 2, "name": "target"},
        ]
        assert not list((tmp_path / "rt.iosql").glob("*rename_tmp*"))
    finally:
        db.close()


def test_database_getitem_concurrent_first_access_returns_one_table(tmp_path):
    db = Database(_uri(tmp_path / "concurrent.iosql"))
    try:
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(db["kv"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8
        assert len({id(table) for table in results}) == 1
    finally:
        db.close()


def test_legacy_utils_partition_module_still_imports():
    from iosql.utils import partition as legacy

    assert legacy.parse_partition("toYYYY(date)") == ("toYYYY", "date")
    assert callable(legacy.get_part_range)


def test_delete_none_warns_before_full_table_delete(tmp_path):
    db = Database(_uri(tmp_path / "del.iosql"))
    try:
        table = db.table(
            "kv", pk=["k"], columns={"k": "text", "v": "integer"},
        )
        table.write([{"k": "a", "v": 1}, {"k": "b", "v": 2}])
        with pytest.warns(DeprecationWarning, match="delete\\(None\\)"):
            deleted = table.delete(None)
        assert deleted == 2
        assert table.select(to_dict=True) == []
    finally:
        db.close()
