"""PLAN-062 PostgreSQL 引擎 + core.query 修复回归测试。

无 live PG：全部通过 psycopg2 Composable 渲染 + 假 connection/cursor 校验
生成的 SQL、参数、提交/回滚调用；少量 live PG 语义风险在用例注释中标注。
"""
import warnings

import pytest
import psycopg2
from psycopg2 import sql

import iosql.engines.postgresql.table as pg_module
from iosql import Database
from iosql import TableContract
from iosql.engines.postgresql import Table
from iosql.exception import BadArgument, SchemaConflict
from iosql.core.query.case import Case
from iosql.core.query.case_util import unique_or_and_cases


def render_sql(query):
    """把 psycopg2 Composable/字符串渲染为可断言的文本。"""
    if query is None:
        return ''
    if isinstance(query, str):
        return query
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return '"' + query.string.replace('"', '""') + '"'
    if isinstance(query, sql.Composed):
        return ''.join(render_sql(part) for part in query.seq)
    string = getattr(query, 'string', None)
    return string if isinstance(string, str) else str(query)


def render_all(cursor):
    return [render_sql(query) for query, _ in cursor.executed]


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.description = []
        self.results = []
        self.fetchone_result = None
        self.itersize = None
        self.rowcount = 0
        self.responder = None

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if self.responder is not None:
            self.responder(self, render_sql(query), params)

    def fetchall(self):
        return list(self.results)

    def fetchmany(self, size=None):
        if size is None:
            rows, self.results = list(self.results), []
            return rows
        rows, self.results = self.results[:size], self.results[size:]
        return rows

    def fetchone(self):
        return self.fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        return False


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.autocommit = True
        self.cursor_obj = FakeCursor()
        self.commit_count = 0
        self.rollback_count = 0
        self.cursor_calls = []

    def cursor(self, name=None, withhold=False, **kwargs):
        self.cursor_calls.append({
            'name': name,
            'withhold': withhold,
            **kwargs,
        })
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pg(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(
        pg_module.psycopg2,
        'connect',
        lambda uri: conn,
    )
    return conn


def _contract(columns=None, pk=('symbol', 'ts'), partition=None,
              order_key='ts'):
    return TableContract(
        primary_key=list(pk),
        columns=columns or {
            'symbol': 'text',
            'ts': 'integer',
            'price': 'float',
        },
        partition=partition,
        order_key=order_key,
    )


def _make_table(fake_pg, table_name='ticks', columns=None, pk=('symbol', 'ts'),
                partition=None, order_key='ts'):
    contract = _contract(columns=columns, pk=pk, partition=partition,
                         order_key=order_key)
    db = Database('postgresql://user:pass@localhost:5432/market')
    return db.table(table_name, spec=contract)


def _insert_statements(cursor):
    return [
        (text, params)
        for text, params in (
            (render_sql(query), params)
            for query, params in cursor.executed
        )
        if text.lstrip().upper().startswith('INSERT')
    ]


class TestDeleteP0_7:
    @pytest.mark.parametrize('bad_where', [[], {}, '', 0, False])
    def test_falsy_non_none_where_rejected(self, fake_pg, bad_where):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()

        with pytest.raises(BadArgument):
            table.delete(bad_where)

        assert not any(
            text.lstrip().upper().startswith('DELETE')
            for text in render_all(fake_pg.cursor_obj)
        )

    def test_valid_where_parameterized_and_returns_rowcount(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.rowcount = 3

        deleted = table.delete(
            {'field': 'symbol', 'op': '==', 'value': 'AAPL'}
        )

        assert deleted == 3
        text, params = fake_pg.cursor_obj.executed[-1]
        rendered = render_sql(text)
        assert 'WHERE' in rendered
        assert '"symbol" = %s' in rendered
        assert params == ['AAPL']

    def test_none_warns_but_delete_all_is_clear_path(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.rowcount = 5

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            assert table.delete(None) == 5
        assert any(
            issubclass(item.category, DeprecationWarning) for item in caught
        )
        assert render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        ).strip() == 'DELETE FROM "ticks"'

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            assert table.delete_all() == 5
        assert not [
            item for item in caught
            if issubclass(item.category, DeprecationWarning)
        ]


class TestColumnTypeP0_8:
    @pytest.mark.parametrize('evil', [
        'text); DROP TABLE users; --',
        'text, extra TEXT',
        'int; DELETE FROM ticks',
        'BIGINT) ',
        'varchar(20)',
    ])
    def test_unknown_or_injecting_type_rejected(self, fake_pg, evil):
        with pytest.raises(BadArgument, match='不支持的 PostgreSQL 列类型'):
            _make_table(
                fake_pg,
                columns={'ts': 'integer', 'evil': evil},
                pk=['ts'],
            )

    @pytest.mark.parametrize('declared,expected', [
        ('blob', 'BYTEA'),
        ('bytes', 'BYTEA'),
        ('datetime', 'TIMESTAMP'),
        ('timestamptz', 'TIMESTAMPTZ'),
        ('jsonb', 'JSONB'),
        ('integer', 'BIGINT'),
        ('double precision', 'DOUBLE PRECISION'),
    ])
    def test_closed_type_mapping(self, fake_pg, declared, expected):
        _make_table(
            fake_pg,
            columns={'ts': 'integer', 'value': declared},
            pk=['ts'],
        )
        ddl = ' '.join(render_all(fake_pg.cursor_obj))
        assert expected in ddl

    def test_injection_never_reaches_execute(self, fake_pg):
        with pytest.raises(BadArgument):
            _make_table(
                fake_pg,
                columns={'ts': 'integer', 'x': 'text); DROP TABLE users; --'},
                pk=['ts'],
            )
        assert not any(
            'DROP TABLE users' in text
            for text in render_all(fake_pg.cursor_obj)
        )


class TestSelectFieldsP1_1:
    def test_identifier_list_quoted(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.description = [('symbol',), ('ts',)]
        fake_pg.cursor_obj.results = [('AAPL', 1)]

        table.select('symbol, ts')

        rendered = render_sql(fake_pg.cursor_obj.executed[-1][0])
        assert rendered.startswith('SELECT "symbol", "ts" FROM "ticks"')

    @pytest.mark.parametrize('bad', [
        'symbol; DROP TABLE ticks',
        'symbol, (select 1)',
        '1 as x',
        'symbol from other',
        'count(*)',
    ])
    def test_sql_syntax_rejected(self, fake_pg, bad):
        table = _make_table(fake_pg)
        with pytest.raises(BadArgument):
            table.select(bad)

    def test_explicit_quoted_identifier_allowed(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.description = [('symbol',)]
        fake_pg.cursor_obj.results = [('AAPL',)]

        table.select('"symbol"')

        assert 'SELECT "symbol" FROM "ticks"' in render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        )

    def test_star_projection_still_works(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.description = [('symbol',), ('ts',)]
        fake_pg.cursor_obj.results = [('AAPL', 1)]

        table.select('*')
        assert 'SELECT * FROM "ticks"' in render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        )

        fake_pg.cursor_obj.results = [('AAPL', 1)]
        table.select(['*'])
        assert 'SELECT * FROM "ticks"' in render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        )

    def test_count_rows_still_works(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.fetchone_result = (7,)

        assert table.count_rows() == 7
        assert render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        ) == 'SELECT count(*) FROM "ticks"'


class TestBatchKeysP1_2:
    def test_union_keys_and_null_fill(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()

        table.write([
            {'symbol': 'A', 'ts': 1, 'price': 1.0},
            {'symbol': 'B', 'ts': 2},
        ])

        inserts = _insert_statements(fake_pg.cursor_obj)
        assert len(inserts) == 1
        text, params = inserts[0]
        assert '("symbol", "ts", "price")' in text
        assert params == ['A', 1, 1.0, 'B', 2, None]

    def test_upsert_union_keys_and_null_fill(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()

        table.write([
            {'symbol': 'A', 'ts': 1, 'price': 1.0},
            {'symbol': 'B', 'ts': 2},
        ], mode='upsert')

        inserts = _insert_statements(fake_pg.cursor_obj)
        assert len(inserts) == 1
        text, params = inserts[0]
        assert 'ON CONFLICT' in text
        assert params == ['A', 1, 1.0, 'B', 2, None]

    def test_row_not_dict_rejected(self, fake_pg):
        table = _make_table(fake_pg)
        with pytest.raises(BadArgument, match='每行必须是 dict'):
            table.insert_all([(1, 2)])

    def test_insert_pages_split_by_param_budget(self, fake_pg, monkeypatch):
        monkeypatch.setattr(pg_module, '_INSERT_MAX_PARAMS', 2)
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()

        table.write([
            {'symbol': 'A', 'ts': 1, 'price': 1.0},
            {'symbol': 'B', 'ts': 2, 'price': 2.0},
            {'symbol': 'C', 'ts': 3, 'price': 3.0},
        ])

        assert len(_insert_statements(fake_pg.cursor_obj)) == 3


class TestTransactionsP1_3:
    def test_write_commits_and_restores_autocommit(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0

        result = table.write([{'symbol': 'A', 'ts': 1, 'price': 1.0}])

        assert result.rows_seen == 1
        assert fake_pg.commit_count == 1
        assert fake_pg.rollback_count == 0
        assert fake_pg.autocommit is True

    def test_failed_batch_rolls_back(self, fake_pg, monkeypatch):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        original = fake_pg.cursor_obj.execute

        def boom(query, params=None):
            if render_sql(query).lstrip().upper().startswith('INSERT'):
                raise psycopg2.OperationalError('mock failure')
            return original(query, params)

        monkeypatch.setattr(fake_pg.cursor_obj, 'execute', boom)
        with pytest.raises(psycopg2.OperationalError):
            table.write([{'symbol': 'A', 'ts': 1, 'price': 1.0}])

        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 1
        assert fake_pg.autocommit is True

    def test_late_page_failure_rolls_back_whole_batch(
        self, fake_pg, monkeypatch
    ):
        monkeypatch.setattr(pg_module, '_INSERT_MAX_PARAMS', 2)
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        original = fake_pg.cursor_obj.execute
        seen = {'inserts': 0}

        def boom(query, params=None):
            text = render_sql(query)
            if text.lstrip().upper().startswith('INSERT'):
                seen['inserts'] += 1
                if seen['inserts'] == 2:
                    raise psycopg2.OperationalError('second page failed')
            return original(query, params)

        monkeypatch.setattr(fake_pg.cursor_obj, 'execute', boom)
        with pytest.raises(psycopg2.OperationalError):
            table.write([
                {'symbol': 'A', 'ts': 1, 'price': 1.0},
                {'symbol': 'B', 'ts': 2, 'price': 2.0},
                {'symbol': 'C', 'ts': 3, 'price': 3.0},
            ])

        # 第一页已执行、第二页失败：整批回滚，无提交
        assert seen['inserts'] == 2
        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 1

    def test_commit_false_defers_until_next_commit(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0

        table.write([{'symbol': 'A', 'ts': 1, 'price': 1.0}], commit=False)
        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 0
        assert fake_pg.autocommit is False

        table.write([{'symbol': 'B', 'ts': 2, 'price': 2.0}], commit=True)
        assert fake_pg.commit_count == 1
        assert fake_pg.autocommit is True

    def test_partitioned_write_is_atomic(self, fake_pg, monkeypatch):
        table = _make_table(
            fake_pg,
            partition='duration(ts,every=1day)',
        )
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        original = fake_pg.cursor_obj.execute

        def boom(query, params=None):
            if render_sql(query).lstrip().upper().startswith('INSERT'):
                raise psycopg2.OperationalError('mock failure')
            return original(query, params)

        monkeypatch.setattr(fake_pg.cursor_obj, 'execute', boom)
        with pytest.raises(psycopg2.OperationalError):
            table.write([
                {'symbol': 'A', 'ts': 1735689600000, 'price': 1.0},
                {'symbol': 'B', 'ts': 1735776000000, 'price': 2.0},
            ])

        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 1
        assert fake_pg.autocommit is True


class TestTimestamptzBoundsP1_4:
    def test_calendar_bounds_carry_utc_offset(self, fake_pg):
        table = _make_table(
            fake_pg,
            columns={'event_time': 'timestamptz', 'name': 'text'},
            pk=('event_time', 'name'),
            order_key='event_time',
            partition='calendar(event_time,every=1day,tz=Asia/Shanghai)',
        )
        fake_pg.cursor_obj.executed.clear()

        table.write({
            'event_time': '2026-01-05 00:00:00+08:00',
            'name': 'open',
        })

        create_params = [
            params
            for query, params in fake_pg.cursor_obj.executed
            if params and len(params) == 2
            and 'PARTITION OF' in render_sql(query)
        ]
        assert create_params
        start_bound, end_bound = create_params[0]
        assert str(start_bound).endswith('+00:00')
        assert str(end_bound).endswith('+00:00')


class TestPlaceholderRewriteP2:
    def test_identifier_with_question_and_percent_safe(self, fake_pg):
        table = _make_table(
            fake_pg,
            columns={'ts': 'integer', 'pct%?': 'float'},
            pk=['ts'],
        )
        fake_pg.cursor_obj.executed.clear()

        table.select(
            ['ts'],
            where={'field': 'pct%?', 'op': '==', 'value': 1.0},
        )

        text, params = fake_pg.cursor_obj.executed[-1]
        rendered = render_sql(text)
        assert '"pct%%?" = %s' in rendered
        assert params == [1.0]

    def test_order_by_nulls_match_sqlite(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()

        table.select('*', order_by='ts')
        assert '"ts" ASC NULLS FIRST' in render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        )

        table.select('*', order_by=[('ts', 'desc')])
        assert '"ts" DESC NULLS LAST' in render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        )

    def test_no_offset_paging(self, fake_pg):
        table = _make_table(fake_pg)
        query, params = table._select_query('*', limit=5)
        assert 'OFFSET' not in query
        assert params == [5]


class TestTablePartsP2:
    def test_sorted_and_to_regclass(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.results = [
            ('ticks_p_20260102',),
            ('ticks_p_20260101',),
        ]

        assert table.table_parts() == [
            'ticks_p_20260101',
            'ticks_p_20260102',
        ]
        text, params = fake_pg.cursor_obj.executed[-1]
        assert 'to_regclass(%s)' in render_sql(text)
        assert params == ('"public"."ticks"',)

    def test_missing_table_returns_empty(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.results = []

        assert table.table_parts() == []


class TestSchemaResolutionP2:
    def test_column_types_use_current_schema(self, fake_pg):
        table = Table('postgresql://user:pass@localhost:5432/market', 'ticks')
        fake_pg.cursor_obj.fetchone_result = ('analytics',)
        fake_pg.cursor_obj.results = [('ts', 'integer')]

        assert table._column_types() == {'ts': 'integer'}

        text, params = fake_pg.cursor_obj.executed[-1]
        assert 'information_schema.columns' in render_sql(text)
        assert params == ('analytics', 'ticks')


class TestTextCalendarBoundsP2:
    def test_hour_bounds_tolerate_missing_seconds(self, fake_pg):
        table = _make_table(
            fake_pg,
            columns={'event_time': 'text', 'name': 'text'},
            pk=('event_time', 'name'),
            order_key='event_time',
            partition='calendar(event_time,every=1hour,tz=UTC)',
        )
        fake_pg.cursor_obj.executed.clear()

        table.write({'event_time': '2026-01-05 09:15', 'name': 'open'})

        create_params = [
            params
            for query, params in fake_pg.cursor_obj.executed
            if params and len(params) == 2
            and 'PARTITION OF' in render_sql(query)
        ]
        assert create_params
        assert create_params[0] == (
            '2026-01-05 09',
            '2026-01-05 10',
        )


class TestPartitionBoundsChecksP2:
    def _partitioned_table(self, fake_pg):
        return _make_table(fake_pg, partition='duration(ts,every=1day)')

    def test_existing_same_bounds_skips_create(self, fake_pg):
        table = self._partitioned_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.fetchone_result = (
            "FOR VALUES FROM ('1735689600000') TO ('1735776000000')",
        )

        table.write({'symbol': 'A', 'ts': 1735689600000, 'price': 1.0})

        assert not [
            text for text in render_all(fake_pg.cursor_obj)
            if 'PARTITION OF' in text
        ]

    def test_existing_different_bounds_surfaced(self, fake_pg):
        table = self._partitioned_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.fetchone_result = (
            "FOR VALUES FROM ('1') TO ('2')",
        )

        with pytest.raises(BadArgument, match='边界与期望不一致'):
            table.write({'symbol': 'A', 'ts': 1735689600000, 'price': 1.0})

    @pytest.mark.parametrize('error, match', [
        (psycopg2.errors.DuplicateTable, '并发创建冲突'),
        (psycopg2.errors.UniqueViolation, '并发创建冲突'),
        (psycopg2.errors.InvalidObjectDefinition, '边界冲突'),
    ])
    def test_partition_create_errors_surfaced_and_rolled_back(
        self, fake_pg, monkeypatch, error, match
    ):
        table = self._partitioned_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        fake_pg.cursor_obj.fetchone_result = None
        original = fake_pg.cursor_obj.execute

        def boom(query, params=None):
            if 'PARTITION OF' in render_sql(query):
                raise error('mock failure')
            return original(query, params)

        monkeypatch.setattr(fake_pg.cursor_obj, 'execute', boom)
        with pytest.raises(BadArgument, match=match):
            table.write({'symbol': 'A', 'ts': 1735689600000, 'price': 1.0})

        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 1


class TestRenameP2:
    def test_database_overwrite_forwards_flag_and_updates_cache(self, fake_pg, monkeypatch):
        db = Database('postgresql://user:pass@localhost:5432/market')
        source = db.table('ticks')
        replaced = db.table('target')
        monkeypatch.setattr(db, 'table_names', lambda: ['ticks', 'target'])
        monkeypatch.setattr(
            source, '_relation_exists', lambda name: name in {'ticks', 'target'},
        )
        monkeypatch.setattr(
            source, '_native_partition_child_renames', lambda old, new: [],
        )

        assert db.rename_table('ticks', 'target', overwrite=True) is source
        assert db.table('target') is source
        assert db.table('ticks') is not source
        assert replaced is not source
        assert 'ALTER TABLE "ticks" RENAME TO "target"' in render_all(fake_pg.cursor_obj)
        db.close()

    def test_failure_rolls_back_and_keeps_old_name(self, fake_pg, monkeypatch):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        monkeypatch.setattr(
            table, '_relation_exists', lambda name: name == 'ticks'
        )
        monkeypatch.setattr(
            table, '_native_partition_child_renames', lambda old, new: []
        )
        original = fake_pg.cursor_obj.execute

        def boom(query, params=None):
            if render_sql(query).lstrip().upper().startswith('ALTER TABLE'):
                raise psycopg2.OperationalError('mock rename failure')
            return original(query, params)

        monkeypatch.setattr(fake_pg.cursor_obj, 'execute', boom)
        with pytest.raises(psycopg2.OperationalError):
            table.rename('new_ticks')

        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 1
        assert table.table_name == 'ticks'

    def test_overwrite_uses_trash_and_commit(self, fake_pg, monkeypatch):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        monkeypatch.setattr(
            table, '_relation_exists', lambda name: name in {'ticks', 'target'}
        )
        monkeypatch.setattr(
            table, '_native_partition_child_renames', lambda old, new: []
        )

        table.rename('target', overwrite=True)

        texts = render_all(fake_pg.cursor_obj)
        assert any(
            text.startswith('ALTER TABLE "target" RENAME TO "target__iosql_old_')
            for text in texts
        )
        assert 'ALTER TABLE "ticks" RENAME TO "target"' in texts
        assert any(
            text.startswith('DROP TABLE "target__iosql_old_')
            for text in texts
        )
        assert fake_pg.commit_count == 1
        assert fake_pg.rollback_count == 0
        assert table.table_name == 'target'

    def test_non_overwrite_conflict_raises_before_changes(
        self, fake_pg, monkeypatch
    ):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0
        monkeypatch.setattr(table, '_relation_exists', lambda name: True)
        monkeypatch.setattr(
            table, '_native_partition_child_renames', lambda old, new: []
        )

        with pytest.raises(BadArgument, match='already exists'):
            table.rename('target')

        assert fake_pg.commit_count == 0
        assert fake_pg.rollback_count == 1
        assert not any(
            text.lstrip().upper().startswith(('ALTER', 'DROP'))
            for text in render_all(fake_pg.cursor_obj)
        )


class TestContractDriftP2:
    def _responder(self, relation_exists=True, columns=('symbol', 'ts', 'price'),
                   partitioned=False):
        def responder(cursor, text, params):
            lower = text.lower()
            if 'current_schema' in lower:
                cursor.fetchone_result = ('public',)
            elif 'information_schema.columns' in lower:
                cursor.results = [(name,) for name in columns]
            elif 'pg_partitioned_table' in lower:
                cursor.fetchone_result = (1,) if partitioned else None
            elif 'pg_class' in lower:
                cursor.fetchone_result = (1,) if relation_exists else None
        return responder

    def test_missing_declared_columns_raise_schema_conflict(self, fake_pg):
        fake_pg.cursor_obj.responder = self._responder(
            columns=('symbol', 'ts'),
        )
        with pytest.raises(SchemaConflict, match='缺少契约列'):
            _make_table(fake_pg)

    def test_partition_declared_but_table_not_partitioned(self, fake_pg):
        fake_pg.cursor_obj.responder = self._responder(
            partitioned=False,
        )
        with pytest.raises(SchemaConflict, match='未启用 native 分区'):
            _make_table(fake_pg, partition='duration(ts,every=1day)')

    def test_matching_existing_table_skips_ddl(self, fake_pg):
        fake_pg.cursor_obj.responder = self._responder(
            partitioned=True,
        )
        fake_pg.cursor_obj.executed.clear()

        _make_table(fake_pg, partition='duration(ts,every=1day)')

        assert not [
            text for text in render_all(fake_pg.cursor_obj)
            if 'CREATE TABLE' in text
        ]


class TestApiParityP2:
    def test_select_rejects_unknown_kwargs(self, fake_pg):
        table = _make_table(fake_pg)
        with pytest.raises(BadArgument, match='未知参数'):
            table.select('*', symbol='AAPL')
        with pytest.raises(BadArgument, match='未知参数'):
            list(table.iter_select_batches('*', offset=5))

    def test_upsert_where_keyword_only_deprecated(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # 第三位置参数现在是 partition（与 SQLite 对齐），不再是 where
            table.upsert_all(
                [{'symbol': 'A', 'ts': 1, 'price': 2.0}],
                ['symbol', 'ts'],
                None,
            )
        assert not [
            item for item in caught
            if issubclass(item.category, DeprecationWarning)
        ]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            table.upsert_all(
                [{'symbol': 'A', 'ts': 1, 'price': 2.0}],
                ['symbol', 'ts'],
                where={'field': 'symbol', 'op': '==', 'value': 'A'},
            )
        assert any(
            issubclass(item.category, DeprecationWarning) for item in caught
        )

    def test_iter_select_batches_no_withhold_and_commits(self, fake_pg):
        table = _make_table(fake_pg)
        fake_pg.cursor_obj.executed.clear()
        fake_pg.cursor_obj.description = [('symbol',), ('ts',)]
        fake_pg.cursor_obj.results = [('A', 1), ('B', 2)]
        fake_pg.commit_count = 0
        fake_pg.rollback_count = 0

        batches = list(table.iter_select_batches('*', batch_size=1))

        assert [len(batch) for batch in batches] == [1, 1]
        named_calls = [
            call for call in fake_pg.cursor_calls if call['name']
        ]
        assert named_calls
        assert named_calls[-1]['withhold'] is False
        assert fake_pg.commit_count == 1
        assert fake_pg.autocommit is True

    def test_named_cursor_description_arrives_after_first_fetch(self, fake_pg):
        table = _make_table(fake_pg)

        class DeferredDescriptionCursor(FakeCursor):
            def __init__(self):
                self._description_ready = False
                super().__init__()
                self.description = [('symbol',), ('ts',)]

            @property
            def description(self):
                return self._description if self._description_ready else None

            @description.setter
            def description(self, value):
                self._description = value

            def fetchmany(self, size=None):
                self._description_ready = True
                return super().fetchmany(size)

        fake_pg.cursor_obj = DeferredDescriptionCursor()
        fake_pg.cursor_obj.results = [('A', 1), ('B', 2)]
        assert list(table.iter_rows(batch_size=1)) == [
            {'symbol': 'A', 'ts': 1}, {'symbol': 'B', 'ts': 2},
        ]

    def test_index_name_truncated_and_hash_unique(self, fake_pg):
        table = _make_table(fake_pg, table_name='t' * 60)

        first = table._index_name('x' * 30)
        second = table._index_name('y' * 30)

        assert len(first.encode('utf-8')) <= 63
        assert first != second

    def test_create_index_exist_ok_toggle(self, fake_pg):
        table = _make_table(fake_pg)

        fake_pg.cursor_obj.executed.clear()
        table.create_index('symbol', exist_ok=False)
        assert render_sql(fake_pg.cursor_obj.executed[-1][0]) == (
            'CREATE INDEX "ticks_symbol_idx" ON "ticks" ("symbol" ASC)'
        )

        table.create_index('symbol', exist_ok=True)
        assert 'IF NOT EXISTS' in render_sql(
            fake_pg.cursor_obj.executed[-1][0]
        )

    def test_file_partition_mode_still_rejected(self, fake_pg):
        with pytest.raises(BadArgument, match='file partition mode'):
            Table(
                'postgresql://user:pass@localhost:5432/market',
                'ticks',
                partition_mode=Table.PARTITION_MODE_FILE,
            )


class TestCoreQueryP2:
    def test_case_eq_returns_not_implemented_and_hashable(self):
        case = Case('ts', '==', 1)

        assert (case == 'not-a-case') is False
        assert case != 3
        assert hash(case) == hash(Case('ts', '==', 1))
        assert len({case, Case('ts', '==', 1), Case('ts', '==', 2)}) == 2

    def test_case_from_dict_missing_keys_typed_error(self):
        with pytest.raises(BadArgument, match='缺少键'):
            Case.from_dict({'field': 'ts'})
        with pytest.raises(BadArgument, match='dict'):
            Case.from_dict('ts')

    def test_unique_or_and_cases_superset_order_independent(self):
        a = Case('a', '==', 1)
        b = Case('b', '==', 2)

        assert unique_or_and_cases([[a, b], [a]]) == [[a]]
        assert unique_or_and_cases([[a], [a, b]]) == [[a]]
        assert unique_or_and_cases([[a], [a], [a, b]]) == [[a]]
