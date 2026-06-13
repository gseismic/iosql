import pytest

from iosql import (
    CalendarPartition,
    Database,
    PartitionSpec,
    TableSpec,
    TimeColumn,
)
from iosql.exception import UnsafeOperation


def _uri(tmp_path, name="market.iosql"):
    return f"sqlite://{tmp_path}/{name}"


def test_timeseries_write_query_and_explain(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1s",
        target_rows_per_file=2,
        min_rows_per_file=1,
        max_rows_per_file=3,
    )

    result = ticks.write([
        {"symbol": "AAPL", "ts": 500, "price": 1.0},
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ])

    assert result.rows_seen == 3
    assert result.partitions_touched == [0, 1000, 2000]
    assert result.partitions_created == [0, 1000, 2000]

    out = ticks.query(
        symbols="AAPL",
        start=1000,
        end=1999,
        columns=["symbol", "ts", "price"],
    ).to_list()
    assert out == [{"symbol": "AAPL", "ts": 1500, "price": 2.0}]

    multi_symbol = ticks.query(
        symbols=["AAPL", "MSFT"],
        start=0,
        end=2999,
        columns=["symbol", "ts", "price"],
        order_by="ts",
    ).to_list()
    assert [row["symbol"] for row in multi_symbol] == ["AAPL", "AAPL", "MSFT"]

    text_where = ticks.query(
        start=0,
        end=2999,
        where="price >= 2",
        columns=["symbol", "ts", "price"],
        order_by="ts",
    ).to_list()
    assert text_where == [
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ]

    plan = ticks.query(symbols="AAPL", start=1000, end=1999).explain()
    assert plan.candidate_partitions == [0, 1000, 2000]
    assert plan.scanned_partitions == [1000]
    assert plan.skipped_partitions == [0, 2000]


def test_table_spec_persists_for_reopen(tmp_path):
    db = Database(_uri(tmp_path))
    db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=2,
        min_rows_per_file=1,
        max_rows_per_file=3,
    ).write([{"symbol": "AAPL", "ts": 1000, "price": 1.0}])
    db.close()

    reopened = Database(_uri(tmp_path))
    spec = reopened.table("ticks").spec()

    assert spec.primary_key == ["symbol", "ts"]
    assert spec.time.name == "ts"
    assert spec.partition.every == "1day"
    assert spec.physical_layout.target_rows_per_file == 2


def test_partition_spec_inherits_time_column_timezone():
    spec = TableSpec(
        primary_key=["date"],
        time=TimeColumn("date", unit="date", timezone="Asia/Shanghai"),
        partition=PartitionSpec.by_time("date", every="1day", kind="calendar"),
        columns={"date": "text"},
    )

    partition = spec.to_partition()

    assert partition.tz == "Asia/Shanghai"


def test_timeseries_supports_calendar_date_unit_and_prunes(tmp_path):
    db = Database(_uri(tmp_path))
    reports = db.timeseries(
        "reports",
        time="published_date",
        time_unit="date",
        partition_kind="calendar",
        partition_every="1day",
        timezone="Asia/Shanghai",
        primary_key=["symbol", "published_date", "statement"],
        columns={
            "symbol": "text",
            "published_date": "text",
            "statement": "text",
            "revenue": "float",
        },
    )
    reports.write([
        {
            "symbol": "AAPL",
            "published_date": "2026-01-05",
            "statement": "income",
            "revenue": 100.0,
        },
        {
            "symbol": "AAPL",
            "published_date": "2026-01-06",
            "statement": "income",
            "revenue": 101.0,
        },
        {
            "symbol": "MSFT",
            "published_date": "2026-01-07",
            "statement": "income",
            "revenue": 102.0,
        },
    ])

    plan = reports.query(date="2026-01-06").explain()
    assert plan.candidate_partitions == ["20260105", "20260106", "20260107"]
    assert plan.scanned_partitions == ["20260106"]
    assert plan.skipped_partitions == ["20260105", "20260107"]

    rows = reports.query(
        symbols="AAPL",
        start="2026-01-06",
        end="2026-01-06",
    ).to_list()
    assert rows == [{
        "symbol": "AAPL",
        "published_date": "2026-01-06",
        "statement": "income",
        "revenue": 101.0,
    }]


def test_calendar_date_range_query_scans_only_overlapping_partitions(tmp_path):
    db = Database(_uri(tmp_path))
    announcements = db.timeseries(
        "announcements",
        time="published_date",
        time_unit="date",
        partition_kind="calendar",
        partition_every="1day",
        timezone="Asia/Shanghai",
        primary_key=["symbol", "published_date", "announcement_id"],
        columns={
            "symbol": "text",
            "published_date": "text",
            "announcement_id": "text",
            "title": "text",
        },
    )
    announcements.write([
        {
            "symbol": "AAPL",
            "published_date": "2026-02-01",
            "announcement_id": "a1",
            "title": "board update",
        },
        {
            "symbol": "AAPL",
            "published_date": "2026-02-02",
            "announcement_id": "a2",
            "title": "earnings date",
        },
        {
            "symbol": "AAPL",
            "published_date": "2026-02-03",
            "announcement_id": "a3",
            "title": "dividend",
        },
    ])

    plan = announcements.query(
        start="2026-02-02",
        end="2026-02-03",
    ).explain()

    assert plan.scanned_partitions == ["20260202", "20260203"]
    assert plan.skipped_partitions == ["20260201"]


def test_layout_report_flags_oversized_partition(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=2,
        min_rows_per_file=1,
        max_rows_per_file=2,
    )

    result = ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
        {"symbol": "NVDA", "ts": 3000, "price": 3.0},
    ])

    assert result.layout_warnings
    report = ticks.layout_report()
    assert report.total_rows == 3
    assert report.oversized_partitions == [0]
    assert report.recommendation.startswith("repartition_to")


def test_repartition_to_writes_new_database_without_touching_source(tmp_path):
    source_uri = _uri(tmp_path, "source.iosql")
    target_uri = _uri(tmp_path, "target.iosql")
    db = Database(source_uri)
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=2,
        min_rows_per_file=1,
        max_rows_per_file=3,
    )
    ticks.write([
        {"symbol": "AAPL", "ts": 500, "price": 1.0},
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ])

    new_spec = ticks.spec().with_partition(every="1s")
    result = ticks.repartition_to(target_uri, spec=new_spec)

    assert result.rows_copied == 3
    assert result.partitions_created == [0, 1000, 2000]
    assert ticks.table_parts() == [0]

    target = Database(target_uri)
    assert target.table("ticks").query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 500, "price": 1.0},
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ]


def test_write_buffer_flushes_by_row_threshold(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=10,
        min_rows_per_file=1,
        max_rows_per_file=20,
    )

    with ticks.write_buffer(max_rows=2) as buffer:
        buffer.append({"symbol": "AAPL", "ts": 1000, "price": 1.0})
        assert ticks.select("*", where=None, to_dict=True) == []
        buffer.append({"symbol": "MSFT", "ts": 2000, "price": 2.0})
        assert len(ticks.select("*", where=None, to_dict=True)) == 2
        buffer.append({"symbol": "NVDA", "ts": 3000, "price": 3.0})

    assert len(ticks.select("*", where=None, to_dict=True)) == 3


def test_truncate_requires_confirm(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
    )
    ticks.write([{"symbol": "AAPL", "ts": 1000, "price": 1.0}])

    with pytest.raises(UnsafeOperation):
        ticks.truncate()

    result = ticks.truncate(confirm=True)
    assert result.partitions_touched == [0]
    assert ticks.select("*", where=None, to_dict=True) == []


def test_calendar_partition_range_uses_configured_timezone():
    partition = CalendarPartition(fmt="YYYYMMDD", tz="Asia/Shanghai", ts_col="date")

    assert partition.get_range("20240115") == (
        1705248000000,
        1705334400000,
    )
