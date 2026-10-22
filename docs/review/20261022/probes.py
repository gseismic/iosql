"""在临时目录中复核审查发现，不修改仓库实现或业务库。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from iosql import Database, Segment, Case


def run_case(name, callback):
    try:
        with tempfile.TemporaryDirectory(prefix='iosql-review-') as root:
            result = callback(Path(root))
        print(json.dumps({'case': name, 'result': result}, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({'case': name, 'error': repr(error)}, ensure_ascii=False), flush=True)


def compact_failure(root):
    uri = 'sqlite://' + str(root / 'db')
    with Database(uri) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'},
                         segment=Segment.rows(max_rows=2), order_key='id')
        table.write([{'id': i} for i in range(4)])
        original = table._write_segment_rows
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('审查注入：第二个 compact 文件写入失败')
            return original(*args, **kwargs)
        with patch.object(table, '_write_segment_rows', fail_second):
            try:
                table.compact(target_rows=2)
            except OSError:
                pass
        rows = table.select(order_by='id')
        validation = table.validate(deep=True)
        return {'before': 4, 'after': len(rows), 'ids': [r['id'] for r in rows],
                'validation': str(validation)}


def append_override(root):
    output = {}
    for backend in ('sqlite', 'parquet'):
        with Database(f'{backend}://{root / backend}') as db:
            table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'}, conflict='replace')
            table.write({'id': 1, 'v': 'before'})
            error = None
            try:
                table.write({'id': 1, 'v': 'after'}, mode='append')
            except Exception as exc:
                error = type(exc).__name__
            output[backend] = {'error': error, 'rows': table.select()}
    return output


def strict_partition(root):
    with Database('sqlite://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'},
                         partition='range(id,width=10)', schema_policy='strict')
        table.write({'id': 1})
        table.write({'id': 11, 'undeclared': 'accepted'})
        return {'columns': table.spec().columns, 'rows': table.select(order_by='id')}


def segment_affinity(root):
    with Database('sqlite://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'},
                         segment=Segment.rows(max_rows=10), order_key='id')
        table.write({'id': 1, 'v': 2})
        import sqlite3
        with sqlite3.connect(table.segments()[0]['path']) as conn:
            native = conn.execute('SELECT * FROM data WHERE v > ?', [10]).fetchall()
        return {'stored': table.select(), 'api': table.select(where=Case('v', '>', 10)),
                'native': native, 'stats': table.segments()[0]['stats']}


def limit_fetch(root):
    from iosql.utils.mysqlite.table import Table as LowTable
    with Database('sqlite://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': i} for i in range(20000)])
        original = LowTable.query
        calls = []
        def observed(self, sql, *args, **kwargs):
            rows = original(self, sql, *args, **kwargs)
            if self.table_name == 'data':
                calls.append({'sql': sql, 'rows': len(rows)})
            return rows
        with patch.object(LowTable, 'query', observed):
            result = table.select(limit=1)
        return {'returned': len(result), 'physical_reads': calls}


def parquet_budget(root):
    # 独立进程设置较小预算，保留超时栈后结束，避免卡住整个审查。
    source = '''
import faulthandler
from iosql import Database
faulthandler.dump_traceback_later(3)
with Database(URI, max_process_connections=2) as db:
    t = db.table('t', pk=['id'], columns={'id':'integer'}, partition='range(id,width=10)')
    t.write([{'id':1},{'id':11}])
    print('before ordered read', flush=True)
    print(list(t.iter_select(order_by='id', timeout_ms=500)), flush=True)
'''.replace('URI', repr('parquet://' + str(root / 'db')))
    try:
        result = subprocess.run([sys.executable, '-c', source], capture_output=True,
                                text=True, timeout=6)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except subprocess.TimeoutExpired as exc:
        return {'timeout': True, 'stdout': (exc.stdout or b'').decode(),
                'stderr': (exc.stderr or b'').decode()}


def rename_cached_target(root):
    uri = 'sqlite://' + str(root / 'db')
    with Database(uri) as first, Database(uri) as second:
        first.table('src', pk=['id'], columns={'id': 'integer'}).write({'id': 1})
        first.table('dst', pk=['id'], columns={'id': 'integer'}).write({'id': 2})
        cached = second.table('dst')
        before = cached.select()
        first.rename_table('src', 'dst', overwrite=True)
        return {'before': before, 'other_instance_after': cached.select(),
                'renaming_instance_after': first.table('dst').select()}


def web_pagination_and_stats(root):
    from fastapi.testclient import TestClient
    from iosql.web.app import create_app
    from iosql.engines.sqlite.table import Table as EngineTable
    uri = 'sqlite://' + str(root / 'db')
    with Database(uri) as db:
        table = db.table('duplicates', pk=['id'], columns={'id': 'integer'},
                         segment=Segment.rows(max_rows=1))
        table.write({'id': 1})
        table.write({'id': 1})
        db.table('plain', pk=['id'], columns={'id': 'integer', 'v': 'float'}).write(
            [{'id': i, 'v': float(i)} for i in range(1000)])
    calls = []
    original = EngineTable._data_file_column_stats
    def observe(self, *args, **kwargs):
        calls.append(str(args[0]))
        return original(self, *args, **kwargs)
    with TestClient(create_app(uri), base_url='http://localhost') as client:
        first = client.post('/api/table/duplicates/rows', json={'limit': 1}).json()
        second = client.post('/api/table/duplicates/rows', json={'limit': 1, 'cursor': first['next_cursor']}).json()
        with patch.object(EngineTable, '_data_file_column_stats', observe):
            plain = client.post('/api/table/plain/rows', json={'limit': 1})
    return {'page1': first, 'page2': second, 'plain_status': plain.status_code,
            'plain_limit1_full_stats_calls': calls}


def segmented_ignore(root):
    output = {}
    for mode in ('append', 'ignore'):
        with Database(f'sqlite://{root / mode}') as db:
            table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'integer'},
                             segment=Segment.rows(max_rows=100))
            calls = {'physical_writes': 0, 'full_stats_refreshes': 0}
            write = table._do_insert_part
            refresh = table._refresh_segment_info
            def spy_write(*args, **kwargs):
                calls['physical_writes'] += 1
                return write(*args, **kwargs)
            def spy_refresh(*args, **kwargs):
                calls['full_stats_refreshes'] += 1
                return refresh(*args, **kwargs)
            with patch.object(table, '_do_insert_part', spy_write), patch.object(table, '_refresh_segment_info', spy_refresh):
                table.write([{'id': i, 'v': i} for i in range(20)], mode=mode)
            output[mode] = calls
    return output


def offset_vm_work(root):
    import sqlite3
    from iosql.utils.mysqlite.table import Table as LowTable
    with Database('sqlite://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': i} for i in range(20000)])
        offset_steps = 0
        single_steps = 0
        original = LowTable.query
        def offset_progress():
            nonlocal offset_steps
            offset_steps += 100
            return 0
        def single_progress():
            nonlocal single_steps
            single_steps += 100
            return 0
        def observe(self, sql, *args, **kwargs):
            if self.table_name != 'data':
                return original(self, sql, *args, **kwargs)
            with self.database.get_connection() as conn:
                conn.set_progress_handler(offset_progress, 100)
                try:
                    return original(self, sql, *args, **kwargs)
                finally:
                    conn.set_progress_handler(None, 0)
        with patch.object(LowTable, 'query', observe):
            count = sum(map(len, table.iter_batches(batch_size=100)))
        with sqlite3.connect(table._get_part_dbfile(None)) as conn:
            conn.set_progress_handler(single_progress, 100)
            cursor = conn.execute('select * from data')
            direct_count = 0
            while batch := cursor.fetchmany(100):
                direct_count += len(batch)
        return {'api_rows': count, 'direct_rows': direct_count,
                'offset_vm_steps_approx': offset_steps, 'cursor_vm_steps_approx': single_steps}


def cli_replace_source(root):
    from typer.testing import CliRunner
    from iosql.cli import app
    uri = 'sqlite://' + str(root / 'db')
    with Database(uri) as db:
        db.table('selected', pk=['id'], columns={'id': 'integer'},
                 partition='range(id,width=10)').write({'id': 1})
        db.table('keep', pk=['id'], columns={'id': 'integer'}).write({'id': 99})
        before = db.table_names()
    result = CliRunner().invoke(app, [
        'repartition', '-s', uri, '-t', 'selected', '--partition', 'range(id,width=20)',
        '--replace-source', '--no-backup', '--yes'])
    with Database(uri) as db:
        after = db.table_names()
    return {'exit_code': result.exit_code, 'before': before, 'after': after,
            'exception': repr(result.exception) if result.exception else None}


def index_future_partition(root):
    import sqlite3
    with Database('sqlite://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'integer'},
                         partition='range(id,width=10)')
        table.write({'id': 1, 'v': 42})
        table.create_index('v')
        table.write({'id': 11, 'v': 42})
        indexes = {}
        for part in table.table_parts():
            with sqlite3.connect(table._get_part_dbfile(part)) as conn:
                indexes[str(part)] = conn.execute('pragma index_list(data)').fetchall()
        return indexes


def pg_control_calls(root):
    from iosql.engines.postgresql.table import Table as PgTable
    import iosql.coordination_postgresql as control
    calls = {'connects': 0, 'closes': 0, 'sql': []}
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, sql, params=None):
            calls['sql'].append(sql)
        def fetchone(self):
            return (None,) if 'to_regclass' in calls['sql'][-1] else (True,)
    class Connection:
        closed = False
        def cursor(self):
            return Cursor()
        def close(self):
            calls['closes'] += 1
            self.closed = True
    def connect(*args, **kwargs):
        calls['connects'] += 1
        return Connection()
    table = PgTable('postgresql://review:review@localhost:5432/isolated', 't')
    with patch.object(control, 'connect_control', connect):
        for _ in range(3):
            table.spec()
    return calls


def parquet_batch_rewrite(root):
    with Database('parquet://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        original = table._mutable_table
        cumulative = []
        def observe(conn, *args, **kwargs):
            result = original(conn, *args, **kwargs)
            cumulative.append(conn.execute('select count(*) from data').fetchone()[0])
            return result
        with patch.object(table, '_mutable_table', observe):
            for i in range(5):
                table.write([{'id': j} for j in range(i * 100, (i + 1) * 100)])
        return {'new_rows': 500, 'existing_rows_reloaded_per_batch': cumulative,
                'rows_rewritten_total': sum(n + 100 for n in cumulative)}



def unknown_projection(root):
    with Database('sqlite://' + str(root / 'db')) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1})
        return {'columns': table.spec().columns,
                'result': table.select(fields=['typo'])}


if __name__ == '__main__':
    for name, callback in [('compact_failure', compact_failure), ('append_override', append_override),
                           ('strict_partition', strict_partition), ('segment_affinity', segment_affinity),
                           ('limit_fetch', limit_fetch), ('parquet_budget', parquet_budget),
                           ('rename_cached_target', rename_cached_target),
                           ('web_pagination_and_stats', web_pagination_and_stats),
                           ('segmented_ignore', segmented_ignore), ('offset_vm_work', offset_vm_work),
                           ('cli_replace_source', cli_replace_source),
                           ('index_future_partition', index_future_partition),
                           ('pg_control_calls', pg_control_calls), ('parquet_batch_rewrite', parquet_batch_rewrite),
                           ('unknown_projection', unknown_projection)]:
        run_case(name, callback)
