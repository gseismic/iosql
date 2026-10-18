"""PLAN-070 PostgreSQL aggregate_by_bucket 单元测试（假连接，无 live PG）。

校验生成的 SQL 形态、参数顺序（桶参数在前、where 参数在后）、
x 模式分类与返回结构；语义与 sqlite 引擎逐字段一致。
"""
import pytest



import iosql.engines.postgresql.table as pg_module
from iosql import Database, TableContract
from iosql.exception import BadArgument

pytest.importorskip("psycopg2")


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.results = []
        self.fetchone_result = None

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        rows, self.results = list(self.results), []
        return rows

    def fetchone(self):
        return self.fetchone_result

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.autocommit = True
        self.closed = False
        self.cursor_obj = FakeCursor()

    def cursor(self, name=None, withhold=False, **kwargs):
        return self.cursor_obj


@pytest.fixture
def fake_pg(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(pg_module.psycopg2, 'connect', lambda uri: conn)
    return conn


def _make_table(fake_pg, columns=None, pk=('symbol', 'ts')):
    contract = TableContract(
        primary_key=list(pk),
        columns=columns or {
            'symbol': 'text',
            'ts': 'integer',
            'price': 'float',
        },
        order_key='ts',
    )
    db = Database('postgresql://user:pass@localhost:5432/market')
    return db.table('ticks', spec=contract)


def _user_statements(cursor):
    """只保留访问目标表的语句，过滤引擎内部探测（current_schema/pg_class）。"""
    return [
        (sql_text, params)
        for sql_text, params in cursor.executed
        if 'FROM "ticks"' in str(sql_text)
    ]


def test_numeric_mode_sql_and_merge(fake_pg):
    table = _make_table(fake_pg)
    cur = fake_pg.cursor_obj
    cur.fetchone_result = (3, 100, 300, 0)
    # GROUP BY 返回 (idx, sum, count)
    cur.results = [(0, 110.0, 1), (2, 590.0, 2)]

    agg = table.aggregate_by_bucket('ts', ['price'], 4)

    stats_sql, stats_params = _user_statements(cur)[0]
    assert 'COUNT("ts")' in stats_sql
    assert 'MIN("ts")' in stats_sql
    assert stats_params == []
    agg_sql, agg_params = _user_statements(cur)[1]
    assert 'FLOOR(("ts" - %s) / %s)' in agg_sql
    assert 'GROUP BY 1' in agg_sql
    # 桶网格参数在前，无 where 参数
    assert agg_params[:2] == [100, 51]  # span=200, width=200//4+1
    assert agg['x_mode'] == 'numeric'
    assert agg['bucket_min'] == 100
    assert agg['bucket_width'] == 51
    assert agg['sums']['price'][0] == 110.0
    assert agg['counts']['price'][2] == 2
    assert agg['scanned'] == 3


def test_text_mode_uses_cast_and_digit_probe(fake_pg):
    columns = {'symbol': 'text', 'ts': 'text', 'price': 'float'}
    table = _make_table(fake_pg, columns=columns, pk=('symbol', 'ts'))
    cur = fake_pg.cursor_obj
    cur.fetchone_result = (2, '20250101', '20250110', 2, 20250101, 20250110)
    cur.results = [(0, 3.0, 2)]

    agg = table.aggregate_by_bucket('ts', ['price'], 3)

    stats_sql, _ = _user_statements(cur)[0]
    assert "~ '^-?[0-9]+$'" in stats_sql
    assert '::numeric' in stats_sql
    agg_sql, agg_params = _user_statements(cur)[1]
    assert 'CAST("ts" AS bigint)' in agg_sql
    # span=9, width=9//3+1=4，网格原点 int 化
    assert agg_params[:2] == [20250101, 4]
    assert agg['x_mode'] == 'text'


def test_text_mode_variable_width_uses_numeric_bounds(fake_pg):
    table = _make_table(fake_pg, columns={
        'symbol': 'text', 'ts': 'text', 'price': 'float',
    })
    cur = fake_pg.cursor_obj
    # SQL 的词典序边界为 10/3，数值边界为 2/10。
    cur.fetchone_result = (3, '10', '3', 3, 2, 10)
    cur.results = [(0, 2.0, 1), (1, 3.0, 1), (3, 10.0, 1)]
    agg = table.aggregate_by_bucket('ts', ['price'], 4)
    assert (agg['x_min'], agg['x_max']) == ('2', '10')
    assert sum(agg['counts']['price']) == 3
    assert _user_statements(cur)[1][1][:2] == [2, 3]


def test_text_mode_non_digit_rejected(fake_pg):
    columns = {'symbol': 'text', 'ts': 'text', 'price': 'float'}
    table = _make_table(fake_pg, columns=columns, pk=('symbol', 'ts'))
    cur = fake_pg.cursor_obj
    cur.fetchone_result = (2, '2026-01-01', '2026-01-02', 0)

    agg = table.aggregate_by_bucket('ts', ['price'], 3)
    assert agg['x_mode'] == 'non_numeric'
    assert agg['sums'] is None
    # 只执行探测，不执行聚合
    assert len(_user_statements(cur)) == 1


def test_text_mode_partial_digit_mixed(fake_pg):
    columns = {'symbol': 'text', 'ts': 'text', 'price': 'float'}
    table = _make_table(fake_pg, columns=columns, pk=('symbol', 'ts'))
    fake_pg.cursor_obj.fetchone_result = (4, '100', 'abc', 2)

    agg = table.aggregate_by_bucket('ts', ['price'], 3)
    assert agg['x_mode'] == 'mixed'
    assert agg['sums'] is None


def test_empty_table(fake_pg):
    table = _make_table(fake_pg)
    fake_pg.cursor_obj.fetchone_result = (0, None, None, 0)
    agg = table.aggregate_by_bucket('ts', ['price'], 3)
    assert agg['x_mode'] == 'empty'
    assert agg['scanned'] == 0
    assert len(_user_statements(fake_pg.cursor_obj)) == 1


def test_where_params_after_bucket_params(fake_pg):
    table = _make_table(fake_pg)
    cur = fake_pg.cursor_obj
    cur.fetchone_result = (1, 100, 100, 0)
    cur.results = [(0, 1.0, 1)]

    table.aggregate_by_bucket(
        'ts', ['price'], 3,
        where=[{'field': 'symbol', 'op': '==', 'value': 'AAPL'}],
    )

    agg_sql, agg_params = _user_statements(cur)[1]
    assert 'WHERE' in agg_sql
    assert '"symbol" = %s' in agg_sql
    assert agg_params[0:2] == [100, 1]  # span=0 → width=0//3+1=1
    assert agg_params[2:] == ['AAPL']


def test_unknown_and_text_value_columns_rejected(fake_pg):
    table = _make_table(fake_pg)
    with pytest.raises(BadArgument):
        table.aggregate_by_bucket('nope', ['price'], 3)
    with pytest.raises(BadArgument):
        table.aggregate_by_bucket('ts', ['symbol'], 3)
    with pytest.raises(BadArgument):
        table.aggregate_by_bucket('ts', ['price'], 0)

pytestmark = pytest.mark.usefixtures("fake_pg_coordination")
