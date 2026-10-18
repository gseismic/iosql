"""另存独立副本的真实数据、对象作用域和失败保护契约。"""

import pytest

from iosql import Database, MigrationResult, TableMigrationResult
from iosql.exception import BadArgument, MigrationError, UnsafeOperation

ROWS = [
    {'symbol': 'AAPL', 'ts': 1000, 'close': 1.0},
    {'symbol': 'MSFT', 'ts': 2000, 'close': 2.0},
    {'symbol': 'AAPL', 'ts': 86_401_000, 'close': 3.0},
]


def make_bars(database):
    table = database.timeseries(
        'bars', time='ts', pk=['symbol', 'ts'],
        columns={'symbol': 'text', 'ts': 'integer', 'close': 'float'},
        partition_every='1day', description='用于比较 engine 的行情',
        column_descriptions={'close': '收盘价'},
        column_ranges={'close': {'low': 0, 'high': 1000}},
    )
    assert table.write(ROWS).rows_written == len(ROWS)
    return table


@pytest.mark.parametrize('source_backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('target_backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('scope', ['database', 'table'])
def test_save_as_keeps_source_and_makes_independent_copy(
        tmp_path, source_backend, target_backend, scope):
    source_uri = f'{source_backend}://{tmp_path}/source'
    target_uri = f'{target_backend}://{tmp_path}/copy'
    with Database(source_uri) as source:
        bars = make_bars(source)
        assert source.table('events', pk=['id'], columns={'id': 'integer'}).write(
            {'id': 7}).rows_written == 1
        source_spec = bars.spec().to_dict()
        owner = source if scope == 'database' else bars

        result = owner.save_as(target_uri, batch_size=2, verify='row-count')

        assert isinstance(result, MigrationResult if scope == 'database' else TableMigrationResult)
        assert result.published and result.verified and result.operation_id
        assert result.rows_copied == result.rows_written == (4 if scope == 'database' else 3)
        assert source.uri == bars.uri == source_uri
        assert source.table_names() == ['bars', 'events']
        assert bars.spec().to_dict() == source_spec
        assert bars.query(order_by='ts').to_list() == ROWS
        with Database(target_uri) as target:
            assert target.table_names() == (['bars', 'events'] if scope == 'database' else ['bars'])
            copied = target.table('bars')
            assert copied.spec().to_dict() == source_spec
            assert copied.query(order_by='ts').to_list() == ROWS
            plan = copied.query(start=86_400_000, end=172_799_999).explain()
            assert len(plan.scanned_partitions) == 1
            assert copied.validate(deep=True).ok
            assert copied.write({'symbol': 'AAPL', 'ts': 3000, 'close': 4.0}).rows_written == 1
            assert bars.count_rows() == 3
            assert bars.write({'symbol': 'AAPL', 'ts': 4000, 'close': 5.0}).rows_written == 1
            assert copied.query(start=4000, end=4000).to_list() == []

    # 两端都关闭重开后仍是独立副本，不能只验证旧对象的缓存。
    with Database(source_uri) as source, Database(target_uri) as target:
        assert source.table('bars').query(start=3000, end=3000).to_list() == []
        assert source.table('bars').count_rows() == target.table('bars').count_rows() == 4
        assert source.table('bars').spec().to_dict() == source_spec


@pytest.mark.parametrize('scope', ['database', 'table'])
def test_save_as_selection_rename_and_layout_leave_source_unchanged(tmp_path, scope):
    with Database(f'sqlite://{tmp_path}/source') as source:
        bars = make_bars(source)
        source.table('events', pk=['id'], columns={'id': 'integer'}).write({'id': 7})
        before = bars.spec().to_dict()
        owner = source if scope == 'database' else bars
        target_uri = f'parquet://{tmp_path}/selected'
        result = owner.save_as(
            target_uri, tables={'bars': 'copied_bars'},
            specs={'bars': {'partition': 'duration(ts,every=1hour)'}},
            verify='row-count', temp_dir=tmp_path / 'conversion', lock_timeout=0,
        )
        assert result.published and result.verified
        assert bars.spec().to_dict() == before
        assert bars.query(order_by='ts').to_list() == ROWS
        with Database(target_uri) as target:
            assert target.table_names() == ['copied_bars']
            copied = target.table('copied_bars')
            assert copied.spec().partition.to_dict() != before['partition']
            assert copied.query(order_by='ts').to_list() == ROWS


@pytest.mark.parametrize('scope', ['database', 'table'])
@pytest.mark.parametrize('target_backend', ['sqlite', 'parquet', 'postgresql'])
def test_save_as_dry_run_never_opens_target(tmp_path, monkeypatch, scope, target_backend):
    with Database(f'sqlite://{tmp_path}/source') as source:
        bars = make_bars(source)
        target_uri = (f'{target_backend}://{tmp_path}/unused' if target_backend != 'postgresql'
                      else 'postgresql://unused:unused@localhost:1/unused')

        def forbidden_target(*args, **kwargs):
            pytest.fail('dry-run 不应打开目标库')

        monkeypatch.setattr(Database, '__init__', forbidden_target)
        owner = source if scope == 'database' else bars
        result = owner.save_as(target_uri, dry_run=True, verify='row-count', batch_size=2)
        assert result.dry_run and result.published is False
        assert result.source_rows == 3 and result.rows_copied == 0 and result.batches == 2
        assert not (tmp_path / 'unused').exists()
        assert not (tmp_path / 'source' / '__iosql_migrations__').exists()
        assert bars.query(order_by='ts').to_list() == ROWS


@pytest.mark.parametrize('scope', ['database', 'table'])
def test_save_as_rejects_self_nested_targets_and_wrong_table_scope(tmp_path, scope):
    with Database(f'sqlite://{tmp_path}/source') as source:
        bars = make_bars(source)
        owner = source if scope == 'database' else bars
        for target_uri in (source.uri, f'parquet://{tmp_path}/source/inside', f'sqlite://{tmp_path}'):
            with pytest.raises(BadArgument):
                owner.save_as(target_uri, dry_run=True)
        if scope == 'table':
            with pytest.raises(BadArgument):
                bars.save_as(f'sqlite://{tmp_path}/unused', tables=['events'], dry_run=True)
        assert bars.query(order_by='ts').to_list() == ROWS


@pytest.mark.parametrize('scope', ['database', 'table'])
def test_save_as_existing_target_uses_mode_and_verification_does_not_overwrite(tmp_path, scope):
    with Database(f'sqlite://{tmp_path}/source') as source, Database(
            f'parquet://{tmp_path}/target') as target:
        bars = make_bars(source)
        copied = target.table('bars', spec=bars.spec())
        assert copied.write(ROWS[:1]).rows_written == 1
        owner = source if scope == 'database' else bars
        with pytest.raises(BadArgument):
            owner.save_as(target.uri, verify='row-count')
        assert copied.query(order_by='ts').to_list() == ROWS[:1]
        result = owner.save_as(target.uri, mode='ignore')
        assert result.published and result.rows_copied == 3
        assert copied.query(order_by='ts').to_list() == ROWS
        assert bars.query(order_by='ts').to_list() == ROWS


@pytest.mark.parametrize('scope', ['database', 'table'])
def test_save_as_failed_copy_preserves_source_and_existing_target(tmp_path, monkeypatch, scope):
    with Database(f'sqlite://{tmp_path}/source') as source, Database(
            f'parquet://{tmp_path}/target') as target:
        bars = make_bars(source)
        copied = target.table('bars', spec=bars.spec())
        old_rows = [{'symbol': 'OLD', 'ts': 5000, 'close': 9.0}]
        assert copied.write(old_rows).rows_written == 1
        before = bars.spec().to_dict()

        def broken_batches(*args, **kwargs):
            yield ROWS[:1]
            raise RuntimeError('复制中途失败')

        monkeypatch.setattr(bars, 'iter_batches', broken_batches)
        owner = source if scope == 'database' else bars
        with pytest.raises(MigrationError) as failure:
            owner.save_as(target.uri, batch_size=1)
        assert failure.value.partial_result.published is False
        assert bars.query(order_by='ts').to_list() == ROWS
        assert bars.spec().to_dict() == before
        assert copied.query(order_by='ts').to_list() == old_rows
        # 失败后两边的维护锁均已释放，普通写入仍可继续。
        assert copied.write({'symbol': 'OLD', 'ts': 6000, 'close': 10.0}).rows_written == 1
        assert bars.write({'symbol': 'AAPL', 'ts': 7000, 'close': 11.0}).rows_written == 1


@pytest.mark.parametrize('scope', ['database', 'table'])
def test_save_as_requires_read_stream_to_be_closed(tmp_path, scope):
    with Database(f'sqlite://{tmp_path}/source') as source:
        bars = make_bars(source)
        stream = bars.query(order_by='ts').iter_rows(batch_size=1)
        assert next(stream) == ROWS[0]
        owner = source if scope == 'database' else bars
        target_uri = f'parquet://{tmp_path}/target'
        try:
            with pytest.raises(UnsafeOperation):
                owner.save_as(target_uri, lock_timeout=0)
        finally:
            stream.close()
        assert owner.save_as(target_uri, verify='row-count').verified
