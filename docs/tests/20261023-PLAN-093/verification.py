"""在自动清理的临时库中记录修复后的执行计数，不接受业务数据库路径。"""

import ast
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import duckdb

from iosql import Database, Segment
from iosql.exception import BadArgument, MigrationError
from iosql.utils.mysqlite.table import Table as LowTable


ROOT = Path(__file__).resolve().parents[3]


def operation_counts(directory):
    """比较可稳定复核的工作量，避免用某台机器的墙钟阈值宣称性能。"""
    result = {}
    with Database(f'sqlite://{directory / "sqlite"}') as db:
        table = db.table('plain', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': i} for i in range(20_000)])
        query = LowTable.query
        reads = []

        def observed_query(self, sql, *args, **kwargs):
            rows = query(self, sql, *args, **kwargs)
            if self.table_name == 'data':
                reads.append({'sql': sql, 'rows': len(rows)})
            return rows

        with patch.object(LowTable, 'query', observed_query):
            assert len(table.select(limit=1)) == 1
        result['limit_1_physical_reads'] = reads
        open_connection = table._open_part_read_conn
        instructions = 0
        opens = 0

        def progress():
            nonlocal instructions
            instructions += 100
            return 0

        def observed_connection(path):
            nonlocal opens
            opens += 1
            connection = open_connection(path)
            connection.set_progress_handler(progress, 100)
            return connection

        with patch.object(table, '_open_part_read_conn', observed_connection):
            rows = sum(len(batch) for batch in table.iter_batches(batch_size=100))
        result['unordered_20k_batch_100'] = {
            'rows': rows, 'read_connections': opens, 'vm_instructions_approx': instructions,
        }
        segmented = db.table('segmented', pk=['id'], columns={'id': 'integer'},
                             segment=Segment.rows(max_rows=100))
        with patch.object(segmented, '_do_insert_part', wraps=segmented._do_insert_part) as writes:
            with patch.object(segmented, '_refresh_segment_info', wraps=segmented._refresh_segment_info) as refreshes:
                segmented.write([{'id': i} for i in range(20)], mode='ignore')
        result['segment_ignore_20'] = {'physical_writes': writes.call_count,
                                       'full_stats_refreshes': refreshes.call_count}
        import iosql.coordination as coordination
        with patch.object(coordination, 'resource', wraps=coordination.resource) as calls:
            with table.buffered_writer(max_rows=1000) as writer:
                for index in range(1000):
                    writer.append({'id': 20_000 + index})
        result['buffered_append_1000_resource_resolutions'] = calls.call_count
    with Database(f'parquet://{directory / "parquet"}') as db:
        table = db.table('archive', pk=['id'], columns={'id': 'integer'})
        publish = table._publish
        written = []

        def observed_publish(connection, path, **kwargs):
            written.append(connection.execute('SELECT COUNT(*) FROM data').fetchone()[0])
            return publish(connection, path, **kwargs)

        with patch.object(table, '_publish', observed_publish):
            for batch in range(5):
                table.write([{'id': i} for i in range(batch * 100, (batch + 1) * 100)])
        assert table.count_rows() == 500
        result['parquet_append_5x100'] = {'rows_written_per_batch': written,
                                         'total_rows_written': sum(written)}
    return result


def pg_counts():
    """真实 PostgreSQL 上连续独立 API 调用的连接及 SQL 次数。"""
    if not os.environ.get('POSTGRES_DSN'):
        return {'tested': False}
    import psycopg2
    import iosql.coordination_postgresql as control
    from iosql.engines.postgresql.table import Table
    uri = os.environ['POSTGRES_DSN']
    control.close_idle_controls()
    connect = control.connect_control
    connections = []
    statements = []

    class Cursor(psycopg2.extensions.cursor):
        """记录控制 SQL 次数，实际查询仍由服务器执行。"""
        def execute(self, sql, params=None):
            statements.append(sql)
            return super().execute(sql, params)

    def observed(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.cursor_factory = Cursor
        connections.append(connection)
        return connection

    table = Table(uri, 'control_only')
    with patch.object(control, 'connect_control', observed):
        for _ in range(3):
            table.spec()
    result = {'tested': True, 'independent_calls': 3, 'connections_created': len(connections),
              'sql_statements': len(statements), 'server_version': connections[0].server_version}
    table.close()
    result['connections_closed_after_close'] = sum(bool(connection.closed) for connection in connections)
    return result


def migration_mode_checks(directory):
    """R04：显式 append 穿过迁移执行器后也不能覆盖默认 replace/ignore 的目标。"""
    output = []
    for backend in ('sqlite', 'parquet'):
        for conflict in ('replace', 'ignore'):
            source_uri = f'sqlite://{directory / (backend + conflict + "-source")}'
            target_uri = f'{backend}://{directory / (backend + conflict + "-target")}'
            with Database(source_uri) as source:
                source.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'}, conflict=conflict).write(
                    {'id': 1, 'v': 'new'})
                with Database(target_uri) as target:
                    target.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'}, conflict=conflict).write(
                        {'id': 1, 'v': 'old'})
                try:
                    source.migrate_to(target_uri, mode='append')
                except MigrationError:
                    pass
                except BadArgument as error:
                    assert '主键冲突' in str(error)
                else:
                    raise AssertionError('append 迁移遇到重复键应失败')
                assert source.table('t').select() == [{'id': 1, 'v': 'new'}]
            with Database(target_uri) as target:
                assert target.table('t').select() == [{'id': 1, 'v': 'old'}]
            output.append({'backend': backend, 'default_conflict': conflict,
                           'explicit_mode': 'append', 'source_and_target_preserved': True})
    return output


def sources_and_assets():
    """确认源码语法、前端语法、示例副本和本次验证的源码指纹。"""
    files = sorted(path for folder in ('iosql', 'cli', 'examples', 'benchmarks')
                   for path in (ROOT / folder).rglob('*.py')
                   if 'runtime' not in path.relative_to(ROOT).parts)
    for path in files:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3, 9))
    javascript = [ROOT / 'iosql/web/static/app.js', *sorted((ROOT / 'iosql/web/static/js').glob('*.js'))]
    for path in javascript:
        subprocess.run(['node', '--check', str(path)], check=True, capture_output=True)
    copies = 0
    for source in (ROOT / 'examples').rglob('*'):
        relative = source.relative_to(ROOT / 'examples')
        if not source.is_file() or 'runtime' in relative.parts or '__pycache__' in relative.parts:
            continue
        target = ROOT / 'skills/iosql-usage/examples' / relative
        assert target.is_file() and source.read_bytes() == target.read_bytes(), relative
        copies += 1
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'iosql').rglob('*.py')):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return {'python_files_parsed_with_39_grammar': len(files), 'javascript_files_checked': len(javascript),
            'example_files_identical': copies, 'production_python_sha256': digest.hexdigest()}


def main():
    output = {
        'generated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'environment': {'python': platform.python_version(), 'platform': platform.system(),
                        'sqlite': sqlite3.sqlite_version, 'duckdb': duckdb.__version__},
        'sources': sources_and_assets(),
    }
    with tempfile.TemporaryDirectory(prefix='iosql-plan093-') as directory:
        output['operations'] = operation_counts(Path(directory))
        output['migration_modes'] = migration_mode_checks(Path(directory))
    output['postgresql'] = pg_counts()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
