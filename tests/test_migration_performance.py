"""迁移加速路径的数据、冲突和资源语义。"""

import builtins
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from iosql import Database, Partition
from iosql._migration_progress import counting, observe
from iosql.exception import BadArgument, MigrationVerificationError, UnsafeOperation
from iosql.utils.storage_identity import is_iosql_table


@pytest.mark.parametrize('arrow_enabled', [False, True])
def test_bulk_append_preserves_values_and_rejects_duplicate_keys(tmp_path, monkeypatch, arrow_enabled):
    import iosql.engines.parquet._bulk as bulk
    import iosql.engines.parquet.table as parquet_table

    if arrow_enabled:
        pytest.importorskip('pyarrow')
    else:
        monkeypatch.setattr(bulk, '_optional_arrow', lambda: None)
    attempts = []
    original_insert = parquet_table._try_bulk_insert

    def tracked_insert(*args):
        result = original_insert(*args)
        attempts.append(result)
        return result

    monkeypatch.setattr(parquet_table, '_try_bulk_insert', tracked_insert)
    uri = f'parquet://{tmp_path / "target"}'
    rows = [
        {'id': i, 'value': -0.0 if i == 0 else float(i) / 10,
         'label': None if i % 5 == 0 else '汉字\x00' + str(i),
         'payload': None if i % 7 == 0 else bytes([0, i % 256])}
        for i in range(140)
    ]
    with Database(uri) as db:
        table = db.table('records', pk=['id'], columns={
            'id': 'integer', 'value': 'float', 'label': 'text', 'payload': 'blob',
        })
        assert table.write(rows).rows_written == len(rows)
        assert attempts == [True]
        actual = table.select(order_by='id')
        assert actual == rows
        assert math.copysign(1, actual[0]['value']) == -1
        path = Path(table.segments()[0]['path'])
        before = path.read_bytes()

        batch_with_duplicate = [dict(row, id=row['id'] + 200) for row in rows]
        batch_with_duplicate[-1]['id'] = batch_with_duplicate[0]['id']
        with pytest.raises(BadArgument, match='主键'):
            table.write(batch_with_duplicate)
        assert path.read_bytes() == before
        assert table.select(order_by='id') == rows

        batch_with_existing = [dict(row, id=row['id'] + 200) for row in rows]
        batch_with_existing[-1]['id'] = rows[0]['id']
        with pytest.raises(BadArgument, match='主键'):
            table.write(batch_with_existing)
        assert table.select(order_by='id') == rows


@pytest.mark.parametrize('arrow_enabled', [False, True])
def test_bulk_fallback_preserves_nan_and_noncanonical_values(tmp_path, monkeypatch, arrow_enabled):
    import iosql.engines.parquet._bulk as bulk
    import iosql.engines.parquet.table as parquet_table

    if arrow_enabled:
        pytest.importorskip('pyarrow')
    else:
        monkeypatch.setattr(bulk, '_optional_arrow', lambda: None)
    attempts = []
    original_insert = parquet_table._try_bulk_insert

    def tracked_insert(*args):
        result = original_insert(*args)
        attempts.append(result)
        return result

    monkeypatch.setattr(parquet_table, '_try_bulk_insert', tracked_insert)
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'float', 'label': 'text'})
        rows = [{'id': i, 'value': float(i), 'label': str(i)} for i in range(140)]
        rows[-1]['value'] = float('nan')
        rows[-1]['label'] = 123
        table.write(rows)
        assert attempts == [False]
        actual = table.select(order_by='id')
        assert math.isnan(actual[-1]['value'])
        assert actual[-1]['label'] == '123'


def test_columnar_migration_does_not_require_pyarrow(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def without_arrow(name, *args, **kwargs):
        if name == 'pyarrow' or name.startswith('pyarrow.'):
            raise ModuleNotFoundError('pyarrow 未安装')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_arrow)
    with Database(f'sqlite://{tmp_path / "source"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'float'})
        rows = [{'id': i, 'value': float(i)} for i in range(140)]
        table.write(rows)
        result = table.migrate_to(f'parquet://{tmp_path / "target"}', verify='row-count')
        assert result.published and result.verified
    with Database(f'parquet://{tmp_path / "target"}') as db:
        assert db['records'].select(order_by='id') == rows


@pytest.mark.parametrize('arrow_enabled', [False, True])
def test_bulk_preserves_ignore_and_replace_conflicts(tmp_path, monkeypatch, arrow_enabled):
    import iosql.engines.parquet._bulk as bulk

    if arrow_enabled:
        pytest.importorskip('pyarrow')
    else:
        monkeypatch.setattr(bulk, '_optional_arrow', lambda: None)
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'text'})
        table.write([{'id': i, 'value': 'old'} for i in range(140)])
        table.write([{'id': i, 'value': 'new'} for i in range(140)], mode='ignore')
        assert all(row['value'] == 'old' for row in table.select())
        table.write([{'id': i, 'value': 'new'} for i in range(140)], mode='replace')
        assert all(row['value'] == 'new' for row in table.select())


def test_interleaved_count_includes_empty_file(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source_uri = f'sqlite://{source}'
    with Database(source_uri) as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'},
                         partition=Partition.range('id', every=10))
        table.write([{'id': 1}, {'id': 11}])
        first = Path(table._get_part_dbfile(0))
    with sqlite3.connect(first) as conn:
        conn.execute('DELETE FROM data')
    events = []
    with Database(source_uri) as db, observe(lambda event, **details: events.append((event, details))):
        result = db.migrate_to(f'parquet://{target}', batch_size=1, verify='row-count')
    assert result.published and result.verified
    assert result.source_rows == result.target_rows == result.rows_copied == 1
    counts = [data for event, data in events
              if event == 'count_progress' and data['phase'] == 'source']
    assert [(item['done'], item['total'], item['rows']) for item in counts] == [(1, 2, 0), (2, 2, 1)]
    with Database(f'parquet://{target}') as db:
        assert db['records'].select() == [{'id': 11}]


@pytest.mark.parametrize('drop_all', [False, True])
def test_interleaved_count_does_not_hide_lost_source_rows(tmp_path, monkeypatch, drop_all):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}, {'id': 3}])
        original = table._iter_part_query_batches

        def truncated(*args, **kwargs):
            batches = original(*args, **kwargs)
            with closing(batches):
                for batch in batches:
                    if not drop_all:
                        yield batch[:-1]

        monkeypatch.setattr(table, '_iter_part_query_batches', truncated)
        error = UnsafeOperation if drop_all else MigrationVerificationError
        with pytest.raises(error):
            table.migrate_to(f'parquet://{target}', batch_size=10, verify='row-count')
    assert not target.exists()


def test_custom_source_count_is_respected(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}])
        monkeypatch.setattr(table, 'count_rows', lambda: 3)
        with pytest.raises(MigrationVerificationError):
            table.migrate_to(f'parquet://{target}', verify='row-count')
    assert not target.exists()


def test_missing_source_partition_does_not_publish_target(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'},
                         partition=Partition.range('id', every=10))
        table.write([{'id': 1}, {'id': 11}])
        missing = Path(table._get_part_dbfile(10))
    missing.unlink()
    with Database(f'sqlite://{source}') as db:
        with pytest.raises(UnsafeOperation):
            db['records'].migrate_to(f'parquet://{target}', batch_size=1, verify='row-count')
    assert not target.exists()


def test_interleaved_count_reads_committed_wal(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'})
        table.write({'id': 1})
        path = Path(table._get_part_dbfile(None))
        with closing(sqlite3.connect(path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('INSERT INTO data (id) VALUES (2)')
            writer.commit()
            assert path.with_name(path.name + '-wal').stat().st_size > 0
            result = table.migrate_to(f'parquet://{target}', batch_size=1, verify='row-count')
        assert result.source_rows == result.target_rows == 2
    with Database(f'parquet://{target}') as db:
        assert db['records'].select(order_by='id') == [{'id': 1}, {'id': 2}]


def test_interleaved_count_with_single_connection_budget(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'sqlite://{source}', max_process_connections=1) as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'n': 'text'},
                         partition=Partition.range('id', every=10))
        rows = [{'id': 1, 'n': 'a'}, {'id': 2, 'n': 'b'}, {'id': 11, 'n': 'c'}]
        table.write(rows)
        result = table.migrate_to(f'parquet://{target}', batch_size=2, verify='row-count')
        assert result.published and result.verified
        assert result.source_rows == result.target_rows == 3
    with Database(f'parquet://{target}') as db:
        assert db['records'].select(order_by='id') == rows


@pytest.mark.parametrize('mode', ['ignore', 'upsert', 'replace'])
def test_interleaved_count_preserves_existing_parquet_conflict_modes(tmp_path, mode):
    source, target = tmp_path / 'source', tmp_path / 'target'
    with Database(f'parquet://{target}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'text'})
        table.write({'id': 1, 'value': 'old'})
    with Database(f'sqlite://{source}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'text'})
        table.write([{'id': 1, 'value': 'new'}, {'id': 2, 'value': 'added'}])
        result = table.migrate_to(f'parquet://{target}', batch_size=1, mode=mode)
        assert result.published and result.source_rows == result.target_rows == 2
        assert result.rows_written == (None if mode == 'ignore' else 2)
    with Database(f'parquet://{target}') as db:
        value = 'old' if mode == 'ignore' else 'new'
        assert db['records'].select(order_by='id') == [{'id': 1, 'value': value}, {'id': 2, 'value': 'added'}]


def test_unchanged_large_partitions_avoid_cross_partition_fragments(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    rows = [{'id': i} for i in list(range(7)) + list(range(10, 17))]
    with Database(f'sqlite://{source}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'},
                         partition=Partition.range('id', every=10))
        table.write(rows)
        result = table.migrate_to(f'parquet://{target}', batch_size=4, verify='row-count')
        assert result.published and result.verified and result.source_rows == len(rows)
    with Database(f'parquet://{target}') as db:
        table = db['records']
        assert table.select(order_by='id') == rows
        for part in (0, 10):
            manifest = Path(table._get_part_dbfile(part) + '.manifest.json')
            assert len(json.loads(manifest.read_text())['files']) == 2


def test_deep_parquet_validation_reuses_connection_and_rejects_damage(tmp_path, monkeypatch):
    duckdb = pytest.importorskip('duckdb')
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'},
                         partition=Partition.range('id', every=10))
        table.write([{'id': 1}, {'id': 11}, {'id': 21}])
        table.write({'id': 2})  # 同一逻辑文件包含多个已发布 Parquet 数据片。
        table_path = Path(table.table_path)
        data_path = Path(table._get_part_dbfile(10))
        connect = duckdb.connect
        connections = []

        def tracked_connect(*args, **kwargs):
            connections.append(1)
            return connect(*args, **kwargs)

        monkeypatch.setattr(duckdb, 'connect', tracked_connect)
        assert is_iosql_table(table_path, backend='parquet', deep=True)
        assert len(connections) == 1
        connections.clear()
        events = []
        with observe(lambda event, **details: events.append((event, details))), counting('target', 'records'):
            assert table._count_migration_rows() == 4
        assert len(connections) == 1
        counts = [data for event, data in events if event == 'count_progress']
        assert [(item['done'], item['total'], item['rows']) for item in counts] == [(1, 3, 2), (2, 3, 3), (3, 3, 4)]
        data_path.write_bytes(b'corrupt')
        assert not is_iosql_table(table_path, backend='parquet', deep=True)
        with pytest.raises(UnsafeOperation):
            table._count_migration_rows()


@pytest.mark.parametrize('row_count', [0, 2])
def test_parquet_data_file_validation_counts_without_shared_connection(tmp_path, row_count):
    duckdb = pytest.importorskip('duckdb')
    from iosql.utils.storage_identity import _valid_data_file

    path = tmp_path / 'records.parquet'
    with closing(duckdb.connect()) as conn:
        conn.sql(f'SELECT range AS id FROM range({row_count})').write_parquet(str(path))
    assert _valid_data_file(path, backend='parquet', expected_rows=row_count)
    assert not _valid_data_file(path, backend='parquet', expected_rows=row_count + 1)
