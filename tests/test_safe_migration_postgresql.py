"""使用显式配置的独立 PostgreSQL 实例验证事务和服务器锁。"""

import os
import threading
import uuid
from contextlib import closing

import pytest

from iosql import Database, Partition
from iosql.exception import BadArgument, DatabaseBusy, MigrationError, MigrationVerificationError, TableSpecConflict, UnsafeOperation

import psycopg2
from psycopg2 import sql

from iosql.migration_postgresql import PostgreSQLPublisher, _stage_uri


@pytest.fixture
def pg_uri():
    base = os.environ.get('POSTGRES_DSN')
    if not base:
        pytest.skip('需要显式设置独立测试库 POSTGRES_DSN')
    schema = 'iosql_safe_test_' + uuid.uuid4().hex
    with closing(psycopg2.connect(base)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        yield _stage_uri(base, schema)
    finally:
        with closing(psycopg2.connect(base)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def source_table(tmp_path, name='ticks'):
    db = Database(f'sqlite://{tmp_path}/source')
    table = db.table(name, pk=['ts'], columns={'ts': 'integer', 'v': 'text'},
                     partition=Partition.duration('ts', every='1day'))
    table.write([{'ts': 1000, 'v': 'a'}, {'ts': 3601000, 'v': 'b'}])
    return db, table


def test_pg_partition_change_and_roundtrip(tmp_path, pg_uri):
    db, table = source_table(tmp_path)
    with db:
        result = table.migrate_to(pg_uri, tables={'ticks': 'archived'},
                                  specs={'ticks': {'partition': 'duration(ts,every=1hour)'}},
                                  verify='row-count', batch_size=1)
        assert result.published and result.target_rows == result.source_rows == 2
        assert len(result.partitions_created) == 2
        with Database(pg_uri) as pg:
            archived = pg.table('archived', spec=table.spec().with_partition(every='1hour'))
            back_uri = f'parquet://{tmp_path}/back'
            back = archived.migrate_to(back_uri, verify='row-count',
                                       specs={'archived': {'partition': None}})
            assert back.published and back.verified and back.target_rows == 2
            assert not archived._in_explicit_tx
        with Database(back_uri) as local:
            assert local['archived'].count_rows() == 2
            assert local['archived'].spec().partition is None


def test_pg_all_tables_rollback_on_final_failure(tmp_path, pg_uri, monkeypatch):
    db, table = source_table(tmp_path)
    from iosql.engines.postgresql.table import Table
    with db:
        second = db.table('second', pk=['id'], columns={'id': 'integer'})
        second.write({'id': 1})
        original = Table.write

        def fail(self, *args, **kwargs):
            if getattr(self, '_iosql_migration_owned', False) and self.table_name == 'second':
                raise RuntimeError('second final table failed')
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Table, 'write', fail)
        with pytest.raises(MigrationError):
            db.migrate_to(pg_uri, tables=['ticks', 'second'])
        with Database(pg_uri) as target:
            assert target.table_names() == []


def test_pg_final_actual_count_is_verified(tmp_path, pg_uri, monkeypatch):
    db, table = source_table(tmp_path)
    from iosql.engines.postgresql.table import Table
    original = Table.write

    def extra(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if getattr(self, '_iosql_migration_owned', False):
            with self._conn.cursor() as cursor:
                cursor.execute(sql.SQL('INSERT INTO {} VALUES (%s,%s)').format(
                    sql.Identifier(self.table_name)), (9999, 'extra'))
        return result

    with db:
        monkeypatch.setattr(Table, 'write', extra)
        with pytest.raises(MigrationVerificationError) as failure:
            table.migrate_to(pg_uri, specs={'ticks': {'partition': None}}, verify='row-count')
        assert failure.value.partial_result.verified is False
        with Database(pg_uri) as target:
            assert target.table_names() == []


def test_pg_existing_target_lock_blocks_sql_and_iosql(tmp_path, pg_uri, monkeypatch):
    db, table = source_table(tmp_path)
    with db, Database(pg_uri) as target:
        existing = target.table('ticks', spec=table.spec())
        existing.write({'ts': 500, 'v': 'old'})
        with closing(psycopg2.connect(pg_uri)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute('CREATE VIEW ticks_view AS SELECT * FROM ticks')
                cursor.execute("SELECT 'ticks'::regclass::oid")
                oid = cursor.fetchone()[0]
        ready, release = threading.Event(), threading.Event()
        failures = []
        original = PostgreSQLPublisher.finish_stage

        def pause(self, plans):
            original(self, plans)
            ready.set()
            assert release.wait(10)

        monkeypatch.setattr(PostgreSQLPublisher, 'finish_stage', pause)

        def migrate():
            try:
                table.migrate_to(pg_uri)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=migrate)
        thread.start()
        try:
            assert ready.wait(10)
            with pytest.raises(DatabaseBusy):
                existing.count_rows()
            with closing(psycopg2.connect(pg_uri)) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET lock_timeout='50ms'")
                    with pytest.raises(psycopg2.errors.LockNotAvailable):
                        cursor.execute('SELECT * FROM ticks')
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive() and not failures
        with closing(psycopg2.connect(pg_uri)) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 'ticks'::regclass::oid, (SELECT count(*) FROM ticks_view)")
                assert cursor.fetchone() == (oid, 3)


def test_pg_source_uses_private_locking_snapshot(tmp_path, pg_uri, monkeypatch):
    db, original = source_table(tmp_path)
    with db:
        original.migrate_to(pg_uri)
        with Database(pg_uri) as pg:
            table = pg.table('ticks', spec=original.spec())
            caller_connection = table.connect()
            ready, release = threading.Event(), threading.Event()
            failures = []
            from iosql.migration import LocalPublisher
            original_prepare = LocalPublisher.prepare

            def pause(self, plans):
                ready.set()
                assert release.wait(10)
                return original_prepare(self, plans)

            monkeypatch.setattr(LocalPublisher, 'prepare', pause)

            def migrate():
                try:
                    table.migrate_to(f'sqlite://{tmp_path}/back', verify='row-count')
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=migrate)
            thread.start()
            try:
                assert ready.wait(10)
                with closing(psycopg2.connect(pg_uri)) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SET lock_timeout='50ms'")
                        with pytest.raises(psycopg2.errors.LockNotAvailable):
                            cursor.execute('SELECT * FROM ticks')
                assert table._conn is caller_connection and not table._in_explicit_tx
            finally:
                release.set()
                thread.join(10)
            assert not thread.is_alive() and not failures
            assert table.count_rows() == 2


def test_pg_pending_caller_transaction_not_hijacked(tmp_path, pg_uri):
    db, original = source_table(tmp_path)
    with db:
        original.migrate_to(pg_uri)
        with Database(pg_uri) as pg:
            table = pg.table('ticks', spec=original.spec())
            table.write({'ts': 8000, 'v': 'uncommitted'}, commit=False)
            connection = table._conn
            with pytest.raises(UnsafeOperation):
                table.migrate_to(f'sqlite://{tmp_path}/back', lock_timeout=0)
            assert table._in_explicit_tx and connection.get_transaction_status() == 2
            assert table.count_rows() == 3
            table.close()
        with closing(psycopg2.connect(pg_uri)) as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT count(*) FROM ticks')
                assert cursor.fetchone()[0] == 2


@pytest.mark.parametrize('commit_happened', [True, False])
def test_pg_uncertain_commit_uses_durable_status(tmp_path, pg_uri, monkeypatch, commit_happened):
    import iosql.migration_postgresql as migration_pg
    connect = migration_pg._connect
    connections = []

    class Connection:
        def __init__(self, raw):
            object.__setattr__(self, 'raw', raw)

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def __setattr__(self, name, value):
            setattr(self.raw, name, value)

        def commit(self):
            if commit_happened:
                self.raw.commit()
            raise psycopg2.OperationalError('lost COMMIT reply')

    def wrapped(*args, **kwargs):
        raw = connect(*args, **kwargs)
        connections.append(raw)
        return Connection(raw) if len(connections) == 1 else raw

    monkeypatch.setattr(migration_pg, '_connect', wrapped)
    db, table = source_table(tmp_path)
    with db:
        if commit_happened:
            result = table.migrate_to(pg_uri, verify='row-count')
            assert result.published and result.target_rows == 2
        else:
            with pytest.raises(MigrationError):
                table.migrate_to(pg_uri, verify='row-count')
        with Database(pg_uri) as target:
            assert target.table_names() == (['ticks'] if commit_happened else [])


def test_pg_existing_partition_layout_mismatch_preflight(tmp_path, pg_uri):
    db, table = source_table(tmp_path)
    with db:
        table.migrate_to(pg_uri)
        with pytest.raises((BadArgument, TableSpecConflict)):
            table.migrate_to(pg_uri, specs={'ticks': {'partition': 'duration(ts,every=1hour)'}})
        with closing(psycopg2.connect(pg_uri)) as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT count(*) FROM ticks')
                assert cursor.fetchone()[0] == 2


def _pg_child_check(uri, queue):
    try:
        Database(uri).table_names()
    except DatabaseBusy:
        queue.put('busy')
    else:
        queue.put('unexpected-access')


def test_pg_fork_does_not_end_parent_advisory_session(pg_uri):
    import multiprocessing
    from iosql.coordination import lease
    from iosql.coordination_postgresql import DATABASE_LOCK
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('需要 fork')
    context = multiprocessing.get_context('fork')
    queue = context.Queue()
    with lease(pg_uri, exclusive=True):
        process = context.Process(target=_pg_child_check, args=(pg_uri, queue))
        process.start()
        process.join(10)
        assert process.exitcode == 0 and queue.get(timeout=1) == 'busy'
        with closing(psycopg2.connect(pg_uri)) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock_shared(%s)', (DATABASE_LOCK,))
                assert cursor.fetchone()[0] is False
    queue.close()


def test_pg_early_epoch_calendar_and_batch_limit(tmp_path, pg_uri, monkeypatch):
    db, table = source_table(tmp_path)
    from iosql.engines.postgresql.table import Table
    original = Table.write
    batch_sizes = []

    def write(self, rows, *args, **kwargs):
        if getattr(self, '_iosql_migration_owned', False):
            batch_sizes.append(len(rows))
        return original(self, rows, *args, **kwargs)

    monkeypatch.setattr(Table, 'write', write)
    with db:
        result = table.migrate_to(pg_uri, batch_size=1, verify='row-count',
                                  specs={'ticks': {'partition': 'calendar(ts,every=1hour,timezone=UTC)'}})
        assert result.published and batch_sizes == [1, 1]
        with Database(pg_uri) as target:
            migrated = target.table('ticks', spec=table.spec().with_partition(
                partition='calendar(ts,every=1hour,timezone=UTC)'))
            assert len(migrated.table_parts()) == 2
            assert migrated.query(start=0, end=3600000).to_list() == [{'ts': 1000, 'v': 'a'}]


def test_pg_bound_selected_and_unselected_objects_survive_publication(tmp_path, pg_uri):
    db, table = source_table(tmp_path)
    with db, Database(pg_uri) as target:
        ticks = target.table('ticks', spec=table.spec())
        keep = target.table('keep', pk=['id'], columns={'id': 'integer'})
        keep.write({'id': 9})
        table.migrate_to(pg_uri, verify='row-count')
        assert target.table_names() == ['keep', 'ticks']
        assert target.table('keep') is keep
        keep.write({'id': 10})
        ticks.write({'ts': 9000, 'v': 'after'})
        assert keep.count_rows() == 2
        assert ticks.count_rows() == 3


def test_migration_connections_preserve_explicit_uri_timeout(monkeypatch):
    from types import SimpleNamespace
    from iosql.coordination_postgresql import connect_control
    from iosql.migration_postgresql import _connect
    calls = []

    def connect(uri, **options):
        calls.append((uri, options))
        return SimpleNamespace()

    monkeypatch.setattr(psycopg2, 'connect', connect)
    uri = 'postgresql://user@localhost:5432/db?connect_timeout=7'
    connect_control(uri, timeout=1)
    _connect(uri, timeout=1)
    assert len(calls) == 2
    assert all(item[0] == uri and 'connect_timeout' not in item[1] for item in calls)
