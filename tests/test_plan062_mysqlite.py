"""PLAN-062 回归测试：utils/mysqlite 与 utils 的 review 修复项。

覆盖：
- P0-9 insert_all 表达式默认值
- M1-1 单行 ensure_column None 类型
- M1-2 drop_table 缓存失效 / Table.invalidate_cache
- M1-3 Database(wal=False)
- M1-4 close/return 竞态
- M1-5 嵌套 checkout 重入
- M1-6 insert(commit=False) / auto_commit=False 丢写
- M1-7 进程预算取最大声明上限
- M1-8 timeout -> busy_timeout
- P2 批量：pragma 应用到空闲连接、numpy/numbers 类型推断、建议列类型保留声明列、
  索引 PRAGMA 化、:memory: 拒绝、create_table 原子性、fork 安全、
  sql_literal(bytes)/getsubsql_insert/getsql_create_table 校验、
  schema :memory: 解析、is_pool_healthy
"""

import os
import re
import threading

import pytest

from iosql.utils.mysqlite import Database
from iosql.utils.mysqlite.connection_pool import (
    DatabaseConnectionPool,
    get_process_connection_budget_status,
)
from iosql.utils.mysqlite.exception import BadArgument
from iosql.utils.mysqlite.sql import (
    getsql_create_table,
    getsubsql_insert,
    group_rows_by_keys,
    sql_literal,
    suggest_value_type,
)
from iosql.utils.mysqlite.database import DatabaseError
from iosql.utils.column import (
    sqlite_type_of_value,
    suggest_column_types,
)
from iosql.utils.schema import parse_db_schema


# ---------------------------------------------------------------- P0-9

def test_insert_all_expression_defaults_evaluated_by_sqlite(tmp_path):
    db = Database(str(tmp_path / 'expr_default.db'), verbose=0)
    try:
        db.execute(
            'CREATE TABLE t ('
            'id INTEGER PRIMARY KEY, '
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "name TEXT DEFAULT '2')"
        )
        db['t'].insert({'id': 1})
        db['t'].insert_all([{'id': 2}, {'id': 3, 'name': 'x'}])

        rows = {row['id']: row for row in db['t'].select_all('*')}
        pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
        for row_id in (1, 2, 3):
            assert rows[row_id]['created_at'] != 'CURRENT_TIMESTAMP'
            assert rows[row_id]['updated_at'] != "(datetime('now'))"
            assert pattern.match(rows[row_id]['created_at']), rows[row_id]
            assert pattern.match(rows[row_id]['updated_at']), rows[row_id]
        # 字面量默认值仍然生效；显式值不被覆盖
        assert rows[2]['name'] == '2'
        assert rows[3]['name'] == 'x'
    finally:
        db.close()


def test_insert_all_literal_defaults_still_work(tmp_path):
    db = Database(str(tmp_path / 'literal_default.db'), verbose=0)
    try:
        db['t'].create_table([
            {'name': 'id', 'type': 'integer'},
            {'name': 'name', 'type': 'text', 'default': '2'},
            {'name': 'value', 'type': 'float', 'default': 666},
        ], pk=['id'])
        db['t'].insert_all([{'id': 1}, {'id': 2, 'name': 'own'}])
        rows = {row['id']: row for row in db['t'].select_all('*')}
        assert rows[1]['name'] == '2'
        assert rows[1]['value'] == 666
        assert rows[2]['name'] == 'own'
    finally:
        db.close()


def test_group_rows_by_keys_groups_heterogeneous_rows():
    groups = group_rows_by_keys([
        {'id': 1, 'name': 'a'},
        {'id': 2},
        {'name': 'b', 'id': 3},
    ])
    # 按连续字段集合分组才能保留异构输入的写入顺序和表达式默认值。
    assert groups == [(['id', 'name'], [[1, 'a']]), (['id'], [[2]]), (['name', 'id'], [['b', 3]])]


# ---------------------------------------------------------------- M1-1

def test_single_row_ensure_column_none_value_uses_text(tmp_path):
    db = Database(str(tmp_path / 'ensure_none.db'), verbose=0)
    try:
        db['t'].insert({'id': 1}, pk='id', ensure_table=True)
        db['t'].insert({'id': 2, 'note': None}, ensure_column=True)
        types = db['t'].column_info()['column_type']
        assert types['note'].lower() == 'text'
        assert db['t'].select_one('*', where='id=2')['note'] is None
    finally:
        db.close()


# ---------------------------------------------------------------- M1-2

def test_table_drop_table_invalidates_schema_cache(tmp_path):
    db = Database(str(tmp_path / 'cache.db'), verbose=0)
    try:
        t = db['t']
        t.create_table([
            {'name': 'id', 'type': 'integer', 'default': 7},
            {'name': 'old_col', 'type': 'float', 'default': 666.0},
        ], pk=['id'])
        assert t.column_info()['column_type']['old_col'].lower() == 'float'
        assert float(t.default_values()['old_col']) == 666.0

        t.drop_table()
        t.insert_all([{'id': 1, 'new_col': 'x'}], ensure_table=True)

        info = t.column_info()
        assert 'old_col' not in info['column_type']
        assert 'new_col' in info['column_type']
        assert 'old_col' not in t.default_values()
        row = t.select_one('*')
        assert row['id'] == 1 and row['new_col'] == 'x'
    finally:
        db.close()


def test_database_drop_table_invalidates_cached_table(tmp_path):
    db = Database(str(tmp_path / 'cache2.db'), verbose=0)
    try:
        t = db['t']
        t.create_table([
            {'name': 'id', 'type': 'integer'},
            {'name': 'old_col', 'type': 'float'},
        ], pk=['id'])
        t.column_info()  # 填充缓存

        db.drop_table('t')
        t.insert_all([{'id': 1, 'new_col': 'x'}], ensure_table=True)

        assert 'old_col' not in t.column_info()['column_type']
    finally:
        db.close()


def test_public_invalidate_cache_after_external_ddl(tmp_path):
    db = Database(str(tmp_path / 'external_ddl.db'), verbose=0)
    try:
        t = db['t']
        t.create_table([{'name': 'id', 'type': 'integer'}], pk=['id'])
        assert 'ext' not in t.column_info()['column_type']

        db.execute("ALTER TABLE t ADD COLUMN ext TEXT DEFAULT 'x'")
        assert 'ext' not in t.column_info()['column_type']

        t.invalidate_cache()
        assert 'ext' in t.column_info()['column_type']
    finally:
        db.close()


# ---------------------------------------------------------------- M1-3

def test_wal_false_is_honored(tmp_path):
    db = Database(str(tmp_path / 'no_wal.db'), wal=False, verbose=0)
    try:
        with db.get_connection() as conn:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        assert str(mode).lower() == 'delete'
    finally:
        db.close()


def test_wal_true_is_honored(tmp_path):
    db = Database(str(tmp_path / 'with_wal.db'), wal=True, verbose=0)
    try:
        with db.get_connection() as conn:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
        assert str(mode).lower() == 'wal'
    finally:
        db.close()


# ---------------------------------------------------------------- M1-4

def test_return_during_close_does_not_leak_connection(tmp_path):
    pool = DatabaseConnectionPool(str(tmp_path / 'close_race.sqlite3'), max_connections=1)
    baseline = get_process_connection_budget_status()['open_process_connections']

    entered = threading.Event()
    release = threading.Event()
    errors = []
    original_is_alive = pool._is_connection_alive
    calls = []

    def delayed_is_alive(conn):
        calls.append(1)
        # 第一次是 checkout（新连接）校验，第二次才是归还路径，只阻塞后者
        if len(calls) >= 2:
            entered.set()
            release.wait(timeout=2)
        return original_is_alive(conn)

    pool._is_connection_alive = delayed_is_alive

    def worker():
        try:
            with pool.get_connection():
                pass
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=2)
    pool.force_close_all_connections()
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert pool.get_pool_status()['created_connections'] == 0
    assert pool.get_pool_status()['available_connections'] == 0
    assert get_process_connection_budget_status()['open_process_connections'] == baseline


# ---------------------------------------------------------------- M1-5

def test_pool_nested_checkout_same_thread_is_reentrant(tmp_path):
    pool = DatabaseConnectionPool(str(tmp_path / 'nested.sqlite3'), max_connections=1)
    try:
        with pool.get_connection() as c1:
            with pool.get_connection() as c2:
                assert c1 is c2
        assert pool.get_pool_status()['available_connections'] == 1
    finally:
        pool.force_close_all_connections()


def test_database_nested_connection_and_transaction_no_deadlock(tmp_path):
    db = Database(str(tmp_path / 'nested_db.db'), verbose=0, max_connections=1)
    result = {}

    def run():
        try:
            with db.get_connection() as c1:
                with db.get_connection() as c2:
                    result['same'] = c1 is c2
                with db.transaction():
                    db['t'].insert({'id': 1}, pk='id', ensure_table=True)
            result['ok'] = True
        except Exception as exc:  # pragma: no cover
            result['error'] = repr(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)
    try:
        assert not thread.is_alive(), '嵌套 checkout 发生死锁'
        assert result.get('ok') is True, result
        assert result.get('same') is True
        assert db['t'].count() == 1
    finally:
        db.close()


# ---------------------------------------------------------------- M1-6

def test_insert_rejects_commit_false(tmp_path):
    db = Database(str(tmp_path / 'commit_false.db'), verbose=0)
    try:
        with pytest.raises(ValueError, match='commit=False'):
            db['t'].insert({'i': 1}, commit=False)
        with pytest.raises(ValueError, match='commit=False'):
            db['t'].executemany('INSERT INTO t (i) VALUES (?)', [(1,)], commit=False)
    finally:
        db.close()


def test_auto_commit_false_requires_transaction_or_explicit_commit(tmp_path):
    db = Database(str(tmp_path / 'auto_commit_false.db'), auto_commit=False, verbose=0)
    try:
        db['t'].create_table([{'name': 'i', 'type': 'integer'}], pk=['i'])

        with pytest.raises(ValueError, match='auto_commit'):
            db['t'].insert({'i': 1})
        with pytest.raises(DatabaseError, match='auto_commit'):
            db.execute('INSERT INTO t (i) VALUES (1)')

        with db.transaction():
            db['t'].insert({'i': 1})
            db.execute('INSERT INTO t (i) VALUES (3)')

        # 显式事务内由外层提交，commit=False 不会把未完成事务归还连接池。
        with db.transaction():
            db['t'].insert({'i': 2}, commit=False)
        assert db['t'].count() == 3

        # insert_all 自带事务并显式提交，auto_commit=False 下依然安全
        db['t'].insert_all([{'i': 4}, {'i': 5}])
        assert db['t'].count() == 5

        # update/delete 显式 commit=True，同样安全
        db['t'].update({'i': 40}, where='i=4')
        db['t'].delete('i=5')
        assert db['t'].count() == 4
    finally:
        db.close()


def test_insert_all_still_rejects_deferred_commit(tmp_path):
    db = Database(str(tmp_path / 'insert_all_commit.db'), verbose=0)
    try:
        with pytest.raises(ValueError, match='commit'):
            db['t'].insert_all([{'i': 1}], pk='i', ensure_table=True, commit=False)
    finally:
        db.close()


# ---------------------------------------------------------------- M1-7

def test_idle_small_pool_does_not_compress_process_budget(tmp_path):
    large = DatabaseConnectionPool(
        str(tmp_path / 'large_budget.sqlite3'),
        max_connections=4,
        max_process_connections=8,
    )
    try:
        baseline = get_process_connection_budget_status()['max_process_connections']
        assert baseline >= 8

        small = DatabaseConnectionPool(
            str(tmp_path / 'small_budget.sqlite3'),
            max_connections=1,
            max_process_connections=1,
        )
        try:
            # 空闲的保守小池加入后不能压缩已有预算（旧 min() 语义会压缩）
            status = get_process_connection_budget_status()
            assert status['max_process_connections'] == baseline
        finally:
            small.force_close_all_connections()
    finally:
        large.force_close_all_connections()


def test_active_conservative_pool_caps_process_budget(tmp_path):
    large = DatabaseConnectionPool(
        str(tmp_path / 'large_budget2.sqlite3'),
        max_connections=4,
        max_process_connections=8,
    )
    small = DatabaseConnectionPool(
        str(tmp_path / 'small_budget2.sqlite3'),
        max_connections=1,
        max_process_connections=1,
    )
    try:
        with small.get_connection() as conn:
            conn.execute('SELECT 1')
            # 活跃的保守池必须被尊重，避免连接超发
            assert get_process_connection_budget_status()[
                'max_process_connections'
            ] == 1
        # 归还后不再压缩全局预算
        assert get_process_connection_budget_status()[
            'max_process_connections'
        ] >= 8
    finally:
        small.force_close_all_connections()
        large.force_close_all_connections()


# ---------------------------------------------------------------- M1-8

def test_timeout_derives_busy_timeout(tmp_path):
    db = Database(str(tmp_path / 'busy_timeout.db'), timeout=1.25, verbose=0)
    try:
        with db.get_connection() as conn:
            busy = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        assert busy == 1250
    finally:
        db.close()


def test_explicit_pragma_busy_timeout_wins(tmp_path):
    db = Database(
        str(tmp_path / 'busy_timeout2.db'),
        timeout=1.0,
        pragma_dict={'busy_timeout': '7000'},
        verbose=0,
    )
    try:
        with db.get_connection() as conn:
            busy = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        assert busy == 7000
    finally:
        db.close()


# ---------------------------------------------------------------- P2

def test_pragma_applies_to_idle_connections_and_zero_value(tmp_path):
    db = Database(str(tmp_path / 'pragma_idle.db'), verbose=0)
    try:
        with db.get_connection() as conn:
            conn.execute('SELECT 1')

        db.pragma('wal_autocheckpoint', 0)
        with db.get_connection() as conn:
            assert conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 0

        # falsy 的 0 也必须写入配置（旧实现 value or '' 会吞掉）
        db._pragma_one('cache_size', 0)
        assert db._pragma_dict['cache_size'] == '0'
    finally:
        db.close()


def test_pragmas_apply_to_new_connections(tmp_path):
    db = Database(str(tmp_path / 'pragma_new.db'), verbose=0)
    try:
        db.pragma('foreign_keys', 'off')
        with db.get_connection() as conn:
            assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 0
    finally:
        db.close()


def test_suggest_value_type_handles_numpy_and_numbers():
    np = pytest.importorskip('numpy')
    assert suggest_value_type(np.int64(1)) == 'integer'
    assert suggest_value_type(np.uint32(1)) == 'integer'
    assert suggest_value_type(np.float32(1.5)) == 'float'
    assert suggest_value_type(np.float64(1.5)) == 'float'
    assert suggest_value_type(np.bool_(True)) == 'integer'
    assert suggest_value_type(1.5) == 'float'
    assert suggest_value_type(True) == 'integer'
    assert suggest_value_type(b'x') == 'blob'
    assert suggest_value_type(None) is None


def test_column_type_of_value_handles_numpy():
    np = pytest.importorskip('numpy')
    assert sqlite_type_of_value(np.int64(5)) == 'integer'
    assert sqlite_type_of_value(np.float32(1.5)) == 'float'
    assert sqlite_type_of_value(np.bool_(False)) == 'integer'
    assert sqlite_type_of_value('text') == 'text'
    assert sqlite_type_of_value(b'x') == 'blob'


def test_suggest_column_types_keeps_declared_columns_absent_from_data():
    # 无未声明字段：走快速路径
    out = suggest_column_types([{'a': 1}], {'a': 'integer', 'b': 'text'})
    assert dict(out) == {'a': 'integer', 'b': 'text'}
    # 数据含未声明字段：走推断路径时也不能丢掉已声明的 b
    out = suggest_column_types([{'a': 1, 'c': 2.5}], {'a': 'integer', 'b': 'text'})
    assert dict(out) == {'a': 'integer', 'b': 'text', 'c': 'float'}
    # 声明列优先于数据推断
    out = suggest_column_types([{'a': 1.5}], {'a': 'integer'})
    assert dict(out) == {'a': 'integer'}


def test_drop_all_index_skips_pk_autoindex(tmp_path):
    db = Database(str(tmp_path / 'drop_all_index.db'), verbose=0)
    try:
        t = db['t']
        t.create_table(
            [{'name': 'id', 'type': 'integer'}, {'name': 'name', 'type': 'text'}],
            pk=['id'],
        )
        t.create_index('name')
        assert any('name_index' in name for name in t.get_all_index())

        t.drop_all_index()

        assert not any('name_index' in name for name in t.get_all_index())
        # 主键自动索引仍可用，表可继续写入
        t.insert({'id': 2, 'name': 'b'})
        assert t.count() == 1
    finally:
        db.close()


def test_drop_column_index_composite_and_expression(tmp_path):
    db = Database(str(tmp_path / 'drop_column_index.db'), verbose=0)
    try:
        t = db['t']
        t.create_table([
            {'name': 'a', 'type': 'integer'},
            {'name': 'b', 'type': 'text'},
            {'name': 'c', 'type': 'text'},
        ], pk=['a'])
        db.execute('CREATE INDEX t_ab_index ON t (a, b)')
        db.execute('CREATE INDEX t_expr_index ON t (lower(c))')

        assert t.drop_column_index('b') == 1
        assert not any('t_ab_index' in name for name in t.get_all_index())
        # 表达式索引无法解析列名，不应被误删
        assert t.drop_column_index('c') == 0
        assert any('t_expr_index' in name for name in t.get_all_index())
    finally:
        db.close()


def test_memory_database_rejected(tmp_path):
    with pytest.raises(BadArgument):
        Database(':memory:', verbose=0)
    with pytest.raises(BadArgument):
        DatabaseConnectionPool(':memory:')


def test_create_table_meta_failure_rolls_back(tmp_path, monkeypatch):
    import iosql.utils.mysqlite.table as table_module

    db = Database(str(tmp_path / 'meta_rollback.db'), verbose=0)
    try:
        def boom(*args, **kwargs):
            raise RuntimeError('meta boom')

        monkeypatch.setattr(table_module, 'getsqlarg_dict_insert', boom)
        with pytest.raises(RuntimeError, match='meta boom'):
            db['half'].create_table([{'name': 'id', 'type': 'integer'}], pk=['id'])
        assert db['half'].table_exists() is False
    finally:
        db.close()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='fork 仅 POSIX 可用')
def test_pool_rebuilds_connections_after_fork(tmp_path):
    pool = DatabaseConnectionPool(str(tmp_path / 'fork.sqlite3'), max_connections=2)
    with pool.get_connection() as conn:
        conn.execute('CREATE TABLE t (i INTEGER)')
        conn.commit()

    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - 子进程
        try:
            with pool.get_connection() as conn:
                conn.execute('INSERT INTO t VALUES (1)')
            pool.force_close_all_connections()
            os._exit(0)
        except BaseException:
            os._exit(1)

    _, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    pool.force_close_all_connections()


def test_sql_literal_bytes_uses_blob_literal():
    assert sql_literal(b'\x00\xff') == "X'00ff'"
    assert sql_literal(bytearray(b'\x01')) == "X'01'"
    assert sql_literal('a') == "'a'"
    assert sql_literal("a'b") == "'a''b'"


def test_getsubsql_insert_rejects_ignore_and_replace():
    with pytest.raises(ValueError):
        getsubsql_insert(ignore=True, replace=True)


def test_getsql_create_table_rejects_empty_columns():
    with pytest.raises(ValueError):
        getsql_create_table('t', [], pk=None)


def test_sql_literal_bytes_default_in_create_table():
    sql, _ = getsql_create_table(
        't',
        [{'name': 'blob_col', 'type': 'blob', 'default': b'\x00\xff'}],
        pk=None,
    )
    assert "DEFAULT X'00ff'" in sql


def test_parse_db_schema_memory_forms():
    for uri in ('sqlite3://:memory:', 'sqlite3:///:memory:', 'sqlite3://:memory'):
        schema, options = parse_db_schema(uri)
        assert schema == 'sqlite3'
        assert options['memory'] is True

    schema, options = parse_db_schema('sqlite3:///tmp/not_memory.db')
    assert schema == 'sqlite3'
    assert options['memory'] is False
    assert options['db'] == 'not_memory.db'


def test_is_pool_healthy_reflects_close(tmp_path):
    pool = DatabaseConnectionPool(str(tmp_path / 'healthy.sqlite3'), max_connections=1)
    assert pool.is_pool_healthy() is True
    pool.force_close_all_connections()
    assert pool.is_pool_healthy() is False


def test_returns_connection_rolls_back_uncommitted_write(tmp_path):
    """连接归还必须回滚未提交写入（防静默脏数据），显式提交才持久化。"""
    db = Database(str(tmp_path / 'rollback.db'), verbose=0)
    try:
        db['t'].create_table([{'name': 'i', 'type': 'integer'}], pk=['i'])
        with db.get_connection() as conn:
            conn.execute('INSERT INTO t (i) VALUES (1)')
            # 不 commit，归还时应回滚
        assert db['t'].count() == 0

        db['t'].insert({'i': 2})
        assert db['t'].count() == 1
    finally:
        db.close()
