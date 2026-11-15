"""Parquet 迁移的根目录布局、已打开连接和中断恢复。"""

import inspect
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from iosql import Database
from iosql.coordination import physical_root, resource
from iosql.exception import MigrationError, MigrationRecoveryRequired, UnsafeOperation


def _uri(path, backend='parquet'):
    return f'{backend}://{path}'


def _table(db, name, value):
    table = db.table(name, pk=['id'], columns={'id': 'integer', 'value': 'text'})
    table.write(value)
    return table


def test_new_parquet_migration_has_the_normal_root_layout(tmp_path):
    target = tmp_path / 'target'
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        _table(source, 'bars', {'id': 1, 'value': 'first'})
        _table(source, 'trades', {'id': 2, 'value': 'second'})
        result = source.migrate_to(_uri(target), verify='row-count')
    assert result.published and result.verified
    assert sorted(path.name for path in target.iterdir()) == ['__iosql_database__.json', 'bars', 'trades']
    assert (target / 'bars' / 'meta.sqlite3').is_file()
    assert (target / 'bars' / 'data').is_dir()
    assert physical_root(_uri(target)) == target
    with Database(_uri(target)) as db:
        assert db.table_names() == ['bars', 'trades']
        assert db.table('bars').select() == [{'id': 1, 'value': 'first'}]


@pytest.mark.skipif(os.name != 'posix', reason='验证 POSIX 目录同步顺序')
def test_new_target_parent_is_synced_before_commit_decision(tmp_path, monkeypatch):
    import iosql.coordination as coordination
    import iosql._parquet_flat_publish as flat

    target = (tmp_path / 'new-parent' / 'target').resolve()
    sync = coordination.sync_directory
    write = coordination.LocalCoordinator.write_state
    events = []

    def tracked_sync(path):
        sync(path)
        if Path(path) == target.parent and target.is_dir():
            events.append('parent-synced')

    def tracked_write(self, generation, transaction=None, **kwargs):
        if self.resource.path == target and (transaction or {}).get('state') == 'FLAT_COMMIT_DECIDED':
            assert 'parent-synced' in events
            events.append('commit-decided')
        return write(self, generation, transaction, **kwargs)

    monkeypatch.setattr(coordination, 'sync_directory', tracked_sync)
    monkeypatch.setattr(flat, 'sync_directory', tracked_sync)
    monkeypatch.setattr(coordination.LocalCoordinator, 'write_state', tracked_write)
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        _table(source, 'bars', {'id': 1, 'value': 'first'})
        result = source.migrate_to(_uri(target), verify='row-count')
        assert result.published and result.verified
    assert events.index('parent-synced') < events.index('commit-decided')


@pytest.mark.skipif(os.name != 'posix', reason='验证 POSIX 目录同步失败')
def test_target_parent_sync_failure_cannot_commit(tmp_path, monkeypatch):
    import iosql.coordination as coordination
    import iosql._parquet_flat_publish as flat

    target = (tmp_path / 'target').resolve()
    sync = coordination.sync_directory
    original_publish = flat.publish
    publishing = False
    reject_sync = True
    events = []

    def publishing_now(*args):
        nonlocal publishing
        events.append(('root-exists', target.exists()))
        publishing = True
        return original_publish(*args)

    def fail_parent_sync(path):
        if publishing and Path(path) == target.parent and target.is_dir():
            if reject_sync:
                raise OSError('模拟新目标父目录同步失败')
            events.append(('parent-synced', True))
        return sync(path)

    monkeypatch.setattr(flat, 'publish', publishing_now)
    monkeypatch.setattr(coordination, 'sync_directory', fail_parent_sync)
    monkeypatch.setattr(flat, 'sync_directory', fail_parent_sync)
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        table = _table(source, 'bars', {'id': 1, 'value': 'first'})
        with pytest.raises(MigrationError) as failure:
            source.migrate_to(_uri(target))
        assert not failure.value.partial_result.published
        state = coordination.LocalCoordinator(resource(_uri(target))).state()
        assert not coordination.needs_recovery(state)
        assert not (target / 'bars').exists()
        assert table.select() == [{'id': 1, 'value': 'first'}]
        reject_sync = False
        result = source.migrate_to(_uri(target), verify='row-count')
        assert result.published and result.verified
        assert events[:2] == [('root-exists', False), ('root-exists', True)]
        assert ('parent-synced', True) in events[2:]


def test_existing_target_preserves_idle_metadata_connection_and_unselected_table(tmp_path):
    target = tmp_path / 'target'
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source, Database(_uri(target)) as db:
        _table(source, 'bars', {'id': 2, 'value': 'new'})
        old = _table(db, 'bars', {'id': 1, 'value': 'old'})
        keep = _table(db, 'keep', {'id': 3, 'value': 'preserved'})
        metadata = target / 'bars' / 'meta.sqlite3'
        original_inode = metadata.stat().st_ino
        result = source.migrate_to(db.uri)
        assert result.published
        assert metadata.stat().st_ino == original_inode
        assert old.select(order_by='id') == [
            {'id': 1, 'value': 'old'}, {'id': 2, 'value': 'new'},
        ]
        assert keep.select() == [{'id': 3, 'value': 'preserved'}]
        old.write({'id': 4, 'value': 'after'})
        assert old.count_rows() == 3
        assert sorted(path.name for path in target.iterdir()) == ['__iosql_database__.json', 'bars', 'keep']


def test_repeated_migration_keeps_flat_layout(tmp_path):
    target = tmp_path / 'target'
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        incoming = _table(source, 'bars', {'id': 1, 'value': 'first'})
        assert source.migrate_to(_uri(target), verify='row-count').verified
        with Database(_uri(target)) as db:
            current = db.table('bars')
            assert current.count_rows() == 1
            incoming.write({'id': 2, 'value': 'second'})
            assert source.migrate_to(db.uri, mode='ignore').published
            assert current.select(order_by='id') == [
                {'id': 1, 'value': 'first'}, {'id': 2, 'value': 'second'},
            ]
            assert sorted(path.name for path in target.iterdir()) == ['__iosql_database__.json', 'bars']


@pytest.mark.parametrize(('mode', 'expected'), [('ignore', 'old'), ('replace', 'new'), ('upsert', 'new')])
def test_existing_target_conflict_modes_survive_flat_publish(tmp_path, mode, expected):
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source, Database(_uri(tmp_path / 'target')) as target:
        _table(source, 'bars', [{'id': 1, 'value': 'new'}, {'id': 2, 'value': 'added'}])
        old = _table(target, 'bars', {'id': 1, 'value': 'old'})
        result = source.migrate_to(target.uri, mode=mode)
        assert result.published
        assert old.select(order_by='id') == [
            {'id': 1, 'value': expected}, {'id': 2, 'value': 'added'},
        ]


def test_predecision_failure_leaves_existing_target_unchanged(tmp_path, monkeypatch):
    import iosql._parquet_flat_publish as flat

    def fail_before_decision(*args):
        raise OSError('模拟暂存身份检查失败')

    monkeypatch.setattr(flat, '_entry', fail_before_decision)
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source, Database(_uri(tmp_path / 'target')) as target:
        _table(source, 'bars', {'id': 2, 'value': 'new'})
        original = _table(target, 'bars', {'id': 1, 'value': 'old'})
        with pytest.raises(MigrationError) as failure:
            source.migrate_to(target.uri)
        assert failure.value.partial_result.published is False
        assert original.select() == [{'id': 1, 'value': 'old'}]
        assert sorted(path.name for path in (tmp_path / 'target').iterdir()) == ['__iosql_database__.json', 'bars']


def test_old_generation_can_be_source_but_not_flat_migration_target(tmp_path):
    from iosql.migration import publish_snapshot

    original = _uri(tmp_path / 'original')
    old = _uri(tmp_path / 'old')
    destination = _uri(tmp_path / 'destination')
    with Database(original) as db:
        _table(db, 'bars', {'id': 1, 'value': 'old'})
    publish_snapshot(original, old)
    assert (tmp_path / 'old' / '__iosql_generations__').is_dir()
    with Database(old) as source:
        assert source.migrate_to(destination, verify='row-count').verified
    with Database(destination) as source:
        with pytest.raises(UnsafeOperation, match='旧代目录'):
            source.migrate_to(old)
    assert sorted(p.name for p in (tmp_path / 'destination').iterdir()) == ['__iosql_database__.json', 'bars']


def test_explicit_cross_filesystem_workspace_still_publishes_flat(tmp_path):
    shared = Path('/dev/shm')
    if not shared.is_dir() or not os.access(shared, os.W_OK) or shared.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip('没有独立可写的文件系统')
    target = tmp_path / 'target'
    with tempfile.TemporaryDirectory(prefix='iosql-flat-workspace-', dir=shared) as temporary:
        with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
            _table(source, 'bars', {'id': 1, 'value': 'first'})
            result = source.migrate_to(_uri(target), temp_dir=temporary, verify='row-count')
        assert result.published and result.verified
        assert list(Path(temporary).iterdir()) == []
    assert sorted(path.name for path in target.iterdir()) == ['__iosql_database__.json', 'bars']
    with Database(_uri(target)) as db:
        assert db.table('bars').select() == [{'id': 1, 'value': 'first'}]


def test_target_workspace_inside_root_is_removed_after_publish(tmp_path, monkeypatch):
    import iosql.migration as migration

    target = tmp_path / 'target'
    original = migration._default_workspace_root

    def within_target(source, destination):
        if destination.path == target:
            return target / '__iosql_migrations__'
        return original(source, destination)

    monkeypatch.setattr(migration, '_default_workspace_root', within_target)
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        _table(source, 'bars', {'id': 1, 'value': 'first'})
        assert source.migrate_to(_uri(target), verify='row-count').verified
    assert sorted(path.name for path in target.iterdir()) == ['__iosql_database__.json', 'bars']


@pytest.mark.skipif(os.name != 'posix' or not shutil.rmtree.avoids_symlink_attacks
                    or 'dir_fd' not in inspect.signature(shutil.rmtree).parameters
                    or os.rmdir not in os.supports_dir_fd,
                    reason='需要支持 dir_fd 的安全目录清理')
def test_mountpoint_workspace_cleanup_removes_empty_management_directory(tmp_path, monkeypatch):
    import iosql.migration as migration

    target = tmp_path / 'target'
    item = resource(_uri(target))
    original_stat = Path.stat

    def other_device(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == item.control_dir:
            fields = list(value)
            fields[2] = value.st_dev + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, 'stat', other_device)
    monkeypatch.setattr(migration, '_default_workspace_root',
                        lambda source, destination: destination.path / '__iosql_migrations__')
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        _table(source, 'bars', {'id': 1, 'value': 'first'})
        assert source.migrate_to(_uri(target), verify='row-count').verified
    assert sorted(path.name for path in target.iterdir()) == ['__iosql_database__.json', 'bars']


def test_mountpoint_without_safe_cleanup_preserves_staging_and_warns(tmp_path, monkeypatch):
    import iosql.migration as migration

    target = tmp_path / 'target'
    item = resource(_uri(target))
    original_stat = Path.stat

    def other_device(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == item.control_dir:
            fields = list(value)
            fields[2] = value.st_dev + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, 'stat', other_device)
    monkeypatch.setattr(migration, '_default_workspace_root',
                        lambda source, destination: destination.path / '__iosql_migrations__')
    monkeypatch.setattr(shutil.rmtree, 'avoids_symlink_attacks', False)
    with Database(_uri(tmp_path / 'source', 'sqlite')) as source:
        _table(source, 'bars', {'id': 1, 'value': 'first'})
        with pytest.warns(RuntimeWarning, match='暂存保留'):
            result = source.migrate_to(_uri(target), verify='row-count')
        assert result.verified
    assert (target / '__iosql_migrations__' / result.operation_id).is_dir()
    with Database(_uri(target)) as db:
        assert db.table('bars').select() == [{'id': 1, 'value': 'first'}]


@pytest.mark.skipif(os.name != 'posix' or not shutil.rmtree.avoids_symlink_attacks
                    or 'dir_fd' not in inspect.signature(shutil.rmtree).parameters
                    or os.rmdir not in os.supports_dir_fd,
                    reason='需要支持 dir_fd 的安全目录清理')
def test_cleanup_uses_open_directory_even_if_target_path_is_reused(tmp_path):
    from iosql.migration import LocalPublisher

    root = tmp_path / 'target'
    parent = root / '__iosql_migrations__'
    candidate = parent / 'task'
    candidate.mkdir(parents=True)
    (candidate / 'old').write_text('old')
    info = candidate.stat()
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    parent_info = os.fstat(fd)
    publisher = object.__new__(LocalPublisher)
    publisher.flat_cleanup = (fd, 'task', [info.st_dev, info.st_ino], root_fd,
                              [parent_info.st_dev, parent_info.st_ino])
    publisher.flat_candidate = candidate
    publisher.workspace = candidate
    publisher.retired = None
    root.rename(tmp_path / 'previous')
    replacement = parent / 'task'
    replacement.mkdir(parents=True)
    (replacement / 'new').write_text('new')
    publisher.cleanup(True)
    assert not (tmp_path / 'previous' / '__iosql_migrations__').exists()
    assert (replacement / 'new').read_text() == 'new'


def _crash_during_flat_publish(source_uri, target_uri, phase):
    from iosql.coordination import LocalCoordinator
    import iosql._parquet_flat_publish as flat

    target = resource(target_uri).path
    original_state = LocalCoordinator.write_state
    original_move = flat._move
    original_backup = flat._meta_backup

    def state(self, generation, transaction=None, **kwargs):
        original_state(self, generation, transaction, **kwargs)
        if transaction and transaction.get('state') == phase:
            os._exit(73)

    def move(src, dst):
        original_move(src, dst)
        if phase == 'old-data' and src == target / 'a' / 'data':
            os._exit(73)
        if phase == 'new-data' and dst == target / 'a' / 'data':
            os._exit(73)

    def backup(src, dst):
        original_backup(src, dst)
        if phase == 'metadata' and dst == target / 'a' / 'meta.sqlite3':
            os._exit(73)

    LocalCoordinator.write_state = state
    flat._move = move
    flat._meta_backup = backup
    with Database(source_uri) as source:
        source.migrate_to(target_uri)


@pytest.mark.parametrize('phase', [
    'FLAT_COMMIT_DECIDED', 'old-data', 'new-data', 'metadata', 'COMMITTED',
])
def test_interrupted_multitable_publish_recovers_before_read_or_write(tmp_path, phase):
    source_uri = _uri(tmp_path / 'source', 'sqlite')
    target_uri = _uri(tmp_path / 'target')
    with Database(source_uri) as source, Database(target_uri) as target:
        for name in ('a', 'b'):
            _table(source, name, {'id': 2, 'value': 'new'})
            _table(target, name, {'id': 1, 'value': 'old'})
        _table(target, 'keep', {'id': 9, 'value': 'unchanged'})
        child = multiprocessing.get_context('spawn').Process(
            target=_crash_during_flat_publish, args=(source_uri, target_uri, phase),
        )
        child.start()
        child.join(30)
        assert child.exitcode == 73
        if phase != 'COMMITTED':
            with pytest.raises(MigrationRecoveryRequired):
                target.migrate_to(_uri(tmp_path / 'preview'), dry_run=True)
        # 第一个普通操作即触发恢复；此前已打开的目标对象也必须刷新。
        target.table('a').write({'id': 3, 'value': 'after'})
        assert target.table('a').select(order_by='id') == [
            {'id': 1, 'value': 'old'}, {'id': 2, 'value': 'new'}, {'id': 3, 'value': 'after'},
        ]
        assert target.table('b').select(order_by='id') == [
            {'id': 1, 'value': 'old'}, {'id': 2, 'value': 'new'},
        ]
        assert target.table('keep').select() == [{'id': 9, 'value': 'unchanged'}]
        assert physical_root(target_uri) == Path(tmp_path / 'target')


def test_recovery_refuses_a_changed_staging_marker(tmp_path):
    source_uri = _uri(tmp_path / 'source', 'sqlite')
    target_uri = _uri(tmp_path / 'target')
    with Database(source_uri) as source, Database(target_uri) as target:
        _table(source, 'a', {'id': 2, 'value': 'new'})
        _table(target, 'a', {'id': 1, 'value': 'old'})
        child = multiprocessing.get_context('spawn').Process(
            target=_crash_during_flat_publish,
            args=(source_uri, target_uri, 'FLAT_COMMIT_DECIDED'),
        )
        child.start()
        child.join(30)
        assert child.exitcode == 73
        item = resource(target_uri)
        import json
        record = json.loads(item.state_path.read_text())['transaction']
        marker = (item.control_dir / (item.token + '.workspaces')
                  / record['operation_id'] / '__flat_publish__.json')
        try:
            marker.write_text('{"operation_id": "wrong"}')
            with pytest.raises(MigrationRecoveryRequired, match='标记身份'):
                target.table_names()
            assert (tmp_path / 'target' / 'a' / 'data').is_dir()
        finally:
            marker.write_text(json.dumps({'operation_id': record['operation_id']}))


def test_recovery_can_retry_after_metadata_update_error(tmp_path, monkeypatch):
    import iosql._parquet_flat_publish as flat

    source_uri = _uri(tmp_path / 'source', 'sqlite')
    target_uri = _uri(tmp_path / 'target')
    with Database(source_uri) as source, Database(target_uri) as target:
        _table(source, 'a', {'id': 2, 'value': 'new'})
        old = _table(target, 'a', {'id': 1, 'value': 'old'})
        child = multiprocessing.get_context('spawn').Process(
            target=_crash_during_flat_publish, args=(source_uri, target_uri, 'old-data'),
        )
        child.start()
        child.join(30)
        assert child.exitcode == 73
        original = flat._meta_backup
        calls = []

        def fail_after_backup(src, dst):
            original(src, dst)
            calls.append(True)
            raise sqlite3.OperationalError('模拟恢复写入之后发生故障')

        with monkeypatch.context() as scope:
            scope.setattr(flat, '_meta_backup', fail_after_backup)
            with pytest.raises(MigrationRecoveryRequired, match='恢复失败'):
                old.select()
        assert calls == [True]
        assert old.select(order_by='id') == [
            {'id': 1, 'value': 'old'}, {'id': 2, 'value': 'new'},
        ]
