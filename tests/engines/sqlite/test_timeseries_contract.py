import threading

import pytest
import iosql.database as database_module

from iosql import (
    Database,
    PartitionSpec,
    PhysicalLayout,
    TableSpec,
    TimeColumn,
)
from iosql.partition.calendar import CalendarPartition
from iosql.exception import (
    BadArgument,
    SchemaConflict,
    TableSpecConflict,
    UnsafeOperation,
)


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
    assert plan.candidate_partitions == [1000]
    assert plan.scanned_partitions == [1000]
    assert plan.skipped_partitions == [0, 2000]


def test_database_table_entrypoint_reopens_after_close(tmp_path):
    db = Database(_uri(tmp_path))
    spec = TableSpec.timeseries(
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=2,
        min_rows_per_file=1,
        max_rows_per_file=3,
    )
    table = db.table("ticks", spec=spec)
    table.write({"symbol": "AAPL", "ts": 1000, "price": 1.0})

    assert db.table_names() == ["ticks"]
    assert db.table("ticks") is table

    db.close()
    reopened = db.table("ticks")

    assert reopened is not table
    assert reopened.spec().primary_key == ["symbol", "ts"]
    assert reopened.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 1.0}
    ]


def test_raw_table_spec_writes_queries_and_partitions_timeseries(tmp_path):
    db = Database(_uri(tmp_path))
    spec = TableSpec(
        primary_key=["symbol", "ts"],
        time=TimeColumn("ts", unit="ms", timezone="UTC"),
        partition=PartitionSpec.by_time("ts", every="1s"),
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        symbol_column="symbol",
        physical_layout=PhysicalLayout(
            target_rows_per_file=2,
            min_rows_per_file=1,
            max_rows_per_file=3,
        ),
    )
    ticks = db.table("ticks", spec=spec)

    result = ticks.write([
        {"symbol": "AAPL", "ts": 500, "price": 1.0},
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ])

    assert result.rows_seen == 3
    assert result.partitions_touched == [0, 1000, 2000]
    assert ticks.table_parts() == [0, 1000, 2000]

    rows = ticks.query(
        symbols="AAPL",
        start=1000,
        end=1999,
        columns=["symbol", "ts", "price"],
    ).to_list()
    assert rows == [{"symbol": "AAPL", "ts": 1500, "price": 2.0}]

    plan = ticks.query(symbols="AAPL", start=1000, end=1999).explain()
    assert plan.candidate_partitions == [1000]
    assert plan.scanned_partitions == [1000]
    assert plan.skipped_partitions == [0, 2000]

    db.close()
    reopened_ticks = Database(_uri(tmp_path)).table("ticks")
    reopened_spec = reopened_ticks.spec()

    assert reopened_spec.primary_key == ["symbol", "ts"]
    assert reopened_spec.time.name == "ts"
    assert reopened_spec.partition.column == "ts"
    assert reopened_ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 500, "price": 1.0},
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ]
    reopened_ticks.close()


def test_database_table_rejects_incompatible_specs(tmp_path):
    db = Database(_uri(tmp_path))
    spec = TableSpec.timeseries(
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
    )
    db.table("ticks", spec=spec).write(
        {"symbol": "AAPL", "ts": 1000, "price": 1.0}
    )

    with pytest.raises(TableSpecConflict):
        db.table(
            "ticks",
            spec=TableSpec.timeseries(
                time="ts",
                primary_key=["symbol", "venue", "ts"],
                columns={
                    "symbol": "text",
                    "venue": "text",
                    "ts": "integer",
                    "price": "float",
                },
            ),
        )

    with pytest.raises(SchemaConflict):
        db.table(
            "ticks",
            spec=TableSpec.timeseries(
                time="ts",
                primary_key=["symbol", "ts"],
                columns={"symbol": "text", "ts": "integer", "price": "text"},
            ),
        )


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
        "0__000002",
    ]
    skipped = [item["part_key"] for item in plan.skipped_segments]
    assert "0__000001" in skipped
    assert "0__000003" in skipped
    assert "0__000004" in skipped


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
    assert spec.partition.every == 86_400_000
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
    assert plan.candidate_partitions == ["20260106"]
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
        "0__000000": "pruned_by_segment_stats"
    }


def test_compact_kline_late_data_rewrites_segments_and_improves_pruning(tmp_path):
    db = Database(_uri(tmp_path))
    bars = db.timeseries(
        "bars",
        time="ts",
        primary_key=["symbol", "timeframe", "ts"],
        columns={
            "symbol": "text",
            "timeframe": "text",
            "ts": "integer",
            "close": "float",
        },
        partition_every="1day",
        target_rows_per_file=3,
        min_rows_per_file=1,
        max_rows_per_file=3,
    )

    bars.write([
        {"symbol": "AAPL", "timeframe": "1m", "ts": ts, "close": float(ts)}
        for ts in [1000, 2000, 3000, 4000, 5000, 6000]
    ])
    bars.write({
        "symbol": "AAPL",
        "timeframe": "1m",
        "ts": 2500,
        "close": 25.0,
    })

    before_rows = bars.query(
        start=2000,
        end=2600,
        order_by="ts",
    ).to_list()
    before_plan = bars.query(start=2000, end=2600, order_by="ts").explain()
    assert {
        item["segment_id"] for item in before_plan.scanned_segments
    } == {0, 2}

    result = bars.compact()

    assert result.rows_read == 7
    assert result.rows_written == 7
    assert result.old_segments == 3
    assert result.obsolete_segments == 3
    assert bars.query(start=2000, end=2600, order_by="ts").to_list() == before_rows

    after_plan = bars.query(start=2000, end=2600, order_by="ts").explain()
    assert len(after_plan.scanned_segments) == 1
    assert {
        item["state"] for item in after_plan.scanned_segments
    } == {"sealed"}
    assert {
        item["segment_id"] for item in after_plan.scanned_segments
    }.isdisjoint({0, 1, 2})
    assert [item["state"] for item in bars.segments()] == [
        "sealed",
        "sealed",
        "sealed",
    ]
    assert [item["state"] for item in bars.segments(include_obsolete=True)].count(
        "obsolete"
    ) == 3

    db.close()


def test_timeseries_query_iter_batches_matches_to_list(tmp_path):
    db = Database(_uri(tmp_path))
    bars = db.timeseries(
        "bars",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={
            "symbol": "text",
            "ts": "integer",
            "close": "float",
        },
        partition_every="1day",
        target_rows_per_file=2,
        min_rows_per_file=1,
        max_rows_per_file=2,
    )
    bars.write([
        {"symbol": "AAPL", "ts": ts, "close": float(ts)}
        for ts in [1000, 2000, 3000, 4000, 5000]
    ])

    query = bars.query(start=1000, end=5000, order_by="ts", limit=4)
    batches = list(query.iter_batches(batch_size=2))
    rows = [row for batch in batches for row in batch]

    assert [len(batch) for batch in batches] == [2, 2]
    assert rows == query.to_list()
    assert list(query.iter_rows(batch_size=3)) == query.to_list()

    db.close()


def test_segment_default_write_appends_duplicate_primary_key(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ])

    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 9.0})

    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 1000, "price": 9.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ]
    assert [item["row_count"] for item in ticks.segments()] == [1, 1, 1]

    with pytest.raises(BadArgument, match="Segment.rows"):
        ticks.write({"symbol": "AAPL", "ts": 1000, "price": 10.0}, mode="upsert")


def test_segment_replace_rejected_until_pk_index(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ])

    with pytest.raises(BadArgument, match="Segment.rows"):
        ticks.write(
            {"symbol": "AAPL", "ts": 1000, "price": 9.0},
            mode="replace",
        )

    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
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


def test_segment_delete_refreshes_segment_metadata(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "MSFT", "ts": 2000, "price": 2.0},
    ])

    deleted = ticks.delete({"field": "symbol", "op": "=", "value": "AAPL"})

    assert deleted == 1
    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "MSFT", "ts": 2000, "price": 2.0}
    ]
    segments = ticks.segments()
    assert [item["row_count"] for item in segments] == [0, 1]
    assert segments[0]["time_min"] is None
    assert segments[0]["time_max"] is None
    assert segments[1]["time_min"] == 2000
    assert segments[1]["time_max"] == 2000


def test_segment_default_same_batch_duplicate_primary_key_appends(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)

    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 1000, "price": 9.0},
    ])

    assert ticks.query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 1000, "price": 9.0},
    ]
    assert [item["row_count"] for item in ticks.segments()] == [1, 1]


def test_segment_append_allows_duplicate_primary_key_across_sealed_segments(tmp_path):
    _, ticks = _segmented_ticks(tmp_path)
    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 1.0}, mode="append")
    ticks.write({"symbol": "MSFT", "ts": 2000, "price": 2.0}, mode="append")

    ticks.write({"symbol": "AAPL", "ts": 1000, "price": 9.0}, mode="append")

    assert [item["part_key"] for item in ticks.segments()] == [
        "0__000000",
        "0__000001",
        "0__000002",
    ]
    assert sorted(row["price"] for row in ticks.query().to_list()) == [1.0, 2.0, 9.0]


def test_many_partition_scan_keeps_part_db_cache_bounded(tmp_path):
    db = Database(
        _uri(tmp_path),
        max_open_partitions=3,
        max_connections_per_file=1,
        max_process_connections=4,
    )
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day",
        target_rows_per_file=100,
        min_rows_per_file=1,
        max_rows_per_file=100,
    )
    ticks.write([
        {
            "symbol": "AAPL",
            "ts": day * 86_400_000,
            "price": float(day),
        }
        for day in range(10)
    ])

    rows = ticks.query(order_by="ts").to_list()

    assert len(rows) == 10
    assert ticks.max_open_partitions == 3
    assert ticks.max_connections_per_file == 1
    assert ticks._open_part_db_count() <= 3
    assert len(ticks._db_parts) <= 3
    assert all(
        part_db.get_connection_pool_status()["created_connections"] <= 1
        for part_db in ticks._db_parts.values()
    )
    assert ticks.table_meta_db.get_connection_pool_status()["created_connections"] <= 1
    assert ticks.meta_db.get_connection_pool_status()["created_connections"] <= 1
    assert db.get_process_connection_budget_status()[
        "open_process_connections"
    ] <= 4


def test_many_partition_scan_is_thread_safe(tmp_path):
    db = Database(
        _uri(tmp_path),
        max_open_partitions=2,
        max_connections_per_file=2,
        max_process_connections=16,
    )
    ticks = db.table(
        "ticks",
        pk=["id"],
        columns={"id": "integer", "ts": "integer"},
        partition="range(ts,width=10)",
    )
    ticks.write([{"id": value, "ts": value} for value in range(100)])
    barrier = threading.Barrier(12)
    errors = []

    def scan():
        try:
            barrier.wait()
            for _ in range(5):
                assert len(ticks.select("*", where=None, to_dict=True)) == 100
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=scan) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert ticks._open_part_db_count() <= 2


def test_many_partition_concurrent_writes_do_not_close_active_pools(tmp_path):
    db = Database(
        _uri(tmp_path),
        max_open_partitions=2,
        max_connections_per_file=2,
        max_process_connections=16,
    )
    ticks = db.table(
        "ticks",
        pk=["id"],
        columns={"id": "integer", "ts": "integer"},
        partition="range(ts,width=10)",
    )
    barrier = threading.Barrier(4)
    errors = []

    def write(worker):
        try:
            barrier.wait()
            ticks.write([
                {"id": worker * 100 + offset, "ts": worker * 100 + offset}
                for offset in range(30)
            ])
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=write, args=(worker,))
        for worker in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(ticks.select("*", where=None, to_dict=True)) == 120


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("max_open_partitions", 0),
        ("max_open_partitions", True),
        ("max_open_partitions", 1.5),
        ("max_connections_per_file", 0),
        ("max_connections_per_file", False),
        ("max_connections_per_file", "2"),
        ("max_process_connections", 0),
        ("max_process_connections", True),
        ("max_process_connections", "32"),
    ],
)
def test_sqlite_file_limits_require_positive_integers(tmp_path, option, value):
    with pytest.raises(BadArgument, match=f"{option} must be a positive integer"):
        Database(_uri(tmp_path), **{option: value})


def test_repartition_to_writes_new_database_without_touching_source(
    tmp_path,
    monkeypatch,
):
    source_uri = _uri(tmp_path, "source.iosql")
    target_uri = _uri(tmp_path, "target.iosql")
    db = Database(
        source_uri,
        pragma_dict={"synchronous": "full"},
        max_open_partitions=2,
        max_connections_per_file=1,
        max_process_connections=7,
    )
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
    created = []
    real_database = Database

    class TrackingDatabase(real_database):
        def __init__(self, uri, *args, **kwargs):
            super().__init__(uri, *args, **kwargs)
            created.append((self, kwargs))

    monkeypatch.setattr(database_module, "Database", TrackingDatabase)
    result = ticks.repartition_to(target_uri, spec=new_spec)

    assert result.rows_copied == 3
    assert result.partitions_created == [0, 1000, 2000]
    assert ticks.table_parts() == [0]
    assert created[0][1]["pragma_dict"]["synchronous"] == "full"
    assert created[0][1]["max_open_partitions"] == 2
    assert created[0][1]["max_connections_per_file"] == 1
    assert created[0][1]["max_process_connections"] == 7
    assert created[0][0]._tables == {}

    target = Database(target_uri)
    assert target.table("ticks").query(order_by="ts").to_list() == [
        {"symbol": "AAPL", "ts": 500, "price": 1.0},
        {"symbol": "AAPL", "ts": 1500, "price": 2.0},
        {"symbol": "MSFT", "ts": 2500, "price": 3.0},
    ]


def test_repartition_to_closes_target_database_on_write_error(tmp_path, monkeypatch):
    source_uri = _uri(tmp_path, "source-error.iosql")
    target_uri = _uri(tmp_path, "target-error.iosql")
    db = Database(source_uri)
    ticks = db.table(
        "ticks",
        pk=["id"],
        columns={"id": "integer"},
    )
    ticks.write({"id": 1})
    created = []
    real_database = Database

    class FailingDatabase(real_database):
        def __init__(self, uri, *args, **kwargs):
            super().__init__(uri, *args, **kwargs)
            created.append(self)

        def table(self, *table_args, **table_kwargs):
            table = super().table(*table_args, **table_kwargs)

            def fail_write(*write_args, **write_kwargs):
                raise RuntimeError("write failed")

            table.write = fail_write
            return table

    monkeypatch.setattr(database_module, "Database", FailingDatabase)

    with pytest.raises(RuntimeError, match="write failed"):
        ticks.repartition_to(target_uri)

    assert created[0]._tables == {}


def test_buffered_writer_flushes_by_row_threshold(tmp_path):
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

    with ticks.buffered_writer(max_rows=2) as buffer:
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
