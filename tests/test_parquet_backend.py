"""Parquet 后端的持久化、查询、维护和迁移契约。"""

import builtins
import hashlib
import threading
from pathlib import Path

import pytest

from iosql import Database, Partition, Segment
from iosql.exception import BadArgument, SchemaConflict, TableSpecConflict, UnsafeOperation
from iosql.utils.storage_identity import is_iosql_table

duckdb = pytest.importorskip('duckdb')


def _uri(tmp_path, name='parquet.iosql', backend='parquet'):
    return f'{backend}://{tmp_path / name}'


def _table(db, **kwargs):
    return db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'float'}, **kwargs)


def test_real_parquet_empty_contract_and_reopen(tmp_path):
    uri = _uri(tmp_path)
    with Database(uri) as db:
        table = _table(db, description='归档数据', column_descriptions={'value': '数值'})
        assert db.table_names() == ['records']
        assert table.count_rows() == 0
        assert table.select() == []
    with Database(uri) as db:
        table = db.table('records')
        assert table.describe()['description'] == '归档数据'
        result = table.write([{'id': 1, 'value': 2.5}])
        assert result.rows_seen == result.rows_written == 1
        assert table.select() == [{'id': 1, 'value': 2.5}]
        path = Path(table.segments()[0]['path'])
        assert path.suffix == '.parquet'
        assert path.read_bytes()[:4] == path.read_bytes()[-4:] == b'PAR1'
        with duckdb.connect() as conn:
            assert conn.read_parquet(str(path)).fetchall() == [(1, 2.5)]
        assert is_iosql_table(table.table_path, backend='parquet', deep=True)
        assert not is_iosql_table(table.table_path)


def test_partition_segment_query_pruning_and_late_data(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = db.timeseries(
            'bars', time='ts', pk=['symbol', 'ts'],
            columns={'symbol': 'text', 'ts': 'integer', 'value': 'float'},
            partition_every='1day', segment=Segment.rows(max_rows=2),
        )
        rows = [
            {'symbol': 'A', 'ts': ts, 'value': float(ts)}
            for ts in [1, 2, 3, 4, 86_400_001]
        ]
        result = table.write(rows)
        assert result.rows_written == 5
        assert result.partitions_created == [0, 86_400_000]
        assert [segment['row_count'] for segment in table.segments()] == [2, 2, 1]
        query = table.query(symbols='A', start=3, end=5, order_by='ts')
        assert query.to_list() == rows[2:4]
        plan = query.explain()
        assert plan.scanned_partitions == [0]
        assert plan.skipped_partitions == [86_400_000]
        assert len(plan.scanned_segments) == len(plan.skipped_segments) == 1
        table.write({'symbol': 'B', 'ts': 0, 'value': 9.0})
        assert table.count_rows() == 6
        assert table.latest(symbols='A', as_of=4).one() == rows[3]
        assert table.validate(deep=True).ok


@pytest.mark.parametrize('kind', ['range', 'calendar'])
def test_partition_types(tmp_path, kind):
    with Database(_uri(tmp_path)) as db:
        if kind == 'range':
            table = _table(db, partition=Partition.range('id', every=10))
            rows = [{'id': 1, 'value': 1.0}, {'id': 11, 'value': 2.0}]
            where = {'field': 'id', 'op': '>=', 'value': 10}
        else:
            table = db.table('records', pk=['id', 'date'], columns={
                'id': 'integer', 'date': 'text', 'value': 'float',
            }, partition=Partition.calendar('date', every='1day', timezone='Asia/Shanghai'))
            rows = [{'id': 1, 'date': '2026-10-01', 'value': 1.0},
                    {'id': 2, 'date': '2026-10-02', 'value': 2.0}]
            where = {'field': 'date', 'op': '==', 'value': '2026-10-02'}
            table.write(rows)
            assert table.query(date='2026-10-02').to_list() == rows[1:]
            assert len(table.table_parts()) == 2
            assert table.explain_select(where=where).scanned_partitions == [table.table_parts()[1]]
            return
        table.write(rows)
        assert table.select(where=where) == rows[1:]
        assert len(table.explain_select(where=where).scanned_partitions) == 1


def test_conflict_modes_and_failed_batch_preserves_file(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = _table(db)
        table.write({'id': 1, 'value': 1.0})
        path = Path(table.segments()[0]['path'])
        before = path.read_bytes()
        with pytest.raises(BadArgument, match='主键'):
            table.write([{'id': 2, 'value': 2.0}, {'id': 1, 'value': 3.0}])
        assert path.read_bytes() == before
        assert table.count_rows() == 1
        table.write([{'id': 1, 'value': 3.0}, {'id': 2, 'value': 2.0}], mode='ignore')
        assert table.select(order_by='id') == [{'id': 1, 'value': 1.0}, {'id': 2, 'value': 2.0}]
        table.write({'id': 1, 'value': 4.0}, mode='upsert')
        table.write({'id': 2}, mode='replace')
        assert table.select(order_by='id') == [{'id': 1, 'value': 4.0}, {'id': 2, 'value': None}]
        table.upsert_all([{'id': 1, 'value': 5.0}], pk=['id'])
        assert table.select(where='id == 1')[0]['value'] == 5.0


@pytest.mark.parametrize('segment', [None, Segment.rows(max_rows=2)])
def test_schema_evolution_projects_null_without_rewriting_old_files(tmp_path, segment):
    with Database(_uri(tmp_path)) as db:
        table = _table(db, partition=Partition.range('id', every=10), segment=segment)
        table.write([{'id': 1, 'value': None}, {'id': 2, 'value': 2.0}])
        path = Path(table.segments()[0]['path'])
        digest = hashlib.sha256(path.read_bytes()).digest()
        result = table.write({'id': 11, 'value': 11.0, 'label': '中文', 'payload': b'\x00\xff'})
        assert result.schema_changed
        assert hashlib.sha256(path.read_bytes()).digest() == digest
        assert table.spec().columns['payload'] == 'blob'
        rows = table.select(order_by='id')
        assert rows[0]['label'] is None and rows[0]['payload'] is None
        assert rows[-1]['label'] == '中文' and rows[-1]['payload'] == b'\x00\xff'
        assert list(table.iter_select(order_by='id', batch_size=1)) == rows
        assert table.validate(deep=True).ok


def test_strict_schema_type_and_binding_conflicts(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = _table(db, schema_policy='strict')
        table.write({'id': 1, 'value': 1.0})
        with pytest.raises(SchemaConflict):
            table.write({'id': 2, 'value': 2.0, 'extra': 'no'})
        with pytest.raises(BadArgument, match='类型冲突'):
            table.write({'id': 2, 'value': 'no'})
        with pytest.raises(SchemaConflict):
            table.write({'id': 2, 'value': 2.0}, column_type={'value': 'text'})
        with pytest.raises(TableSpecConflict):
            db.table('records', pk=['value'], columns={'id': 'integer', 'value': 'float'})
        assert table.count_rows() == 1


@pytest.mark.parametrize('order', ['value asc, id desc', 'value desc, id asc'])
def test_ordered_stream_projection_nulls_and_limit(tmp_path, order, monkeypatch):
    with Database(_uri(tmp_path)) as db:
        table = _table(db, partition=Partition.range('id', every=2))
        table.write([{'id': i, 'value': None if i % 2 else float(i)} for i in range(7)])
        expected = table.select(fields=['id'], order_by=order, limit=5)
        monkeypatch.setattr(table, 'select', lambda *a, **k: pytest.fail('流式读取不能调用 select'))
        assert list(table.iter_select(columns=['id'], order_by=order, limit=5, batch_size=2)) == expected
        tuples = list(table.iter_select(columns=['id'], order_by=order, limit=5, to_dict=False))
        assert [tuple(row) for row in tuples] == [(row['id'],) for row in expected]


def test_buffered_delete_compact_and_cleanup(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = _table(db, segment=Segment.rows(max_rows=3, target_rows=2))
        with table.buffered_writer(max_rows=2) as writer:
            writer.append([{'id': i, 'value': float(i)} for i in range(7)])
        assert table.count_rows() == 7
        assert table.delete('id >= 5') == 2
        expected = table.select(order_by='id')
        compact = table.compact(target_rows=2)
        assert compact.rows_read == compact.rows_written == 5
        assert table.select(order_by='id') == expected
        assert table.validate(deep=True, include_obsolete=True).ok
        assert table.cleanup_obsolete(dry_run=False).files_deleted > 0
        assert table.validate(deep=True).ok
        table.truncate(confirm=True)
        assert table.count_rows() == 0
        table.write({'id': 10, 'value': 10.0})
        assert table.count_rows() == 1


@pytest.mark.parametrize('segment', [None, Segment.rows(max_rows=2)])
@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_damaged_files_raise_and_validate_reports(tmp_path, segment, damage):
    with Database(_uri(tmp_path)) as db:
        table = _table(db, segment=segment)
        table.write({'id': 1, 'value': 1.0})
        path = Path(table.segments()[0]['path'])
        if damage == 'missing':
            path.unlink()
        else:
            path.write_bytes(b'corrupt')
        with pytest.raises(UnsafeOperation):
            table.select()
        assert not table.validate(deep=True).ok
        with pytest.raises(UnsafeOperation):
            table.write({'id': 2, 'value': 2.0})


def test_publish_failure_preserves_original_and_cleans_temporary(tmp_path, monkeypatch):
    with Database(_uri(tmp_path)) as db:
        table = _table(db)
        table.write({'id': 1, 'value': 1.0})
        path = Path(table.segments()[0]['path'])
        before = path.read_bytes()
        def fail(*args):
            raise OSError('模拟替换失败')
        monkeypatch.setattr('iosql.engines.parquet.table.os.replace', fail)
        with pytest.raises(OSError, match='模拟'):
            table.write({'id': 2, 'value': 2.0})
        assert path.read_bytes() == before
        assert not list(path.parent.glob('.iosql-*.tmp'))
        assert table.count_rows() == 1


def test_wrong_backend_and_plain_directory_are_rejected(tmp_path):
    with Database(_uri(tmp_path)) as db:
        _table(db).write({'id': 1, 'value': 1.0})
    with pytest.raises(UnsafeOperation, match='存储类型不匹配'):
        Database(_uri(tmp_path, backend='sqlite'))
    with Database(_uri(tmp_path, 'sqlite.iosql', 'sqlite')) as db:
        _table(db).write({'id': 1, 'value': 1.0})
    with pytest.raises(UnsafeOperation, match='存储类型不匹配'):
        Database(_uri(tmp_path, 'sqlite.iosql'))
    plain = tmp_path / 'plain.iosql' / 'records'
    plain.mkdir(parents=True)
    (plain / 'keep.txt').write_text('业务文件')
    with pytest.raises(UnsafeOperation):
        Database(_uri(tmp_path, 'plain.iosql'))
    assert not (plain / 'meta.sqlite3').exists()


def test_missing_default_dependency_error_before_files_are_created(tmp_path, monkeypatch):
    original_import = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'duckdb':
            raise ImportError('missing')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing)
    with Database(_uri(tmp_path)) as db:
        with pytest.raises(BadArgument, match=r'pip install --upgrade iosql'):
            db.table('records')
    assert not (tmp_path / 'parquet.iosql').exists()


@pytest.mark.parametrize('operation', [
    lambda t: t.write({'id': 1}, commit=False),
    lambda t: t.pragma('journal_mode'),
    lambda t: t.create_index('value'),
    lambda t: t.drop_index('value'),
])
def test_unsupported_operations_are_explicit(tmp_path, operation):
    with Database(_uri(tmp_path)) as db:
        table = _table(db)
        with pytest.raises(BadArgument):
            operation(table)
        assert table.count_rows() == 0


def test_sqlite_parquet_roundtrip_repartition_and_dry_run(tmp_path):
    parquet_uri = _uri(tmp_path)
    with Database(_uri(tmp_path, 'sqlite.iosql', 'sqlite')) as source:
        table = _table(source, segment=Segment.rows(max_rows=2))
        rows = [{'id': i, 'value': float(i)} for i in range(5)]
        table.write(rows)
        preview = source.migrate_to(parquet_uri, dry_run=True, verify='row-count', batch_size=2)
        assert preview.source_rows == 5
        assert not (tmp_path / 'parquet.iosql').exists()
        assert source.migrate_to(parquet_uri, verify='row-count', batch_size=2).verified
    with Database(parquet_uri) as source:
        table = source.table('records')
        assert table.select(order_by='id') == rows
        result = source.migrate_to(_uri(tmp_path, 'back.iosql', 'sqlite'), verify='row-count')
        assert result.verified
        repartitioned = table.repartition_to(
            _uri(tmp_path, 'range.iosql'), spec=table.spec().with_partition(Partition.range('id', every=2)), batch_size=2,
        )
        assert repartitioned.rows_copied == 5
        assert table.count_rows() == 5
    with Database(_uri(tmp_path, 'back.iosql', 'sqlite')) as target:
        assert target.table('records').select(order_by='id') == rows


@pytest.mark.parametrize('backend', ['parquet', 'sqlite'])
@pytest.mark.parametrize('nested', [False, True])
def test_migration_rejects_self_or_nested_directory(tmp_path, backend, nested):
    with Database(_uri(tmp_path)) as db:
        table = _table(db)
        table.write({'id': 1, 'value': 1.0})
        target = f'{backend}://{tmp_path / "parquet.iosql"}' + ('/nested' if nested else '')
        with pytest.raises(BadArgument):
            db.migrate_to(target, dry_run=True)
        with pytest.raises(BadArgument):
            table.migrate_to(target, dry_run=True)
        with pytest.raises(BadArgument):
            table.repartition_to(target)
        assert table.count_rows() == 1


def test_rename_table_and_stream_deadline(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = _table(db)
        table.write([{'id': i, 'value': float(i)} for i in range(3)])
        table.rename('renamed')
        assert db.table_names() == ['renamed']
        assert db.table('renamed') is table
        assert table.count_rows() == 3
        stream = table.iter_select_batches(batch_size=1, timeout_ms=1)
        with pytest.raises(TimeoutError):
            list(stream)


def test_cli_migration_both_directions(tmp_path):
    from typer.testing import CliRunner
    from iosql.cli import app
    sqlite_uri = _uri(tmp_path, 'sqlite.iosql', 'sqlite')
    parquet_uri = _uri(tmp_path)
    with Database(sqlite_uri) as source:
        _table(source).write({'id': 1, 'value': 1.0})
    runner = CliRunner()
    for source, target in [(sqlite_uri, parquet_uri),
                           (parquet_uri, _uri(tmp_path, 'back.iosql', 'sqlite'))]:
        result = runner.invoke(app, ['migrate', '--source', source, '--target', target,
                                     '--verify', 'row-count'])
        assert result.exit_code == 0, result.output
    with Database(_uri(tmp_path, 'back.iosql', 'sqlite')) as target:
        assert target.table('records').count_rows() == 1


def test_segment_statistics_use_converted_stored_values(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = db.table('typed', pk=['id'], columns={
            'id': 'integer', 'integer_value': 'integer', 'text_value': 'text',
        }, segment=Segment.rows(max_rows=2))
        table.write({'id': 1, 'integer_value': 1.7, 'text_value': 123})
        assert table.select(where='integer_value == 2')[0]['integer_value'] == 2
        assert table.select(where="text_value == '123'")[0]['text_value'] == '123'
        assert table.validate(deep=True).ok


def test_open_stream_blocks_other_writer_and_rejects_same_thread_mutation(tmp_path):
    uri = _uri(tmp_path)
    with Database(uri) as reader_db, Database(uri) as writer_db:
        table = _table(reader_db)
        table.write([{'id': i, 'value': float(i)} for i in range(3)])
        other = writer_db.table('records')
        stream = table.iter_select(batch_size=1)
        assert next(stream)['id'] == 0
        with pytest.raises(UnsafeOperation, match='读取流'):
            table.write({'id': 4, 'value': 4.0})
        with pytest.raises(UnsafeOperation, match='读取流'):
            other.write({'id': 4, 'value': 4.0})
        with pytest.raises(UnsafeOperation, match='租约'):
            table.close()
        started, finished = threading.Event(), threading.Event()
        failures = []
        def write():
            started.set()
            try:
                other.write({'id': 4, 'value': 4.0})
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=write)
        thread.start()
        try:
            assert started.wait(5)
            assert not finished.wait(0.05)
            assert [row['id'] for row in stream] == [1, 2]
        finally:
            stream.close()
            thread.join(5)
        assert finished.is_set()
        assert not failures
        assert table.count_rows() == 4


def test_ordered_stream_connection_limit_can_be_configured(tmp_path):
    with Database(_uri(tmp_path), max_ordered_read_connections=2) as db:
        table = _table(db, partition=Partition.range('id', every=1))
        table.write([{'id': i, 'value': float(i)} for i in range(3)])
        with pytest.raises(BadArgument, match='max_ordered_read_connections'):
            list(table.iter_select(order_by='id'))
    with Database(_uri(tmp_path), max_ordered_read_connections=3) as db:
        assert len(list(db.table('records').iter_select(order_by='id'))) == 3


def test_bad_fields_values_and_unsupported_low_level_constraints(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = _table(db)
        table.write({'id': 1, 'value': 1.0})
        with pytest.raises(BadArgument):
            table.select(fields=['missing'])
        with pytest.raises(BadArgument):
            list(table.iter_select(fields=['missing']))
        with pytest.raises(BadArgument):
            table.insert_all([{'id': 2, 'value': 2.0}], unique={'value': True})
        with pytest.raises(BadArgument, match='主键'):
            table.write({'id': None, 'value': 2.0})
        with pytest.raises(BadArgument):
            table.write({'value': 2.0})
        with pytest.raises(BadArgument):
            table.write([1, 2])
        assert table.count_rows() == 1
        with pytest.raises(BadArgument, match='列类型'):
            db.table('unsupported', pk=['id'], columns={'id': 'integer; drop table data'})


def test_partition_type_conversion_cannot_change_routing(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = _table(db, partition=Partition.range('id', every=1))
        with pytest.raises(BadArgument, match='分区'):
            table.write({'id': 1.7, 'value': 1.0})
        with pytest.raises(BadArgument, match='分区'):
            table.write({'id': float('inf'), 'value': 1.0})
        assert table.count_rows() == 0
