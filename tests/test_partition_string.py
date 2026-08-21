import pytest

from iosql import (
    Database,
    Partition,
    TableContract,
    TimeColumn,
    format_partition_spec,
    parse_partition_spec,
)
from iosql.exception import PartitionSpecError


def _db_uri(path):
    return f"sqlite://{path}"


def test_parse_and_format_duration_partition_expr():
    # 验证 duration 字符串可以作为 CLI 和 Python API 的共同表示。
    spec = parse_partition_spec("duration(ts,every=1day)")

    assert spec.kind == "duration"
    assert spec.column == "ts"
    assert spec.every == "1day"
    assert spec.to_string() == "duration(ts,every=1day)"
    assert spec.to_partition().interval_ms == 86_400_000


def test_format_duration_partition_prefers_readable_units():
    # doctor 反推多日周期时，CLI 应输出 5day 而不是裸毫秒。
    spec = parse_partition_spec("duration(ts,every=432000000ms)")

    assert spec.to_string() == "duration(ts,every=5day)"


def test_parse_and_format_calendar_partition_expr():
    # 验证 calendar 能表达日历粒度和时区，且能映射到底层格式。
    spec = parse_partition_spec("calendar(event_date,every=1month,tz=Asia/Shanghai)")
    runtime_partition = spec.to_partition()

    assert spec.kind == "calendar"
    assert spec.column == "event_date"
    assert spec.timezone == "Asia/Shanghai"
    assert runtime_partition.fmt == "YYYYMM"
    assert spec.to_string() == "calendar(event_date,every=1month,timezone=Asia/Shanghai)"


def test_calendar_partition_expr_accepts_multi_day_bucket():
    # 验证 doctor 推荐的 4day 这类时间桶可以被 CLI 字符串表达。
    spec = parse_partition_spec("calendar(date,every=4day,tz=Asia/Shanghai)")
    runtime_partition = spec.to_partition()

    assert spec.to_string() == "calendar(date,every=4day,timezone=Asia/Shanghai)"
    assert runtime_partition.get_key({"date": "2025-01-03"}) == "20250101"
    assert runtime_partition.get_key({"date": "2025-01-05"}) == "20250105"
    assert runtime_partition.to_meta()["every"] == "4day"


def test_parse_and_format_range_partition_expr():
    # range 对外使用 width，内部仍复用 PartitionSpec.every 存储宽度。
    spec = parse_partition_spec("range(partition_key,width=1107)")

    assert spec.kind == "range"
    assert spec.column == "partition_key"
    assert spec.every == 1107
    assert spec.to_string() == "range(partition_key,width=1107)"
    assert spec.to_partition().width == 1107


def test_format_runtime_partition_uses_canonical_expr():
    # 旧对象 API 也应格式化为新的规范字符串。
    partition = Partition.duration("ts", every="1day")

    assert format_partition_spec(partition) == "duration(ts,every=1day)"


def test_database_table_accepts_partition_expr(tmp_path):
    # 表契约直接接收字符串 partition，写入后仍按分区路由。
    db_path = tmp_path / "partition_expr.iosql"
    db = Database(_db_uri(db_path))
    try:
        table = db.table(
            "items",
            pk=["id"],
            columns={"id": "integer", "value": "float"},
            partition="range(id,width=5)",
        )
        table.write([{"id": i, "value": i * 1.0} for i in range(8)])

        assert table.spec().partition.to_string() == "range(id,width=5)"
        assert table.table_parts() == [0, 5]
    finally:
        db.close()


def test_partition_expr_rejects_ambiguous_range_every():
    # range 使用 width，避免 every 在时间和数值分区间出现同名不同义。
    with pytest.raises(PartitionSpecError, match="Unsupported option"):
        parse_partition_spec("range(id,every=5)")


def test_partition_expr_requires_range_width():
    # range 没有安全默认宽度，必须由用户显式给出。
    with pytest.raises(PartitionSpecError, match="requires width"):
        parse_partition_spec("range(id)")


def test_partition_expr_does_not_inherit_wrong_time_unit():
    # 从 calendar/date 表切换分区类型时，duration/range 不能继承 date time_unit。
    old_spec = TableContract(
        primary_key=["event_date"],
        time=TimeColumn("event_date", unit="date"),
        partition="calendar(event_date,every=1day,tz=Asia/Shanghai)",
    )

    duration_spec = old_spec.with_partition(partition="duration(ts,every=1hour)")
    range_spec = old_spec.with_partition(partition="range(seq,width=5)")

    assert duration_spec.partition.kind == "duration"
    assert duration_spec.partition.time_unit == "ms"
    assert duration_spec.partition.to_partition().interval_ms == 3_600_000
    assert range_spec.partition.kind == "range"
    assert range_spec.partition.time_unit is None
    assert range_spec.partition.to_partition().width == 5
