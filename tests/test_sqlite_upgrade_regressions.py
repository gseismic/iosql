"""目录表与 mysqlite 升级后的分区、查询、DDL 和绑定回归。"""

import os
import sqlite3
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from threading import Event

import pytest

from iosql import Case, Database, Partition, PartitionSpec, Segment, TableContract
from iosql.exception import BadArgument, SchemaConflict, TableSpecConflict
from iosql.utils.mysqlite import Database as MyDatabase
from iosql.utils.mysqlite import sql as mysqlite_sql
from iosql.utils.mysqlite.exception import BadArgument as MyBadArgument, DuplicateColumn


@pytest.mark.parametrize('segmented', [False, True])
def test_inferred_partition_type_cannot_round_distinct_primary_keys(tmp_path, segmented):
    rows = [{'v': v, 'tag': 'A'} for v in (2**53, 2**53 + 1, 3.5)]
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['v', 'tag'], columns={'tag': 'text'},
                         partition=Partition.range('v', every=1),
                         segment=Segment.rows(max_rows=2) if segmented else None)
        with pytest.raises(BadArgument, match='整数精度'):
            table.spec().parts_for(rows)
        with pytest.raises(BadArgument, match='整数精度'):
            table.write(rows, mode='ignore')
        assert table.table_parts() == []
        assert table.get_table_meta() is None
        result = table.write(rows, mode='ignore', column_type={'v': 'integer'})
        assert result.partitions_touched == [3, 2**53, 2**53 + 1]
        assert table.select(order_by='v') == [rows[2], rows[0], rows[1]]


def test_legacy_misrouted_real_primary_key_can_be_read_and_migrated(tmp_path):
    source = f'sqlite://{tmp_path / "source"}'
    target = f'sqlite://{tmp_path / "target"}'
    stored = 2**53
    legacy_part = stored + 1
    with Database(source) as db:
        table = db.table('items', pk=['v', 'tag'], columns={'v': 'real', 'tag': 'text'},
                         partition=Partition.range('v', every=1))
        table.write({'v': stored, 'tag': 'A'})
        # 旧写入按整数选址，SQLite 文件内的 REAL 主键已经舍入。
        table.close()
        os.replace(table._get_part_dbfile(stored), table._get_part_dbfile(legacy_part))
        table.connect()
        meta = table.get_table_meta()
        meta.pop('sqlite_partition_routing')
        table._overwrite_table_meta(meta)
        with table.table_meta_db.transaction() as conn:
            conn.execute('UPDATE table_part SET part=?', (str(legacy_part),))
            conn.execute('UPDATE table_part_column_range SET part=?, column_min=?, column_max=?',
                         (str(legacy_part), legacy_part, legacy_part + 1))

    with Database(source) as db:
        table = db.table('items')
        assert table.select(where=Case('v', '=', stored)) == [{'v': float(stored), 'tag': 'A'}]
        assert table.select(where=Case('v', '=', legacy_part)) == []
        for value in (legacy_part, stored, float(stored)):
            with pytest.raises(TableSpecConflict, match=r'validate.*repartition_to'):
                table.write({'v': value, 'tag': 'A'}, mode='replace')
        assert table.count_rows() == 1
        assert table.table_parts() == [legacy_part]
        assert not table.validate(deep=True).ok
        assert table.repartition_to(target).rows_copied == 1
    with Database(target) as db:
        table = db.table('items')
        assert table.table_parts() == [stored]
        assert table.validate(deep=True).ok
        assert table.write({'v': legacy_part, 'tag': 'A'}, mode='replace').partitions_touched == [stored]
        assert table.count_rows() == 1


def test_aligned_legacy_real_layout_becomes_prunable_after_write(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['v'], columns={'v': 'real'},
                         partition=Partition.range('v', every=1))
        table.write([{'v': 2}, {'v': 4}])
        meta = table.get_table_meta()
        meta.pop('sqlite_partition_routing')
        table._overwrite_table_meta(meta)
        case = Case('v', '=', 2)
        assert table.explain_select(where=case).scanned_partitions == [2, 4]
        table.write({'v': 5})
        assert table.explain_select(where=case).scanned_partitions == [2]


def test_first_writers_choose_physical_type_inside_table_lock(tmp_path, monkeypatch):
    uri = f'sqlite://{tmp_path / "db"}'
    entered, release = Event(), Event()
    with Database(uri) as db_a, Database(uri) as db_b:
        def declare(db):
            return db.table('items', pk=['v', 'tag'], columns={'tag': 'text'},
                            partition=Partition.range('v', every=1))

        first, second = declare(db_a), declare(db_b)
        original_lock = first._mutation_lock

        @contextmanager
        def delayed_lock():
            entered.set()
            assert release.wait(10)
            with original_lock():
                yield

        monkeypatch.setattr(first, '_mutation_lock', delayed_lock)
        value = 2**53 + 1
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first.write, {'v': value, 'tag': 'A'}, column_type={'v': 'real'})
            try:
                assert entered.wait(10)
                second.write({'v': 1, 'tag': 'B'}, column_type={'v': 'integer'})
            finally:
                release.set()
            assert future.result(timeout=10).partitions_touched == [value]
        assert second.select(order_by='v') == [{'v': 1, 'tag': 'B'}, {'v': value, 'tag': 'A'}]


def test_parts_for_queries_and_upsert_share_physical_partition_address(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        real = db.table('floats', pk=['id'], columns={'id': 'integer', 'v': 'integer'},
                        partition=Partition.range('v', every=1))
        row = {'id': 1, 'v': 2**53 + 1}
        touched = real.write(row, column_type={'v': 'real'}).partitions_touched
        assert real.spec().columns['v'] == 'integer'
        assert real.spec().parts_for([row]) == touched == [2**53]
        assert TableContract.from_dict(real.spec().to_dict()).parts_for([row]) == [row['v']]
        assert real.select(where=Case('v', '=', row['v'])) == []
        assert real.select(where=Case('v', '=', 2**53)) == [{'id': 1, 'v': float(2**53)}]

        numeric = db.table('numeric', pk=['v'], partition=Partition.range('v', every=1),
                           columns={'v': 'STRING', 'keep': 'text', 'note': 'text'})
        row = {'v': '001', 'keep': 'before'}
        assert numeric.spec().parts_for([row]) == numeric.write(row).partitions_touched == [1]
        numeric.upsert_all([{'v': '001', 'note': 'updated'}], pk=['v'])
        expected = [{'v': 1, 'keep': 'before', 'note': 'updated'}]
        assert numeric.select(where=Case('v', '=', '001')) == expected
        assert numeric.select(where=Case('v', 'in', ['002', '001'])) == expected


@pytest.mark.parametrize('segmented', [False, True])
def test_rounded_real_key_matches_actual_write_conflict_target(tmp_path, segmented):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['v'], columns={'v': 'real', 'keep': 'text', 'update': 'text'},
                         partition=Partition.range('v', every=1),
                         segment=Segment.rows(max_rows=1) if segmented else None)
        table.write({'v': 2**53, 'keep': 'old'})
        if segmented:
            table.write({'v': 2**53 + 1, 'keep': 'duplicate'}, mode='ignore')
            assert table.select() == [{'v': float(2**53), 'keep': 'old', 'update': None}]
        else:
            table.upsert_all({'v': 2**53 + 1, 'update': 'new'}, pk=['v'])
            assert table.select() == [{'v': float(2**53), 'keep': 'old', 'update': 'new'}]


def test_native_integer_routing_reuses_rows_without_staging_sql(tmp_path, monkeypatch):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        partition = Partition.range('v', every=100)
        table = db.table('items', pk=['v'], columns={'v': 'integer'}, partition=partition)
        contract = table.spec()
        payload = {f'field{i}': i for i in range(128)}
        rows = [dict(payload, v=i) for i in range(1000)]

        def fail(*args, **kwargs):
            pytest.fail('原生整数路由不应创建临时 SQLite 库')

        monkeypatch.setattr(sqlite3, 'connect', fail)
        groups = table._group_partition_rows(rows, partition, ['v'], contract, None, {})
        assert list(groups) == list(range(0, 1000, 100))
        assert all(actual is source for actual, source in zip(
            (row for group in groups.values() for row in group), rows,
        ))


def _price_table(db, segmented=False):
    table = db.table('items', pk=['id'], columns={'id': 'integer'},
                     partition=Partition.range('id', every=10),
                     segment=Segment.rows(max_rows=2) if segmented else None)
    table.write([{'id': 1}, {'id': 11}])
    return table


@pytest.mark.parametrize('segmented', [False, True])
@pytest.mark.parametrize('inline', [False, True])
def test_alter_uses_full_column_definition_on_old_and_new_partitions(tmp_path, segmented, inline):
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        table = _price_table(db, segmented=segmented)
        if inline:
            table.create_table({'price': 'INTEGER NOT NULL DEFAULT 7'}, pk=['id'], alter=True)
        else:
            table.create_table({'price': 'integer'}, pk=['id'], alter=True,
                               not_null=['price'], default={'price': 7})
        table.write([{'id': 2}, {'id': 21}])
        for value in (3, 22):
            with pytest.raises(sqlite3.IntegrityError):
                table.write({'id': value, 'price': None})
    with Database(uri) as db:
        table = db.table('items')
        assert table.select(order_by='id') == [{'id': id_, 'price': 7} for id_ in (1, 2, 11, 21)]


@pytest.mark.parametrize('invalid', ['column_order', 'reorder', 'unique', 'check', 'metadata'])
def test_invalid_manual_alter_preserves_existing_files_and_manifest(tmp_path, invalid):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = _price_table(db)
        before = table.get_table_meta()
        arguments = {
            'column_order': ({'price': 'INTEGER'}, {'column_order': ['missing']}, BadArgument),
            'reorder': ({'price': 'INTEGER'}, {'column_order': ['price', 'id']}, SchemaConflict),
            'unique': ({'price': 'INTEGER'}, {'unique': {'price': True}}, SchemaConflict),
            # 缺省 7 满足分区 0 的历史行，亦须预检分区 10 的历史行。
            'check': ({'price': 'INTEGER CHECK(price >= id) DEFAULT 7'}, {}, SchemaConflict),
            'metadata': ({'price': 'BLOB'}, {'default': {'price': b'bytes'}}, BadArgument),
        }
        columns, kwargs, error = arguments[invalid]
        with pytest.raises(error):
            table.create_table(columns, pk=['id'], alter=True, **kwargs)
        assert table.get_table_meta() == before
        assert table.select(order_by='id') == [{'id': 1}, {'id': 11}]


@pytest.mark.parametrize('layout', ['partition', 'segment', 'both'])
def test_deferred_create_validates_actual_sqlite_ddl(tmp_path, layout):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'],
                         partition=Partition.range('id', every=10) if layout != 'segment' else None,
                         segment=Segment.rows(max_rows=2) if layout != 'partition' else None)
        with pytest.raises(DuplicateColumn):
            table.create_table({'id': 'INTEGER', 'ID': 'INTEGER'}, pk=['id'])
        with pytest.raises(sqlite3.OperationalError, match='unknown_column'):
            table.create_table({'id': 'INTEGER CHECK(id >= unknown_column)'}, pk=['id'])
        assert db.table_names() == [] and table.table_parts() == []
        assert table.get_table_meta() is None


@pytest.mark.parametrize('declare_first', [False, True])
def test_manual_calendar_create_uses_local_date_key(tmp_path, declare_first):
    uri = f'sqlite://{tmp_path / "db"}'
    partition = Partition.calendar('day', every='1day', timezone='America/New_York')
    row = {'day': '2020-02-01'}
    with Database(uri) as db:
        table = db.table('events', pk=['day'], columns={'day': 'text'}, partition=partition) \
            if declare_first else db['events']
        table.create_table({'day': 'text'}, pk=['day'], partition=partition)
        assert table.spec().partition.date_key == 'local'
        assert table.write(row).partitions_touched == ['20200201']
    with Database(uri) as db:
        table = db.table('events')
        assert table.query(date=row['day']).to_list() == [row]


def test_manual_calendar_create_retains_legacy_date_key_on_reopen(tmp_path):
    uri = f'sqlite://{tmp_path / "db"}'
    tz = 'America/New_York'
    row = {'day': '2020-02-01'}
    with Database(uri) as db:
        table = db.table('events', pk=['day'], columns={'day': 'text'}, partition=PartitionSpec(
            column='day', kind='calendar', every='1day', timezone=tz, date_key='legacy',
        ))
        assert table.write(row).partitions_touched == ['20200131']
    with Database(uri) as db:
        table = db.table('events')
        table.create_table({'day': 'text'}, pk=['day'], partition=Partition.calendar('day', timezone=tz))
        assert table.write(row, mode='replace').partitions_touched == ['20200131']
        assert table.query(date=row['day']).to_list() == [row]
        assert table.table_parts() == ['20200131']


@pytest.mark.parametrize('engine', ['sqlite', 'parquet'])
def test_latest_empty_symbols_and_literal_single_symbol(tmp_path, engine):
    with Database(f'{engine}://{tmp_path / "db"}') as db:
        table = db.table('prices', pk=['ts', 'symbol'], time='ts', symbol_column='symbol',
                         columns={'ts': 'integer', 'symbol': 'text', 'venue': 'text', 'other': 'text'},
                         partition=Partition.range('ts', every=1))
        older = {'ts': 1, 'symbol': '[[other]]', 'venue': 'X', 'other': 'B'}
        newest = {'ts': 2, 'symbol': 'A', 'venue': 'X', 'other': 'A'}
        table.write([older, newest])
        assert table.latest(by='venue', symbols=None).to_list() == [newest]
        assert table.latest(by='venue', symbols='[[other]]').to_list() == [older]
        assert table.query(symbols='[[other]]').to_list() == [older]
        for empty in ([], (), set()):
            for by in (None, 'venue'):
                query = table.latest(by=by, symbols=empty)
                assert query.to_list() == []
                assert list(query.iter_rows()) == []
                assert query.explain().scanned_partitions == []


def test_parquet_latest_uses_duckdb_list_comparison_for_request_order(tmp_path):
    with Database(f'parquet://{tmp_path / "db"}') as db:
        table = db.table('prices', pk=['ts'], time='ts', symbol_column='symbol',
                         columns={'ts': 'integer', 'symbol': 'integer'})
        table.write([{'ts': ts, 'symbol': symbol} for ts, symbol in
                     enumerate((1, 2, 2**53, 2**53 + 1))])
        assert table.latest(symbols=2**63).to_list() == table.query(symbols=2**63).to_list() == []
        assert [row['symbol'] for row in table.latest(symbols=[1.5, 1, 2]).to_list()] == [1, 2]
        assert [row['symbol'] for row in table.latest(symbols=[2**63, 1]).to_list()] == [1]
        # 混合 DOUBLE 请求时，DuckDB IN 可在共同类型下命中两个相邻的大整数。
        symbols = [1.5, 2**53 + 1, 1]
        assert [row['symbol'] for row in table.latest(symbols=symbols).to_list()] == [
            2**53 + 1, 2**53, 1,
        ]
        assert {row['symbol'] for row in table.query(symbols=symbols).to_list()} == {2**53 + 1, 2**53, 1}


@pytest.mark.parametrize('partitioned', [False, True])
@pytest.mark.parametrize('symbol_type', ['integer', 'float'])
def test_parquet_comparison_does_not_prune_rounded_matches(tmp_path, partitioned, symbol_type):
    with Database(f'parquet://{tmp_path / "db"}') as db:
        table = db.table(
            'prices', pk=['ts', 'symbol'], time='ts', symbol_column='symbol',
            columns={'ts': 'integer', 'symbol': symbol_type}, segment=Segment.rows(max_rows=1),
            partition=Partition.range('symbol', every=1) if partitioned else None,
        )
        first = {'ts': 1, 'symbol': 2**53}
        second = {'ts': 2, 'symbol': 2**53 + 1}
        if symbol_type == 'integer':
            table.write([first, second])
            requested, expected = [1.5, 2**53 + 1], [second, first]
        else:
            table.write(first)
            requested, expected = [2**53 + 1, 1], [first]
        assert table.latest(symbols=requested).to_list() == expected
        assert table.query(symbols=requested, order_by='ts desc').to_list() == expected


def test_latest_real_comparison_keeps_business_value_range(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('prices', pk=['ts'], time='ts', symbol_column='symbol',
                         columns={'ts': 'integer', 'symbol': 'real'},
                         column_ranges={'symbol': {'low': 0, 'high': 10}})
        table.write([{'ts': 1, 'symbol': 2.0}, {'ts': 2, 'symbol': 3.0}])
        assert table.latest(symbols=[2, 3]).to_list() == [{'ts': 1, 'symbol': 2.0}, {'ts': 2, 'symbol': 3.0}]
        assert table.spec().column_ranges == {'symbol': {'low': 0, 'high': 10}}


def test_order_by_resolves_collation_with_dependent_columns(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': 'INTEGER CHECK(v >= id)'},
                         segment=Segment.rows(max_rows=1))
        first, second = {'id': 1, 'v': 8}, {'id': 2, 'v': 3}
        table.write([first, second])
        assert table.select(order_by='v') == [second, first]
        assert list(table.iter_select(order_by='v')) == [second, first]


def test_compact_uses_physical_order_and_collation(tmp_path):
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('items', pk=['id'], columns={'id': 'integer', 'v': 'TEXT COLLATE NOCASE'},
                         order_key='v', segment=Segment.rows(max_rows=2))
        table.write([{'id': 1, 'v': 'a'}, {'id': 2, 'v': 'B'}], column_type={'v': 'TEXT'})
        table.compact(target_rows=1)
        refs = table.segments()
        assert [table._rows_from_part_dbfile(ref['path'])[0]['v'] for ref in refs] == ['B', 'a']
        assert [row['v'] for row in table.select(order_by='v')] == ['B', 'a']


@pytest.mark.parametrize('alias', ['rowid', '_rowid_', 'OID'])
def test_mysqlite_ensure_column_preserves_writable_rowid_alias(tmp_path, alias):
    with MyDatabase(str(tmp_path / 'db.sqlite3')) as db:
        table = db['items']
        table.execute('CREATE TABLE items(id INTEGER PRIMARY KEY)')
        table.insert_all([{alias: 42}], ensure_column=True)
        assert table.select_all('*') == [{'id': 42}]


def test_mysqlite_ensure_column_distinguishes_unicode_case_and_without_rowid(tmp_path):
    with MyDatabase(str(tmp_path / 'db.sqlite3')) as db:
        table = db['items']
        table.execute('CREATE TABLE items(id INTEGER PRIMARY KEY, "Ä" TEXT) WITHOUT ROWID')
        table.insert_all([{'id': 1, 'ä': '值', 'rowid': 42}], ensure_column=True)
        assert table.select_all('*') == [{'id': 1, 'Ä': None, 'ä': '值', 'rowid': 42}]


def test_mysqlite_native_parameter_fast_path_without_numpy(tmp_path, monkeypatch):
    monkeypatch.setattr(mysqlite_sql, '_has_np', False)
    for params in ([1, 2], (1, 2), {'id': 1}):
        assert mysqlite_sql.normalize_sqlite_parameters(params) is params
    values = [(1, '一'), (2, '二')]
    assert mysqlite_sql.normalize_sqlite_parameter_rows(values) is values
    with MyDatabase(str(tmp_path / 'db.sqlite3')) as db:
        table = db['items']
        table.execute('CREATE TABLE items(id INTEGER, tag TEXT)')
        table.executemany('INSERT INTO items VALUES (?, ?)', values)
        table.executemany('INSERT INTO items VALUES (:id, :tag)', [UserDict(id=3, tag='三')])
        assert table.select_all('*', order_by='id') == [dict(id=id_, tag=tag)
                                                     for id_, tag in [*values, (3, '三')]]


def _legacy_numpy_database(path, rows, extra_ddl=''):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('CREATE TABLE items(k INTEGER, tag TEXT COLLATE NOCASE, v TEXT,'
                     ' PRIMARY KEY(k, tag)) WITHOUT ROWID')
        if extra_ddl:
            conn.execute(extra_ddl)
        conn.executemany('INSERT INTO items VALUES (?, ?, ?)', rows)


def test_mysqlite_numpy_blob_key_upgrade_is_explicit_and_preserves_business_blob(tmp_path):
    np = pytest.importorskip('numpy')
    path = tmp_path / 'db.sqlite3'
    legacy = np.int64(10)
    _legacy_numpy_database(path, [(legacy, 'A', 'legacy'), (memoryview(legacy), 'B', 'business')])
    with MyDatabase(str(path)) as db:
        table = db['items']
        incoming = {'k': legacy, 'tag': 'a', 'v': 'new'}
        for insert in (lambda: table.insert(incoming, replace=True),
                       lambda: table.insert_all([incoming], replace=True)):
            with pytest.raises(MyBadArgument, match='migrate_numpy_blob_keys'):
                insert()
        assert table.count() == 2
        assert table.migrate_numpy_blob_keys({'k': '=i8'}, where="v='legacy'") == 1
        assert table.fetchone("SELECT typeof(k) FROM items WHERE v='legacy'")[0] == 'blob'
        assert table.migrate_numpy_blob_keys({'k': '=i8'}, where="v='legacy'", dry_run=False) == 1
        assert table.fetchone('SELECT v FROM items WHERE k=? AND tag=?', [legacy, 'a'])[0] == 'legacy'
        table.insert_all([incoming], replace=True)
        assert table.select_all('*', order_by='tag') == [
            {'k': 10, 'tag': 'a', 'v': 'new'}, {'k': memoryview(legacy).tobytes(), 'tag': 'B', 'v': 'business'},
        ]


@pytest.mark.parametrize('dry_run', [True, False])
def test_mysqlite_numpy_blob_key_conflicts_leave_all_rows_intact(tmp_path, dry_run):
    np = pytest.importorskip('numpy')
    path = tmp_path / 'db.sqlite3'
    _legacy_numpy_database(path, [(np.int64(5), 'Z', 'safe'), (np.int64(10), 'A', 'old'), (10, 'a', 'new')])
    with MyDatabase(str(path)) as db:
        table = db['items']
        before = table.select_all('*', order_by='v')
        with pytest.raises(MyBadArgument, match='与已有主键冲突'):
            table.migrate_numpy_blob_keys({'k': '=i8'}, dry_run=dry_run)
        assert table.select_all('*', order_by='v') == before


def test_mysqlite_numpy_candidates_are_checked_against_each_other(tmp_path):
    np = pytest.importorskip('numpy')
    path = tmp_path / 'db.sqlite3'
    _legacy_numpy_database(path, [(np.float32(-0.0), 'A', 'negative'),
                                  (np.float32(0.0), 'a', 'positive')])
    with MyDatabase(str(path)) as db:
        table = db['items']
        with db.transaction():
            with pytest.raises(MyBadArgument, match='相互冲突'):
                table.migrate_numpy_blob_keys({'k': '=f4'}, dry_run=False)
            assert [row[0] for row in table.fetchall('SELECT typeof(k) FROM items')] == ['blob', 'blob']
            table.insert({'k': 1, 'tag': 'B', 'v': 'still writable'})
        assert table.count() == 3
