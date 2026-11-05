"""Parquet 目录库的平坦布局发布和可重放恢复。"""

import inspect
import os
import shutil
import sqlite3
import stat
import warnings
from contextlib import closing
from .coordination import atomic_json, ensure_directory, sync_directory
from .exception import MigrationRecoveryRequired, UnsafeOperation
from .utils.storage_identity import is_iosql_table


def _identity(path, kind, *, missing=False):
    try:
        value = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise MigrationRecoveryRequired(f'发布所需路径不存在: {path}')
    if (stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400
            or not kind(value.st_mode)):
        raise MigrationRecoveryRequired(f'发布路径不是预期的普通文件或目录: {path}')
    return [value.st_dev, value.st_ino]


def _matches(path, expected, kind):
    return _identity(path, kind, missing=True) == expected


def _move(src, dst):
    if os.path.lexists(dst):
        raise MigrationRecoveryRequired(f'发布目标已存在，拒绝覆盖: {dst}')
    os.rename(src, dst)
    sync_directory(src.parent)
    if src.parent != dst.parent:
        sync_directory(dst.parent)


def _child_dir(parent, name):
    path = parent / name
    if os.path.lexists(path):
        _identity(path, stat.S_ISDIR)
    else:
        path.mkdir()
        sync_directory(parent)
    return path


def _candidate(coordinator, record):
    item = coordinator.resource
    operation_id = record.get('operation_id')
    if (not isinstance(operation_id, str) or len(operation_id) != 32
            or any(ch not in '0123456789abcdef' for ch in operation_id)):
        raise MigrationRecoveryRequired('平坦发布任务 ID 非法')
    location = record.get('location')
    if location == 'control':
        parent = item.control_dir / (item.token + '.workspaces')
    elif location == 'root':
        parent = item.path / '__iosql_migrations__'
    else:
        raise MigrationRecoveryRequired('平坦发布暂存位置非法')
    _identity(parent, stat.S_ISDIR)
    candidate = parent / operation_id
    if _identity(candidate, stat.S_ISDIR) != record.get('candidate_identity'):
        raise MigrationRecoveryRequired('平坦发布暂存目录身份不匹配')
    marker = candidate / '__flat_publish__.json'
    _identity(marker, stat.S_ISREG)
    try:
        import json
        with marker.open(encoding='utf-8') as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MigrationRecoveryRequired('平坦发布标记无法读取') from exc
    if value != {'operation_id': operation_id}:
        raise MigrationRecoveryRequired('平坦发布标记身份不匹配')
    return candidate


def _meta_backup(source, target):
    # 原目标文件 inode 不变：已打开的 SQLite 元数据连接不会因路径重用冲突。
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(str(target), timeout=30)) as dst:
            dst.execute('PRAGMA synchronous=FULL')
            src.backup(dst)
    sync_directory(target.parent)


def _entry(root, payload, name):
    staged = payload / name
    dest = root / name
    staged_id = _identity(staged, stat.S_ISDIR)
    staged_meta = _identity(staged / 'meta.sqlite3', stat.S_ISREG)
    staged_data = _identity(staged / 'data', stat.S_ISDIR, missing=True)
    dest_id = _identity(dest, stat.S_ISDIR, missing=True)
    if dest_id is None:
        dest_meta = dest_data = None
    else:
        dest_meta = _identity(dest / 'meta.sqlite3', stat.S_ISREG)
        dest_data = _identity(dest / 'data', stat.S_ISDIR, missing=True)
    return {'name': name, 'staged': staged_id, 'staged_meta': staged_meta,
            'staged_data': staged_data, 'destination': dest_id,
            'destination_meta': dest_meta, 'destination_data': dest_data}


def publish(publisher, operation_id):
    """准备完整暂存，记录提交决定并通过恢复流程一次性发布。"""
    from .migration import _default_workspace_root, _snapshot_stat, _sync_tree, copy_snapshot

    item = publisher.item
    root = item.path
    if publisher.previous != root:
        raise UnsafeOperation('旧代目录需要先整理为普通 Parquet 目录后才能迁移到该库')
    root.mkdir(parents=True, exist_ok=True)
    _identity(root, stat.S_ISDIR)
    parent = _default_workspace_root(None, item)
    ensure_directory(parent)
    _snapshot_stat(parent)
    location = 'root' if parent == root / '__iosql_migrations__' else 'control'
    candidate = parent / operation_id
    workspace = publisher.workspace
    if candidate != workspace:
        candidate.mkdir(mode=0o700)
        source = workspace / 'payload'
        payload = candidate / 'payload'
        if source.stat().st_dev == candidate.stat().st_dev:
            os.rename(source, payload)
        else:
            copy_snapshot(source, payload)
    payload = candidate / 'payload'
    if candidate.stat().st_dev != root.stat().st_dev:
        raise UnsafeOperation('平坦发布暂存与目标不在同一文件系统')
    atomic_json(candidate / '__flat_publish__.json', {'operation_id': operation_id})
    _sync_tree(candidate)
    sync_directory(parent)
    entries = [_entry(root, payload, name) for name in publisher.flat_table_names]
    state = publisher.coordinator.state()
    record = {'state': 'FLAT_COMMIT_DECIDED', 'operation_id': operation_id,
              'location': location, 'candidate_identity': _identity(candidate, stat.S_ISDIR),
              'tables': entries}
    publisher.flat_candidate = candidate
    publisher.decided = True
    publisher.coordinator.write_state(state.get('generation'), record, physical_root=None)
    recover(publisher.coordinator, publisher.coordinator.state())
    # 记录完成后在锁内隔离暂存；清理可在解锁后进行，不会误删新库。
    if candidate.stat().st_dev == item.control_dir.stat().st_dev:
        try:
            retired = item.control_dir / (item.token + '.retired')
            ensure_directory(retired)
            _identity(retired, stat.S_ISDIR)
            destination = retired / operation_id
            _move(candidate, destination)
            publisher.retired = destination
            publisher.flat_candidate = None
            if parent == root / '__iosql_migrations__':
                try:
                    parent.rmdir()
                except OSError:
                    pass
        except (OSError, MigrationRecoveryRequired) as exc:
            warnings.warn(f'已发布，平坦发布暂存未退休: {exc}', RuntimeWarning)
    else:
        # 挂载点根目录不能移到父目录的其他文件系统；持有目录描述符后在锁外清理。
        if (os.name == 'posix' and shutil.rmtree.avoids_symlink_attacks
                and 'dir_fd' in inspect.signature(shutil.rmtree).parameters):
            fd = None
            root_fd = None
            try:
                fd = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                             | getattr(os, 'O_NOFOLLOW', 0))
                parent_stat = os.fstat(fd)
                if parent == root / '__iosql_migrations__' and os.rmdir in os.supports_dir_fd:
                    root_fd = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                                      | getattr(os, 'O_NOFOLLOW', 0))
                    linked_parent = os.stat(parent.name, dir_fd=root_fd, follow_symlinks=False)
                    if (linked_parent.st_dev, linked_parent.st_ino) != (parent_stat.st_dev, parent_stat.st_ino):
                        raise MigrationRecoveryRequired('暂存目录父路径身份变化')
                publisher.flat_cleanup = (fd, operation_id, _identity(candidate, stat.S_ISDIR),
                                          root_fd, [parent_stat.st_dev, parent_stat.st_ino])
            except (OSError, MigrationRecoveryRequired) as exc:
                if fd is not None:
                    os.close(fd)
                if root_fd is not None:
                    os.close(root_fd)
                warnings.warn(f'已发布，暂存保留在 {candidate}: {exc}', RuntimeWarning)
        else:
            warnings.warn(f'已发布，暂存保留在 {candidate}', RuntimeWarning)


def _recover_existing(candidate, payload, root, entry):
    name = entry['name']
    staged, dest = payload / name, root / name
    if _identity(staged, stat.S_ISDIR) != entry['staged']:
        raise MigrationRecoveryRequired(f'暂存表身份不匹配: {name}')
    if _identity(dest, stat.S_ISDIR) != entry['destination']:
        raise MigrationRecoveryRequired(f'目标表身份不匹配: {name}')
    staged_meta, dest_meta = staged / 'meta.sqlite3', dest / 'meta.sqlite3'
    if (_identity(staged_meta, stat.S_ISREG) != entry['staged_meta']
            or _identity(dest_meta, stat.S_ISREG) != entry['destination_meta']):
        raise MigrationRecoveryRequired(f'表元数据身份不匹配: {name}')
    old_data, new_data = entry['destination_data'], entry['staged_data']
    target_data, stage_data = dest / 'data', staged / 'data'
    retired = _child_dir(_child_dir(candidate, 'retired'), name)
    backup_data = retired / 'data'
    if old_data is not None:
        if _matches(target_data, old_data, stat.S_ISDIR):
            _move(target_data, backup_data)
        elif not _matches(backup_data, old_data, stat.S_ISDIR):
            raise MigrationRecoveryRequired(f'原数据目录身份不匹配: {name}')
    elif backup_data.exists():
        raise MigrationRecoveryRequired(f'意外出现原数据目录: {name}')
    if new_data is not None:
        if _matches(stage_data, new_data, stat.S_ISDIR):
            _move(stage_data, target_data)
        elif not _matches(target_data, new_data, stat.S_ISDIR):
            raise MigrationRecoveryRequired(f'新数据目录身份不匹配: {name}')
    elif target_data.exists():
        raise MigrationRecoveryRequired(f'意外出现目标数据目录: {name}')
    _meta_backup(staged_meta, dest_meta)


def _recover(coordinator, state):
    from .engines.sqlite.table import Table

    record = state.get('transaction') or {}
    if record.get('state') != 'FLAT_COMMIT_DECIDED':
        return
    item = coordinator.resource
    if item.backend != 'parquet' or state.get('physical_root') is not None:
        raise MigrationRecoveryRequired('平坦发布控制记录不适用于该目标')
    candidate = _candidate(coordinator, record)
    root, payload = item.path, candidate / 'payload'
    _identity(root, stat.S_ISDIR)
    _identity(payload, stat.S_ISDIR)
    entries = record.get('tables')
    if not isinstance(entries, list) or not entries:
        raise MigrationRecoveryRequired('平坦发布表清单非法')
    names = set()
    required = {'name', 'staged', 'staged_meta', 'staged_data', 'destination',
                'destination_meta', 'destination_data'}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required:
            raise MigrationRecoveryRequired('平坦发布表记录非法')
        name = entry['name']
        try:
            valid = isinstance(name, str) and Table._validate_table_name(name) == name
        except Exception as exc:
            raise MigrationRecoveryRequired('平坦发布表名非法') from exc
        if not valid or name in names:
            raise MigrationRecoveryRequired('平坦发布表名非法或重复')
        names.add(name)
    for entry in entries:
        name = entry['name']
        staged, dest = payload / name, root / name
        if entry['destination'] is not None:
            _recover_existing(candidate, payload, root, entry)
        elif _matches(staged, entry['staged'], stat.S_ISDIR):
            _move(staged, dest)
        elif not _matches(dest, entry['staged'], stat.S_ISDIR):
            raise MigrationRecoveryRequired(f'新表目录身份不匹配: {name}')
    for name in names:
        if not is_iosql_table(root / name, deep=True, backend='parquet'):
            raise MigrationRecoveryRequired(f'平坦发布后的表完整性校验失败: {name}')
    op = record['operation_id']
    coordinator.write_state(op, {'state': 'COMMITTED', 'operation_id': op}, physical_root=None)


def recover(coordinator, state):
    """在独占租约内幂等重放各表，完成前对其他 iosql 操作保持恢复屏障。"""
    try:
        return _recover(coordinator, state)
    except MigrationRecoveryRequired:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise MigrationRecoveryRequired('Parquet 平坦发布恢复失败，保留现场以便重试') from exc
