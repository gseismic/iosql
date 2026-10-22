"""2026-10-22 全库审查回归：同时校验结果、发布故障和可观察的执行成本。"""

import json
import sqlite3
import subprocess
import sys
import multiprocessing as mp
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from iosql import Case, Database, Segment
from iosql.cli import app
from iosql.exception import BadArgument, SchemaConflict, UnsafeOperation
from iosql.utils.mysqlite.table import Table as LowTable


def _cached_rename_reader(pipe, uri):
    """在独立进程预热两个文件句柄，等待发布后再次使用同一对象。"""
    try:
        with Database(uri) as db:
            source, destination = db.table('src'), db.table('dst')
            pipe.send((source.select(), destination.select()))
            pipe.recv()
            destination_rows = destination.select()
            try:
                source.select()
            except UnsafeOperation:
                source_rejected = True
            else:
                source_rejected = False
            pipe.send((destination_rows, source_rejected))
    finally:
        pipe.close()


def test_repartition_replace_preserves_unselected_tables(tmp_path):
    """R01：单表重分区不能改变其他表的数据、契约和真实索引。"""
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        db.table('selected', pk=['id'], columns={'id': 'integer'},
                 partition='range(id,width=10)').write({'id': 1})
        keep = db.table('keep', pk=['id'], columns={'id': 'integer', 'v': 'text'})
        keep.write({'id': 99, 'v': '保留'})
        keep.create_index('v')
        before = keep.spec().to_dict()
    command = ['repartition', '-s', uri, '-t', 'selected', '--partition',
               'range(id,width=20)', '--replace-source', '--no-backup', '--yes']
    preview = CliRunner().invoke(app, command + ['--dry-run'])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)['plan']['preserved_tables'] == ['keep']
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 0, result.output
    with Database(uri) as db:
        assert db.table_names() == ['keep', 'selected']
        keep = db.table('keep')
        assert keep.select() == [{'id': 99, 'v': '保留'}]
        assert keep.spec().to_dict() == before
        with sqlite3.connect(keep._get_part_dbfile(None)) as conn:
            assert 'data_v_index' in [row[1] for row in conn.execute('PRAGMA index_list(data)')]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('failure', ['second_chunk', 'publish'])
def test_compact_generation_failure_is_invisible(tmp_path, backend, failure):
    """R02：中途写失败或 manifest 发布失败均不能泄漏部分新代。"""
    uri = f'{backend}://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'},
                         segment=Segment.rows(max_rows=2), order_key='id')
        table.write([{'id': i} for i in range(4)])
        before = table.select(order_by='id')
        write = table._write_segment_rows
        mark = table._mark_segments_obsolete
        calls = 0

        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('第二个 chunk 失败')
            # 构建过程中读者也只能看到旧代。
            result = write(*args, **kwargs)
            assert table.select(order_by='id') == before
            return result

        def fail_publish(refs):
            mark(refs)
            raise OSError('发布事务失败')

        method = '_write_segment_rows' if failure == 'second_chunk' else '_mark_segments_obsolete'
        with patch.object(table, method, fail if failure == 'second_chunk' else fail_publish):
            with pytest.raises(OSError):
                table.compact(target_rows=2)
        assert table.select(order_by='id') == before
    with Database(uri) as reopened:
        table = reopened.table('t')
        assert table.select(order_by='id') == before
        table.compact(target_rows=2)
        assert table.select(order_by='id') == before
        assert table.validate(deep=True).errors == []


@pytest.mark.parametrize('op,value', [('>', 10), ('>=', 2), ('<', 9), ('<=', '2'),
                                     ('=', 2), ('!=', 10), ('in', [2, 10]),
                                     ('between', [1, 9])])
def test_text_affinity_stats_match_native_sql(tmp_path, op, value):
    """R03：裁剪必须与 SQLite 真实存储及参数亲和性等价，包括混合输入和 NULL。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'},
                         segment=Segment.rows(max_rows=2))
        table.write([{'id': 1, 'v': 2}, {'id': 2, 'v': '10'}, {'id': 3, 'v': None}])
        condition = {'in': 'v IN (?, ?)', 'between': 'v BETWEEN ? AND ?'}.get(op, f'v {op} ?')
        params = value if isinstance(value, list) else [value]
        expected = []
        for ref in table._physical_part_refs(lightweight=True):
            with sqlite3.connect(ref.path) as conn:
                expected.extend(row[0] for row in conn.execute(f'SELECT id FROM data WHERE {condition}', params))
        actual = [row['id'] for row in table.select(where=Case('v', op, value))]
        assert sorted(actual) == sorted(expected)


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('default', ['append', 'ignore', 'replace', 'upsert'])
@pytest.mark.parametrize('mode', ['append', 'ignore', 'replace', 'upsert'])
def test_explicit_mode_overrides_default_conflict(tmp_path, backend, default, mode):
    """R04：每种显式 mode 都优先于表默认 conflict，覆盖完整交叉矩阵。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'}, conflict=default)
        table.write({'id': 1, 'v': '原值'})
        if mode == 'append':
            with pytest.raises(Exception):
                table.write({'id': 1, 'v': '覆盖'}, mode=mode)
        else:
            table.write({'id': 1, 'v': '覆盖'}, mode=mode)
        expected = '覆盖' if mode in ('replace', 'upsert') else '原值'
        assert table.select() == [{'id': 1, 'v': expected}]


def test_rename_invalidates_other_instances_and_process(tmp_path):
    """R05：覆盖重命名后，其他对象读新目标，旧源对象拒绝读取旧 inode。"""
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as first, Database(uri) as second:
        first.table('src', pk=['id'], columns={'id': 'integer'}).write({'id': 1})
        first.table('dst', pk=['id'], columns={'id': 'integer'}).write({'id': 2})
        cached_dst = second.table('dst')
        cached_src = second.table('src')
        assert cached_dst.select() == [{'id': 2}]
        assert cached_src.select() == [{'id': 1}]
        first.rename_table('src', 'dst', overwrite=True)
        assert cached_dst.select() == [{'id': 1}]
        with pytest.raises(UnsafeOperation):
            cached_src.select()
        assert cached_src._db_parts_closed
        script = 'from iosql import Database; import sys;\nwith Database(sys.argv[1]) as db: print(db.table("dst").select())'
        result = subprocess.run([sys.executable, '-c', script, uri], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "'id': 1" in result.stdout


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_rename_invalidates_warmed_handles_in_another_process(tmp_path, backend):
    """R05：进程必须在重命名前已缓存目标，重开库不能替代失效协议验证。"""
    uri = f'{backend}://{tmp_path / "db"}'
    with Database(uri) as db:
        db.table('src', pk=['id'], columns={'id': 'integer'}).write({'id': 1})
        db.table('dst', pk=['id'], columns={'id': 'integer'}).write({'id': 2})
        context = mp.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_cached_rename_reader, args=(child, uri))
        process.start()
        child.close()
        try:
            assert parent.poll(10)
            assert parent.recv() == ([{'id': 1}], [{'id': 2}])
            db.rename_table('src', 'dst', overwrite=True)
            parent.send('published')
            assert parent.poll(10)
            assert parent.recv() == ([{'id': 1}], True)
            process.join(10)
            assert process.exitcode == 0
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                process.join()


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_compact_process_exit_preserves_previous_generation(tmp_path, backend):
    """R02：进程退出没有异常清理机会，仍不能留下部分可见的新代。"""
    uri = f'{backend}://{tmp_path / "db"}'
    with Database(uri) as db:
        db.table('t', pk=['id'], columns={'id': 'integer'}, segment=Segment.rows(max_rows=2)).write(
            [{'id': i} for i in range(4)])
    script = '''
import os, sys
from iosql import Database
with Database(sys.argv[1]) as db:
    table = db.table('t')
    original = table._write_segment_rows
    calls = 0
    def crash(*args, **kwargs):
        global calls
        calls += 1
        if calls == 2:
            os._exit(27)
        return original(*args, **kwargs)
    table._write_segment_rows = crash
    table.compact(target_rows=2)
'''
    result = subprocess.run([sys.executable, '-c', script, uri], capture_output=True, text=True, timeout=15)
    assert result.returncode == 27, result.stderr
    with Database(uri) as db:
        table = db.table('t')
        assert table.select(order_by='id') == [{'id': i} for i in range(4)]
        table.compact(target_rows=2)
        assert table.count_rows() == 4
        assert table.validate(deep=True).ok


def test_validate_checks_committed_generation_membership(tmp_path):
    """R02：即使所有剩余文件自身完整，也要发现发布代缺少成员。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'}, segment=Segment.rows(max_rows=2))
        table.write([{'id': i} for i in range(4)])
        table.compact(target_rows=2)
        part = table._segment_infos()[0]
        table.table_meta_db['_iosql_segments'].execute_params(
            'DELETE FROM _iosql_segments WHERE part_key=?', [part.part_key], commit=True)
        result = table.validate(deep=True)
        assert 'incomplete_generation' in [issue.code for issue in result.errors]


def test_parquet_ordered_stream_at_budget_limit(tmp_path):
    """R06：恰好用满预算时必须返回首行，提前关闭后可继续查询和写入。"""
    with Database(f'parquet://{tmp_path / "db"}', max_process_connections=2) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'}, partition='range(id,width=10)')
        table.write([{'id': 1}, {'id': 11}])
        stream = table.iter_select(order_by='id', timeout_ms=1000, batch_size=1)
        assert next(stream)['id'] == 1
        stream.close()
        assert list(table.iter_select(order_by='id', timeout_ms=1000)) == [{'id': 1}, {'id': 11}]
        table.write({'id': 12})


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('segment', [None, Segment.rows(max_rows=1)])
def test_strict_rejects_whole_batch_before_files_change(tmp_path, backend, segment):
    """R08：批次后部的未知列不能让前部行或新的物理文件先落盘。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'}, schema_policy='strict',
                         partition='range(id,width=10)', segment=segment)
        with pytest.raises(SchemaConflict):
            table.write([{'id': 1}, {'id': 11, 'unknown': '拒绝'}])
        assert table.count_rows() == 0
        table.write({'id': 1})
        before = table.table_parts()
        with pytest.raises(SchemaConflict):
            table.write({'id': 11, 'unknown': '拒绝'})
        assert table.table_parts() == before
        assert table.select() == [{'id': 1}]


def test_unordered_limit_only_reads_requested_rows(tmp_path):
    """R09：检查物理读行数，OR 不能重复返回同一物理行。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'}, partition='range(id,width=10)')
        table.write([{'id': i} for i in range(40)])
        query = LowTable.query
        reads = []

        def observe(self, sql, *args, **kwargs):
            rows = query(self, sql, *args, **kwargs)
            if self.table_name == 'data':
                reads.append(len(rows))
            return rows

        with patch.object(LowTable, 'query', observe):
            assert len(table.select(where=Case('id', '>=', 0) + Case('id', '<', 20), limit=3)) == 3
        assert reads == [3]


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_segment_ignore_writes_batches(tmp_path, backend):
    """R11：批内重复键保留首值，新键一次写入，不能逐行重算统计。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'},
                         segment=Segment.rows(max_rows=1000))
        table.write({'id': 0, 'v': '已有'})
        with patch.object(table, '_do_insert_part', wraps=table._do_insert_part) as writes:
            table.write([{'id': i, 'v': '首值'} for i in range(101)] + [{'id': 1, 'v': '末值'}], mode='ignore')
        assert writes.call_count == 1
        assert table.count_rows() == 101
        assert table.select(where=Case('id', '=', 1)) == [{'id': 1, 'v': '首值'}]


def test_logical_index_survives_new_files_compact_and_migration(tmp_path):
    """R16：重开后新增文件、compact 和迁移都继承同一索引；删除定义也持久化。"""
    uri = f'sqlite://{tmp_path / "db"}'
    with Database(uri) as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer', 'v': 'text'},
                         partition='range(id,width=10)', segment=Segment.rows(max_rows=1))
        table.write({'id': 1, 'v': 'a'})
        table.create_index('v')
    with Database(uri) as db:
        table = db.table('t')
        table.write([{'id': 2, 'v': 'b'}, {'id': 11, 'v': 'c'}])
        table.compact(target_rows=1)
        for ref in table._physical_part_refs(lightweight=True):
            with sqlite3.connect(ref.path) as conn:
                assert 'data_v_index' in [row[1] for row in conn.execute('PRAGMA index_list(data)')]
                assert any('data_v_index' in row[3] for row in conn.execute(
                    'EXPLAIN QUERY PLAN SELECT * FROM data WHERE v=?', ['a']))
        target_uri = f'sqlite://{tmp_path / "copy"}'
        table.save_as(target_uri, verify='row-count')
        with Database(target_uri) as copied:
            for ref in copied.table('t')._physical_part_refs(lightweight=True):
                with sqlite3.connect(ref.path) as conn:
                    assert 'data_v_index' in [row[1] for row in conn.execute('PRAGMA index_list(data)')]
        table.drop_index('v')
        assert table._logical_indexes() == []
        table.write({'id': 21, 'v': 'd'})
        for ref in table._physical_part_refs([20], lightweight=True):
            with sqlite3.connect(ref.path) as conn:
                assert 'data_v_index' not in [row[1] for row in conn.execute('PRAGMA index_list(data)')]


@pytest.mark.parametrize('method', ['select', 'query', 'iter_select', 'iter_select_batches'])
def test_unknown_projection_raises(tmp_path, method):
    """R17：不同读取入口均拒绝拼错的投影列。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1})
        with pytest.raises(BadArgument):
            if method == 'query':
                table.query(columns=['typo']).to_list()
            else:
                list(getattr(table, method)(['typo']))


@pytest.mark.parametrize('method', ['select', 'iter_select_batches'])
def test_empty_declared_table_validates_projection(tmp_path, method):
    """R17：同一错误不能因为表暂时没有物理文件而被隐藏。"""
    with Database(f'sqlite://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'})
        with pytest.raises(BadArgument):
            list(getattr(table, method)(['typo']))


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_invalid_row_is_rejected_before_partial_write(tmp_path, backend):
    """整批字段校验之前先验证行形态，不能在冷路径抛出不明确的 Python 异常。"""
    with Database(f'{backend}://{tmp_path / "db"}') as db:
        table = db.table('t', pk=['id'], columns={'id': 'integer'}, schema_policy='strict')
        with pytest.raises(BadArgument):
            table.write([{'id': 1}, 2])
        assert table.count_rows() == 0
