"""SQLite/mysqlite 十一项审查缺陷的行为回归。"""

import sqlite3
import multiprocessing as mp
from collections import OrderedDict
from contextlib import nullcontext

import pytest

from iosql import Case, Database, Partition, Segment
from iosql.engines.sqlite.table import Table as SQLiteTable
from iosql.exception import BadArgument
from iosql.utils.mysqlite import Database as MyDatabase
from iosql.utils.mysqlite.table import Table as MyTable


@pytest.mark.parametrize('batch_size', [1, 10])
@pytest.mark.parametrize('transaction', [False, True])
def test_mysqlite_evolve_preserves_all_rows(tmp_path, batch_size, transaction):
    with MyDatabase(str(tmp_path / 'single.db')) as db:
        table = db['items']
        table.create_table([{'name': 'id', 'type': 'integer'},
                            {'name': 'x', 'type': 'text'}], pk='id')
        rows = [{'id': 1, 'x': '先前批次'}, {'id': 2, 'x': '补列', 'y': 20},
                {'id': 3, 'x': '末批次', 'y': 30}]
        with db.transaction() if transaction else nullcontext():
            table.insert_all(rows, batch_size=batch_size, ensure_column=True)
        assert table.select_all('*', order_by='id') == [
            {'id': 1, 'x': '先前批次', 'y': None}, rows[1], rows[2]]


def test_mysqlite_evolve_failure_rolls_back_rows_and_schema(tmp_path):
    with MyDatabase(str(tmp_path / 'single.db')) as db:
        table = db['items']
        table.create_table([{'name': 'id', 'type': 'integer'}], pk='id')
        with pytest.raises(sqlite3.IntegrityError):
            table.insert_all([{'id': 1}, {'id': 2, 'new': 1}, {'id': 2}],
                             batch_size=1, ensure_column=True)
        assert table.count() == 0
        table.invalidate_cache()
        assert list(table.column_info()['column_type']) == ['id']


@pytest.mark.parametrize('operation', ['insert', 'insert_all', 'execute_params', 'executemany', 'update'])
def test_mysqlite_numpy_values_are_native_storage_classes(tmp_path, operation):
    np = pytest.importorskip('numpy')
    with MyDatabase(str(tmp_path / 'single.db')) as db:
        table = db['items']
        table.create_table([{'name': 'id', 'type': 'integer'},
                            {'name': 'value', 'type': 'real'},
                            {'name': 'flag', 'type': 'integer'}], pk='id')
        row = {'id': np.int64(5), 'value': np.float32(1.25), 'flag': np.bool_(True)}
        if operation == 'insert':
            table.insert(row)
        elif operation == 'insert_all':
            table.insert_all([row])
        elif operation == 'execute_params':
            table.execute_params('INSERT INTO items VALUES (:id, :value, :flag)', row, commit=True)
        elif operation == 'executemany':
            table.executemany('INSERT INTO items VALUES (?, ?, ?)', [tuple(row.values())], commit=True)
        else:
            table.insert({'id': 1})
            table.update(row, 'id=1')
        result = table.query('SELECT *, typeof(id) AS kind FROM items WHERE id=?',
                             params=[np.int64(5)], to_dict=True)
        assert result == [{'id': 5, 'value': 1.25, 'flag': 1, 'kind': 'integer'}]
        assert dict(table.fetchone('SELECT id FROM items WHERE id=?', [np.int64(5)])) == {'id': 5}


def test_numpy_primary_keys_work_with_segment_ignore_and_stream_queries(tmp_path):
    np = pytest.importorskip('numpy')
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': 'real'},
                         partition=Partition.range('id', every=10), segment=Segment.rows(max_rows=1))
        table.write([{'id': np.int64(5), 'v': np.float32(1.25)}], mode='ignore')
        table.write([{'id': np.int64(5), 'v': np.float32(2.5)}], mode='ignore')
        assert table.select() == [{'id': 5, 'v': 1.25}]
        assert list(table.iter_select(where=Case('id', '=', 5), order_by='id')) == [
            {'id': 5, 'v': 1.25}]


@pytest.mark.parametrize('dtype,type_,value,kind', [
    *[(dtype, 'integer', 5, 'integer') for dtype in (
        'int8', 'int16', 'int32', 'int64', 'uint8', 'uint16', 'uint32', 'uint64')],
    *[(dtype, 'real', 1.25, 'real') for dtype in ('float16', 'float32', 'float64', 'longdouble')],
    ('bool_', 'integer', True, 'integer'), ('str_', 'text', '值', 'text'),
    ('bytes_', 'blob', b'\0\xff', 'blob'),
])
def test_numpy_scalar_families_bind_without_buffers(tmp_path, dtype, type_, value, kind):
    np = pytest.importorskip('numpy')
    with MyDatabase(str(tmp_path / 'single.db')) as db:
        table = db['items']
        table.create_table([{'name': 'v', 'type': type_}])
        table.insert_all([{'v': getattr(np, dtype)(value)}])
        assert tuple(table.fetchone('SELECT v, typeof(v) FROM items')) == (value, kind)


def test_manual_create_registers_empty_and_written_logical_table(tmp_path):
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db['items']
        table.create_table({'id': 'integer', 'x': 'text'}, pk=['id'])
        assert db.table_names() == ['items']
        assert table.table_parts() == ['None']
        assert table.count_rows() == 0
        assert table.get_table_meta()['pk'] == ['id']
        table.insert_all([{'id': 1, 'x': '可见'}], pk=['id'])
        assert table.select() == [{'id': 1, 'x': '可见'}]
        assert table.count_rows() == 1
    with Database(uri) as reopened:
        assert reopened.table_names() == ['items']
        assert reopened.table('items').select() == [{'id': 1, 'x': '可见'}]


@pytest.mark.parametrize('declaration,values', [
    ('TEXT COLLATE NOCASE', ['b', 'A', 'a', 'B', 'é', 'É', 'a\0x', 'A\0yy', None]),
    ('TEXT COLLATE RTRIM', ['b ', 'a  ', 'a', 'b', 'a\t', None]),
    ('ANY', [2, 10, 'a', None, b'\0', -1, 1.5]),
])
@pytest.mark.parametrize('direction', ['asc', 'desc'])
def test_global_sort_and_limit_match_sqlite(tmp_path, declaration, values, direction):
    with sqlite3.connect(':memory:') as oracle:
        oracle.execute(f'CREATE TABLE data (id INTEGER PRIMARY KEY, v {declaration})')
        oracle.executemany('INSERT INTO data VALUES (?, ?)', list(enumerate(values)))
        expected = [dict(zip(['id', 'v'], row)) for row in oracle.execute(
            f'SELECT * FROM data ORDER BY v {direction}, id')]
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': declaration},
                         partition=Partition.range('id', every=1))
        table.write([{'id': i, 'v': v} for i, v in enumerate(values)])
        order = [('v', direction), ('id', 'asc')]
        assert table.select(order_by=order) == expected
        assert table.select(order_by=order, limit=2) == expected[:2]
        assert list(table.iter_select(order_by=order, batch_size=1)) == expected
        assert list(table.iter_select(order_by=order, limit=2)) == expected[:2]


@pytest.mark.parametrize('declaration,symbols,requested', [
    ('integer', [1, 1, 2, 2], ['2', '1']),
    ('TEXT COLLATE NOCASE', ['A', 'a', 'B', 'b'], ['b', 'A']),
    ('TEXT COLLATE RTRIM', ['a ', 'a', 'b  ', 'b'], ['b ', 'a  ']),
])
def test_latest_matches_symbols_and_groups_using_sqlite_equality(tmp_path, declaration, symbols, requested):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('ticks', time='ts', pk=['ts'],
                         columns={'ts': 'integer', 'symbol': declaration}, symbol_column='symbol',
                         partition=Partition.range('ts', every=1))
        table.write([{'ts': i + 1, 'symbol': value} for i, value in enumerate(symbols)])
        assert len(table.query(symbols=requested).to_list()) == 4
        expected = [{'ts': 4, 'symbol': symbols[3]}, {'ts': 2, 'symbol': symbols[1]}]
        assert table.latest(symbols=requested).to_list() == expected
        assert table.latest(symbols=requested + requested).to_list() == expected
        assert table.latest().to_list() == expected


def test_latest_request_order_uses_comparison_affinity_without_real_rounding(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('ticks', time='ts', pk=['ts'],
                         columns={'ts': 'integer', 'symbol': 'real'}, symbol_column='symbol',
                         segment=Segment.rows(max_rows=1))
        table.write([{'ts': 1, 'symbol': 2**53}, {'ts': 2, 'symbol': 1}])
        assert table.query(symbols=2**53 + 1).to_list() == []
        assert table.latest(symbols=[2**53 + 1, 1, 2**53]).to_list() == [
            {'ts': 2, 'symbol': 1.0}, {'ts': 1, 'symbol': float(2**53)}]


@pytest.mark.parametrize('collation,keys', [
    ('NOCASE', ['a', 'z', 'A', 'y']), ('RTRIM', ['a ', 'z', 'a', 'y']),
])
def test_compact_preserves_collation_equivalent_primary_keys(tmp_path, collation, keys):
    uri = f'sqlite://{tmp_path / "db"}'
    rows = [{'id': key, 'seq': i} for i, key in enumerate(keys)]
    with Database(uri) as db:
        table = db.table('items', pk=['id'], columns={'id': f'TEXT COLLATE {collation}', 'seq': 'integer'},
                         segment=Segment.rows(max_rows=2))
        table.write(rows)
        table.compact(target_rows=2)
        assert table.select(order_by='seq') == rows
        assert table.validate(deep=True).ok
    with Database(uri) as reopened:
        assert reopened.table('items').select(order_by='seq') == rows


@pytest.mark.parametrize('values', [[1, 'BAD'], ['BAD', 1], [1, 2.5, 'BAD']])
def test_contract_rejects_every_incompatible_type_in_sample(tmp_path, values):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': 'integer'})
        with pytest.raises(BadArgument, match='类型冲突'):
            table.write([{'id': i, 'v': value} for i, value in enumerate(values)])
        assert table.count_rows() == 0


@pytest.mark.parametrize('segment', [False, True])
def test_real_partition_routes_the_value_sqlite_actually_stores(tmp_path, segment):
    original = 2**53 + 1
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': 'real'},
                         partition=Partition.range('v', every=1),
                         segment=Segment.rows(max_rows=2) if segment else None)
        row = {'id': 1, 'v': original}
        result = table.write(row)
        stored = float(original)
        assert row['v'] == original and isinstance(row['v'], int)
        assert result.partitions_touched == [int(stored)]
        assert table.select() == [{'id': 1, 'v': stored}]
        assert table.select(where=Case('v', '=', stored)) == [{'id': 1, 'v': stored}]
        assert table.explain_select(where=Case('v', '=', stored)).candidate_partitions == [int(stored)]


def test_column_stats_cache_keeps_latest_file_version_and_releases_on_close(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteTable, '_column_stats_cache', OrderedDict())
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer'})
        for i in range(8):
            table.write({'id': i})
            table.explain_select()
            assert len(SQLiteTable._column_stats_cache) == 1
        assert table._data_file_column_stats(None) == {'id': {'min': 0, 'max': 7}}
    assert len(SQLiteTable._column_stats_cache) == 0


def test_column_stats_cache_has_a_global_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteTable, '_column_stats_cache', OrderedDict())
    monkeypatch.setattr(SQLiteTable, '_column_stats_cache_limit', 3, raising=False)
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer'},
                         partition=Partition.range('id', every=1))
        table.write([{'id': i} for i in range(8)])
        table.explain_select()
        assert len(SQLiteTable._column_stats_cache) <= 3


@pytest.mark.parametrize('operation', ['create_table', 'insert_all'])
def test_mysqlite_create_without_meta_invalidates_schema_cache(tmp_path, operation):
    with MyDatabase(str(tmp_path / 'single.db')) as db:
        table = db['items']
        assert table.column_info()['column_type'] == {}
        assert table.pk() == []
        if operation == 'create_table':
            table.create_table([{'name': 'id', 'type': 'integer'}], pk='id', save_meta=False)
        else:
            table.insert_all([{'id': 1}], pk='id', ensure_table=True, save_meta=False)
        assert list(table.column_info()['column_type']) == ['id']
        assert table.pk() == ['id']


@pytest.mark.parametrize('layout', ['plain', 'partition', 'segment', 'both'])
def test_manual_create_preserves_declared_layout_on_reopen(tmp_path, layout):
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': 'integer'},
                         partition=Partition.range('v', every=10) if layout in ('partition', 'both') else None,
                         segment=Segment.rows(max_rows=1) if layout in ('segment', 'both') else None)
        table.create_table({'id': 'integer', 'v': 'integer'}, pk=['id'])
        assert table.select() == []
        assert db.table_names() == ['items']
    with Database(uri) as reopened:
        table = reopened.table('items')
        assert bool(table.spec().partition) == (layout in ('partition', 'both'))
        assert bool(table.spec().segment) == (layout in ('segment', 'both'))
        table.write([{'id': 1, 'v': 1}, {'id': 2, 'v': 11}])
        assert table.select(order_by='id') == [{'id': 1, 'v': 1}, {'id': 2, 'v': 11}]
        assert table.validate(deep=True).ok
        # 幂等建表保留已有数据和存量列。
        table.create_table({'id': 'integer'}, pk=['id'])
        assert table.count_rows() == 2


def test_manual_create_failure_does_not_register_logical_table(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db['items']
        with pytest.raises(BadArgument, match='主键'):
            table.create_table({'v': 'integer'}, pk=['id'])
        assert table.get_table_meta() is None
        assert table.table_parts() == []
        assert db.table_names() == []


def test_latest_and_compact_use_physical_collation_over_contract_alias(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('ticks', time='ts', pk=['symbol'],
                         columns={'ts': 'integer', 'symbol': 'text'}, symbol_column='symbol',
                         segment=Segment.rows(max_rows=2))
        table.write([{'ts': 1, 'symbol': 'a'}, {'ts': 2, 'symbol': 'z'},
                     {'ts': 3, 'symbol': 'A'}, {'ts': 4, 'symbol': 'y'}],
                    column_type={'symbol': 'TEXT COLLATE NOCASE'})
        assert table.latest(symbols='a').to_list() == [{'ts': 3, 'symbol': 'A'}]
        table.compact(target_rows=2)
        assert table.count_rows() == 4
        assert table.latest(symbols='a').to_list() == [{'ts': 3, 'symbol': 'A'}]


def test_column_stats_never_cache_an_old_snapshot_under_a_new_version(tmp_path, monkeypatch):
    monkeypatch.setattr(SQLiteTable, '_column_stats_cache', OrderedDict())
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1})
        original = MyTable.query
        changed = False

        def query_with_write(self, sql, *args, **kwargs):
            nonlocal changed
            result = original(self, sql, *args, **kwargs)
            if 'MIN(' in sql and not changed:
                changed = True
                table.write({'id': 2})
            return result

        monkeypatch.setattr(MyTable, 'query', query_with_write)
        assert table._data_file_column_stats(None) == {'id': {'min': 1, 'max': 1}}
        assert table._data_file_column_stats(None) == {'id': {'min': 1, 'max': 2}}
        # 返回值不能污染后续命中的缓存。
        stats = table._data_file_column_stats(None)
        stats['id']['max'] = 999
        assert table._data_file_column_stats(None)['id']['max'] == 2


def _read_stats_in_child(uri, pipe):
    with Database(uri) as db:
        stats = db.table('items')._data_file_column_stats(None)
    pipe.send(stats)
    pipe.close()


@pytest.mark.skipif('fork' not in mp.get_all_start_methods(), reason='平台没有 fork')
def test_column_stats_cache_resets_in_fork_child(tmp_path):
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        db.table('items', pk=['id'], columns={'id': 'integer'}).write({'id': 1})
    context = mp.get_context('fork')
    parent, child = context.Pipe()
    process = context.Process(target=_read_stats_in_child, args=(uri, child))
    try:
        with SQLiteTable._column_stats_cache_lock:
            process.start()
            child.close()
            process.join(timeout=10)
            assert not process.is_alive(), 'fork 子进程继承了已占用的缓存锁'
        assert process.exitcode == 0
        assert parent.poll(1)
        assert parent.recv() == {'id': {'min': 1, 'max': 1}}
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        child.close()
        parent.close()
