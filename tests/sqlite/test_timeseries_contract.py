import pytest

from iosql import (
    CalendarPartition,
    Database,
    PartitionSpec,
    TableSpec,
    TimeColumn,
)
from iosql.exception import BadArgument, UnsafeOperation


def _uri(tmp_path, name="market.iosql"):
    return f"sqlite://{tmp_path}/{name}"


def _segmented_ticks(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=1,
        min_rows_per_file=1,
        max_rows_per_file=1,
    )
    return db, ticks


def _latest_ticks(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={
            "symbol": "text",
            "timeframe": "text",
            "ts": "integer",
            "price": "float",
            "venue": "text",
        },
        partition_every="1day",
        target_rows_per_file=1,
        min_rows_per_file=1,
        max_rows_per_file=1,
    )
    ticks.write([
        {
            "symbol": "AAPL",
            "timeframe": "1m",
            "ts": 1000,
            "price": 1.0,
            "venue": "NASDAQ",
        },
        {
            "symbol": "MSFT",
            "timeframe": "1m",
            "ts": 1500,
            "price": 2.0,
            "venue": "NASDAQ",
        },
        {
            "symbol": "AAPL",
            "timeframe": "1m",
            "ts": 2000,
            "price": 3.0,
            "venue": "NASDAQ",
        },
        {
            "symbol": "MSFT",
            "timeframe": "1m",
            "ts": 2500,
            "price": 4.0,
            "venue": "NYSE",
        },
        {
            "symbol": "AAPL",
            "timeframe": "5m",
            "ts": 2800,
            "price": 4.5,
            "venue": "NASDAQ",
        },
        {
            "symbol": "AAPL",
            "timeframe": "1m",
            "ts": 3000,
            "price": 5.0,
            "venue": "NYSE",
        },
    ])
    return db, ticks


def _session_ticks(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "futures_ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={
            "symbol": "text",
            "ts": "integer",
            "trading_date": "text",
            "session_type": "text",
            "price": "float",
        },
        session_column="trading_date",
        partition_every="1day",
        target_rows_per_file=1,
        min_rows_per_file=1,
        max_rows_per_file=1,
    )
    day_start = 86_400_000
    ticks.write([
        {
            "symbol": "AU",
            "ts": day_start - 2 * 3_600_000,
            "trading_date": "1970-01-02",
            "session_type": "night",
            "price": 1.0,
        },
        {
            "symbol": "AU",
            "ts": day_start + 10 * 3_600_000,
            "trading_date": "1970-01-02",
            "session_type": "day",
            "price": 2.0,
        },
        {
            "symbol": "AG",
            "ts": day_start + 11 * 3_600_000,
            "trading_date": "1970-01-03",
            "session_type": "day",
            "price": 3.0,
        },
    ])
    return db, ticks


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


def test_query_session_uses_declared_session_column(tmp_path):
    _, ticks = _session_ticks(tmp_path)

    rows = ticks.query(
        symbols="AU",
        session="1970-01-02",
        order_by="ts",
    ).to_list()

    assert [row["session_type"] for row in rows] == ["night", "day"]
    assert [row["price"] for row in rows] == [1.0, 2.0]
    assert [
        (item["part_key"], item["session_min"], item["session_max"])
        for item in ticks.segments()
    ] == [
        ("0__000000", "1970-01-02", "1970-01-02"),
        ("86400000__000000", "1970-01-02", "1970-01-02"),
        ("86400000__000001", "1970-01-03", "1970-01-03"),
    ]


def test_query_session_prunes_segments_by_session_range(tmp_path):
    _, ticks = _session_ticks(tmp_path)

    plan = ticks.query(session="1970-01-02").explain()

    assert [item["part_key"] for item in plan.scanned_segments] == [
        "0__000000",
        "86400000__000000",
    ]
    assert [item["part_key"] for item in plan.skipped_segments] == [
        "86400000__000001",
    ]
    assert plan.segment_skip_reasons == {
        "86400000__000001": "pruned_by_segment_session_range"
    }


def test_query_date_remains_natural_day_not_session(tmp_path):
    _, ticks = _session_ticks(tmp_path)

    rows = ticks.query(
        symbols="AU",
        date="1970-01-02",
        order_by="ts",
    ).to_list()

    assert [row["session_type"] for row in rows] == ["day"]


def test_query_session_combines_with_text_where(tmp_path):
    _, ticks = _session_ticks(tmp_path)

    rows = ticks.query(
        session=["1970-01-02", "1970-01-03"],
        where="session_type = 'day'",
        order_by="symbol",
        columns=["symbol", "trading_date", "session_type"],
    ).to_list()

    assert rows == [
        {
            "symbol": "AG",
            "trading_date": "1970-01-03",
            "session_type": "day",
        },
        {
            "symbol": "AU",
            "trading_date": "1970-01-02",
            "session_type": "day",
        },
    ]


def test_latest_accepts_session_filter(tmp_path):
    _, ticks = _session_ticks(tmp_path)

    row = ticks.latest(
        symbols="AU",
        session="1970-01-02",
        as_of=86_400_000 + 12 * 3_600_000,
        columns=["symbol", "trading_date", "session_type", "price"],
    ).one()

    assert row == {
        "symbol": "AU",
        "trading_date": "1970-01-02",
        "session_type": "day",
        "price": 2.0,
    }


def test_query_session_requires_session_column(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
    )
    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 1.0})

    with pytest.raises(BadArgument, match="session_column"):
        ticks.query(session="1970-01-02").to_list()


def test_session_column_persists_for_reopen(tmp_path):
    db, ticks = _session_ticks(tmp_path)
    db.close()

    reopened = Database(_uri(tmp_path))
    spec = reopened.table("futures_ticks").spec()

    assert spec.session_column == "trading_date"


def test_latest_returns_single_symbol_as_of_row(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    row = ticks.latest(
        symbols="AAPL",
        as_of=2500,
        columns=["symbol", "ts", "price"],
    ).one()

    assert row == {"symbol": "AAPL", "ts": 2000, "price": 3.0}


def test_latest_returns_multi_symbol_rows_in_requested_order(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    rows = ticks.latest(
        symbols=["MSFT", "AAPL"],
        as_of=2500,
        columns=["symbol", "ts", "price"],
    ).to_list()

    assert rows == [
        {"symbol": "MSFT", "ts": 2500, "price": 4.0},
        {"symbol": "AAPL", "ts": 2000, "price": 3.0},
    ]


def test_latest_combines_text_where_and_asof_alias(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    rows = ticks.asof(
        symbols=["AAPL", "MSFT"],
        as_of=4000,
        where="venue = 'NASDAQ' and timeframe = '1m'",
        columns=["symbol", "ts", "venue"],
    ).to_list()

    assert rows == [
        {"symbol": "AAPL", "ts": 2000, "venue": "NASDAQ"},
        {"symbol": "MSFT", "ts": 1500, "venue": "NASDAQ"},
    ]


def test_latest_groups_by_symbol_and_timeframe(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    rows = ticks.latest(
        symbols=["AAPL", "MSFT"],
        as_of=4000,
        by=["symbol", "timeframe"],
        columns=["symbol", "timeframe", "ts", "price"],
    ).to_list()

    assert rows == [
        {"symbol": "AAPL", "timeframe": "1m", "ts": 3000, "price": 5.0},
        {"symbol": "AAPL", "timeframe": "5m", "ts": 2800, "price": 4.5},
        {"symbol": "MSFT", "timeframe": "1m", "ts": 2500, "price": 4.0},
    ]


def test_asof_groups_by_symbol_and_venue(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    rows = ticks.asof(
        symbols=["AAPL"],
        as_of=4000,
        by=["symbol", "venue"],
        columns=["symbol", "venue", "ts", "price"],
    ).to_list()

    assert rows == [
        {"symbol": "AAPL", "venue": "NYSE", "ts": 3000, "price": 5.0},
        {"symbol": "AAPL", "venue": "NASDAQ", "ts": 2800, "price": 4.5},
    ]


def test_latest_group_by_alias(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    rows = ticks.latest(
        symbols="AAPL",
        as_of=4000,
        group_by=["symbol", "timeframe"],
        columns=["symbol", "timeframe", "ts"],
    ).to_list()

    assert rows == [
        {"symbol": "AAPL", "timeframe": "1m", "ts": 3000},
        {"symbol": "AAPL", "timeframe": "5m", "ts": 2800},
    ]


def test_latest_rejects_conflicting_by_arguments(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    with pytest.raises(BadArgument, match="by and group_by conflict"):
        ticks.latest(
            symbols="AAPL",
            as_of=4000,
            by=["symbol"],
            group_by=["symbol", "timeframe"],
        ).to_list()


def test_latest_rejects_missing_group_field(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    with pytest.raises(BadArgument, match="latest grouping field missing"):
        ticks.latest(
            symbols="AAPL",
            as_of=4000,
            by=["symbol", "session"],
        ).to_list()


def test_latest_explain_uses_segment_pruning(tmp_path):
    _, ticks = _latest_ticks(tmp_path)

    plan = ticks.latest(symbols="AAPL", as_of=2500).explain()

    assert [item["part_key"] for item in plan.scanned_segments] == [
        "0__000000",
        "0__000001",
        "0__000002",
        "0__000003",
    ]
    assert "0__000004" in [
        item["part_key"] for item in plan.skipped_segments
    ]


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


def test_segment_layout_splits_oversized_logical_partition(tmp_path):
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

    assert result.layout_warnings == []
    assert result.partitions_touched == [0]

    report = ticks.layout_report()
    assert report.total_rows == 3
    assert report.total_partitions == 2
    assert report.oversized_partitions == []
    assert [item.row_count for item in report.partitions] == [2, 1]
    assert [item.partition_id for item in report.partitions] == [
        "0__000000",
        "0__000001",
    ]
    assert [item["part_key"] for item in ticks.segments()] == [
        "0__000000",
        "0__000001",
    ]

    rows = ticks.query(
        start=2500,
        end=3500,
        order_by="ts",
    ).to_list()
    assert rows == [{"symbol": "NVDA", "ts": 3000, "price": 3.0}]

    plan = ticks.query(start=2500, end=3500).explain()
    assert [item["part_key"] for item in plan.candidate_segments] == [
        "0__000000",
        "0__000001",
    ]
    assert [item["part_key"] for item in plan.scanned_segments] == ["0__000001"]
    assert [item["part_key"] for item in plan.skipped_segments] == ["0__000000"]
    assert plan.segment_skip_reasons == {
        "0__000000": "pruned_by_segment_time_range"
    }


def test_segment_upsert_rewrites_existing_primary_key(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ])

    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 9.0})

    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 9.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ]
    assert [item["row_count"] for item in ticks.segments()] == [1, 1]


def test_segment_ignore_skips_existing_primary_key(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ])

    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 9.0}, mode="ignore")

    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ]
    assert [item["part_key"] for item in ticks.segments()] == [
        "0__000000",
        "0__000001",
    ]


def test_segment_upsert_same_batch_duplicate_primary_key_last_wins(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)

    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 1000, "price": 9.0},
    ])

    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 9.0},
    ]
    assert [item["row_count"] for item in ticks.segments()] == [1]


def test_segment_append_duplicate_primary_key_raises(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 1.0}, mode="append")
    ticks.write({"symbol": "MSFT", "ts": 2000, "price": 2.0}, mode="append")

    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        ticks.write({"symbol": "AAPL", "ts": 1000, "price": 9.0}, mode="append")

    assert [item["part_key"] for item in ticks.segments()] == [
        "0__000000",
        "0__000001",
    ]


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
