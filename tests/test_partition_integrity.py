"""分区契约、历史布局与跨后端一致性回归。"""

import threading
from dataclasses import replace
from datetime import date

import arrow
import pytest

from iosql import Database, Partition, PartitionSpec, Segment, TableContract, TimeColumn
from iosql.exception import PartitionSpecError, TableSpecConflict


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize("form", ["string", "object", "spec", "dict"])
def test_calendar_timeseries_partition_forms_share_time_semantics(tmp_path, engine, form):
    uri = f"{engine}://{tmp_path / 'calendar.iosql'}"
    partition = {
        "string": "calendar(d,every=1day,timezone=Asia/Shanghai)",
        "object": Partition.calendar("d", every="1day", timezone="Asia/Shanghai"),
        "spec": PartitionSpec(column="d", kind="calendar", every="1day", timezone="Asia/Shanghai"),
        "dict": {"column": "d", "kind": "calendar", "every": "1day", "timezone": "Asia/Shanghai"},
    }[form]
    with Database(uri) as db:
        table = db.timeseries(
            "events", time="d", pk=["d"], columns={"d": "text"},
            symbol_column=None, partition=partition,
        )
        table.write([{"d": "2024-12-31"}, {"d": "2025-01-01"}])
        assert table.spec().time.to_dict() == {
            "name": "d", "unit": "date", "timezone": "Asia/Shanghai",
        }
        assert table.query(date="2025-01-01").to_list() == [{"d": "2025-01-01"}]
        assert len(table.query(date="2025-01-01").explain().scanned_partitions) == 1

    with Database(uri) as db:
        table = db.table("events")
        assert table.spec().time.unit == "date"
        assert table.spec().time.timezone == "Asia/Shanghai"
        assert table.query(date="2025-01-01").to_list() == [{"d": "2025-01-01"}]


def test_calendar_timeseries_preserves_explicit_numeric_time_unit(tmp_path):
    with Database(f"sqlite://{tmp_path / 'numeric.iosql'}") as db:
        table = db.timeseries(
            "events", time=TimeColumn("ts", unit="s", timezone="UTC"),
            pk=["ts"], columns={"ts": "integer"}, symbol_column=None,
            partition="calendar(ts,every=1day,timezone=UTC)",
        )
        table.write({"ts": 1_735_689_600})
        assert table.spec().time.unit == "s"
        assert table.query(start=1_735_689_600, end=1_735_689_600).to_list() == [
            {"ts": 1_735_689_600},
        ]


@pytest.mark.parametrize("year,expected_start", [(2023, "20231231"), (2024, "20241230")])
def test_calendar_multiday_year_end_is_contiguous(year, expected_start):
    partition = Partition.calendar("d", every="4day", timezone="UTC")
    last_key = partition.get_key({"d": f"{year}-12-31"})
    first_key = partition.get_key({"d": f"{year + 1}-01-01"})
    assert last_key == expected_start
    assert first_key == f"{year + 1}0101"
    last_start, last_end = partition.get_range(last_key)
    first_start, first_end = partition.get_range(first_key)
    assert last_start < last_end == first_start < first_end
    assert first_start == int(arrow.get(year + 1, 1, 1).timestamp() * 1000)


def test_calendar_range_does_not_overflow_next_year_when_bucket_stays_in_year_9999():
    partition = Partition.calendar("d", every="1day", timezone="UTC")
    start, end = partition.get_range("99990101")
    assert end - start == 86_400_000


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("remove_partition", [False, True])
def test_force_partition_rejects_nonempty_layout_change(
    tmp_path, engine, segmented, remove_partition,
):
    uri = f"{engine}://{tmp_path / 'force.iosql'}"
    kwargs = {
        "pk": ["ts"], "columns": {"ts": "integer", "v": "integer"},
        "segment": Segment.rows(max_rows=10) if segmented else None,
    }
    old = Partition.duration("ts", every="1day")
    new = None if remove_partition else Partition.duration("ts", every="1hour")
    with Database(uri) as db:
        table = db.table("events", partition=old, **kwargs)
        table.write({"ts": 7_200_000, "v": 1})
        before = table.get_table_meta()
        with pytest.raises(TableSpecConflict, match="repartition_to|migrate_to"):
            db.table("events", partition=new, force_partition=True, **kwargs)
        assert table.get_table_meta() == before
        assert table.spec().partition.to_string() == "duration(ts,every=1day)"
        assert table.select(where={"field": "ts", "op": "==", "value": 7_200_000}) == [
            {"ts": 7_200_000, "v": 1},
        ]
    with Database(uri) as db:
        table = db.table("events")
        assert table.spec().partition.to_string() == "duration(ts,every=1day)"
        assert table.select() == [{"ts": 7_200_000, "v": 1}]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_force_partition_can_change_empty_declared_table(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'empty.iosql'}"
    kwargs = {"pk": ["ts"], "columns": {"ts": "integer"}}
    with Database(uri) as db:
        db.table("events", partition=Partition.duration("ts", every="1day"), **kwargs)
        table = db.table(
            "events", partition=Partition.duration("ts", every="1hour"),
            force_partition=True, **kwargs,
        )
        table.write({"ts": 7_200_000})
    with Database(uri) as db:
        table = db.table("events")
        assert table.spec().partition.to_string() == "duration(ts,every=1hour)"
        assert table.table_parts() == [7_200_000]
        assert table.select() == [{"ts": 7_200_000}]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize("segmented", [False, True])
def test_low_level_insert_cannot_add_partition_to_existing_unpartitioned_table(
    tmp_path, engine, segmented,
):
    uri = f"{engine}://{tmp_path / 'unpartitioned.iosql'}"
    with Database(uri) as db:
        table = db.table(
            "events", pk=["ts"], columns={"ts": "integer", "v": "integer"},
            segment=Segment.rows(max_rows=10) if segmented else None,
        )
        table.write({"ts": 0, "v": 1})
        before = table.get_table_meta()
        with pytest.raises(TableSpecConflict, match="[Pp]artition"):
            table.insert_all(
                {"ts": 7_200_000, "v": 2},
                partition=Partition.duration("ts", every="1hour"),
            )
        assert table.get_table_meta() == before
        table.write({"ts": 1, "v": 3})
        assert table.spec().partition is None
        assert table.select(order_by="ts") == [{"ts": 0, "v": 1}, {"ts": 1, "v": 3}]
    with Database(uri) as db:
        assert db.table("events").spec().partition is None
        assert db.table("events").count_rows() == 2


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_low_level_insert_cannot_override_unwritten_declared_partition(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'declared.iosql'}"
    with Database(uri) as db:
        table = db.table(
            "events", pk=["ts"], columns={"ts": "integer"},
            partition=Partition.duration("ts", every="1day"),
        )
        with pytest.raises(TableSpecConflict, match="[Pp]artition"):
            table.insert_all(
                {"ts": 7_200_000},
                partition=Partition.duration("ts", every="1hour"),
            )
        assert table.table_parts() == []
        table.write({"ts": 7_200_000})
        assert table.table_parts() == [0]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_empty_force_rebinding_races_with_first_write_without_mixing_layouts(
    tmp_path, engine, monkeypatch,
):
    uri = f"{engine}://{tmp_path / 'race.iosql'}"
    args = {"pk": ["ts"], "columns": {"ts": "integer"}}
    with Database(uri) as db:
        table = db.table("events", partition=Partition.duration("ts", every="1day"), **args)
        entered = threading.Event()
        release = threading.Event()
        errors = []
        original_insert = table.do_insert_all

        def delayed_insert(*call_args, **call_kwargs):
            entered.set()
            assert release.wait(timeout=10)
            return original_insert(*call_args, **call_kwargs)

        def writer():
            try:
                table.write({"ts": 7_200_000})
            except Exception as exc:
                errors.append(exc)

        monkeypatch.setattr(table, "do_insert_all", delayed_insert)
        worker = threading.Thread(target=writer)
        worker.start()
        try:
            assert entered.wait(timeout=10)
            db.table(
                "events", partition=Partition.duration("ts", every="1hour"),
                force_partition=True, **args,
            )
        finally:
            release.set()
            worker.join(timeout=10)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], TableSpecConflict)
        assert table.table_parts() == []
        table.write({"ts": 7_200_000})
        assert table.table_parts() == [7_200_000]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize("segmented", [False, True])
def test_deep_validation_finds_legacy_misplaced_rows_and_migration_recovers_them(
    tmp_path, engine, segmented,
):
    source = f"{engine}://{tmp_path / 'legacy.iosql'}"
    target = f"{engine}://{tmp_path / 'recovered.iosql'}"
    with Database(source) as db:
        table = db.table(
            "events", pk=["ts"], columns={"ts": "integer", "v": "integer"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=10) if segmented else None,
        )
        table.write({"ts": 7_200_000, "v": 1})
        # 模拟旧版强制改写契约后的持久化状态，保留旧数据文件。
        changed = replace(
            table.spec().with_partition(partition="duration(ts,every=1hour)"),
            force_partition=True,
        )
        meta = table.get_table_meta()
        meta["table_contract"] = changed.to_dict()
        meta["partition"] = changed.to_partition().to_meta()
        table._overwrite_table_meta(meta)

    with Database(source) as db:
        table = db.table("events")
        assert table.select() == [{"ts": 7_200_000, "v": 1}]
        result = table.validate(deep=True)
        assert not result.ok
        assert any(issue.code == "partition_row_mismatch" for issue in result.errors)
        migration = table.migrate_to(target, verify="row-count")
        assert migration.published and migration.verified

    with Database(target) as db:
        table = db.table("events")
        assert table.validate(deep=True).ok
        assert table.select(where={"field": "ts", "op": "==", "value": 7_200_000}) == [
            {"ts": 7_200_000, "v": 1},
        ]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_legacy_incorrect_calendar_time_contract_can_be_copied_without_data_loss(tmp_path, engine):
    source = f"{engine}://{tmp_path / 'bad-clock.iosql'}"
    target = f"{engine}://{tmp_path / 'corrected-clock.iosql'}"
    rows = [{"d": "2024-12-31", "v": 1}, {"d": "2025-01-01", "v": 2}]
    args = {
        "time": "d", "pk": ["d"], "columns": {"d": "text", "v": "integer"},
        "symbol_column": None,
        "partition": "calendar(d,every=1day,timezone=Asia/Shanghai)",
    }
    with Database(source) as db:
        table = db.timeseries("events", **args)
        table.write(rows)
        # 旧版将字符串 calendar 误判为毫秒时间列；保留原行与分区。
        original = table.spec()
        legacy = replace(original, time=TimeColumn("d", unit="ms", timezone="UTC"),
                         partition=replace(original.partition, date_key=None))
        meta = table.get_table_meta()
        meta["table_contract"] = legacy.to_dict()
        meta["partition"] = legacy.to_partition().to_meta()
        table._overwrite_table_meta(meta)

    with Database(source) as old, Database(target) as new:
        old_table = old.table("events")
        assert old_table.spec().time.unit == "ms"
        corrected = new.timeseries("events", **args)
        for batch in old_table.iter_batches(batch_size=1):
            corrected.write(batch)
        assert old_table.count_rows() == corrected.count_rows() == len(rows)
        assert old_table.select(order_by="d") == corrected.select(order_by="d") == rows

    with Database(target) as db:
        table = db.table("events")
        assert table.spec().time.unit == "date"
        assert table.query(date="2025-01-01").to_list() == [rows[1]]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_deep_validation_reports_unpartitioned_contract_and_partitioned_metadata(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'conflicting-meta.iosql'}"
    with Database(uri) as db:
        table = db.table("events", pk=["ts"], columns={"ts": "integer"})
        table.write({"ts": 0})
        meta = table.get_table_meta()
        meta["partition"] = Partition.duration("ts", every="1hour").to_meta()
        table._overwrite_table_meta(meta)
    with Database(uri) as db:
        result = db.table("events").validate(deep=True)
        assert any(issue.code == "partition_contract_mismatch" for issue in result.errors)


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_migrating_legacy_duplicate_keys_does_not_discard_rows(tmp_path, engine):
    from iosql.exception import BadArgument, MigrationError

    source = f"{engine}://{tmp_path / 'duplicates.iosql'}"
    target = f"{engine}://{tmp_path / 'duplicate-target.iosql'}"
    with Database(source) as db:
        table = db.table("events", pk=["ts"], columns={"ts": "integer", "v": "integer"},
                         partition=Partition.duration("ts", every="1day"))
        table.write({"ts": 7_200_000, "v": 1})
        changed = replace(table.spec().with_partition(partition="duration(ts,every=1hour)"),
                          force_partition=True)
        meta = table.get_table_meta()
        meta["table_contract"] = changed.to_dict()
        meta["partition"] = changed.to_partition().to_meta()
        table._overwrite_table_meta(meta)
    with Database(source) as db:
        table = db.table("events")
        table.write({"ts": 7_200_000, "v": 2})
        assert table.count_rows() == 2
        with pytest.raises((BadArgument, MigrationError)):
            table.migrate_to(target, verify="row-count")
        assert table.count_rows() == 2
    with Database(target) as db:
        assert "events" not in db.table_names()


def test_deep_validation_reports_corrupt_partitioned_parquet_file(tmp_path):
    from pathlib import Path

    with Database(f"parquet://{tmp_path / 'corrupt.iosql'}") as db:
        table = db.table("events", pk=["ts"], columns={"ts": "integer"},
                         partition=Partition.duration("ts", every="1day"))
        table.write({"ts": 1})
        path = Path(table.segments()[0]["path"])
        path.write_bytes(b"corrupt parquet")
        result = table.validate(deep=True)
        assert not result.ok
        assert any(issue.code in {"parquet_integrity_failed", "partition_validation_failed"}
                   for issue in result.errors)


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_timeseries_date_unit_partition_spec_uses_calendar_semantics(tmp_path, engine):
    partition = PartitionSpec(column="d", kind="duration", every="1day",
                              time_unit="date", timezone="Asia/Shanghai")
    with Database(f"{engine}://{tmp_path / 'date-unit.iosql'}") as db:
        table = db.timeseries("events", time="d", pk=["d"], columns={"d": "text"},
                              symbol_column=None, partition=partition)
        table.write({"d": "2025-01-01"})
        assert table.spec().time.unit == "date"
        assert table.spec().time.timezone == "Asia/Shanghai"
        assert table.query(date="2025-01-01").to_list() == [{"d": "2025-01-01"}]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize("segmented", [False, True])
def test_calendar_fall_back_queries_include_both_occurrences_and_old_metadata(tmp_path, engine, segmented):
    uri = f"{engine}://{tmp_path / 'fallback.iosql'}"
    # 2024-11-03 01:00/01:30 在纽约各发生两次。
    values = [1_730_610_000_000, 1_730_611_800_000, 1_730_613_600_000, 1_730_615_400_000]
    with Database(uri) as db:
        table = db.timeseries(
            "events", time=TimeColumn("ts", unit="ms", timezone="America/New_York"),
            pk=["ts"], columns={"ts": "integer"}, symbol_column=None,
            partition=Partition.calendar("ts", every="30min", timezone="America/New_York"),
            segment=Segment.rows(max_rows=10) if segmented else None,
        )
        table.write([{"ts": value} for value in values])
        # 模拟旧版为 01:00 分区持久化的范围，只覆盖第一次 01:00。
        table.upsert_part_column_range("202411030100", "ts", values[0], values[1])
    with Database(uri) as db:
        table = db.table("events")
        for value in values:
            assert table.query(start=value, end=value).to_list() == [{"ts": value}]
        assert table.query(start=values[0], end=values[-1], order_by="ts").to_list() == [
            {"ts": value} for value in values
        ]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_calendar_spring_forward_query_covers_adjusted_bucket_start(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'spring.iosql'}"
    value = 1_711_846_800_000
    with Database(uri) as db:
        table = db.timeseries(
            "events", time=TimeColumn("ts", unit="ms", timezone="Europe/London"),
            pk=["ts"], columns={"ts": "integer"}, symbol_column=None,
            partition=Partition.calendar("ts", every="90min", timezone="Europe/London"),
        )
        table.write({"ts": value})
        table.upsert_part_column_range("202403310230", "ts", 1_711_848_600_000, 1_711_854_000_000)
    with Database(uri) as db:
        table = db.table("events")
        assert table.query(start=value, end=value).to_list() == [{"ts": value}]


def test_utc_calendar_half_hour_bounds_remain_exact_on_dst_change_date():
    partition = Partition.calendar("ts", every="30min", timezone="UTC")
    key = partition.get_key({"ts": arrow.get("2024-11-03T06:00:00Z").datetime})
    lower, upper = partition.get_range(key)
    assert lower == 1_730_613_600_000
    assert upper - lower == 1_800_000


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize(
    "timezone,day,values,expected",
    [
        ("America/New_York", "2024-11-03",
         ["2024-11-03T03:59:59Z", "2024-11-03T04:00:00Z", "2024-11-03T05:00:00Z",
          "2024-11-03T06:00:00Z", "2024-11-04T04:59:59Z", "2024-11-04T05:00:00Z"],
         [1, 2, 3, 4]),
        ("Europe/London", "2024-03-31",
         ["2024-03-30T23:59:59Z", "2024-03-31T00:00:00Z", "2024-03-31T12:00:00Z",
          "2024-03-31T22:59:59Z", "2024-03-31T23:00:00Z"],
         [1, 2, 3]),
    ],
)
def test_query_date_uses_business_timezone_day_with_dst(tmp_path, engine, timezone, day, values, expected):
    timestamps = [int(arrow.get(value).timestamp() * 1000) for value in values]
    with Database(f"{engine}://{tmp_path / 'business-day.iosql'}") as db:
        table = db.timeseries(
            "events", time=TimeColumn("ts", unit="ms", timezone=timezone),
            pk=["ts"], columns={"ts": "integer"}, symbol_column=None,
            partition=Partition.calendar("ts", every="30min", timezone=timezone),
        )
        table.write([{"ts": value} for value in timestamps])
        assert table.query(date=day, order_by="ts").to_list() == [
            {"ts": timestamps[index]} for index in expected
        ]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_date_only_values_remain_calendar_dates_in_negative_offset_timezone(tmp_path, engine):
    with Database(f"{engine}://{tmp_path / 'calendar-dates.iosql'}") as db:
        table = db.timeseries("events", time="d", pk=["d"], columns={"d": "text"},
                              symbol_column=None,
                              partition=Partition.calendar("d", timezone="America/New_York"))
        table.write([{"d": "2024-11-02"}, {"d": "2024-11-03"}, {"d": "2024-11-04"}])
        assert table.table_parts() == ['20241102', '20241103', '20241104']
        assert table.spec().partition.date_key == 'local'
        assert table.query(date="2024-11-03").to_list() == [{"d": "2024-11-03"}]
        assert table.query(date=date(2024, 11, 3)).to_list() == [{"d": "2024-11-03"}]
        assert table.query(start="2024-11-03", end="2024-11-04", order_by="d").to_list() == [
            {"d": "2024-11-03"}, {"d": "2024-11-04"},
        ]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_legacy_date_key_reopen_upsert_and_explicit_migration(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'legacy-date.iosql'}"
    default_uri = f"{engine}://{tmp_path / 'legacy-copy.iosql'}"
    upgraded_uri = f"{engine}://{tmp_path / 'upgraded-copy.iosql'}"
    old = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='legacy')
    args = dict(time='d', pk=['d'], columns={'d': 'text', 'v': 'integer'}, symbol_column=None)
    with Database(uri) as db:
        table = db.timeseries('events', partition=old, **args)
        table.write({'d': '2024-11-03', 'v': 1})
        assert table.table_parts() == ['20241102']
        # 模拟旧版保存的无版本契约。
        meta = table.get_table_meta()
        meta['table_contract']['partition'].pop('date_key')
        table._overwrite_table_meta(meta)

    with Database(uri) as db:
        table = db.timeseries('events', partition=Partition.calendar('d', timezone='America/New_York'), **args)
        assert table.spec().partition.date_key in (None, 'legacy')
        table.write({'d': '2024-11-03', 'v': 2}, mode='upsert')
        assert table.table_parts() == ['20241102']
        assert table.select() == [{'d': '2024-11-03', 'v': 2}]
        upgraded = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='local')
        with pytest.raises(TableSpecConflict):
            db.timeseries('events', partition=upgraded, **args)
        db.migrate_to(default_uri, verify='row-count')
        db.migrate_to(upgraded_uri, specs={'events': {'partition': upgraded}}, verify='row-count')
        assert table.table_parts() == ['20241102']

    with Database(default_uri) as db:
        assert db['events'].table_parts() == ['20241102']
        assert db['events'].select() == [{'d': '2024-11-03', 'v': 2}]
    with Database(upgraded_uri) as db:
        table = db['events']
        assert table.table_parts() == ['20241103']
        assert table.spec().partition.date_key == 'local'
        assert table.query(date='2024-11-03').to_list() == [{'d': '2024-11-03', 'v': 2}]


@pytest.mark.parametrize('source_engine,target_engine', [('sqlite', 'parquet'), ('parquet', 'sqlite')])
def test_legacy_date_key_upgrade_across_directory_backends(tmp_path, source_engine, target_engine):
    source = f"{source_engine}://{tmp_path / 'source.iosql'}"
    target = f"{target_engine}://{tmp_path / 'upgraded.iosql'}"
    old = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='legacy')
    new = replace(old, date_key='local')
    with Database(source) as db:
        table = db.timeseries('events', time='d', pk=['d'], columns={'d': 'text'},
                              symbol_column=None, partition=old)
        table.write([{'d': '2024-11-03'}, {'d': '2024-11-04'}])
        assert table.table_parts() == ['20241102', '20241103']
        result = db.migrate_to(target, specs={'events': {'partition': new}}, verify='row-count')
        assert result.verified
    with Database(target) as db:
        table = db['events']
        assert table.table_parts() == ['20241103', '20241104']
        assert table.select(order_by='d') == [{'d': '2024-11-03'}, {'d': '2024-11-04'}]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_new_date_key_accepts_equivalent_low_level_partition(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'new-date.iosql'}"
    part = Partition.calendar('d', timezone='America/New_York')
    with Database(uri) as db:
        table = db.timeseries('events', time='d', pk=['d'], columns={'d': 'text'},
                              symbol_column=None, partition=part)
        table.insert_all([{'d': '2024-11-03'}], partition=part)
        table.partition = part
        assert table.table_parts() == ['20241103']
        old = PartitionSpec(column='d', kind='calendar', timezone='America/New_York',
                            time_unit='date', date_key='legacy').to_partition()
        with pytest.raises(TableSpecConflict):
            table.insert_all([{'d': '2024-11-04'}], partition=old)
    with Database(uri) as db:
        table = db.timeseries('events', time='d', pk=['d'], columns={'d': 'text'},
                              symbol_column=None, partition=part)
        table.insert_all([{'d': '2024-11-04'}], partition=part)
        assert table.table_parts() == ['20241103', '20241104']


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_concurrent_first_writers_cannot_mix_date_key_versions(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'concurrent-date.iosql'}"
    args = dict(time='d', pk=['d'], columns={'d': 'text'}, symbol_column=None)
    local = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='local')
    legacy = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='legacy')
    with Database(uri) as first, Database(uri) as second:
        first_table = first.timeseries('events', partition=local, **args)
        try:
            second_table = second.timeseries('events', partition=legacy, **args)
        except TableSpecConflict:
            # Parquet 在绑定时固化空表契约，第二个实例会更早拒绝。
            assert engine == 'parquet'
            first_table.write({'d': '2024-11-03'})
        else:
            gate = threading.Barrier(2)
            results = []

            def write(table):
                try:
                    gate.wait(timeout=10)
                    table.write({'d': '2024-11-03'})
                    results.append('ok')
                except Exception as exc:
                    results.append(exc)

            threads = [threading.Thread(target=write, args=(table,))
                       for table in (first_table, second_table)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            assert all(not thread.is_alive() for thread in threads)
            assert results.count('ok') == 1
            assert len([result for result in results if isinstance(result, TableSpecConflict)]) == 1
    with Database(uri) as db:
        assert db['events'].select() == [{'d': '2024-11-03'}]
        assert len(db['events'].table_parts()) == 1


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_direct_use_contract_persists_new_date_routing(tmp_path, engine):
    uri = f"{engine}://{tmp_path / 'use-contract.iosql'}"
    spec = TableContract(primary_key=['d'], time=TimeColumn('d', unit='date', timezone='America/New_York'),
                         columns={'d': 'text'}, partition=Partition.calendar('d', timezone='America/New_York'))
    with Database(uri) as db:
        table = db.table('events')
        table.use_contract(spec)
        table.write({'d': '2024-11-03'})
        assert table.table_parts() == ['20241103']
        assert table.get_table_meta()['partition']['date_key'] == 'local'
    with Database(uri) as db:
        assert db['events'].query(date='2024-11-03').to_list() == [{'d': '2024-11-03'}]


def test_date_key_partition_expression_round_trips():
    spec = PartitionSpec.from_string('calendar(d,every=1month,timezone=America/New_York,date_key=local)')
    assert PartitionSpec.from_string(spec.to_string()).to_dict() == spec.to_dict()
    assert spec.to_partition().get_key({'d': '2024-11-01'}) == '202411'
    with pytest.raises(PartitionSpecError, match='date'):
        spec.to_partition(TimeColumn('d', unit='ms'))
    with pytest.raises(PartitionSpecError, match='date_key'):
        PartitionSpec(column='d', kind='calendar', date_key='unknown')


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_duration_date_alias_uses_business_date_for_new_table(tmp_path, engine):
    part = PartitionSpec(column='d', kind='duration', every='1day',
                         time_unit='date', timezone='America/New_York')
    with Database(f"{engine}://{tmp_path / 'duration-date.iosql'}") as db:
        table = db.timeseries('events', time='d', pk=['d'], columns={'d': 'text'},
                              symbol_column=None, partition=part)
        table.write({'d': '2024-11-03'})
        assert table.table_parts() == ['20241103']
        assert table.query(date='2024-11-03').to_list() == [{'d': '2024-11-03'}]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
@pytest.mark.parametrize("date_key", ["legacy", "local"])
@pytest.mark.parametrize("every,time_timezone,partition_timezone", [
    ("1day", "Pacific/Kiritimati", "Pacific/Pago_Pago"),
    ("1day", "Pacific/Pago_Pago", "Pacific/Kiritimati"),
    ("1month", "America/New_York", "America/New_York"),
    ("1week", "America/New_York", "America/New_York"),
])
def test_date_range_pruning_covers_legacy_routing_across_timezones(
        tmp_path, engine, date_key, every, time_timezone, partition_timezone):
    values = ["2024-10-30", "2024-10-31", "2024-11-01", "2024-11-02"]
    uri = f"{engine}://{tmp_path / 'date-range.iosql'}"
    with Database(uri) as db:
        table = db.timeseries(
            "events", time=TimeColumn("d", unit="date", timezone=time_timezone),
            pk=["d"], columns={"d": "text"}, symbol_column=None,
            partition=PartitionSpec(column="d", kind="calendar", every=every,
                                    timezone=partition_timezone, date_key=date_key),
        )
        table.write([{"d": value} for value in values])
    with Database(uri) as db:
        table = db["events"]
        assert table.query(start=values[1], end=values[2], order_by="d").to_list() == [
            {"d": values[1]}, {"d": values[2]},
        ]


@pytest.mark.parametrize("engine", ["sqlite", "parquet"])
def test_business_day_query_handles_timezone_jump_at_midnight(tmp_path, engine):
    values = ["2018-11-04T02:59:59Z", "2018-11-04T03:00:00Z",
              "2018-11-05T01:59:59Z", "2018-11-05T02:00:00Z"]
    timestamps = [int(arrow.get(value).timestamp() * 1000) for value in values]
    with Database(f"{engine}://{tmp_path / 'midnight-jump.iosql'}") as db:
        table = db.timeseries("events", time=TimeColumn("ts", unit="ms", timezone="America/Sao_Paulo"),
                              pk=["ts"], columns={"ts": "integer"}, symbol_column=None,
                              partition=Partition.calendar("ts", every="90min", timezone="America/Sao_Paulo"))
        table.write([{"ts": value} for value in timestamps])
        assert table.query(date="2018-11-04", order_by="ts").to_list() == [
            {"ts": timestamps[1]}, {"ts": timestamps[2]},
        ]


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
@pytest.mark.parametrize('unit', ['ms', 's'])
def test_natural_day_before_epoch_excludes_next_midnight(tmp_path, engine, unit):
    with Database(f"{engine}://{tmp_path / 'pre-epoch.iosql'}") as db:
        table = db.timeseries(
            'events', time=TimeColumn('ts', unit=unit, timezone='UTC'),
            pk=['ts'], columns={'ts': 'integer'}, symbol_column=None,
            partition=Partition.calendar('ts', timezone='UTC'),
        )
        table.write([{'ts': -1}, {'ts': 0}])
        assert table.query(date='1969-12-31').to_list() == [{'ts': -1}]
        assert table.query(date='1970-01-01').to_list() == [{'ts': 0}]
