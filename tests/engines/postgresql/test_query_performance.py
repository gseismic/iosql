"""PLAN-078：真实数据库证明时序语义、计划、索引和字段增量更新。"""

import json
import os
import time
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
import pytest

from iosql import Database, Partition
from iosql.core.query import Case
from iosql.engines.postgresql import Table
from iosql.exception import BadArgument, SchemaConflict


@pytest.fixture
def pg_factory():
    """每例使用随机表名，仅清理本例创建的对象，支持任意专用测试实例。"""
    dsn = os.environ.get('POSTGRES_DSN')
    if not dsn:
        pytest.skip('POSTGRES_DSN 未配置')
    db = Database(dsn)
    names = []

    def create(**kwargs):
        name = 'iosql_078_' + uuid.uuid4().hex[:16]
        names.append(name)
        return db.table(name, **kwargs)
    try:
        yield create
    finally:
        db.close()
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            for name in names:
                cur.execute(sql.SQL('DROP TABLE IF EXISTS {} CASCADE').format(sql.Identifier(name)))


@pytest.fixture
def pair(pg_factory, tmp_path):
    """同一输入用于 SQLite/PG 结果对照，包括跨日、session 与多维分组。"""
    kwargs = dict(pk=['symbol', 'ts'],
                  columns={'symbol': 'text', 'ts': 'integer', 'value': 'float',
                           'venue': 'text', 'session': 'text'},
                  partition=Partition.duration('ts', every='1day'),
                  symbol_column='symbol', session_column='session')
    rows = [{'symbol': symbol, 'ts': ts, 'value': value, 'venue': venue, 'session': session}
            for symbol, ts, value, venue, session in [
                ('A', 0, None, 'X', 'night'), ('B', 1, 2., 'X', 'night'),
                ('A', 2, 3., 'Y', 'day'), ('B', 86400000, 4., 'Y', 'day'),
                ('A', 86400001, 5., 'X', 'day')]]
    pg = pg_factory(**kwargs)
    with Database(f'sqlite://{tmp_path}/compare.iosql') as db:
        lite = db.table('ticks', **kwargs)
        pg.write(rows)
        lite.write(rows)
        yield pg, lite


@pytest.mark.parametrize('kwargs', [
    {'start': 1, 'end': 2}, {'symbols': ['B', 'A'], 'start': 0, 'end': 86400000},
    {'date': '1970-01-01'}, {'session': 'night'}, {'symbols': []},
    {'where': 'value >= 3 OR value < 3', 'symbols': 'A'},
    {'where': Case('value', '>=', 3), 'session': ['day']},
    {'where': [[{'field': 'venue', 'op': '=', 'value': 'X'}],
               [{'field': 'value', 'op': '=', 'value': 3}]], 'end': 86400000},
    {'columns': ['symbol', 'value'], 'limit': 2}, {'limit': 0},
])
def test_query_semantics_match_sqlite(pair, kwargs):
    pg, lite = pair
    expected = lite.query(order_by='ts', **kwargs).to_list()
    q = pg.query(order_by='ts', **kwargs)
    assert q.to_list() == expected
    assert list(q.iter_rows(batch_size=2)) == expected
    assert [row for batch in q.iter_batches(batch_size=2) for row in batch] == expected


@pytest.mark.parametrize('kwargs', [
    {}, {'symbols': ['B', 'A']}, {'symbols': ['B', 'A', 'B']}, {'symbols': []},
    {'symbols': 'A', 'as_of': 2, 'columns': ['value']},
    {'as_of': 86400000, 'by': ['symbol', 'venue']},
    {'symbols': ['A', 'B'], 'by': ['symbol', 'venue'], 'session': 'day'},
    {'by': ['venue'], 'symbols': ['A', 'B'], 'where': 'value >= 3'},
])
def test_latest_semantics_match_sqlite(pair, kwargs):
    pg, lite = pair
    expected = lite.latest(**kwargs).to_list()
    assert pg.latest(**kwargs).to_list() == expected
    assert pg.asof(**kwargs).to_list() == expected
    assert list(pg.latest(**kwargs).iter_rows(batch_size=1)) == expected


def test_latest_tie_projection_and_real_plan(pg_factory):
    """同时间记录按剩余主键升序决胜；计划包含 Unique，解释真实分组 SQL。"""
    t = pg_factory(pk=['symbol', 'ts', 'venue'], time='ts', symbol_column='symbol',
                   columns={'symbol': 'text', 'ts': 'integer', 'venue': 'text', 'x,y': 'float'})
    t.write([{'symbol': 'A', 'ts': 1, 'venue': v, 'x,y': float(i)}
             for i, v in enumerate(['Z', 'A'])])
    assert t.latest(columns=['x,y']).to_list() == [{'x,y': 1.0}]
    assert 'Unique' in json.dumps(t.latest().explain().engine_plan)
    assert t.latest(symbols='A').one()['venue'] == 'A'
    assert t.latest(symbols='missing').one() is None


def test_explain_pruning_metrics_and_redaction(pair):
    """默认 EXPLAIN 不执行，脱敏不保留业务常量；ANALYZE 提供实际指标。"""
    pg, _ = pair
    q = pg.query(symbols='A', start=0, end=2)
    plan = q.explain()
    assert plan.execution_time_ms is None
    assert len(plan.candidate_partitions) == 2
    assert len(plan.scanned_partitions) == 1
    assert len(plan.skipped_partitions) == 1
    assert 'Filter' not in json.dumps(plan.to_dict())
    raw = q.explain(raw_plan=True)
    assert "'A'" in json.dumps(raw.engine_plan)
    actual = q.explain(analyze=True)
    assert actual.execution_time_ms >= 0
    assert actual.planning_time_ms >= 0
    assert 'Actual Rows' in json.dumps(actual.engine_plan)
    assert 'Shared Hit Blocks' in json.dumps(actual.engine_plan)
    assert actual.scanned_partitions == plan.scanned_partitions


def test_explain_does_not_evaluate_filter(pair):
    """有错的除数表达式仅在执行时出错，证明默认计划确实不运行查询。"""
    pg, _ = pair
    spec = pg.query().spec()
    query = f'SELECT value / (ts-ts) FROM "{pg.table_name}"'
    assert pg._explain_query(spec, query, []).execution_time_ms is None
    with pytest.raises(psycopg2.errors.DivisionByZero):
        pg._explain_query(spec, query, [], analyze=True)


@pytest.mark.parametrize('kind,value,day', [
    ('text', '2026-01-06', '2026-01-06'),
    ('text', '20260106', '2026-01-06'),
    ('date', '2026-01-06', '2026-01-06'),
    ('timestamp', '2026-01-06 10:00:00', '2026-01-06'),
    ('timestamptz', '2026-01-06 10:00:00+08:00', '2026-01-06'),
])
def test_calendar_date_queries(pg_factory, kind, value, day):
    """生成直接的原生日期/时间谓词，整日查询不会退化为午夜等值。"""
    t = pg_factory(pk=['symbol', 'date'], symbol_column='symbol',
                   columns={'symbol': 'text', 'date': kind, 'value': 'float'},
                   partition=Partition.calendar('date', every='1day', timezone='Asia/Shanghai'))
    t.write([{'symbol': 'A', 'date': value, 'value': 1.}])
    assert t.query(date=day).to_list()[0]['value'] == 1.
    assert t.latest(as_of='2026-01-06T23:59:00+08:00').one()['value'] == 1.


def test_compound_indexes_inherit_and_primary_is_reused(pair):
    """父表复合索引覆盖现有/未来分区；等价主键不重复创建也不误删。"""
    t, _ = pair
    before = t._index_definitions()
    t.create_index(columns=['symbol', 'ts'])
    assert t._index_definitions() == before
    t.drop_index(columns=['symbol', 'ts'])
    assert t._index_definitions() == before
    t.create_index(columns=['venue', 'ts'])
    t.create_index(columns=['venue', 'ts'])
    t.write([{'symbol': 'A', 'ts': 172800000, 'value': 9., 'venue': 'Y', 'session': 'day'}])
    with t.connect().cursor() as cur:
        cur.execute('SELECT count(*) FROM pg_inherits WHERE inhparent=to_regclass(%s)',
                    (t._index_name(['venue', 'ts']),))
        assert cur.fetchone()[0] == 3
    t.drop_index(columns=['venue', 'ts'])
    t.drop_index(columns=['venue', 'ts'])
    assert len(t._index_definitions()) == len(before)


def test_index_ownership_and_collision_are_checked(pair):
    """用户同名对象不能被覆盖或删除，方向不同也不能静默忽略。"""
    t, _ = pair
    name = t._index_name('venue')
    with t.connect().cursor() as cur:
        cur.execute(sql.SQL('CREATE INDEX {} ON {} (value)').format(
            sql.Identifier(name), sql.Identifier(t.table_name)))
    with pytest.raises(SchemaConflict):
        t.create_index('venue')
    with pytest.raises(SchemaConflict):
        t.drop_index('venue')
    t.create_index(columns=['value', 'ts'], order='desc')
    with pytest.raises(SchemaConflict):
        t.create_index(columns=['value', 'ts'], order='asc')


def test_upsert_preserves_fields_nulls_duplicates_and_unrelated_rows(pair, monkeypatch):
    """只更新输入字段，合并重复主键，禁止调用原来的全表 select。"""
    t, _ = pair
    monkeypatch.setattr(t, 'select', lambda *a, **k: pytest.fail('不允许全表读取'))
    t.upsert_all([{'symbol': 'A', 'ts': 2, 'value': 8.},
                  {'symbol': 'A', 'ts': 2, 'venue': None},
                  {'symbol': 'B', 'ts': 1, 'session': 'new'},
                  {'symbol': 'C', 'ts': 3, 'value': 6.}], pk=['symbol', 'ts'])
    assert t.query(symbols='A', end=2).to_list()[-1] == {
        'symbol': 'A', 'ts': 2, 'value': 8., 'venue': None, 'session': 'day'}
    assert t.query(symbols='B', end=1).one()['value'] == 2.
    assert t.query(symbols='C').one()['venue'] is None
    assert t.query(symbols='A', start=86400001).one()['value'] == 5.


def test_stream_close_timeout_and_explicit_transaction(pair):
    """提前关闭释放读取事务，超时回滚，显式事务内流式读取保持未提交写入。"""
    t, _ = pair
    stream = t.query(order_by='ts').iter_rows(batch_size=1)
    assert next(stream)['ts'] == 0
    stream.close()
    assert t.connect().autocommit
    assert t.connect().get_transaction_status() == TRANSACTION_STATUS_IDLE
    stream = t.query().iter_batches(batch_size=1, timeout_ms=10)
    next(stream)
    time.sleep(.03)
    with pytest.raises(TimeoutError):
        next(stream)
    assert t.connect().autocommit
    t.write({'symbol': 'C', 'ts': 3, 'value': 9.}, commit=False)
    stream = t.query(symbols='C').iter_batches(batch_size=1)
    assert next(stream)[0]['value'] == 9.
    stream.close()
    assert t._in_explicit_tx
    t.close()
    assert t.query(symbols='C').to_list() == []


@pytest.mark.parametrize('drift', ['type', 'pk', 'partition_key', 'strategy', 'unpartitioned'])
def test_contract_binding_rejects_drift(pg_factory, drift):
    """重绑校验真实类型、主键顺序与分区方式，失败恢复原内存契约。"""
    t = pg_factory(pk=['symbol', 'ts', 'value'] if drift == 'partition_key' else ['symbol', 'ts'],
                   columns={'symbol': 'text', 'ts': 'integer', 'value': 'float'},
                   partition=Partition.duration('ts', every='1day'))
    original = t.spec()
    data = original.to_dict()
    if drift == 'type':
        data['columns']['value'] = 'text'
    elif drift == 'pk':
        data['primary_key'] = ['ts', 'symbol']
    elif drift == 'unpartitioned':
        data['partition'] = None
    elif drift == 'partition_key':
        data['partition'] = {
            'kind': 'range', 'column': 'value', 'width': 100}
        data['primary_key'] = ['symbol', 'ts', 'value']
    else:
        with t.connect().cursor() as cur:
            cur.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(t.table_name)))
            cur.execute(sql.SQL('CREATE TABLE {} (symbol text, ts bigint, value double precision, '
                                'PRIMARY KEY(symbol, ts)) PARTITION BY HASH(ts)').format(sql.Identifier(t.table_name)))
    with pytest.raises((SchemaConflict, BadArgument)):
        t.use_contract(data)
    assert t.spec() == original


def test_bad_partition_primary_key_rejected_before_ddl():
    """无需连接即可拒绝 PG 无法保证跨分区唯一性的主键。"""
    t = Table('postgresql://user:pass@unreachable:5432/unreachable', 'bad')
    with pytest.raises(BadArgument, match='主键必须包含分区列'):
        t.create_table({'symbol': 'text', 'ts': 'integer'}, ['symbol'], Partition.duration('ts', every='1day'))


@pytest.mark.parametrize('kind,every,value,start,end', [
    ('date', '1day', '2026-01-06', '2026-01-06', '2026-01-06'),
    ('timestamp', '1hour', '2026-01-06 10:30:00', '2026-01-06 10:00:00', '2026-01-06 11:00:00'),
    ('timestamptz', '1hour', '2026-01-06 10:30:00-05:00', '2026-01-06 10:00:00-05:00', '2026-01-06 11:00:00-05:00'),
    ('text', '1day', '20260106', '2026-01-06', '2026-01-06'),
])
def test_negative_timezone_calendar_bounds(pg_factory, kind, every, value, start, end):
    """负时区不把裸日期移到前一天；原生小时分区写入与谓词处于同一时钟。"""
    t = pg_factory(pk=['symbol', 'date'], symbol_column='symbol',
                   columns={'symbol': 'text', 'date': kind},
                   partition=Partition.calendar('date', every=every, timezone='America/New_York'))
    t.write({'symbol': 'A', 'date': value})
    assert len(t.query(start=start, end=end).to_list()) == 1
    assert len(t.query(date='2026-01-06').to_list()) == 1


def test_indexed_latest_uses_seek_and_matches_scan(pair, monkeypatch):
    """目录有 symbol/time 前缀索引时使用 LATERAL LIMIT 1，结果与 DISTINCT ON 一致。"""
    pg, _ = pair
    with monkeypatch.context() as patch:
        patch.setattr(pg, '_has_latest_seek_index', lambda *args: False)
        expected = pg.latest(symbols=['B', 'A', 'B'], as_of=86400000).to_list()
    query = pg.latest(symbols=['B', 'A', 'B'], as_of=86400000)
    assert query.to_list() == expected
    raw = query.explain(raw_plan=True)
    assert 'Nested Loop' in json.dumps(raw.engine_plan)
    assert 'Limit' in json.dumps(raw.engine_plan)


def test_upsert_failure_rolls_back_entire_batch(pair):
    """后一字段组写入失败时，前面的合法增量更新也不能留下半批状态。"""
    t, _ = pair
    with pytest.raises(psycopg2.Error):
        t.upsert_all([{'symbol': 'A', 'ts': 2, 'value': 99.},
                      {'symbol': 'B', 'ts': 1, 'venue': 'Y', 'unknown': 1}], pk=['symbol', 'ts'])
    assert t.query(symbols='A', end=2).to_list()[-1]['value'] == 3.


def test_server_statement_timeout_preserves_original_error(pair):
    """服务端取消时 CLOSE 的失败不能覆盖超时，事务仍须释放。"""
    t, _ = pair
    with pytest.raises(TimeoutError):
        list(t._iter_query_batches('SELECT pg_sleep(1)', [], timeout_ms=20))
    assert t.connect().autocommit
    assert t.connect().get_transaction_status() == TRANSACTION_STATUS_IDLE


@pytest.mark.parametrize('every,values,selected_day', [
    ('1month', ['2026-01-05', '2026-01-06'], '2026-01-06'),
    ('1month', ['20260105', '20260106'], '2026-01-06'),
    ('1day', ['2026-01-05 10:30:00', '2026-01-06 10:30:00'], '2026-01-06'),
    ('1hour', ['202601051030', '202601061030'], '2026-01-06'),
])
def test_calendar_text_query_precision_independent_of_partition(pg_factory, every, values, selected_day):
    """按月分区仍按日筛选，datetime 文本的 date 查询涵盖整日而非午夜。"""
    t = pg_factory(pk=['symbol', 'date'], symbol_column='symbol',
                   columns={'symbol': 'text', 'date': 'text'},
                   partition=Partition.calendar('date', every=every, timezone='UTC'))
    t.write([{'symbol': 'A', 'date': value} for value in values])
    assert t.query(date=selected_day).one()['date'] == values[1]


def test_index_management_keeps_explicit_transaction(pair):
    """索引 DDL 参与已有显式事务，不应提交同连接尚未提交的行情。"""
    t, _ = pair
    t.write({'symbol': 'C', 'ts': 3, 'value': 9.}, commit=False)
    t.create_index(columns=['venue', 'ts'])
    assert t._in_explicit_tx
    t.drop_index(columns=['venue', 'ts'])
    assert t._in_explicit_tx
    t.close()
    assert t.query(symbols='C').to_list() == []


def test_zero_column_physical_table_raises_typed_conflict(pg_factory):
    """空列物理表也必须在绑定时给出 SchemaConflict，不能泄露 KeyError。"""
    t = pg_factory(pk=['id'], columns={'id': 'integer'})
    with t.connect().cursor() as cur:
        cur.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(t.table_name)))
        cur.execute(sql.SQL('CREATE TABLE {} ()').format(sql.Identifier(t.table_name)))
    with pytest.raises(SchemaConflict, match='缺少契约列'):
        t.use_contract(t.spec())


def test_nullable_ordering_matches_sqlite(pair):
    """实测 NULL 升序在前、降序在后；投影不包含排序列也保持正确顺序。"""
    pg, lite = pair
    for direction in ('asc', 'desc'):
        kwargs = dict(columns=['symbol', 'ts'], order_by=f'value {direction}')
        assert pg.query(**kwargs).to_list() == lite.query(**kwargs).to_list()


def test_numeric_symbols_bound_in_values(pg_factory):
    """VALUES 显式类型允许数值 symbol 使用绑定参数；两种 latest SQL 均有效。"""
    t = pg_factory(pk=['symbol', 'ts'], time='ts', symbol_column='symbol',
                   columns={'symbol': 'integer', 'ts': 'integer', 'venue': 'text'})
    t.write([{'symbol': n, 'ts': n, 'venue': 'X'} for n in (1, 2)])
    assert [row['symbol'] for row in t.latest(symbols=['2', '1', '2']).to_list()] == [2, 1, 2]
    assert [row['symbol'] for row in t.latest(symbols=['2', '1'], by=['symbol', 'venue']).to_list()] == [2, 1]
