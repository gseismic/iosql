"""针对主键、连接配额和空表读取的审查回归。"""

import os
import sqlite3
import subprocess
import sys

import pytest

from iosql import Case, Database, Segment
from iosql.exception import BadArgument, UnsafeOperation


@pytest.mark.parametrize('type_name', ['numeric', 'decimal', 'numeric(20,2)', 'string'])
def test_segment_ignore_uses_sqlite_physical_affinity(tmp_path, type_name):
    """主键去重必须与真实 SQLite 的亲和性一致，不能丢失相邻大整数。"""
    rows = [
        {'id': 2**53, 'v': '首行'},
        {'id': 2**53 + 1, 'v': '次行'},
        {'id': 1, 'v': '首次'},
        {'id': 1.0, 'v': '重复'},
    ]
    if type_name == 'string':
        # SQLite 的 STRING 是 NUMERIC 亲和性，即使库的类型别名将其视为 text。
        rows.append({'id': '1', 'v': '字符串重复'})
    with sqlite3.connect(':memory:') as oracle:
        oracle.execute(f'CREATE TABLE data (id {type_name} PRIMARY KEY, v TEXT)')
        oracle.executemany('INSERT OR IGNORE INTO data VALUES (?, ?)',
                           [(row['id'], row['v']) for row in rows])
        expected = oracle.execute('SELECT id, v FROM data ORDER BY id').fetchall()
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': type_name, 'v': 'text'},
                         segment=Segment.rows(max_rows=2))
        table.write(rows, mode='ignore')
        table.write(rows, mode='ignore')
        assert [(row['id'], row['v']) for row in table.select(order_by='id')] == expected
        assert table.select(where=Case('id', '=', 2**53 + 1)) == [
            {'id': 2**53 + 1, 'v': '次行'}]


@pytest.mark.parametrize('type_name', ['numeric', 'decimal'])
def test_upsert_preserves_fields_for_large_numeric_keys(tmp_path, type_name):
    """局部合并使用实际存储值，避免相邻 64 位键覆盖彼此的旧字段。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': type_name, 'keep': 'text', 'v': 'text'})
        table.write([{'id': 2**53, 'keep': 'a'}, {'id': 2**53 + 1, 'keep': 'b'}])
        table.upsert_all({'id': 2**53 + 1, 'v': '更新'}, pk=['id'])
        assert table.select(order_by='id') == [
            {'id': 2**53, 'keep': 'a', 'v': None},
            {'id': 2**53 + 1, 'keep': 'b', 'v': '更新'},
        ]


@pytest.mark.parametrize('type_name', [
    'numeric', 'decimal(20,2)', 'string', 'floating point', 'varchar(80)',
    'blob', 'integer', 'double precision', 'boolean',
    "text DEFAULT 'INTEGER'", "numeric DEFAULT 'TEXT'", "blob DEFAULT 'INTEGER'",
    "varchar(20) DEFAULT 'FLOAT'", "real DEFAULT 'INTEGER'",
])
def test_key_normalization_matches_sqlite_affinity(tmp_path, type_name):
    """覆盖 SQLite 亲和性优先顺序以及数字字符串和 BLOB 的实际存储类型。"""
    values = [2**53, 2**53 + 1, '1', '1.0', '3.0e+5', '0x10', b'\x00', -3, 1.25, 'abc', None]
    with sqlite3.connect(':memory:') as oracle:
        oracle.execute(f'CREATE TABLE keys (k {type_name})')
        oracle.executemany('INSERT INTO keys VALUES (?)', [(value,) for value in values])
        expected = [row[0] for row in oracle.execute('SELECT k FROM keys')]
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': type_name})
        actual = [key[0] for key in table._normalize_pk_keys(
            [{'id': value} for value in values], ['id'], table.spec())]
    assert [(type(value), value) for value in actual] == [(type(value), value) for value in expected]


@pytest.mark.parametrize('declaration', ["TEXT DEFAULT 'INTEGER'", 'TEXT CHECK(length(id) > 0)'])
def test_column_constraints_do_not_change_key_affinity(tmp_path, declaration):
    """DEFAULT 和 CHECK 中出现的文本不能改变 SQLite 实际的 TEXT 亲和性。"""
    with sqlite3.connect(':memory:') as oracle:
        oracle.execute(f'CREATE TABLE data (id {declaration} PRIMARY KEY)')
        oracle.executemany('INSERT OR IGNORE INTO data VALUES (?)', [('01',), ('1',)])
        expected = [row[0] for row in oracle.execute('SELECT id FROM data ORDER BY id')]
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': declaration}, segment=Segment.rows(max_rows=1))
        table.write([{'id': '01'}, {'id': '1'}], mode='ignore')
        assert [row['id'] for row in table.select(order_by='id')] == expected == ['01', '1']


@pytest.mark.parametrize('segment', [False, True])
def test_physical_primary_key_type_overrides_contract_alias(tmp_path, segment):
    """物理建表类型由写入参数覆盖时，以已持久化的列类型归一后续键。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'float', 'keep': 'text'},
                         segment=Segment.rows(max_rows=10) if segment else None)
        table.write([{'id': 2**53, 'keep': 'a'}, {'id': 2**53 + 1, 'keep': 'b'}],
                    mode='ignore', column_type={'id': 'numeric'})
        path = table._physical_part_refs(lightweight=True)[0].path
        with sqlite3.connect(path) as conn:
            assert conn.execute('PRAGMA table_info(data)').fetchone()[2].lower() == 'numeric'
        if segment:
            table.write({'id': 2**53 + 1, 'keep': '覆盖'}, mode='ignore')
        else:
            table.upsert_all({'id': 2**53 + 1}, pk=['id'])
        assert table.select(order_by='id') == [
            {'id': 2**53, 'keep': 'a'}, {'id': 2**53 + 1, 'keep': 'b'},
        ]


@pytest.mark.parametrize('legacy_stats', [False, True])
def test_numeric_affinity_stats_never_prune_matching_string_keys(tmp_path, legacy_stats):
    """新旧 segment 统计及 session 专用裁剪均不能将数值字符串按文本排序。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'string', 'v': 'text'},
                         segment=Segment.rows(max_rows=1))
        table.write({'id': '01', 'v': '保留'})
        ticks = db.timeseries('ticks', time='ts', pk=['symbol', 'ts'],
                              columns={'symbol': 'text', 'ts': 'integer', 'session': 'string'},
                              session_column='session', segment=Segment.rows(max_rows=1))
        ticks.write({'symbol': 'A', 'ts': 1, 'session': '01'})
        if legacy_stats:
            # 模拟旧版本已持久化的错误文本范围，验证重开后仍保守扫描。
            for target, column in [(table, 'id'), (ticks, 'session')]:
                info = target._segment_infos()[0]
                info.stats[column] = {'min': '01', 'max': '01'}
                if column == 'session':
                    info.session_min = info.session_max = '01'
                target._upsert_segment_info(info)
        else:
            assert table.segments()[0]['stats']['id']['complete'] is False
            assert ticks.segments()[0]['session_min'] is None
        assert table.select(where=Case('id', '=', '1')) == [{'id': 1, 'v': '保留'}]
        assert ticks.query(session='1').to_list() == [{'symbol': 'A', 'ts': 1, 'session': 1}]
    if legacy_stats:
        with Database(f'sqlite://{tmp_path / "db"}') as reopened:
            assert reopened.table('t').select(where=Case('id', '=', '1')) == [{'id': 1, 'v': '保留'}]
            assert reopened.table('ticks').query(session='1').to_list() == [
                {'symbol': 'A', 'ts': 1, 'session': 1}]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_session_variable_comparison_is_not_pruned_as_a_literal(tmp_path, backend):
    """session 等于另一列时不能用该列名作为常量裁剪范围。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.timeseries('ticks', time='ts', pk=['symbol', 'ts'],
                              columns={'symbol': 'text', 'ts': 'integer',
                                       'session': 'text', 'other': 'text'},
                              session_column='session', segment=Segment.rows(max_rows=1))
        table.write({'symbol': 'A', 'ts': 1, 'session': 'a', 'other': 'a'})
        assert table.select(where=Case('session', '=', '[[other]]')) == [
            {'symbol': 'A', 'ts': 1, 'session': 'a', 'other': 'a'}]


@pytest.mark.parametrize('declaration,original,equivalent', [
    ('text COLLATE NOCASE', 'abc', 'ABC'),
    ('text COLLATE RTRIM', 'abc  ', 'abc'),
    ('text COLLATE "NOCASE"', 'a\x00x', 'A\x00y'),
])
def test_primary_key_collation_matches_sqlite(tmp_path, declaration, original, equivalent):
    """主键查询、批内去重和已存在键使用相同 SQLite 内置排序规则。"""
    with sqlite3.connect(':memory:') as oracle:
        oracle.execute(f'CREATE TABLE data (id {declaration} PRIMARY KEY)')
        oracle.executemany('INSERT OR IGNORE INTO data VALUES (?)', [(original,), (equivalent,)])
        assert oracle.execute('SELECT COUNT(*) FROM data').fetchone()[0] == 1
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        columns = {'id': declaration, 'part': 'integer', 'keep': 'text', 'v': 'text'}
        table = db.table('t', pk=['id', 'part'], columns=columns)
        table.write({'id': original, 'part': 1, 'keep': '保留'})
        table.upsert_all({'id': equivalent, 'part': 1, 'v': '更新'}, pk=['id', 'part'])
        assert table.select() == [{'id': equivalent, 'part': 1, 'keep': '保留', 'v': '更新'}]
        segments = db.table('segments', pk=['id', 'part'], columns=columns, segment=Segment.rows(max_rows=1))
        segments.write([{'id': original, 'part': 1, 'keep': '首值'},
                        {'id': equivalent, 'part': 1, 'keep': '重复'}], mode='ignore')
        segments.write({'id': equivalent, 'part': 1, 'keep': '覆盖'}, mode='ignore')
        assert segments.select() == [{'id': original, 'part': 1, 'keep': '首值', 'v': None}]


def test_nocase_preserves_sqlite_distinct_unicode_and_nul_lengths(tmp_path):
    """SQLite NOCASE 只折叠 ASCII，NUL 之后仍区分不同字节长度。"""
    keys = ['é', 'É', 'a\x00x', 'A\x00yy']
    with sqlite3.connect(':memory:') as oracle:
        oracle.execute('CREATE TABLE data (id TEXT COLLATE NOCASE PRIMARY KEY)')
        oracle.executemany('INSERT OR IGNORE INTO data VALUES (?)', [(key,) for key in keys])
        expected = {row[0] for row in oracle.execute('SELECT id FROM data')}
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'text COLLATE NOCASE'},
                         segment=Segment.rows(max_rows=2))
        table.write([{'id': key} for key in keys], mode='ignore')
        assert {row['id'] for row in table.select()} == expected


@pytest.mark.parametrize('collation,stored,query', [('NOCASE', 'a', 'A'), ('RTRIM', 'a ', 'a')])
def test_non_binary_collation_never_uses_binary_segment_stats(tmp_path, collation, stored, query):
    """非二进制排序的范围与 Python 字符串顺序不等价，必须保守扫描。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': f'text COLLATE {collation}'},
                         segment=Segment.rows(max_rows=1))
        table.write({'id': 1, 'v': stored})
        assert table.segments()[0]['stats']['v']['complete'] is False
        assert table.select(where=Case('v', '=', query)) == [{'id': 1, 'v': stored}]
        # 模拟旧版使用 Python 次序记录的范围，重开后也不能错误裁剪。
        info = table._segment_infos()[0]
        info.stats['v'] = {'min': stored, 'max': stored}
        table._upsert_segment_info(info)
    with Database(f'sqlite://{tmp_path / "db"}') as reopened:
        assert reopened.table('t').select(where=Case('v', '=', query)) == [{'id': 1, 'v': stored}]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('key', ['[[foo]]', b'\x00\xff'])
def test_primary_key_lookup_binds_literal_values(tmp_path, backend, key):
    """字段引用形态的文本和二进制主键都是字面值，包括复合主键。"""
    key_type = 'blob' if isinstance(key, bytes) else 'text'
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id', 'part'], columns={
            'id': key_type, 'part': 'integer', 'foo': 'text', 'keep': 'text', 'v': 'text',
        })
        table.write({'id': key, 'part': 1, 'foo': '其他值', 'keep': '保留'})
        table.upsert_all({'id': key, 'part': 1, 'v': '更新'}, pk=['id', 'part'])
        assert table.select() == [{'id': key, 'part': 1, 'foo': '其他值', 'keep': '保留', 'v': '更新'}]
        segments = db.table('segments', pk=['id', 'part'], columns={
            'id': key_type, 'part': 'integer', 'foo': 'text', 'keep': 'text',
        }, segment=Segment.rows(max_rows=2))
        segments.write({'id': key, 'part': 1, 'foo': '其他值', 'keep': '保留'})
        segments.write({'id': key, 'part': 1, 'foo': '其他值', 'keep': '覆盖'}, mode='ignore')
        assert segments.select() == [{'id': key, 'part': 1, 'foo': '其他值', 'keep': '保留'}]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_first_upsert_infers_schema_before_matching(tmp_path, backend):
    """首次 upsert 不查询尚不存在的物理列，并保留批内重复键的合并语义。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'])
        table.upsert_all([{'id': 1, 'keep': '保留'}, {'id': '1', 'v': '更新'}], pk=['id'])
        assert table.select() == [{'id': '1', 'keep': '保留', 'v': '更新'}]
        assert table.spec().columns['id'] == 'text'


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_first_segment_ignore_uses_inferred_key_type(tmp_path, backend):
    """首次 segment ignore 的批内去重与最终推断出的物理键类型一致。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], segment=Segment.rows(max_rows=2))
        table.write([{'id': 1, 'v': '首行'}, {'id': '1', 'v': '重复'}], mode='ignore')
        assert table.spec().columns['id'] == 'text'
        assert table.select() == [{'id': '1', 'v': '首行'}]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_ignore_rejects_missing_registered_segment_even_if_part_index_is_missing(tmp_path, backend, monkeypatch):
    """未登记的候选路径可跳过，但 manifest 已登记且丢失的段不能静默忽略。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'}, segment=Segment.rows(max_rows=2))
        table.write({'id': 1})
        ref = table._physical_part_refs(lightweight=True)[0]
        assert ref.segment_id >= 0
        os.unlink(ref.path)
        monkeypatch.setattr(table, 'table_parts', lambda: [])
        with pytest.raises(UnsafeOperation, match='分区数据文件缺失'):
            table.write({'id': 2}, mode='ignore')


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_latest_on_uninferred_empty_timeseries(tmp_path, backend):
    """未推断列的空表可查询；列已确定后仍要拒绝不存在的分组和投影。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.timeseries('ticks', time='ts', pk=['symbol', 'ts'])
        assert table.latest().to_list() == []
        assert table.asof(columns=['symbol', 'ts']).to_list() == []
        with pytest.raises(BadArgument, match='通配符'):
            table.latest(columns='*,ts').to_list()
        table.write({'symbol': 'A', 'ts': 1, 'price': 2})
        assert table.latest().to_list() == [{'symbol': 'A', 'ts': 1, 'price': 2}]
        with pytest.raises(BadArgument, match='grouping field missing'):
            table.latest(by=['missing']).to_list()
        with pytest.raises(BadArgument, match='Unknown field'):
            table.latest(columns=['missing']).to_list()
        declared = db.timeseries('declared', time='ts', pk=['symbol', 'ts'],
                                 columns={'ts': 'integer'})
        with pytest.raises(BadArgument, match='grouping field missing'):
            declared.latest().to_list()


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('fields', [['*', 'id'], ['*', '*'], ['id', '*'], [' * ', 'id']])
def test_list_projection_preserves_wildcards(tmp_path, backend, fields):
    """列表投影中的通配符必须保留全部列、重复列和排序语义。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'a,b': 'text', 'v': 'integer'})
        table.write([{'id': 1, 'a,b': '甲', 'v': 2}, {'id': 2, 'a,b': '乙', 'v': 1}])
        expected = [{'id': 2, 'a,b': '乙', 'v': 1}, {'id': 1, 'a,b': '甲', 'v': 2}]
        assert table.select(fields, order_by='v') == expected
        assert list(table.iter_select(fields, order_by='v', batch_size=1)) == expected
        assert table.query(columns=fields, order_by='v').to_list() == expected
        timeseries = db.timeseries('ticks', time='ts', pk=['symbol', 'ts'], columns={
            'symbol': 'text', 'ts': 'integer', 'v': 'integer',
        })
        timeseries.write({'symbol': 'A', 'ts': 1, 'v': 2})
        assert timeseries.latest(columns=['*', 'ts']).to_list() == [{'symbol': 'A', 'ts': 1, 'v': 2}]
        assert timeseries.latest(columns=[' * ', 'ts']).to_list() == [{'symbol': 'A', 'ts': 1, 'v': 2}]
        with pytest.raises(BadArgument, match='Unknown field'):
            table.select(['*', 'missing'])
        assert len(tuple(table.select(fields, to_dict=False)[0])) == (6 if fields == ['*', '*'] else 4)


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('target_backend', ['sqlite', 'parquet'])
def test_single_connection_stream_and_save_as_do_not_deadlock(tmp_path, backend, target_backend):
    """子进程设置硬超时，验证流消费期间可读写、关闭后可复制及校验。"""
    script = '''
import sys, time
from unittest.mock import patch
from iosql import Database
from iosql.utils.mysqlite.connection_pool import _PROCESS_CONNECTION_BUDGET as budget
backend, target_backend, base = sys.argv[1:]
with Database(f'{backend}://{base}/source', max_process_connections=1) as db:
    table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'})
    table.write([{'id': i, 'v': str(i)} for i in range(3)])
    stream = table.iter_select_batches(batch_size=1)
    assert next(stream) == [{'id': 0, 'v': '0'}]
    assert table.count_rows() == 3
    table.write({'id': 10, 'v': 'new'})
    assert [row for batch in stream for row in batch] == [
        {'id': 1, 'v': '1'}, {'id': 2, 'v': '2'}]
    # 首批返回后推进时钟，覆盖消费中的超时而不依赖 DuckDB 冷启动耗时。
    stream = table.iter_select_batches(batch_size=1, timeout_ms=60_000)
    next(stream)
    with patch.object(time, 'monotonic', return_value=time.monotonic() + 120):
        try:
            next(stream)
            raise AssertionError('未超时')
        except TimeoutError:
            pass
    stream = table.iter_select_batches(batch_size=1)
    next(stream)
    stream.close()
    assert not budget._external_reservations
    result = db.save_as(f'{target_backend}://{base}/copy', batch_size=1, verify='row-count')
    assert result.published and result.verified and result.rows_copied == 4
    with Database(f'{target_backend}://{base}/copy', max_process_connections=1) as copied:
        assert copied.table('t').count_rows() == 4
'''
    result = subprocess.run([sys.executable, '-c', script, backend, target_backend, str(tmp_path)],
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr


def test_single_connection_stream_preserves_row_type_and_pushes_limit(tmp_path, monkeypatch):
    """单连接暂存保留原生 Row；limit 只读取所需物理行，提前关闭会清理。"""
    with Database(f'sqlite://{tmp_path / "db"}', max_process_connections=1) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'})
        table.write([{'id': i, 'v': str(i)} for i in range(50)])
        source = table._part_ordered_rows
        observed = []

        def track(*args, **kwargs):
            for row in source(*args, **kwargs):
                observed.append(tuple(row))
                yield row

        monkeypatch.setattr(table, '_part_ordered_rows', track)
        stream = table.iter_select_batches(['*', 'id'], batch_size=1, limit=1, to_dict=False)
        row = next(stream)[0]
        assert isinstance(row, sqlite3.Row)
        assert row.keys() == ['id', 'v', 'id']
        assert tuple(row) == (0, '0', 0)
        assert observed == [(0, '0', 0)]
        stream.close()
        assert table.count_rows() == 50


def test_single_connection_stream_spool_failure_releases_resources(tmp_path, monkeypatch):
    """临时文件写失败时关闭文件与源游标，连接配额仍可复用。"""
    import iosql.engines.sqlite.table as sqlite_table
    from iosql.utils.mysqlite.connection_pool import _PROCESS_CONNECTION_BUDGET as budget

    with Database(f'sqlite://{tmp_path / "db"}', max_process_connections=1) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}])
        opened = []
        create_spool = sqlite_table.tempfile.TemporaryFile

        def observe_spool(*args, **kwargs):
            file = create_spool(*args, **kwargs)
            opened.append(file)
            return file

        def fail_dump(*args, **kwargs):
            raise OSError('临时文件写入失败')

        with monkeypatch.context() as patch:
            patch.setattr(sqlite_table.tempfile, 'TemporaryFile', observe_spool)
            patch.setattr(sqlite_table.pickle, 'dump', fail_dump)
            with pytest.raises(OSError, match='临时文件写入失败'):
                next(table.iter_select_batches(batch_size=1))
        assert len(opened) == 1 and opened[0].closed
        assert not budget._external_reservations
        assert table.count_rows() == 2


def test_single_connection_stream_spool_truncation_is_an_error(tmp_path, monkeypatch):
    """临时结果意外结束必须报错，不能把缺失的批次当成正常结束。"""
    import iosql.engines.sqlite.table as sqlite_table
    from iosql.utils.mysqlite.connection_pool import _PROCESS_CONNECTION_BUDGET as budget

    with Database(f'sqlite://{tmp_path / "db"}', max_process_connections=1) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}])
        load = sqlite_table.pickle.load
        calls = 0

        def truncated(file):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise EOFError('模拟截断')
            return load(file)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite_table.pickle, 'load', truncated)
            stream = table.iter_select_batches(batch_size=1)
            assert next(stream) == [{'id': 1}]
            with pytest.raises(UnsafeOperation, match='临时文件不完整或损坏'):
                next(stream)
        assert not budget._external_reservations
        assert table.count_rows() == 2


@pytest.mark.parametrize('budget_limit', [1, 2])
@pytest.mark.parametrize('method', ['iter_select', 'iter_select_batches'])
def test_closing_outer_stream_closes_retained_inner_stream(tmp_path, monkeypatch, budget_limit, method):
    """即使内部迭代器仍被引用，关闭外层也立即释放连接或临时文件。"""
    from iosql.utils.mysqlite.connection_pool import _PROCESS_CONNECTION_BUDGET as budget

    with Database(f'sqlite://{tmp_path / "db"}', max_process_connections=budget_limit) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}])
        source = table._iter_select_raw_batches
        retained = []

        def observe(*args, **kwargs):
            inner = source(*args, **kwargs)
            retained.append(inner)
            return inner

        monkeypatch.setattr(table, '_iter_select_raw_batches', observe)
        stream = getattr(table, method)(batch_size=1)
        assert next(stream)
        assert retained and retained[0].gi_frame is not None
        stream.close()
        assert retained[0].gi_frame is None
        assert not budget._external_reservations
        assert table.count_rows() == 2


def test_saturated_streams_fail_fast_then_release_budget(tmp_path):
    """多个流占满配额时连接申请快速失败，关闭流之后连接恢复可用。"""
    script = '''
import sys
from iosql import Database
from iosql.exception import BadArgument
with Database('sqlite://' + sys.argv[1], max_process_connections=2) as db:
    table = db.table('t', pk=['id'], columns={'id': 'integer'})
    table.write([{'id': 1}, {'id': 2}])
    first = table.iter_select_batches(batch_size=1)
    second = table.iter_select_batches(batch_size=1)
    next(first)
    next(second)
    try:
        table.count_rows()
        raise AssertionError('配额耗尽时必须快速失败')
    except BadArgument as exc:
        assert 'max_process_connections' in str(exc)
    first.close()
    second.close()
    assert table.count_rows() == 2
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'db')],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
