"""审查修复的成本和资源回归：使用调用计数与真实数据库证明执行路径。"""

import gc
import os
import multiprocessing as mp
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from iosql import Case, Database, Segment
from iosql.exception import SchemaConflict, UnsafeOperation


def test_unordered_batches_use_one_linear_cursor(tmp_path):
    """R10：读取工作量随行数线性增长，一次迭代只开一个只读游标。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': i} for i in range(20_000)])
        connections = []
        instructions = 0
        open_connection = table._open_part_read_conn

        def progress():
            nonlocal instructions
            instructions += 100
            return 0

        def observed(path):
            conn = open_connection(path)
            conn.set_progress_handler(progress, 100)
            connections.append(conn)
            return conn

        with patch.object(table, '_open_part_read_conn', observed):
            assert sum(len(batch) for batch in table.iter_batches(batch_size=100)) == 20_000
        assert len(connections) == 1
        assert 0 < instructions < 20_000 * 20
        with pytest.raises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_unordered_stream_close_and_timeout_release_budget(tmp_path, backend, monkeypatch):
    """R06/R10：异常或提前关闭必须释放外部预留，后续写读仍可用。"""
    from iosql.utils.mysqlite.connection_pool import _PROCESS_CONNECTION_BUDGET as budget
    with Database(f'{backend}://{tmp_path / "db"}', max_process_connections=2) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': i} for i in range(20)])
        stream = table.iter_select_batches(batch_size=1, timeout_ms=10_000)
        assert next(stream)[0]['id'] == 0
        # 首批启动成本不属于此反例；取得首批后再确定性地越过迭代截止时间。
        monotonic = time.monotonic
        with monkeypatch.context() as clock:
            clock.setattr(time, 'monotonic', lambda: monotonic() + 11)
            with pytest.raises(TimeoutError):
                next(stream)
        assert not budget._external_reservations
        stream = table.iter_batches(batch_size=1)
        assert next(stream)[0]['id'] == 0
        stream.close()
        assert not budget._external_reservations
        table.write({'id': 21})
        assert table.count_rows() == 21


def test_pool_wait_obeys_query_deadline(tmp_path):
    """R06：进程配额和单文件池等待都继承同一截止时间。"""
    from iosql.utils.mysqlite.connection_pool import DatabaseConnectionPool, connection_deadline
    pool = DatabaseConnectionPool(str(tmp_path / 'busy.sqlite3'), max_connections=1, max_process_connections=1)
    def blocked_checkout():
        with connection_deadline(time.monotonic() + 0.02):
            with pytest.raises(TimeoutError):
                with pool.get_connection():
                    pass
    try:
        with pool.get_connection():
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(blocked_checkout).result(timeout=2)
    finally:
        pool.force_close_all_connections()


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('direction', ['asc', 'desc'])
@pytest.mark.parametrize('layout', ['segments', 'partitions'])
def test_web_pages_preserve_all_physical_duplicates(tmp_path, backend, direction, layout):
    """R07：不同文件的同一主键和同一排序值必须逐条可达。"""
    from fastapi.testclient import TestClient
    from iosql.web.app import create_app
    uri = f'{backend}://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text', 'part': 'integer'},
                         segment=Segment.rows(max_rows=1) if layout == 'segments' else None,
                         partition='range(part,width=10)' if layout == 'partitions' else None)
        for index, value in enumerate(['a', 'b', 'c']):
            table.write({'id': 1, 'v': value, 'part': index * 10})
    with TestClient(create_app(uri), base_url='http://localhost') as client:
        cursor = None
        values = []
        for _ in range(4):
            result = client.post('/api/table/t/rows', json={
                'order_by': f'id {direction}', 'limit': 1, 'cursor': cursor,
            })
            assert result.status_code == 200, result.text
            payload = result.json()
            values.extend(row['v'] for row in payload['rows'])
            cursor = payload['next_cursor']
            if cursor is None:
                break
        assert values == (['a', 'b', 'c'] if direction == 'asc' else ['c', 'b', 'a'])


def test_web_lightweight_stats_never_scan_column_bounds(tmp_path):
    """R12：legacy 和 segment 混合库的首页、计数、stats 不能做全列 MIN/MAX。"""
    from fastapi.testclient import TestClient
    from iosql.web.app import create_app
    from iosql.engines.sqlite.table import Table
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        for name, segment in [('legacy', None), ('segments', Segment.rows(max_rows=2))]:
            db.table(name, pk=['id'], columns={'id': 'integer'}, segment=segment).write([{'id': i} for i in range(10)])
    with patch.object(Table, '_data_file_column_stats', side_effect=AssertionError('不应扫描列统计')):
        with TestClient(create_app(uri), base_url='http://localhost') as client:
            for name in ('legacy', 'segments'):
                assert client.post(f'/api/table/{name}/rows', json={'limit': 1}).status_code == 200
                assert client.post(f'/api/table/{name}/count', json={}).json()['count'] == 10
                assert client.get(f'/api/table/{name}/stats').json()['row_count'] == 10
            assert client.get('/api/tables?with_stats=true').status_code == 200


def test_parquet_append_writes_only_new_rows_and_recovers_failed_publish(tmp_path):
    """R14：每批实际输出量固定；清单发布失败后重开仍只见先前已提交的批次。"""
    import iosql.engines.parquet.table as parquet
    uri = f'parquet://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        publish = table._publish
        written = []

        def observed(conn, path, **kwargs):
            written.append(conn.execute('SELECT COUNT(*) FROM data').fetchall()[0][0])
            return publish(conn, path, **kwargs)

        with patch.object(table, '_publish', observed):
            for batch in range(5):
                table.write([{'id': i} for i in range(batch * 100, (batch + 1) * 100)])
        assert written == [100] * 5
        assert len(table._parquet_files(table._get_part_dbfile(None))) == 5
        with patch.object(parquet, 'atomic_json', side_effect=OSError('清单发布失败')):
            with pytest.raises(OSError):
                table.write({'id': 500})
        assert table.count_rows() == 500
    with Database(uri) as db:
        table = db.table('t')
        assert table.count_rows() == 500
        table.write({'id': 500})
        assert table.count_rows() == 501
        table.write({'id': 0}, mode='replace')
        assert table.count_rows() == 501
        assert len(table._parquet_files(table._get_part_dbfile(None))) == 1


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_latest_compact_and_upsert_avoid_full_python_materialization(tmp_path, backend):
    """R15：禁用原来的全表物化 helper，便利 API 仍返回正确数据。"""
    uri = f'{backend}://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db.timeseries('t', time='ts', pk=['symbol', 'ts'],
                              columns={'symbol': 'text', 'ts': 'integer', 'v': 'text'},
                              segment=Segment.rows(max_rows=20))
        table.write([{'symbol': symbol, 'ts': i, 'v': str(i)} for symbol in ('a', 'b') for i in range(100)])
        with patch.object(table, 'select', side_effect=AssertionError('latest 不应全表 select')):
            latest = list(table.latest().iter_rows())
        assert {(row['symbol'], row['ts']) for row in latest} == {('a', 99), ('b', 99)}
        with patch.object(table, '_rows_from_part_dbfile', side_effect=AssertionError('compact 不应全量载入')):
            table.compact(target_rows=20)
        assert table.count_rows() == 200
        plain = db.table('plain', pk=['id'], columns={'id': 'text', 'v': 'text', 'keep': 'text'})
        plain.write([{'id': str(i), 'v': 'old', 'keep': '保留'} for i in range(200)])
        if backend == 'sqlite':
            from iosql.utils.mysqlite.table import Table as Adapter
        else:
            from iosql.engines.parquet.table import _Scan as Adapter
        query = Adapter.query
        reads = []

        def observed(self, sql, *args, **kwargs):
            rows = query(self, sql, *args, **kwargs)
            if 'select * from data' in sql.lower():
                assert 'where' in sql.lower()
                reads.append(len(rows))
            return rows

        with patch.object(Adapter, 'query', observed):
            plain.upsert_all([{'id': 1, 'v': 'new'}, {'id': '1', 'v': 'last'}], pk=['id'])
        assert sum(reads) == 1
        assert plain.select(where=Case('id', '=', '1')) == [{'id': '1', 'v': 'last', 'keep': '保留'}]
        assert plain.count_rows() == 200


def test_fixed_schema_writes_do_not_infer_all_types(tmp_path):
    """KI 类型推断：固定结构路径不扫描值推断，同时保留异构行和默认值。"""
    from iosql.utils.mysqlite import Database as LowDatabase
    with LowDatabase(str(tmp_path / 'low.sqlite3')) as db:
        table = db['t']
        table.create_table([{'name': 'id', 'type': 'integer'},
                            {'name': 'v', 'type': 'text', 'default': '默认'}], pk=['id'])
        with patch('iosql.utils.mysqlite.table._union_fields_of_dictlist', side_effect=AssertionError('不应推断类型')):
            table.insert_all([{'id': 1, 'v': 'first'}, {'id': 2}, {'v': 'last', 'id': 1}], replace=True)
        rows = table.select('*', order_by='id', to_dict=True)
        assert rows == [{'id': 1, 'v': 'last'}, {'id': 2, 'v': '默认'}]


def test_buffered_append_reuses_resource_identity(tmp_path):
    """KI 缓冲租约：逐行追加仍有维护屏障，但不重复解析库路径。"""
    import iosql.coordination as coordination
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        with patch.object(coordination, 'resource', wraps=coordination.resource) as calls:
            with table.buffered_writer(max_rows=1000) as writer:
                for i in range(1000):
                    writer.append({'id': i})
        assert calls.call_count < 20
        assert table.count_rows() == 1000


def test_budget_snapshot_survives_reentrant_unregistration():
    """KI 预算遍历：模拟析构在读取状态时注销另一池，旧弱字典迭代会报错。"""
    from iosql.utils.mysqlite.connection_pool import _ProcessConnectionBudget
    budget = _ProcessConnectionBudget()
    class Pool:
        victim = None
        @property
        def _checked_out(self):
            if self.victim is not None:
                budget.unregister(self.victim)
                self.victim = None
            return 0
    first, second = Pool(), Pool()
    first.victim = second
    budget.register(first, 3)
    budget.register(second, 5)
    assert budget.status()['max_process_connections'] in (3, 5)
    assert budget.status()['registered_pools'] == 1


def test_budget_pool_lifecycle_is_safe_under_gc_and_threads(tmp_path):
    """KI 预算遍历：并发创建、回收及 GC 不能损坏注册表或泄漏真实连接。"""
    from iosql.utils.mysqlite.connection_pool import DatabaseConnectionPool
    def cycle(index):
        for iteration in range(15):
            pool = DatabaseConnectionPool(str(tmp_path / f'{index}-{iteration}.sqlite3'), max_connections=1)
            with pool.get_connection() as conn:
                assert conn.execute('SELECT 1').fetchone()[0] == 1
            pool.force_close_all_connections()
            gc.collect()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(cycle, range(4)))


def test_missing_physical_column_is_an_iosql_error(tmp_path):
    """R17：逻辑列存在但文件缺列也必须报库级错误，不能返回列名字面量。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'})
        table.write({'id': 1, 'v': 'a'})
        with sqlite3.connect(table._get_part_dbfile(None)) as conn:
            conn.execute('ALTER TABLE data DROP COLUMN v')
        with pytest.raises(SchemaConflict):
            table.select(['v'])
        with pytest.raises(UnsafeOperation):
            list(table.iter_select(['v']))


def _pg_child_spec(pipe, uri):
    import iosql.coordination_postgresql as control
    from iosql.engines.postgresql.table import Table
    table = Table(uri, 'control_only')
    try:
        table.spec()
        pipe.send(control._idle[uri].connection.get_backend_pid())
    finally:
        table.close()
        pipe.close()


@pytest.mark.skipif(not os.environ.get('POSTGRES_DSN'), reason='需要独立 POSTGRES_DSN')
def test_pg_controls_reuse_reconnect_fork_and_close():
    """R13：真实服务上确认连接/SQL 次数、断连重建、fork 隔离及显式关闭。"""
    import psycopg2
    import iosql.coordination_postgresql as control
    from iosql.engines.postgresql.table import Table
    uri = os.environ['POSTGRES_DSN']
    control.close_idle_controls()
    original = control.connect_control
    connections = []
    statements = []
    class Cursor(psycopg2.extensions.cursor):
        def execute(self, sql, params=None):
            statements.append(sql)
            return super().execute(sql, params)
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.cursor_factory = Cursor
        connections.append(connection)
        return connection
    table = Table(uri, 'control_only')
    try:
        with patch.object(control, 'connect_control', connect):
            for _ in range(3):
                table.spec()
            assert len(connections) == 1
            assert len(statements) <= 10
            assert sum('pg_try_advisory_lock_shared' in sql for sql in statements) == 3
            assert 'to_regclass' not in statements[0]
            assert 'to_regclass' in statements[1]
            old = control._idle[uri].connection
            if hasattr(os, 'fork'):
                context = mp.get_context('fork')
                parent, child = context.Pipe()
                process = context.Process(target=_pg_child_spec, args=(child, uri))
                process.start()
                child.close()
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join()
                    pytest.fail('fork 子进程未完成控制查询')
                assert process.exitcode == 0
                assert parent.recv() != old.get_backend_pid()
                parent.close()
                with old.cursor() as cursor:
                    cursor.execute('SELECT 1')
                    assert cursor.fetchone()[0] == 1
            with psycopg2.connect(uri) as killer:
                with killer.cursor() as cursor:
                    cursor.execute('SELECT pg_terminate_backend(%s)', (old.get_backend_pid(),))
            table.spec()
            assert len(connections) == 2
    finally:
        table.close()
    assert uri not in control._idle
    assert all(connection.closed for connection in connections)
