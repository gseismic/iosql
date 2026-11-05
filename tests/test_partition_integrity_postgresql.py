"""真实 PostgreSQL 的分区契约、并发及历史 calendar 布局回归。"""

import os
import threading
import uuid
from contextlib import closing

import psycopg2
from psycopg2 import sql
import pytest

from iosql import Database, Partition, PartitionSpec, TimeColumn
from iosql.exception import TableSpecConflict
from iosql.migration_postgresql import _stage_uri


@pytest.fixture
def pg_uri():
    base = os.environ.get("POSTGRES_DSN")
    if not base:
        pytest.skip("需要显式配置独立 PostgreSQL 测试实例")
    schema = "iosql_partition_test_" + uuid.uuid4().hex
    with closing(psycopg2.connect(base)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield _stage_uri(base, schema)
    finally:
        with closing(psycopg2.connect(base)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _duration(every):
    return Partition.duration("ts", every=every)


def test_pg_migration_accepts_equivalent_directory_date_routing_versions(tmp_path, pg_uri):
    partition = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='legacy')
    args = dict(time='d', pk=['d'], columns={'d': 'date', 'v': 'integer'}, symbol_column=None)
    with Database(f"sqlite://{tmp_path / 'old.iosql'}") as old:
        table = old.timeseries('events', partition=partition, **args)
        table.write({'d': '2024-11-03', 'v': 1})
        old.migrate_to(pg_uri, verify='row-count')
    with Database(f"sqlite://{tmp_path / 'new.iosql'}") as new:
        table = new.timeseries('events', partition=Partition.calendar('d', timezone='America/New_York'), **args)
        assert table.spec().partition.date_key == 'local'
        table.write({'d': '2024-11-04', 'v': 2})
        new.migrate_to(pg_uri)
    with Database(pg_uri) as target:
        table = target.timeseries('events', partition=partition, **args)
        assert [str(row['d']) for row in table.select(order_by='d')] == ['2024-11-03', '2024-11-04']


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_pg_to_directory_migration_preserves_local_date_key(tmp_path, pg_uri, engine):
    partition = PartitionSpec(column='d', kind='calendar', timezone='America/New_York', date_key='local')
    uri = f"{engine}://{tmp_path / 'date-copy.iosql'}"
    with Database(pg_uri) as db:
        # Parquet 目前只支持 integer/float/text/blob 声明类型。
        column_type = 'date' if engine == 'sqlite' else 'text'
        table = db.timeseries('events', time='d', pk=['d'], columns={'d': column_type},
                              symbol_column=None, partition=partition)
        table.write({'d': '2024-11-03'})
        table.migrate_to(uri, verify='row-count')
    with Database(uri) as db:
        table = db['events']
        assert table.spec().partition.date_key == 'local'
        assert table.table_parts() == ['20241103']
        assert [str(row['d']) for row in table.select()] == ['2024-11-03']


@pytest.mark.parametrize('unit', ['ms', 's'])
def test_pg_natural_day_before_epoch_excludes_next_midnight(pg_uri, unit):
    with Database(pg_uri) as db:
        table = db.timeseries('events', time=TimeColumn('ts', unit=unit, timezone='UTC'),
                              pk=['ts'], columns={'ts': 'bigint'}, symbol_column=None,
                              partition=Partition.calendar('ts', timezone='UTC'))
        table.write([{'ts': -1}, {'ts': 0}])
        assert table.query(date='1969-12-31').to_list() == [{'ts': -1}]
        assert table.query(date='1970-01-01').to_list() == [{'ts': 0}]


@pytest.mark.parametrize("forced", [False, True])
def test_pg_rebinding_partition_rejected_in_same_instance_and_after_reopen(pg_uri, forced):
    args = {"pk": ["ts"], "columns": {"ts": "integer", "v": "integer"}}
    with Database(pg_uri) as db:
        table = db.table("events", partition=_duration("1day"), **args)
        table.write({"ts": 7_200_000, "v": 1})
        with pytest.raises(TableSpecConflict, match="[Pp]artition|分区"):
            db.table("events", partition=_duration("1hour"), force_partition=forced, **args)
        assert table.spec().partition.to_string() == "duration(ts,every=1day)"
        assert table.select() == [{"ts": 7_200_000, "v": 1}]

    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="[Pp]artition|分区"):
            db.table("events", partition=_duration("1hour"), force_partition=forced, **args)
        table = db.table("events", partition=_duration("1day"), **args)
        table.write({"ts": 90_000_000, "v": 2})
        assert table.select(order_by="ts") == [
            {"ts": 7_200_000, "v": 1}, {"ts": 90_000_000, "v": 2},
        ]


def test_pg_low_level_write_cannot_override_bound_partition(pg_uri):
    with Database(pg_uri) as db:
        table = db.table("events", pk=["ts"], columns={"ts": "integer"}, partition=_duration("1day"))
        table.write({"ts": 0})
        with pytest.raises(TableSpecConflict, match="[Pp]artition|分区"):
            table.insert_all({"ts": 90_000_000}, partition=_duration("1hour"))
        assert table.select() == [{"ts": 0}]


def test_pg_concurrent_empty_bindings_cannot_create_mixed_granularity(pg_uri):
    args = {"pk": ["ts"], "columns": {"ts": "integer"}}
    with Database(pg_uri) as first, Database(pg_uri) as second:
        day = first.table("events", partition=_duration("1day"), **args)
        hour = second.table("events", partition=_duration("1hour"), **args)
        gate = threading.Barrier(2)
        results = []

        def write(table, value):
            try:
                gate.wait(timeout=10)
                table.write({"ts": value})
                results.append("ok")
            except Exception as exc:
                results.append(exc)

        threads = [
            threading.Thread(target=write, args=(day, 0)),
            threading.Thread(target=write, args=(hour, 90_000_000)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert results.count("ok") == 1
        failures = [item for item in results if item != "ok"]
        assert len(failures) == 1 and isinstance(failures[0], TableSpecConflict)
        assert len(day.select()) == 1


def test_pg_multiday_calendar_writes_across_year(pg_uri):
    with Database(pg_uri) as db:
        table = db.table(
            "events", pk=["d"], columns={"d": "text"},
            partition=Partition.calendar("d", every="4day", timezone="UTC"),
        )
        table.write([{"d": "2024-12-31"}, {"d": "2025-01-01"}])
        assert table.select(order_by="d") == [{"d": "2024-12-31"}, {"d": "2025-01-01"}]
        assert table.query(date="2025-01-01").to_list() == [{"d": "2025-01-01"}]


def _create_legacy_calendar_table(uri, extra_new_year_row=False):
    with closing(psycopg2.connect(uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE events (d TEXT PRIMARY KEY) PARTITION BY RANGE (d)")
            cursor.execute(
                "CREATE TABLE events_p_p_20241230 PARTITION OF events "
                "FOR VALUES FROM ('2024-12-30') TO ('2025-01-03')"
            )
            cursor.execute("INSERT INTO events VALUES ('2024-12-31')")
            if extra_new_year_row:
                cursor.execute("INSERT INTO events VALUES ('2025-01-02')")


def test_pg_legacy_calendar_boundary_safely_repaired_on_write(pg_uri):
    _create_legacy_calendar_table(pg_uri)
    with Database(pg_uri) as db:
        table = db.table(
            "events", pk=["d"], columns={"d": "text"},
            partition=Partition.calendar("d", every="4day", timezone="UTC"),
        )
        table.write({"d": "2025-01-01"})
        assert table.select(order_by="d") == [{"d": "2024-12-31"}, {"d": "2025-01-01"}]
        assert len(table.table_parts()) == 2


def test_pg_legacy_calendar_with_overflow_refuses_repair_and_can_migrate(pg_uri, tmp_path):
    _create_legacy_calendar_table(pg_uri, extra_new_year_row=True)
    with Database(pg_uri) as db:
        table = db.table(
            "events", pk=["d"], columns={"d": "text"},
            partition=Partition.calendar("d", every="4day", timezone="UTC"),
        )
        with pytest.raises(TableSpecConflict, match="迁移|migrate"):
            table.write({"d": "2025-01-01"})
        assert table.select(order_by="d") == [{"d": "2024-12-31"}, {"d": "2025-01-02"}]
        assert len(table.table_parts()) == 1
        target = f"sqlite://{tmp_path / 'recovered.iosql'}"
        result = table.migrate_to(target, verify="row-count")
        assert result.published and result.verified
    with Database(target) as db:
        table = db.table("events")
        assert table.select(order_by="d") == [{"d": "2024-12-31"}, {"d": "2025-01-02"}]
        assert table.query(date="2025-01-02").to_list() == [{"d": "2025-01-02"}]


def test_pg_reopen_rejects_different_calendar_even_if_existing_bounds_match(pg_uri):
    args = {"pk": ["d"], "columns": {"d": "text"}}
    with Database(pg_uri) as db:
        table = db.table("events", partition=Partition.calendar("d", timezone="UTC"), **args)
        table.write({"d": "2024-01-15"})
    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="分区|[Pp]artition"):
            db.table("events", partition=Partition.calendar("d", timezone="Europe/London"), **args)
        table = db.table("events", partition=Partition.calendar("d", timezone="UTC"), **args)
        assert table.select() == [{"d": "2024-01-15"}]


def test_pg_same_partition_after_reopen_is_writable(pg_uri):
    args = {"pk": ["ts"], "columns": {"ts": "integer"}, "partition": _duration("1day")}
    with Database(pg_uri) as db:
        db.table("events", **args).write({"ts": 0})
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"ts": 90_000_000})
        assert len(table.select()) == 2


@pytest.mark.parametrize(
    "column_type,first,second",
    [
        ("text", "20240115", "20240116"),
        ("text", "2024-01-15", "2024-01-16"),
        ("date", "2024-01-15", "2024-01-16"),
        ("timestamp", "2024-01-15 12:30:00", "2024-01-16 12:30:00"),
        ("timestamptz", "2024-01-15 12:30:00-05:00", "2024-01-16 12:30:00-05:00"),
    ],
)
def test_pg_existing_calendar_bounds_validate_column_types(pg_uri, column_type, first, second):
    args = {
        "pk": ["d"], "columns": {"d": column_type},
        "partition": Partition.calendar("d", every="1day", timezone="America/New_York"),
    }
    with Database(pg_uri) as db:
        db.table("events", **args).write({"d": first})
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"d": second})
        assert table.count_rows() == 2


def test_pg_text_bounds_compare_lexically_instead_of_as_dates(pg_uri):
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE events (d TEXT PRIMARY KEY) PARTITION BY RANGE (d)")
            cursor.execute(
                "CREATE TABLE legacy_part PARTITION OF events "
                "FOR VALUES FROM ('2024-01-15T00:00') TO ('2024-01-15T01:00')"
            )
    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="分区|[Pp]artition"):
            db.table("events", pk=["d"], columns={"d": "text"},
                     partition=Partition.calendar("d", every="1hour", timezone="UTC"))


def test_pg_does_not_replace_user_check_constraint_with_reserved_prefix(pg_uri):
    name = "__iosql_part_v1_" + "0" * 32
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE TABLE events (ts BIGINT PRIMARY KEY, CONSTRAINT {} CHECK (ts > 0)) PARTITION BY RANGE (ts)").format(sql.Identifier(name)))
    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="约束|分区|[Pp]artition"):
            table = db.table("events", pk=["ts"], columns={"ts": "integer"}, partition=_duration("1day"))
            table.write({"ts": -1})
    with closing(psycopg2.connect(pg_uri)) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='events'::regclass AND conname=%s", (name,))
            assert "ts > 0" in cursor.fetchone()[0]


def test_pg_older_migration_contract_is_checked_even_if_bounds_match(pg_uri):
    import json

    args = {"pk": ["d"], "columns": {"d": "text"}}
    with Database(pg_uri) as db:
        table = db.table("events", partition=Partition.calendar("d", timezone="UTC"), **args)
        table.write({"d": "2024-01-15"})
        contract = table.spec().to_dict()
        marker = table._partition_marker(table.spec().to_partition())
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("ALTER TABLE events DROP CONSTRAINT {}").format(sql.Identifier(marker)))
            cursor.execute("CREATE SCHEMA IF NOT EXISTS __iosql_control")
            cursor.execute("""CREATE TABLE IF NOT EXISTS __iosql_control.table_contracts (
                schema_name TEXT NOT NULL, table_name TEXT NOT NULL,
                relation_oid OID NOT NULL, contract JSONB NOT NULL,
                PRIMARY KEY (schema_name, table_name))""")
            cursor.execute("""INSERT INTO __iosql_control.table_contracts
                VALUES (current_schema(), 'events', 'events'::regclass, %s)""", (json.dumps(contract),))
    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="分区|[Pp]artition"):
            db.table("events", partition=Partition.calendar("d", timezone="Europe/London"), **args)
        table = db.table("events", partition=Partition.calendar("d", timezone="UTC"), **args)
        table.write({"d": "2024-01-16"})
        assert table.count_rows() == 2


def test_pg_force_rebinds_empty_persisted_contract_atomically(pg_uri):
    import json

    args = {"pk": ["ts"], "columns": {"ts": "integer"}}
    with Database(pg_uri) as db:
        table = db.table("events", partition=_duration("1day"), description="旧说明", **args)
        contract = table.spec().to_dict()
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS __iosql_control")
            cursor.execute("""CREATE TABLE IF NOT EXISTS __iosql_control.table_contracts (
                schema_name TEXT NOT NULL, table_name TEXT NOT NULL,
                relation_oid OID NOT NULL, contract JSONB NOT NULL,
                PRIMARY KEY (schema_name, table_name))""")
            cursor.execute("""INSERT INTO __iosql_control.table_contracts
                VALUES (current_schema(), 'events', 'events'::regclass, %s)""", (json.dumps(contract),))
    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="分区|[Pp]artition"):
            db.table("events", partition=_duration("1hour"), **args)
        table = db.table("events", partition=_duration("1hour"), force_partition=True, **args)
        table.write({"ts": 7_200_000})
    with closing(psycopg2.connect(pg_uri)) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT contract FROM __iosql_control.table_contracts WHERE schema_name=current_schema() AND table_name='events'")
            stored = cursor.fetchone()[0]
            assert stored["description"] == "旧说明"
            assert stored["partition"]["every"] == 3_600_000
    with Database(pg_uri) as db:
        table = db.table("events", partition=_duration("1hour"), **args)
        assert table.select() == [{"ts": 7_200_000}]
        with pytest.raises(TableSpecConflict, match="分区|[Pp]artition"):
            db.table("events", partition=_duration("1day"), **args)


def test_pg_uncommitted_first_write_holds_layout_lock_until_rollback(pg_uri):
    args = {"pk": ["ts"], "columns": {"ts": "integer"}}
    first = Database(pg_uri)
    second = Database(pg_uri)
    errors = []
    entered = threading.Event()
    finished = threading.Event()
    try:
        day = first.table("events", partition=_duration("1day"), **args)
        hour = second.table("events", partition=_duration("1hour"), **args)
        day.write({"ts": 0}, commit=False)

        def write_hour():
            try:
                entered.set()
                hour.write({"ts": 90_000_000})
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=write_hour)
        worker.start()
        assert entered.wait(timeout=5)
        assert not finished.wait(timeout=0.2)
        day.close()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []
        assert hour.select() == [{"ts": 90_000_000}]
    finally:
        first.close()
        second.close()


def test_pg_legacy_first_write_requires_owner_but_existing_marker_does_not(pg_uri):
    from urllib.parse import urlsplit, urlunsplit

    role = "iosql_part_user_" + uuid.uuid4().hex[:12]
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
            if not cursor.fetchone()[0]:
                pytest.skip("权限测试需要独立实例的超级用户")
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    try:
        with closing(psycopg2.connect(pg_uri)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute("CREATE TABLE events (ts BIGINT PRIMARY KEY) PARTITION BY RANGE (ts)")
                cursor.execute("CREATE TABLE events_p_p_0 PARTITION OF events FOR VALUES FROM (0) TO (86400000)")
                cursor.execute("SELECT current_schema()")
                schema = cursor.fetchone()[0]
                cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
                cursor.execute(sql.SQL("GRANT SELECT, INSERT ON events TO {}").format(sql.Identifier(role)))
                cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname='__iosql_control'")
                if cursor.fetchone():
                    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA __iosql_control TO {}").format(sql.Identifier(role)))
                    cursor.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA __iosql_control TO {}").format(sql.Identifier(role)))
        parts = urlsplit(pg_uri)
        role_uri = urlunsplit((parts.scheme, f"{role}@{parts.hostname}:{parts.port}", parts.path, parts.query, parts.fragment))
        args = {"pk": ["ts"], "columns": {"ts": "integer"}, "partition": _duration("1day")}
        with Database(role_uri) as db:
            table = db.table("events", **args)
            with pytest.raises(TableSpecConflict, match="所有者"):
                table.write({"ts": 1})
        with Database(pg_uri) as db:
            db.table("events", **args).write({"ts": 1})
        with Database(role_uri) as db:
            table = db.table("events", **args)
            table.write({"ts": 2})
            assert table.select(order_by="ts") == [{"ts": 1}, {"ts": 2}]
    finally:
        with closing(psycopg2.connect(pg_uri)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_pg_native_parent_lock_blocks_external_ddl_during_layout_validation(pg_uri, monkeypatch):
    args = {"pk": ["ts"], "columns": {"ts": "integer"}, "partition": _duration("1day")}
    errors = []
    reached = threading.Event()
    release = threading.Event()
    ddl_started = threading.Event()
    ddl_done = threading.Event()
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"ts": 0})
        original = table._inspect_native_layout

        def paused(partition):
            reached.set()
            assert release.wait(timeout=10)
            return original(partition)

        def writer():
            try:
                table.write({"ts": 1})
            except Exception as exc:
                errors.append(exc)

        def external_ddl():
            try:
                with closing(psycopg2.connect(pg_uri)) as conn:
                    conn.autocommit = True
                    with conn.cursor() as cursor:
                        ddl_started.set()
                        cursor.execute("ALTER TABLE events ADD COLUMN extra TEXT")
            except Exception as exc:
                errors.append(exc)
            finally:
                ddl_done.set()

        monkeypatch.setattr(table, "_inspect_native_layout", paused)
        write_thread = threading.Thread(target=writer)
        write_thread.start()
        try:
            assert reached.wait(timeout=10)
            ddl_thread = threading.Thread(target=external_ddl)
            ddl_thread.start()
            assert ddl_started.wait(timeout=5)
            assert not ddl_done.wait(timeout=0.2)
        finally:
            release.set()
            write_thread.join(timeout=10)
            if ddl_started.is_set():
                ddl_thread.join(timeout=10)
        assert not write_thread.is_alive()
        assert ddl_done.is_set()
        assert errors == []
        assert len(table.select()) == 2


def test_pg_range_partition_supports_negative_and_positive_keys(pg_uri):
    args = {"pk": ["id"], "columns": {"id": "integer"},
            "partition": Partition.range("id", every=100)}
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"id": -1})
        table.write({"id": 100})
        assert table.select(order_by="id") == [{"id": -1}, {"id": 100}]
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"id": -2})
        table.write({"id": 101})
        assert table.select(order_by="id") == [
            {"id": -2}, {"id": -1}, {"id": 100}, {"id": 101},
        ]


def test_pg_legacy_negative_partition_name_is_reused_without_collision(pg_uri):
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE events (id BIGINT PRIMARY KEY) PARTITION BY RANGE (id)")
            cursor.execute("CREATE TABLE events_p_p_100 PARTITION OF events FOR VALUES FROM (-100) TO (0)")
            cursor.execute("INSERT INTO events VALUES (-1)")
    args = {"pk": ["id"], "columns": {"id": "integer"}, "partition": Partition.range("id", every=100)}
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write([{"id": -2}, {"id": 100}])
        assert table.select(order_by="id") == [{"id": -2}, {"id": -1}, {"id": 100}]
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"id": 101})
        assert table.count_rows() == 4


def test_pg_long_table_names_do_not_truncate_distinct_partition_names(pg_uri):
    name = "events_" + "a" * 51
    args = {"pk": ["id"], "columns": {"id": "integer"}, "partition": Partition.range("id", every=100)}
    with Database(pg_uri) as db:
        table = db.table(name, **args)
        table.write([{"id": 1}, {"id": 101}, {"id": -1}])
        assert table.count_rows() == 3
    with Database(pg_uri) as db:
        table = db.table(name, **args)
        table.write({"id": 102})
        assert table.count_rows() == 4


def test_pg_legacy_calendar_with_renamed_child_remains_writable(pg_uri):
    _create_legacy_calendar_table(pg_uri)
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE events_p_p_20241230 RENAME TO old_tail")
    with Database(pg_uri) as db:
        table = db.table("events", pk=["d"], columns={"d": "text"},
                         partition=Partition.calendar("d", every="4day", timezone="UTC"))
        table.write([{"d": "2024-12-30"}, {"d": "2025-01-01"}])
        assert table.select(order_by="d") == [
            {"d": "2024-12-30"}, {"d": "2024-12-31"}, {"d": "2025-01-01"},
        ]


def test_pg_migration_checks_physical_layout_during_preflight_even_with_saved_contract(pg_uri, tmp_path):
    with Database(f"sqlite://{tmp_path / 'source.iosql'}") as source:
        table = source.table("events", pk=["ts"], columns={"ts": "integer"}, partition=_duration("1day"))
        table.write({"ts": 1})
        assert table.migrate_to(pg_uri).published
        with closing(psycopg2.connect(pg_uri)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute("DROP TABLE events_p_p_0")
                cursor.execute("CREATE TABLE events_p_p_0 PARTITION OF events FOR VALUES FROM (0) TO (3600000)")
        with pytest.raises(TableSpecConflict) as failure:
            table.migrate_to(pg_uri)
        assert failure.value.stage == "preflight"
        assert table.select() == [{"ts": 1}]


def test_pg_empty_table_with_saved_marker_requires_force_to_change_partition(pg_uri):
    args = {"pk": ["ts"], "columns": {"ts": "integer"}}
    with Database(pg_uri) as db:
        db.table("events", partition=_duration("1day"), **args).write({"ts": 1})
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("DROP TABLE events_p_p_0")
    with Database(pg_uri) as db:
        with pytest.raises(TableSpecConflict, match="分区|[Pp]artition"):
            db.table("events", partition=_duration("1hour"), **args)
        table = db.table("events", partition=_duration("1hour"), force_partition=True, **args)
        table.write({"ts": 3_600_001})
        assert table.select() == [{"ts": 3_600_001}]


def test_pg_calendar_fall_back_numeric_time_accepts_both_occurrences(pg_uri):
    from iosql import TimeColumn

    values = [1_730_610_000_000, 1_730_611_800_000, 1_730_613_600_000, 1_730_615_400_000]
    args = {
        "time": TimeColumn("ts", unit="ms", timezone="America/New_York"),
        "pk": ["ts"], "columns": {"ts": "integer"}, "symbol_column": None,
        "partition": Partition.calendar("ts", every="30min", timezone="America/New_York"),
    }
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        table.write([{"ts": value} for value in values])
        assert table.select(order_by="ts") == [{"ts": value} for value in values]
        assert len(table.table_parts()) == 1
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        for value in values:
            assert table.query(start=value, end=value).to_list() == [{"ts": value}]


def test_pg_transition_day_does_not_overlap_cross_midnight_buckets(pg_uri):
    from iosql import TimeColumn

    values = [1_730_606_340_000, 1_730_606_400_000, 1_730_696_340_000, 1_730_696_400_000]
    args = {
        "time": TimeColumn("ts", unit="ms", timezone="America/New_York"),
        "pk": ["ts"], "columns": {"ts": "integer"}, "symbol_column": None,
        "partition": Partition.calendar("ts", every="7min", timezone="America/New_York"),
    }
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        table.write([{"ts": value} for value in values])
        assert table.select(order_by="ts") == [{"ts": value} for value in values]
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        table.write([{"ts": values[0] - 1_000}, {"ts": values[-1] + 1_000}])
        assert table.count_rows() == len(values) + 2


def test_pg_old_transition_day_layout_stays_readable_and_requires_migration(pg_uri, tmp_path):
    from iosql import TimeColumn

    first = 1_730_610_000_000
    second = 1_730_613_600_000
    with closing(psycopg2.connect(pg_uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE events (ts BIGINT PRIMARY KEY) PARTITION BY RANGE (ts)")
            cursor.execute("CREATE TABLE events_old_halfhour PARTITION OF events FOR VALUES FROM (%s) TO (%s)",
                           (first, first + 1_800_000))
            cursor.execute("INSERT INTO events VALUES (%s)", (first,))
    args = {
        "time": TimeColumn("ts", unit="ms", timezone="America/New_York"),
        "pk": ["ts"], "columns": {"ts": "integer"}, "symbol_column": None,
        "partition": Partition.calendar("ts", every="30min", timezone="America/New_York"),
    }
    target = f"sqlite://{tmp_path / 'dst-recovered.iosql'}"
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        assert table.select() == [{"ts": first}]
        with pytest.raises(TableSpecConflict, match="迁移|migrate"):
            table.write({"ts": second})
        assert table.select() == [{"ts": first}]
        result = table.migrate_to(target, verify="row-count")
        assert result.published and result.verified
    with Database(target) as db:
        table = db.table("events")
        table.write({"ts": second})
        assert table.query(start=second, end=second).to_list() == [{"ts": second}]


def test_pg_timestamptz_fall_back_uses_one_safe_day_partition(pg_uri):
    args = {"pk": ["ts"], "columns": {"ts": "timestamptz"},
            "partition": Partition.calendar("ts", every="30min", timezone="America/New_York")}
    values = ["2024-11-03T01:00:00-04:00", "2024-11-03T01:00:00-05:00"]
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write([{"ts": value} for value in values])
        assert table.count_rows() == 2
        assert len(table.table_parts()) == 1
    with Database(pg_uri) as db:
        table = db.table("events", **args)
        table.write({"ts": "2024-11-03T01:30:00-05:00"})
        assert table.count_rows() == 3


def test_pg_spring_forward_ninety_minute_partition_accepts_adjusted_bucket(pg_uri):
    from iosql import TimeColumn

    value = 1_711_846_800_000
    args = {
        "time": TimeColumn("ts", unit="ms", timezone="Europe/London"),
        "pk": ["ts"], "columns": {"ts": "integer"}, "symbol_column": None,
        "partition": Partition.calendar("ts", every="90min", timezone="Europe/London"),
    }
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        table.write({"ts": value})
        assert table.query(start=value, end=value).to_list() == [{"ts": value}]


def test_pg_utc_minute_partition_keeps_normal_granularity_during_dst_dates(pg_uri):
    from iosql import TimeColumn

    values = [1_730_610_000_000, 1_730_611_800_000, 1_730_613_600_000, 1_730_615_400_000]
    with Database(pg_uri) as db:
        table = db.timeseries("events", time=TimeColumn("ts", unit="ms", timezone="UTC"),
                              pk=["ts"], columns={"ts": "integer"}, symbol_column=None,
                              partition=Partition.calendar("ts", every="30min", timezone="UTC"))
        table.write([{"ts": value} for value in values])
        assert table.count_rows() == len(values)
        assert len(table.table_parts()) == 4


@pytest.mark.parametrize("physical_type", ["integer", "timestamptz"])
def test_pg_business_day_query_handles_midnight_timezone_transition(pg_uri, physical_type):
    import arrow
    from iosql import TimeColumn

    values = ["2018-11-04T02:59:59Z", "2018-11-04T03:00:00Z",
              "2018-11-05T01:59:59Z", "2018-11-05T02:00:00Z"]
    instants = [int(arrow.get(value).timestamp() * 1000) for value in values]
    inserted = instants if physical_type == "integer" else values
    args = {"time": TimeColumn("ts", unit="ms", timezone="America/Sao_Paulo"),
            "pk": ["ts"], "columns": {"ts": physical_type}, "symbol_column": None,
            "partition": Partition.calendar("ts", every="90min", timezone="America/Sao_Paulo")}
    with Database(pg_uri) as db:
        table = db.timeseries("events", **args)
        table.write([{"ts": value} for value in inserted])
        rows = table.query(date="2018-11-04", order_by="ts").to_list()
        observed = [row["ts"] if physical_type == "integer" else int(arrow.get(row["ts"]).timestamp() * 1000)
                    for row in rows]
        assert observed == instants[1:3]
