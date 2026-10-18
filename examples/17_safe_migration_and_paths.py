"""安全迁移、大小写路径及链接拒绝示例；只操作脚本自己创建的临时目录。

运行：uv run --with-editable . python examples/17_safe_migration_and_paths.py
Windows 的大小写敏感目录及符号链接需要对应系统能力/权限，未满足时会明确跳过。
"""

import errno
import os
import subprocess
import tempfile
from pathlib import Path

from iosql import Database
from iosql.coordination import lease, resource
from iosql.exception import UnsafeOperation
from iosql.migration import backup_snapshot


def write_rows(uri, name, rows):
    """创建并关闭示例表，让后面的维护操作处理完整持久数据。"""
    with Database(uri) as database:
        table = database.table(name, pk=['id'], columns={'id': 'integer'})
        result = table.write([{'id': value} for value in rows])
        assert result.rows_written == len(rows)


def demonstrate_case_preservation(root):
    parent = root / 'CaseParent'
    parent.mkdir()
    if os.name == 'nt':
        enabled = subprocess.run(
            ['fsutil.exe', 'file', 'setCaseSensitiveInfo', str(parent), 'enable'],
            capture_output=True, check=False,
        )
        if enabled.returncode:
            print('跳过 Windows 大小写敏感目录：当前环境不支持或缺少权限')
            return
    path, sibling = parent / 'MyDB', parent / 'mydb'
    sibling.mkdir()
    marker = sibling / 'keep.txt'
    marker.write_bytes(b'unchanged')
    uri = f'sqlite://{path}'
    write_rows(uri, 'ticks', [7])
    with Database(uri) as database:
        assert database.table('ticks').select(to_dict=True) == [{'id': 7}]
    assert (path / 'ticks' / 'meta.sqlite3').is_file()
    assert not os.path.samefile(path, sibling)
    assert sorted(child.name for child in sibling.iterdir()) == ['keep.txt']
    assert marker.read_bytes() == b'unchanged'
    print('大小写路径通过：MyDB 正常读写，mydb 保持完整')


def demonstrate_migration_and_backup(root):
    source_uri = f'sqlite://{root}/MigrationSource'
    target_uri = f'parquet://{root}/Archive'
    # 合法业务表名不能被当作 WAL 文件或内部锁目录而跳过。
    write_rows(source_uri, 'events-wal', [1, 2])
    write_rows(target_uri, '.iosql-locks', [99])
    with Database(source_uri) as source:
        preview = source.migrate_to(target_uri, dry_run=True, verify='row-count')
        assert not preview.published
        result = source.migrate_to(target_uri, verify='row-count')
        assert result.published and result.verified
    with Database(target_uri) as archive:
        assert archive.table('events-wal').count_rows() == 2
        assert archive.table('.iosql-locks').select(to_dict=True) == [{'id': 99}]
    backup = root / 'Snapshot'
    # 与 CLI 的 iosql backup 共用快照入口；目标路径必须尚未存在。
    backup_snapshot(source_uri, backup)
    with Database(f'sqlite://{backup}') as restored:
        assert restored.table('events-wal').count_rows() == 2
    print('迁移与备份通过：row-count 校验成功，未选中的合法业务表保留')


def demonstrate_snapshot_link_rejection(root):
    source = root / 'LinkedSource'
    uri = f'sqlite://{source}'
    write_rows(uri, 'ticks', [1])
    outside = root / 'Outside'
    outside.mkdir()
    secret = outside / 'secret.txt'
    secret.write_bytes(b'outside data')
    link = source / 'ticks' / 'outside'
    if os.name == 'nt':
        # mklink 是 cmd 内建命令；只使用本示例的临时目录并为路径加引号。
        linked = subprocess.run(
            ['cmd.exe', '/d', '/c', f'mklink /J "{link}" "{outside}"'],
            capture_output=True, check=False,
        )
        if linked.returncode:
            print('跳过 Windows junction：当前文件系统或权限不支持创建目录联接')
            return
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        try:
            backup_snapshot(uri, root / 'RejectedSnapshot')
        except UnsafeOperation as error:
            print('快照链接已拒绝:', error)
        else:
            raise AssertionError('快照不应遍历指向库外的目录链接')
        assert secret.read_bytes() == b'outside data'
    finally:
        # Windows 删除 junction 本身，POSIX 删除符号链接，不递归处理链接目标。
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()
    backup_snapshot(uri, root / 'CleanSnapshot')
    print('清除链接后备份通过，库外文件保持完整')


def demonstrate_lock_link_rejection(root):
    item = resource(f'sqlite://{root}/LockedSource')
    item.control_dir.mkdir(exist_ok=True)
    link = item.control_dir / (item.token + '.lock')
    foreign = root / 'foreign.lock'
    foreign.write_bytes(b'unchanged')
    try:
        link.symlink_to(foreign)
    except OSError as error:
        if os.name == 'nt' and error.winerror == 1314:
            print('跳过 Windows 锁文件符号链接：缺少创建符号链接权限')
            return
        raise
    try:
        try:
            with lease(item.uri, exclusive=True):
                raise AssertionError('不应从符号链接取得库锁')
        except UnsafeOperation as error:
            print('Windows 库锁重解析点已拒绝:', error)
        except OSError as error:
            if os.name == 'nt' or error.errno not in (errno.ELOOP, errno.EMLINK):
                raise
            print('POSIX 库锁符号链接已拒绝:', error.strerror)
        assert foreign.read_bytes() == b'unchanged'
    finally:
        link.unlink()


def run():
    runtime = Path(__file__).resolve().parent / 'runtime'
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='17-safe-paths-', dir=runtime) as directory:
        root = Path(directory)
        demonstrate_case_preservation(root)
        demonstrate_migration_and_backup(root)
        demonstrate_snapshot_link_rejection(root)
        demonstrate_lock_link_rejection(root)
    platform = 'Windows' if os.name == 'nt' else 'POSIX（本次不验证 Windows 原机行为）'
    print(f'示例执行完成，运行平台：{platform}；临时演示目录已清理')


if __name__ == '__main__':
    run()
