"""Parquet 路径、完整集合、浮点排序与无新增写入的回归。"""

import json
import math
import os
from pathlib import Path

import pytest

from iosql import Database, Partition, Segment
from iosql.exception import UnsafeOperation
from iosql.utils.storage_identity import _valid_data_file, is_iosql_table

duckdb = pytest.importorskip('duckdb')


@pytest.mark.parametrize('location', ['database', 'table'])
@pytest.mark.parametrize(('special', 'neighbor'), [
    ('db[1]', 'db1'), ('db*', 'db_other'), ('db?', 'db1'),
    ('db[', 'dbx'), ('db]', 'dbx'), ('db[*?]', 'db1'),
])
def test_literal_paths_never_scan_or_modify_neighbors(tmp_path, location, special, neighbor):
    if os.name == 'nt' and any(char in special for char in '*?'):
        pytest.skip('Windows 文件名不允许星号或问号')
    left_root = tmp_path / (special if location == 'database' else 'source')
    right_root = tmp_path / (neighbor if location == 'database' else 'source')
    left_name, right_name = (special, neighbor) if location == 'table' else ('records', 'records')
    with Database.create(left_root, engine='parquet') as left_db:
        right_db = left_db if left_root == right_root else Database.create(right_root, engine='parquet')
        try:
            left = left_db.table(left_name, pk=['id'], columns={'id': 'integer', 'value': 'text'})
            right = right_db.table(right_name, pk=['id'], columns={'id': 'integer', 'value': 'text'})
            left.write({'id': 1, 'value': 'left'})
            right.write({'id': 2, 'value': 'right'})
            assert left.select() == [{'id': 1, 'value': 'left'}]
            left.write({'id': 2, 'value': 'left-2'})
            assert left._count_migration_rows() == 2
            assert is_iosql_table(left.table_path, backend='parquet', deep=True)
            assert list(left.iter_select(columns=['id'], order_by='id', batch_size=1)) == [{'id': 1}, {'id': 2}]
            left.write({'id': 1, 'value': 'updated'}, mode='replace')
            assert left.delete('id == 2') == 1
            assert left.select() == [{'id': 1, 'value': 'updated'}]
            assert right.select() == [{'id': 2, 'value': 'right'}]
            result = left_db.migrate_to(f'parquet://{tmp_path / "copy[1]"}', verify='row-count', batch_size=1)
            assert result.published and result.verified
        finally:
            if right_db is not left_db:
                right_db.close()


@pytest.mark.parametrize('source_backend', ['sqlite', 'parquet'])
def test_small_migration_batches_validate_complete_segment(tmp_path, source_backend):
    rows = [{'id': i} for i in range(3)]
    with Database(f'{source_backend}://{tmp_path / "source"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'}, segment=Segment.rows(max_rows=10))
        table.write(rows)
        result = db.migrate_to(f'parquet://{tmp_path / "target"}', batch_size=1, verify='row-count')
        assert result.published and result.verified
    with Database(tmp_path / 'target') as db:
        table = db.table('records')
        assert table.select(order_by='id') == rows
        assert table.validate(deep=True).ok
        assert is_iosql_table(table.table_path, backend='parquet', deep=True)


@pytest.mark.parametrize('damage', [
    'missing', 'corrupt', 'duplicate', 'traversal', 'format', 'manifest_symlink', 'directory_symlink',
])
def test_complete_set_validation_rejects_damaged_fragments(tmp_path, damage):
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'}, segment=Segment.rows(max_rows=10))
        for i in range(3):
            table.write({'id': i})
        path = table.segments()[0]['path']
        assert _valid_data_file(path, backend='parquet', expected_rows=3)
        assert not _valid_data_file(path, backend='parquet', expected_rows=1)
        assert is_iosql_table(table.table_path, backend='parquet', deep=True)
        manifest = Path(path + '.manifest.json')
        payload = json.loads(manifest.read_text())
        fragment = Path(table._parquet_files(path)[-1])
        if damage == 'missing':
            fragment.unlink()
        elif damage == 'corrupt':
            fragment.write_bytes(b'corrupt')
        elif damage == 'manifest_symlink':
            manifest.unlink()
            manifest.symlink_to(tmp_path / 'missing-manifest')
        elif damage == 'directory_symlink':
            directory = fragment.parent
            moved = directory.with_name('moved-parts')
            directory.rename(moved)
            directory.symlink_to(moved, target_is_directory=True)
        else:
            if damage == 'duplicate':
                payload['files'].append(payload['files'][-1])
            elif damage == 'traversal':
                payload['files'][-1] = '../outside.parquet'
            else:
                payload['format'] = 'unknown'
            manifest.write_text(json.dumps(payload))
        assert not _valid_data_file(path, backend='parquet', expected_rows=3)
        assert not is_iosql_table(table.table_path, backend='parquet', deep=True)
        with pytest.raises(UnsafeOperation):
            table.select()


def test_validation_counts_only_manifest_visible_files_after_replace(tmp_path):
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}, {'id': 2}])
        path = table.segments()[0]['path']
        table.write({'id': 3}, mode='replace')
        assert '@base' not in json.loads(Path(path + '.manifest.json').read_text())['files']
        assert _valid_data_file(path, backend='parquet', expected_rows=3)
        assert not _valid_data_file(path, backend='parquet', expected_rows=5)
        assert is_iosql_table(table.table_path, backend='parquet', deep=True)


@pytest.mark.parametrize('order_key', ['value', 'id'])
@pytest.mark.parametrize('connection_budget', [1, 2, 32])
def test_compact_preserves_nan_null_infinities_and_other_values(tmp_path, order_key, connection_budget):
    values = [math.nan, 1.25, None, -math.inf, math.inf, -0.0, math.nan]
    rows = [{'id': i, 'value': value, 'label': '汉字\x00' + str(i), 'payload': b'\x00\xff' + bytes([i])}
            for i, value in enumerate(values)]
    with Database(f'parquet://{tmp_path / "target"}', max_process_connections=connection_budget) as db:
        table = db.table('records', pk=['id'], columns={
            'id': 'integer', 'value': 'float', 'label': 'text', 'payload': 'blob',
        }, segment=Segment.rows(max_rows=3), order_key=order_key)
        table.write(rows[:2])
        table.write(rows[2:])
        for _ in range(2):
            compact = table.compact(target_rows=2)
            assert compact.rows_read == compact.rows_written == len(rows)
            actual = table.select(order_by='id')
            for expected, found in zip(rows, actual):
                assert {k: v for k, v in found.items() if k != 'value'} == {
                    k: v for k, v in expected.items() if k != 'value'
                }
                if isinstance(expected['value'], float) and math.isnan(expected['value']):
                    assert isinstance(found['value'], float) and math.isnan(found['value'])
                else:
                    assert found['value'] == expected['value']
                    if expected['value'] == 0.0:
                        assert math.copysign(1, found['value']) == math.copysign(1, expected['value'])
            assert table.validate(deep=True, include_obsolete=True).ok
        assert table.cleanup_obsolete(dry_run=False).files_deleted > 0


@pytest.mark.parametrize('layout', ['single', 'segment', 'partition'])
@pytest.mark.parametrize('order', ['value asc, id desc', 'value desc, id asc'])
def test_global_nan_order_matches_duckdb_for_select_topn_and_stream(tmp_path, layout, order):
    values = [math.nan, 1.0, 0.0, None, math.nan, -math.inf, None, math.inf, -1.0]
    rows = [{'id': i, 'value': value} for i, value in enumerate(values)]
    with duckdb.connect(config={'threads': 1}) as oracle:
        oracle.execute('CREATE TABLE data (id BIGINT, value DOUBLE)')
        oracle.executemany('INSERT INTO data VALUES (?, ?)', [(r['id'], r['value']) for r in rows])
        null_order = 'FIRST' if order.startswith('value asc') else 'LAST'
        expected = [dict(id=row[0]) for row in oracle.execute(
            'SELECT id FROM data ORDER BY ' + order.replace(', id', f' NULLS {null_order}, id')
        ).fetchall()]
    options = {'segment': Segment.rows(max_rows=1)} if layout == 'segment' else {}
    if layout == 'partition':
        options['partition'] = Partition.range('id', every=2)
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'float'}, **options)
        table.write(rows)
        for limit in [None, 1, 5]:
            wanted = expected if limit is None else expected[:limit]
            assert table.select(fields=['id'], order_by=order, limit=limit) == wanted
            assert list(table.iter_select(columns=['id'], order_by=order, limit=limit, batch_size=2)) == wanted
            assert list(table.query(columns=['id'], order_by=order, limit=limit).iter_rows(batch_size=2)) == wanted


@pytest.mark.parametrize('initial_count', [1, 2])
def test_ignore_without_new_rows_leaves_files_unchanged_but_updates_schema(tmp_path, initial_count):
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'float'})
        for i in range(1, initial_count + 1):
            table.write({'id': i, 'value': float(i)})
        data_dir = Path(table.table_part_path)
        before = {p.relative_to(data_dir): p.read_bytes() for p in data_dir.rglob('*') if p.is_file()}
        for _ in range(10):
            result = table.write({'id': 1, 'value': 999.0}, mode='ignore')
            assert result.rows_seen == 1
            assert result.rows_written is None
        result = table.write({'id': 1, 'extra': 'ignored'}, mode='ignore')
        assert result.schema_changed and result.rows_written is None
        assert {p.relative_to(data_dir): p.read_bytes() for p in data_dir.rglob('*') if p.is_file()} == before
        assert table.select(order_by='id') == [
            {'id': i, 'value': float(i), 'extra': None} for i in range(1, initial_count + 1)
        ]
        assert table.validate(deep=True).ok


def test_compact_keeps_duplicate_nan_primary_keys_in_separate_segments(tmp_path):
    with Database(f'parquet://{tmp_path / "target"}') as db:
        table = db.table('records', pk=['key'], columns={'key': 'float', 'id': 'integer'},
                         segment=Segment.rows(max_rows=2))
        for i, key in enumerate([math.nan, 0.0, math.nan, 1.0, math.nan]):
            table.write({'key': key, 'id': i})
        compact = table.compact(target_rows=2)
        assert compact.rows_read == compact.rows_written == 5
        actual = table.select(order_by='id')
        assert [row['id'] for row in actual] == list(range(5))
        assert all(math.isnan(actual[index]['key']) for index in [0, 2, 4])
        assert table.validate(deep=True, include_obsolete=True).ok
        assert is_iosql_table(table.table_path, backend='parquet', deep=True)


@pytest.mark.parametrize('connection_budget', [1, 2])
def test_compact_streams_multiple_sort_batches_without_changing_values(tmp_path, connection_budget):
    rows = [{'id': i, 'value': math.nan if i % 200 == 0 else float(i)} for i in range(2105)]
    with Database(f'parquet://{tmp_path / "target"}', max_process_connections=connection_budget) as db:
        table = db.table('records', pk=['id'], columns={'id': 'integer', 'value': 'float'},
                         segment=Segment.rows(max_rows=700), order_key='value')
        table.write(rows)
        compact = table.compact(target_rows=333)
        assert compact.rows_written == compact.rows_read == len(rows)
        actual = table.select(order_by='id')
        assert [row['id'] for row in actual] == list(range(len(rows)))
        for expected, found in zip(rows, actual):
            assert (math.isnan(found['value']) if math.isnan(expected['value'])
                    else found['value'] == expected['value'])
        assert table.validate(deep=True).ok
