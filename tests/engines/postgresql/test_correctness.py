"""PostgreSQL 逻辑审查反例：事务、批内冲突、文本精度和分区布局并发。"""

import os
import uuid
from contextlib import closing

import psycopg2
from psycopg2 import sql
import pytest

from iosql import Database, Partition
from iosql.exception import BadArgument, UnsafeOperation
from iosql.coordination import lease
from iosql.migration_postgresql import _stage_uri
import iosql.engines.postgresql.table as pg_module


@pytest.fixture
def pg_uri():
    base = os.environ.get('POSTGRES_DSN')
    if not base:
        pytest.skip('需要显式配置独立 PostgreSQL 测试实例')
    schema = 'iosql_correctness_' + uuid.uuid4().hex
    with closing(psycopg2.connect(base)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        yield _stage_uri(base, schema)
    finally:
        with closing(psycopg2.connect(base)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def _terminate(uri, table):
    pid = table.connect().get_backend_pid()
    with closing(psycopg2.connect(uri)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('SELECT pg_terminate_backend(%s)', (pid,))
            assert cur.fetchone()[0]


@pytest.mark.parametrize('name', ['x' * 64, '表' * 22])
def test_long_table_and_column_names_are_rejected_before_ddl(pg_uri, name):
    with Database(pg_uri) as db:
        with pytest.raises(BadArgument, match='63'):
            db.table(name, pk=['id'], columns={'id': 'integer'})
        with pytest.raises(BadArgument, match='63'):
            db.table('events', pk=[name], columns={name: 'integer'})
        assert db.table_names() == []


def test_identifier_at_utf8_byte_limit_remains_writable(pg_uri):
    with Database(pg_uri) as db:
        name = '表' * 21
        table = db.table(name, pk=['ts'], columns={'ts': 'integer'},
                         partition=Partition.duration('ts'))
        table.write({'ts': 0})
        with pytest.raises(BadArgument, match='63'):
            table.rename(name + 'x')
        assert table.table_name == name
        assert table.select() == [{'ts': 0}]


def test_reconnect_refuses_to_continue_lost_transaction(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1}, commit=False)
        _terminate(pg_uri, table)
        with pytest.raises(psycopg2.OperationalError):
            table.select()
        with pytest.raises(UnsafeOperation, match='事务'):
            table.write({'id': 2}, commit=False)
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        with lease(pg_uri, exclusive=True):
            pass
        # 丢失事务的失败已经明确告知，调用方可以重新开始一笔可回滚事务。
        table.write({'id': 3}, commit=False)
        assert not table.connect().autocommit
        table.close()
    with Database(pg_uri) as db:
        assert db['events'].select() == []


def test_disconnect_during_write_preserves_error_and_releases_transaction(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1}, commit=False)
        _terminate(pg_uri, table)
        # 回滚断开的连接会再次失败，但不能用 InterfaceError 覆盖原始断连错误。
        with pytest.raises(psycopg2.OperationalError):
            table.write({'id': 2})
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        table.write({'id': 3}, commit=False)
        table.close()
    with Database(pg_uri) as db:
        assert db['events'].select() == []


def test_close_clears_disconnected_transaction_state(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1}, commit=False)
        _terminate(pg_uri, table)
        with pytest.raises(psycopg2.OperationalError):
            table.select()
        table.close()
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        table.write({'id': 2}, commit=False)
        table.close()
    with Database(pg_uri) as db:
        assert db['events'].select() == []


def test_stream_disconnect_preserves_original_error_and_releases_lease(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}])
        stream = table.query(order_by='id').iter_rows(batch_size=1)
        assert next(stream) == {'id': 1}
        _terminate(pg_uri, table)
        with pytest.raises(psycopg2.OperationalError):
            next(stream)
        with lease(pg_uri, exclusive=True):
            pass
        assert table.connect().autocommit
        assert table.count_rows() == 2


def test_deferred_commit_failure_resets_transaction_and_lease(pg_uri):
    with Database(pg_uri) as db:
        parent = db.table('parent', pk=['id'], columns={'id': 'integer'})
        table = db.table('events', pk=['id'], columns={'id': 'integer', 'parent_id': 'integer'})
        with table.connect().cursor() as cur:
            cur.execute('ALTER TABLE events ADD FOREIGN KEY (parent_id) REFERENCES parent(id) '
                        'DEFERRABLE INITIALLY DEFERRED')
        table.write({'id': 1, 'parent_id': 9}, commit=False)
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            table.write({'id': 2, 'parent_id': 9})
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        assert table.connect().autocommit
        assert table.select() == []
        parent.write({'id': 9})
        table.write({'id': 3, 'parent_id': 9})
        assert table.count_rows() == 1


@pytest.mark.parametrize('all_rows', [False, True])
def test_delete_commits_pending_writes(pg_uri, all_rows):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 0})
        table.write({'id': 1}, commit=False)
        deleted = table.delete_all() if all_rows else table.delete({'field': 'id', 'op': '=', 'value': 0})
        assert deleted == (2 if all_rows else 1)
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        assert table.connect().autocommit
    with Database(pg_uri) as db:
        assert db['events'].select() == ([] if all_rows else [{'id': 1}])


@pytest.mark.parametrize('all_rows', [False, True])
def test_delete_failure_rolls_back_pending_writes(pg_uri, all_rows):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        child = db.table('child', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 0})
        with child.connect().cursor() as cur:
            cur.execute('ALTER TABLE child ADD FOREIGN KEY (id) REFERENCES events(id)')
        child.write({'id': 0})
        table.write({'id': 1}, commit=False)
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            if all_rows:
                table.delete_all()
            else:
                table.delete({'field': 'id', 'op': '=', 'value': 0})
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        assert table.connect().autocommit
        assert table.select() == [{'id': 0}]


@pytest.mark.parametrize('mode', ['upsert', 'replace'])
@pytest.mark.parametrize('partition', [None, 'range(id,width=10)'])
@pytest.mark.parametrize('budget', [3, 60_000])
def test_repeated_primary_keys_do_not_depend_on_sql_pages(pg_uri, monkeypatch, mode, partition, budget):
    monkeypatch.setattr(pg_module, '_INSERT_MAX_PARAMS', budget)
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer', 'a': 'integer', 'b': 'integer'},
                         partition=partition)
        result = table.write([{'id': 1, 'a': 10, 'b': 11}, {'id': 2, 'a': 20},
                              {'id': 1 if partition else '1', 'b': 12}], mode=mode)
        assert result.rows_seen == 3
        assert result.rows_written == (3 if budget == 3 else 2)
        assert table.select(order_by='id') == [
            {'id': 1, 'a': None, 'b': 12}, {'id': 2, 'a': 20, 'b': None}]


def test_primary_key_only_upsert_counts_only_inserted_rows(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        assert table.write([{'id': 1}, {'id': '1'}], mode='upsert').rows_written == 1
        assert table.write({'id': 1}, mode='replace').rows_written == 0


def test_uncertain_commit_does_not_leave_a_phantom_transaction(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer'})
        connection = table.connect()

        class LostReply:
            def __getattr__(self, name):
                return getattr(connection, name)

            @property
            def autocommit(self):
                return connection.autocommit

            @autocommit.setter
            def autocommit(self, value):
                connection.autocommit = value

            def commit(self):
                connection.commit()
                raise psycopg2.OperationalError('提交回复丢失')

        table._conn = LostReply()
        with pytest.raises(psycopg2.OperationalError, match='提交回复丢失'):
            table.write({'id': 1})
        assert not table._in_explicit_tx
        assert table._iosql_transaction_lease is None
        table._conn = connection
        # 不能把“未收到提交回复”当作“未落库”；新事务仍可正常回滚。
        table.write({'id': 2}, commit=False)
        table.close()
    with Database(pg_uri) as db:
        assert db['events'].select() == [{'id': 1}]


@pytest.mark.parametrize('budget', [3, 60_000])
def test_partial_upsert_preserves_input_order_for_equivalent_primary_keys(pg_uri, monkeypatch, budget):
    monkeypatch.setattr(pg_module, '_INSERT_MAX_PARAMS', budget)
    with Database(pg_uri) as db:
        table = db.table('events', pk=['id'], columns={'id': 'integer', 'a': 'integer', 'b': 'integer'})
        table.write({'id': 1, 'a': 0, 'b': 0})
        table.upsert_all([{'id': 1, 'a': 1}, {'id': '1', 'a': 2, 'b': 2},
                          {'id': 1, 'b': 3}], pk=['id'])
        assert table.select() == [{'id': 1, 'a': 2, 'b': 3}]


def test_repeated_native_timestamp_keys_are_compared_after_conversion(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['ts'], columns={'ts': 'timestamptz', 'v': 'integer'},
                         partition=Partition.calendar('ts'))
        table.write([{'ts': '2026-01-06T00:00:00Z', 'v': 1},
                     {'ts': '2026-01-06T08:00:00+08:00', 'v': 2}], mode='upsert')
        assert table.count_rows() == 1
        assert table.query(date='2026-01-06').one()['v'] == 2


@pytest.mark.parametrize('values,start,end', [
    (['2026-01-06 10:00:00', '2026-01-06 10:00:01', '2026-01-06 10:00:02'],
     '2026-01-06 10:00:00.5', '2026-01-06 10:00:01.5'),
    (['2026-01-06 10:00', '2026-01-06 10:01', '2026-01-06 10:02'],
     '2026-01-06 10:00:30', '2026-01-06 10:01:30'),
    (['20260106100000', '20260106100001', '20260106100002'],
     '2026-01-06 10:00:00.5', '2026-01-06 10:00:01.5'),
    (['2026-01-06T10:00:00.1', '2026-01-06T10:00:00.2', '2026-01-06T10:00:00.3'],
     '2026-01-06T10:00:00.15', '2026-01-06T10:00:00.25'),
])
def test_text_time_bounds_respect_finer_query_precision(pg_uri, values, start, end):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['ts'], columns={'ts': 'text'},
                         partition=Partition.calendar('ts'))
        table.write([{'ts': value} for value in values])
        table.write({'ts': '20260107100000' if values[0].isdigit() else
                     values[0].replace('2026-01-06', '2026-01-07')})
        query = table.query(start=start, end=end, order_by='ts')
        assert query.to_list() == [{'ts': values[1]}]
        assert list(query.iter_rows(batch_size=1)) == [{'ts': values[1]}]
        assert len(query.explain().scanned_partitions) == 1
        assert table.query(start=values[1], end=values[1]).to_list() == [{'ts': values[1]}]


def test_text_lower_bound_can_advance_to_next_day(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['ts'], columns={'ts': 'text'},
                         partition=Partition.calendar('ts', timezone='America/New_York'))
        table.write([{'ts': '2026-01-06 23:59:59'}, {'ts': '2026-01-07 00:00:00'}])
        assert table.query(start='2026-01-07T04:59:59.5Z').to_list() == [
            {'ts': '2026-01-07 00:00:00'}]


def test_text_time_column_with_percent_is_not_double_escaped(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events', pk=['symbol', 'ts%'], symbol_column='symbol',
                         columns={'symbol': 'text', 'ts%': 'text'},
                         partition=Partition.calendar('ts%'))
        row = {'symbol': 'A', 'ts%': '2026-01-06 10:00:00'}
        table.write(row)
        assert table.query(date='2026-01-06').one() == row
        assert table.latest(symbols='A').one() == row


def test_parameterized_partition_delete_and_index_support_percent_names(pg_uri):
    with Database(pg_uri) as db:
        table = db.table('events%history', pk=['ts%'],
                         columns={'ts%': 'text', 'value%': 'integer'},
                         partition=Partition.calendar('ts%'))
        table.write([{'ts%': '2026-01-06', 'value%': 1}, {'ts%': '2026-01-07', 'value%': 2}])
        table.create_index(columns=['value%'])
        assert table.query(date='2026-01-06').one()['value%'] == 1
        assert table.delete({'field': 'value%', 'op': '=', 'value': 1}) == 1
        assert table.count_rows() == 1
        table.drop_index(columns=['value%'])


def test_parent_lock_blocks_attach_until_write_finishes(pg_uri, monkeypatch):
    with Database(pg_uri) as db, closing(psycopg2.connect(pg_uri)) as external:
        external.autocommit = True
        table = db.table('events', pk=['id'], columns={'id': 'integer'},
                         partition=Partition.range('id', 100))
        table.write({'id': 0})
        with external.cursor() as cur:
            cur.execute('CREATE TABLE candidate (LIKE events INCLUDING ALL)')
            cur.execute("SET lock_timeout='150ms'")
        original = table._inspect_native_layout
        blocked = []

        def inspect_while_attaching(partition):
            children = original(partition)
            with external.cursor() as cur:
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cur.execute('ALTER TABLE events ATTACH PARTITION candidate '
                                'FOR VALUES FROM (100) TO (200)')
            blocked.append(True)
            return children

        monkeypatch.setattr(table, '_inspect_native_layout', inspect_while_attaching)
        table.write({'id': 1})
        assert blocked == [True]
        # 提交后同一 DDL 可以完成，证明锁随事务释放。
        with external.cursor() as cur:
            cur.execute('ALTER TABLE events ATTACH PARTITION candidate FOR VALUES FROM (100) TO (200)')
        monkeypatch.setattr(table, '_inspect_native_layout', original)
        table.write({'id': 101})
        assert table.select(order_by='id') == [{'id': 0}, {'id': 1}, {'id': 101}]
