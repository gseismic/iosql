"""真实目录数据上的迁移、分区调整、互斥、崩溃与恢复。"""

import multiprocessing
import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from iosql import Database, Partition, Segment
from iosql.coordination import LocalCoordinator, lease, physical_root, resource
from iosql.exception import (
    BadArgument, DatabaseBusy, MigrationError, MigrationLockTimeout,
    MigrationRecoveryRequired, MigrationVerificationError, TableSpecConflict, UnsafeOperation,
)
from iosql.migration import LocalPublisher, backup_snapshot, publish_snapshot


def uri(path, backend='sqlite'):
    return f'{backend}://{path}'


def make(db, name='ticks', rows=None, partition=None, segment=None):
    table = db.table(name, pk=['ts'], columns={'ts': 'integer', 'v': 'text'},
                     partition=partition, segment=segment)
    if rows:
        table.write(rows)
    return table


@pytest.mark.parametrize('target_backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('change', ['duration(ts,every=1hour)', 'range(ts,width=1000000)', None])
def test_repartition_data_reopen_and_source_unchanged(tmp_path, target_backend, change):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1000, 'v': 'a'}, {'ts': 3601000, 'v': 'b'}],
                     partition=Partition.duration('ts', every='1day'))
        before = table.spec().to_dict()
        target_uri = uri(tmp_path / 'target', target_backend)
        result = source.migrate_to(target_uri, tables={'ticks': 'archive'}, batch_size=1,
                                   specs={'ticks': {'partition': change}}, verify='row-count')
        assert result.published and result.verified and result.rows_copied == 2
        assert result.tables[0].target_spec['partition'] != before['partition']
        assert table.spec().to_dict() == before
        assert table.table_parts() == [0]
        with Database(target_uri) as target:
            archived = target.table('archive')
            assert archived.select_all('*', order_by='ts') == table.select_all('*', order_by='ts')
            assert archived.count_rows() == 2
            assert archived.validate(deep=True).ok
            assert len(archived.table_parts()) == (1 if change is None else 2)


def test_calendar_month_and_segment_target(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        table = source.table('events', pk=['date'], columns={'date': 'text'},
                             partition=Partition.calendar('date', every='1day', timezone='Asia/Shanghai'))
        table.write([{'date': day} for day in ['2026-01-01', '2026-01-02', '2026-02-01']])
        result = table.migrate_to(uri(tmp_path / 'target'), verify='row-count',
                                 specs={'events': {'partition': 'calendar(date,every=1month,timezone=Asia/Shanghai)',
                                                   'segment': Segment.rows(max_rows=2).to_dict()}})
        assert result.source_rows == result.target_rows == 3
        with Database(uri(tmp_path / 'target')) as target:
            assert target['events'].table_parts() == ['202601', '202602']
            assert target['events'].spec().segment.max_rows == 2


def test_add_partition_and_empty_contract_survives_reopen(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        make(source)
    with Database(uri(tmp_path / 'source')) as source:
        make(source)
        result = source.migrate_to(uri(tmp_path / 'target'), verify='row-count',
                                   specs={'ticks': {'partition': 'range(ts,width=100)'}})
        assert result.source_rows == result.target_rows == result.rows_written == 0
    with Database(uri(tmp_path / 'target')) as target:
        assert target.table_names() == ['ticks']
        target['ticks'].write({'ts': 123, 'v': 'after'})
        assert target['ticks'].table_parts() == [100]


def test_common_tables_legacy_and_preview(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])
        for bad in ([], {}, ['other'], {'other': 'new'}):
            with pytest.raises(BadArgument):
                table.migrate_to(uri(tmp_path / 'unused'), tables=bad, dry_run=True)
        with pytest.raises(BadArgument):
            table.migrate_to(uri(tmp_path / 'unused'), tables=None, target_table='x')
        preview = table.migrate_to(uri(tmp_path / 'unused'), tables={'ticks': 'x'}, dry_run=True,
                                   specs={'ticks': {'partition': 'range(ts,width=10)'}})
        assert preview.target_spec['partition']['every'] == 10
        assert preview.rows_written is None and preview.published is False
        assert not (tmp_path / 'unused').exists()
        assert not (tmp_path / 'source' / '__iosql_migrations__').exists()
        unused = resource(uri(tmp_path / 'unused'))
        assert not (unused.control_dir / (unused.token + '.workspaces')).exists()
        assert table.migrate_to(uri(tmp_path / 'legacy1'), 'x').target_table == 'x'
        assert table.migrate_to(uri(tmp_path / 'legacy2'), target_table='x').target_table == 'x'
        assert source.migrate_to(uri(tmp_path / 'selected'), tables=['ticks', 'ticks']).rows_copied == 1


def test_merge_preserves_other_tables_and_refreshes_idle_wal_connections(tmp_path):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        table = make(source, rows=[{'ts': 2, 'v': 'new'}])
        previous = make(target, rows=[{'ts': 1, 'v': 'old'}])
        other = make(target, 'keep', rows=[{'ts': 3, 'v': 'keep'}])
        old_path = Path(previous.table_path)
        table.migrate_to(target.uri)
        assert not old_path.exists()
        assert previous.select_all('*', order_by='ts') == [{'ts': 1, 'v': 'old'}, {'ts': 2, 'v': 'new'}]
        assert Path(previous.table_path) != old_path
        assert other.select_all('*') == [{'ts': 3, 'v': 'keep'}]
        assert target.table_names() == ['keep', 'ticks']
        previous.write({'ts': 4, 'v': 'after'})
        assert previous.count_rows() == 3
        assert previous.validate(deep=True).ok


def test_preflight_layout_collision_and_unknown_overrides(tmp_path):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        first = make(source, rows=[{'ts': 1, 'v': 'a'}])
        make(source, 'second', rows=[{'ts': 2, 'v': 'b'}])
        old = make(target, rows=[{'ts': 9, 'v': 'old'}])
        with pytest.raises(BadArgument, match='同一个'):
            source.migrate_to(target.uri, tables={'ticks': 'same', 'second': 'same'})
        with pytest.raises(TableSpecConflict):
            first.migrate_to(target.uri, specs={'ticks': {'partition': 'range(ts,width=10)'}})
        with pytest.raises(BadArgument, match='未知字段'):
            first.migrate_to(target.uri, specs={'ticks': {'partiton': None}})
        with pytest.raises(BadArgument, match='已选源表'):
            first.migrate_to(target.uri, specs={'wrong': {'partition': None}})
        assert old.select_all('*') == [{'ts': 9, 'v': 'old'}]


def test_midstream_failure_retains_stage_and_releases_locks(tmp_path, monkeypatch):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        table = make(source, rows=[{'ts': 1, 'v': 'new'}, {'ts': 2, 'v': 'new'}])
        old = make(target, rows=[{'ts': 9, 'v': 'old'}])
        closed = []

        def broken(*args, **kwargs):
            try:
                yield [{'ts': 1, 'v': 'new'}]
                raise RuntimeError('injected source failure')
            finally:
                closed.append(True)

        monkeypatch.setattr(table, 'iter_batches', broken)
        with pytest.raises(MigrationError) as failure:
            table.migrate_to(target.uri)
        assert failure.value.__cause__.args == ('injected source failure',)
        assert closed == [True]
        assert old.select_all('*') == [{'ts': 9, 'v': 'old'}]
        item = resource(target.uri)
        workspace = item.control_dir / (item.token + '.workspaces') / failure.value.operation_id
        assert (workspace / 'task.json').is_file()
        assert table.select_all('*', order_by='ts') == [
            {'ts': 1, 'v': 'new'}, {'ts': 2, 'v': 'new'},
        ]
        old.write({'ts': 10, 'v': 'unlocked'})


def test_verification_precedes_publication(tmp_path, monkeypatch):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])
        original = LocalPublisher.finish_stage

        def mismatch(self, plans):
            plans[0].result.source_rows += 1
            return original(self, plans)

        monkeypatch.setattr(LocalPublisher, 'finish_stage', mismatch)
        with pytest.raises(MigrationVerificationError) as failure:
            table.migrate_to(uri(tmp_path / 'target'), verify='row-count')
        assert failure.value.partial_result.published is False
        assert not (tmp_path / 'target').exists()


def test_migration_blocks_existing_objects_and_query_streams(tmp_path, monkeypatch):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])
        old = make(target, rows=[{'ts': 2, 'v': 'b'}])
        query = table.query()
        ready, release = threading.Event(), threading.Event()
        failures = []
        original = LocalPublisher.prepare

        def pause(self, plans):
            ready.set()
            assert release.wait(10)
            original(self, plans)

        monkeypatch.setattr(LocalPublisher, 'prepare', pause)

        def worker():
            try:
                table.migrate_to(target.uri)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            assert ready.wait(10)
            for callback in (table.count_rows, lambda: table.write({'ts': 3, 'v': 'bad'}),
                             old.count_rows, lambda: next(query.iter_rows()), source.table_names):
                with pytest.raises(DatabaseBusy):
                    callback()
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive() and not failures
        assert old.count_rows() == 2


def test_stream_holds_lease_and_close_allows_migration(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}, {'ts': 2, 'v': 'b'}])
        stream = table.iter_rows(batch_size=1)
        next(stream)
        with pytest.raises(UnsafeOperation):
            table.migrate_to(uri(tmp_path / 'target'), lock_timeout=0)
        failures = []

        def other_thread():
            try:
                table.migrate_to(uri(tmp_path / 'target'), lock_timeout=0.01)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(5)
        assert len(failures) == 1 and isinstance(failures[0], MigrationLockTimeout)
        stream.close()
        assert table.migrate_to(uri(tmp_path / 'target')).published


def _crash(source_uri, target_uri, phase):
    original = LocalCoordinator.write_state

    def write(self, generation, transaction=None, **kwargs):
        original(self, generation, transaction, **kwargs)
        if transaction and transaction.get('state') == phase:
            os._exit(73)

    LocalCoordinator.write_state = write
    if phase == 'READY':
        original_finish = LocalPublisher.finish_stage

        def ready(self, plans):
            original_finish(self, plans)
            os._exit(73)

        LocalPublisher.finish_stage = ready
    Database(source_uri).migrate_to(target_uri)


@pytest.mark.parametrize('phase', ['READY', 'COMMIT_DECIDED', 'COMMITTED'])
def test_process_crash_recovery_is_complete_and_write_checks_barrier(tmp_path, phase):
    source_uri, target_uri = uri(tmp_path / 'source'), uri(tmp_path / 'target')
    with Database(source_uri) as source, Database(target_uri) as target:
        for name in ('ticks', 'other'):
            make(source, name, rows=[{'ts': 2, 'v': 'new'}])
            make(target, name, rows=[{'ts': 1, 'v': 'old'}])
        process = multiprocessing.get_context('spawn').Process(target=_crash, args=(source_uri, target_uri, phase))
        process.start()
        process.join(10)
        assert process.exitcode == 73
        if phase == 'COMMIT_DECIDED':
            before = resource(target_uri).state_path.read_bytes()
            with pytest.raises(MigrationRecoveryRequired) as failure:
                target.migrate_to(uri(tmp_path / 'preview-target'), dry_run=True)
            assert failure.value.recovery_path == str(resource(target_uri).state_path)
            assert resource(target_uri).state_path.read_bytes() == before
        # 第一个操作直接写入，必须先恢复，不能依赖之前执行过 SELECT。
        target['ticks'].write({'ts': 3, 'v': 'after'})
        expected = [1, 3] if phase == 'READY' else [1, 2, 3]
        assert [row['ts'] for row in target['ticks'].select_all('*', order_by='ts')] == expected
        assert target['other'].count_rows() == (1 if phase == 'READY' else 2)


def test_cleanup_runs_after_all_locks_released(tmp_path, monkeypatch):
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[{'ts': 1, 'v': 'a'}])
        original = LocalPublisher.cleanup
        observed = []

        def cleanup(self, published):
            def check():
                with Database(self.uri) as db:
                    observed.append(db['ticks'].count_rows())
                with lease(source.uri, exclusive=True):
                    observed.append('source-unlocked')
            thread = threading.Thread(target=check)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            return original(self, published)

        monkeypatch.setattr(LocalPublisher, 'cleanup', cleanup)
        source.migrate_to(uri(tmp_path / 'target'))
        assert observed == [1, 'source-unlocked']


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_default_workspace_uses_target_filesystem(tmp_path, monkeypatch, backend):
    shared = Path('/dev/shm')
    if not shared.is_dir() or shared.stat().st_dev == tmp_path.stat().st_dev or not os.access(shared, os.W_OK):
        pytest.skip('没有独立可写的内存文件系统')
    import iosql.migration as migration

    original_prepare = LocalPublisher.prepare
    original_copy = migration.copy_snapshot
    workspaces = []
    published_copies = []

    def prepare(publisher, plans):
        workspaces.append(publisher.workspace)
        assert publisher.workspace.stat().st_dev == shared.stat().st_dev
        return original_prepare(publisher, plans)

    def copy_snapshot(source, destination):
        if Path(source).name == 'payload':
            published_copies.append((source, destination))
        return original_copy(source, destination)

    monkeypatch.setattr(LocalPublisher, 'prepare', prepare)
    monkeypatch.setattr(migration, 'copy_snapshot', copy_snapshot)
    with tempfile.TemporaryDirectory(prefix='iosql-migration-test-', dir=shared) as dest:
        target_uri = uri(Path(dest) / 'target', backend)
        target_item = resource(target_uri)
        with Database(uri(tmp_path / 'source')) as source:
            table = make(source, rows=[{'ts': 1, 'v': 'a'}])
            result = source.migrate_to(target_uri, verify='row-count')
            assert result.published and result.verified
            assert table.select_all('*') == [{'ts': 1, 'v': 'a'}]
        assert workspaces[0].parent == target_item.control_dir / (target_item.token + '.workspaces')
        assert not workspaces[0].exists()
        assert not (tmp_path / 'source' / '__iosql_migrations__').exists()
        assert not published_copies
        with Database(target_uri) as target:
            assert target['ticks'].select_all('*') == [{'ts': 1, 'v': 'a'}]


@pytest.mark.parametrize('has_existing_table', [False, True])
def test_mountpoint_workspace_is_created_after_target_preflight(tmp_path, monkeypatch, has_existing_table):
    import iosql.migration as migration

    target_path = tmp_path / 'target'
    target_path.mkdir()
    source_uri, target_uri = uri(tmp_path / 'source'), uri(target_path)
    if has_existing_table:
        with Database(target_uri) as target:
            make(target, 'keep', rows=[{'ts': 2, 'v': 'keep'}])
    original_stat = Path.stat

    def mountpoint_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == target_path:
            fields = list(value)
            fields[2] = value.st_dev + 1
            return os.stat_result(fields)
        return value

    with monkeypatch.context() as context:
        context.setattr(Path, 'stat', mountpoint_stat)
        assert migration._default_workspace_root(resource(source_uri), resource(target_uri)) == (
            target_path / '__iosql_migrations__'
        )

    monkeypatch.setattr(
        migration, '_default_workspace_root',
        lambda source, target: target.path / '__iosql_migrations__',
    )
    with Database(source_uri) as source:
        make(source, rows=[{'ts': 1, 'v': 'a'}])
        result = source.migrate_to(target_uri, verify='row-count')
        assert result.published and result.verified
    assert not (target_path / '__iosql_migrations__' / result.operation_id).exists()
    with Database(target_uri) as target:
        assert target['ticks'].select_all('*') == [{'ts': 1, 'v': 'a'}]
        if has_existing_table:
            assert target['keep'].select_all('*') == [{'ts': 2, 'v': 'keep'}]


def test_local_target_preflight_failure_creates_no_workspace(tmp_path):
    target_path = tmp_path / 'target'
    target_path.mkdir()
    foreign = target_path / 'foreign'
    foreign.mkdir()
    (foreign / 'keep.txt').write_text('保留', encoding='utf-8')
    target_uri = uri(target_path)
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[{'ts': 1, 'v': 'a'}])
        with pytest.raises(UnsafeOperation, match='目标不是完整的 iosql 数据库'):
            source.migrate_to(target_uri)
    target_item = resource(target_uri)
    assert not (target_item.control_dir / (target_item.token + '.workspaces')).exists()
    assert (foreign / 'keep.txt').read_text(encoding='utf-8') == '保留'


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_repartition_to_another_table_in_same_database(tmp_path, backend):
    database_uri = uri(tmp_path / 'database', backend)
    rows = [{'ts': 1, 'v': 'a'}, {'ts': 12, 'v': 'b'}]
    with Database(database_uri) as db:
        table = make(db, rows=rows)
        result = table.repartition_to(
            database_uri, table_name='archive',
            spec=table.spec().with_partition(Partition.range('ts', every=10)),
        )
        assert result.rows_copied == 2
    with Database(database_uri) as db:
        assert db['ticks'].select_all('*', order_by='ts') == rows
        assert db['archive'].select_all('*', order_by='ts') == rows
        assert db['archive'].table_parts() == [0, 10]


def test_postgresql_target_keeps_previous_default_workspace(tmp_path, monkeypatch):
    import iosql.migration as migration

    target = resource('postgresql://user:password@localhost:5432/db')
    local_source = resource(uri(tmp_path / 'source'))
    remote_source = resource('postgresql://user:password@otherhost:5432/db')
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path))
    assert migration._default_workspace_root(local_source, target) == local_source.path / '__iosql_migrations__'
    assert migration._default_workspace_root(remote_source, target) == tmp_path / 'iosql-migrations'


def test_cross_filesystem_temp_copy(tmp_path, monkeypatch):
    shared = Path('/dev/shm')
    if not shared.is_dir() or shared.stat().st_dev == tmp_path.stat().st_dev or not os.access(shared, os.W_OK):
        pytest.skip('没有独立可写的内存文件系统')
    import iosql.migration as migration
    original_copy = migration.copy_snapshot
    published_copies = []

    def copy_snapshot(source, destination):
        if Path(source).name == 'payload':
            published_copies.append((source, destination))
        return original_copy(source, destination)

    monkeypatch.setattr(migration, 'copy_snapshot', copy_snapshot)
    temporary = Path(tempfile.mkdtemp(prefix='iosql-migration-test-', dir=shared))
    try:
        with Database(uri(tmp_path / 'source')) as source:
            make(source, rows=[{'ts': 1, 'v': 'a'}])
            assert source.migrate_to(uri(tmp_path / 'target'), temp_dir=temporary, verify='row-count').verified
            assert not list(temporary.iterdir())
            assert len(published_copies) == 1
    finally:
        shutil.rmtree(temporary)


def test_maintenance_after_published_generation(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[{'ts': 1, 'v': 'a'}])
        source.migrate_to(uri(tmp_path / 'target'))
    target_uri = uri(tmp_path / 'target')
    backup_snapshot(target_uri, tmp_path / 'backup')
    assert not (tmp_path / 'backup' / '__iosql_generations__').exists()
    publish_snapshot(uri(tmp_path / 'backup'), uri(tmp_path / 'restored'))
    with Database(uri(tmp_path / 'restored')) as restored:
        assert restored['ticks'].count_rows() == 1
        restored.rename('renamed')
        assert not (tmp_path / 'restored').exists()
        assert restored['ticks'].count_rows() == 1
    with Database(uri(tmp_path / 'renamed')) as renamed:
        assert renamed['ticks'].count_rows() == 1
        assert physical_root(renamed.uri) != tmp_path / 'renamed'


def test_nested_maintenance_cleanup_waits_for_outer_lease(tmp_path, monkeypatch):
    from iosql.coordination import exclusive_resources
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[{'ts': 1, 'v': 'a'}])
        observed = []
        original = LocalPublisher.cleanup

        def cleanup(self, published):
            observed.append(True)
            original(self, published)

        monkeypatch.setattr(LocalPublisher, 'cleanup', cleanup)
        with exclusive_resources([source.uri, uri(tmp_path / 'target')]):
            source.migrate_to(uri(tmp_path / 'target'))
            assert not observed
        assert observed == [True]


def test_cli_specs_uses_unified_migration(tmp_path, invoke_separate):
    import json
    from iosql.cli import app
    with Database(uri(tmp_path / 'source')) as source:
        make(source, rows=[{'ts': 1000, 'v': 'a'}, {'ts': 3601000, 'v': 'b'}])
    result = invoke_separate(app,
        'migrate', '-s', str(tmp_path / 'source'), '--target', uri(tmp_path / 'target', 'parquet'),
        '--table', 'ticks:archive', '--specs', '{"ticks":{"partition":"range(ts,width=1000000)"}}',
        '--verify', 'row-count', '--temp-dir', str(tmp_path / 'temporary'), '--lock-timeout', '0.1',
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload['published'] and payload['verified']
    assert payload['tables'][0]['target_spec']['partition']['kind'] == 'range'
    with Database(uri(tmp_path / 'target', 'parquet')) as target:
        assert target['archive'].table_parts() == [0, 3000000]


def test_corrupt_control_blocks_reads_and_writes(tmp_path):
    from iosql.coordination import atomic_json
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])
        atomic_json(resource(source.uri).state_path, {'format': 'foreign', 'generation': None})
        for callback in (table.count_rows, lambda: table.write({'ts': 2, 'v': 'b'})):
            with pytest.raises(MigrationRecoveryRequired):
                callback()
        # 恢复测试环境，正常 close 也不能绕过损坏屏障。
        resource(source.uri).state_path.unlink()


def _read_while_parent_locked(source_uri, queue):
    try:
        Database(source_uri).table_names()
    except DatabaseBusy:
        queue.put('busy')
    else:
        queue.put('unexpected-access')


def test_fork_does_not_unlock_parent_file_lease(tmp_path):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('需要 fork')
    source_uri = uri(tmp_path / 'source')
    context = multiprocessing.get_context('fork')
    queue = context.Queue()
    with lease(source_uri, exclusive=True):
        process = context.Process(target=_read_while_parent_locked, args=(source_uri, queue))
        process.start()
        process.join(10)
        assert process.exitcode == 0 and queue.get(timeout=1) == 'busy'
    queue.close()


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_early_epoch_milliseconds_to_calendar_after_reopen(tmp_path, backend):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1000, 'v': 'a'}, {'ts': 3601000, 'v': 'b'}],
                     partition=Partition.duration('ts', every='1day'))
        table.migrate_to(uri(tmp_path / 'target', backend),
                         specs={'ticks': {'partition': 'calendar(ts,every=1hour,timezone=UTC)'}},
                         verify='row-count')
    with Database(uri(tmp_path / 'target', backend)) as target:
        table = target['ticks']
        assert table.table_parts() == ['1970010100', '1970010101']
        assert table.spec().time.unit == 'ms'
        assert table.query(start=0, end=3600000).to_list() == [{'ts': 1000, 'v': 'a'}]
        table.write({'ts': 3602000, 'v': 'later'})
        assert table.count_rows() == 3
        assert table.table_parts() == ['1970010100', '1970010101']


@pytest.mark.parametrize('mode', ['ignore', 'upsert', 'replace'])
def test_merge_conflict_modes_preserve_target_and_result_meaning(tmp_path, mode):
    with Database(uri(tmp_path / 'source')) as source, Database(uri(tmp_path / 'target')) as target:
        table = make(source, rows=[{'ts': 1, 'v': 'new'}, {'ts': 2, 'v': 'added'}])
        previous = make(target, rows=[{'ts': 1, 'v': 'old'}])
        result = table.migrate_to(target.uri, mode=mode, batch_size=1)
        expected = 'old' if mode == 'ignore' else 'new'
        assert previous.select_all('*', order_by='ts') == [{'ts': 1, 'v': expected}, {'ts': 2, 'v': 'added'}]
        assert result.rows_copied == 2 and result.target_rows == 2
        assert result.partitions_created == []
        if mode == 'ignore':
            assert result.rows_written is None
        else:
            assert result.rows_written == 2


def test_invalid_specs_precede_target_connection(tmp_path, monkeypatch):
    import iosql.coordination_postgresql as pg_coordination

    def unexpected(*args, **kwargs):
        raise AssertionError('不应为参数错误连接目标')

    monkeypatch.setattr(pg_coordination, 'connect_control', unexpected)
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])
        for specs in ({'ticks': {'partiton': 'bad'}},
                      {'ticks': {'partition': 'calendar(ts,every=1day,timezone=No/SuchZone)'}}):
            with pytest.raises(BadArgument):
                table.migrate_to('postgresql://u:p@unreachable:5432/db', specs=specs)


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_seconds_time_column_to_calendar_preserves_values(tmp_path, backend):
    rows = [{'ts': 1, 'v': 'a'}, {'ts': 3601, 'v': 'b'}]
    with Database(uri(tmp_path / 'source')) as source:
        table = source.table('ticks', pk=['ts'], columns={'ts': 'integer', 'v': 'text'},
                             time={'name': 'ts', 'unit': 's'})
        table.write(rows)
        table.migrate_to(uri(tmp_path / 'target', backend), verify='row-count',
                         specs={'ticks': {'partition': 'calendar(ts,every=1hour,timezone=UTC)'}})
    with Database(uri(tmp_path / 'target', backend)) as target:
        assert target['ticks'].table_parts() == ['1970010100', '1970010101']
        assert target['ticks'].select_all('*', order_by='ts') == rows
        assert target['ticks'].spec().time.unit == 's'


def test_cli_raw_scans_hold_database_lease(tmp_path):
    from iosql.cli import _guard_command_errors
    ready, release = threading.Event(), threading.Event()
    failures = []

    def inspect_raw(source):
        ready.set()
        assert release.wait(10)

    command = _guard_command_errors(inspect_raw)
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])

        def run():
            try:
                command(source=source.uri)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert ready.wait(10)
            with pytest.raises(MigrationLockTimeout):
                table.migrate_to(uri(tmp_path / 'target'), lock_timeout=0.01)
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive() and not failures
        assert table.migrate_to(uri(tmp_path / 'target')).published


def test_restore_rechecks_target_permission_after_locking(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from typer.testing import CliRunner
    import iosql.cli as cli
    with Database(uri(tmp_path / 'backup')) as backup:
        make(backup, rows=[{'ts': 1, 'v': 'backup'}])
    original = cli.exclusive_resources
    injected = []

    @contextmanager
    def racing(uris, timeout=30):
        if not injected:
            injected.append(True)
            with Database(uri(tmp_path / 'target')) as target:
                make(target, rows=[{'ts': 9, 'v': 'created-during-preflight'}])
        with original(uris, timeout):
            yield

    monkeypatch.setattr(cli, 'exclusive_resources', racing)
    result = CliRunner().invoke(cli.app, ['restore', '--backup', str(tmp_path / 'backup'),
                                          '--target', str(tmp_path / 'target')])
    assert result.exit_code == 1 and '--replace --yes' in result.output
    with Database(uri(tmp_path / 'target')) as target:
        assert target['ticks'].select_all('*') == [{'ts': 9, 'v': 'created-during-preflight'}]


def test_completed_task_survives_later_publication_cleanup(tmp_path):
    import json
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'a'}])
        target_uri = uri(tmp_path / 'target')
        first = table.migrate_to(target_uri)
        first_root = physical_root(target_uri)
        second = table.migrate_to(target_uri, mode='ignore')
        assert not first_root.exists()
        item = resource(target_uri)
        archive = item.control_dir / (item.token + '.migrations') / (first.operation_id + '.json')
        old = json.loads(archive.read_text())
        assert old['transaction']['state'] == 'COMMITTED'
        assert old['generation'] == first.operation_id
        assert LocalCoordinator(item).state()['generation'] == second.operation_id


def test_buffered_writer_busy_keeps_pending_input(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source, rows=[{'ts': 1, 'v': 'initial'}])
        writer = table.buffered_writer(max_rows=100)
        writer.append({'ts': 2, 'v': 'pending'})
        ready, release = threading.Event(), threading.Event()

        def lock():
            with lease(source.uri, exclusive=True):
                ready.set()
                assert release.wait(10)

        thread = threading.Thread(target=lock)
        thread.start()
        try:
            assert ready.wait(10)
            with pytest.raises(DatabaseBusy):
                writer.append({'ts': 3, 'v': 'rejected'})
            with pytest.raises(DatabaseBusy):
                writer.flush()
        finally:
            release.set()
            thread.join(10)
        assert writer.flush().rows_written == 1
        assert table.select_all('*', order_by='ts') == [
            {'ts': 1, 'v': 'initial'}, {'ts': 2, 'v': 'pending'}]


def test_buffered_context_blocks_migration_until_flushed(tmp_path):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source)
        with table.buffered_writer() as writer:
            writer.append({'ts': 1, 'v': 'pending'})
            with pytest.raises(UnsafeOperation):
                table.migrate_to(uri(tmp_path / 'target'), lock_timeout=0)
        result = table.migrate_to(uri(tmp_path / 'target'), verify='row-count')
        assert result.published and result.source_rows == 1


def test_buffered_write_failure_keeps_input_for_explicit_retry(tmp_path, monkeypatch):
    with Database(uri(tmp_path / 'source')) as source:
        table = make(source)
        writer = table.buffered_writer()
        writer.append({'ts': 1, 'v': 'pending'})
        original = table.write

        def fail(*args, **kwargs):
            raise RuntimeError('injected before write')

        monkeypatch.setattr(table, 'write', fail)
        with pytest.raises(RuntimeError):
            writer.flush()
        monkeypatch.setattr(table, 'write', original)
        assert writer.flush().rows_written == 1
        assert table.count_rows() == 1
