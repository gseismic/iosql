"""覆盖大小写保真的真实访问，以及不表现为符号链接的重解析点。"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from iosql import Database, coordination
from iosql.exception import UnsafeOperation
from iosql.migration import backup_snapshot, copy_snapshot


@pytest.fixture
def lowercase_identity(monkeypatch):
    """在真实大小写敏感文件系统上模拟 Windows 的身份规范化。"""
    monkeypatch.setattr(coordination.os.path, 'normcase', lambda path: str(path).lower())


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
@pytest.mark.parametrize('exists', [False, True])
def test_resource_keeps_filesystem_case_separate_from_identity(tmp_path, lowercase_identity, backend, exists):
    path = tmp_path / 'CaseParent' / 'MyDB'
    if exists:
        path.mkdir(parents=True)
    item = coordination.resource(f'{backend}://{path}')
    actual = os.path.realpath(path)
    assert str(item.path) == actual
    assert item.key == 'local:' + actual.lower()
    assert str(item.control_dir) == str(Path(actual).parent / '.iosql-locks')


@pytest.mark.parametrize('published', [False, True])
def test_physical_path_keeps_actual_case_and_finds_matching_state(tmp_path, lowercase_identity, published):
    """控制记录身份可以小写，实际库根及其已发布代际必须保留大小写。"""
    path = tmp_path / 'CaseParent' / 'MyDB'
    path.mkdir(parents=True)
    item = coordination.resource(f'sqlite://{path}')
    actual = Path(os.path.realpath(path))
    expected = actual
    if published:
        generation = 'a' * 32
        relative = '__iosql_generations__/' + generation
        expected = actual / relative
        expected.mkdir(parents=True)
        (expected / '__generation__.json').write_text(json.dumps({'operation_id': generation}))
        coordination.LocalCoordinator(item).write_state(generation, physical_root=relative)
    assert str(coordination.physical_path(path)) == str(expected)
    assert str(coordination.physical_root(f'sqlite://{path}')) == str(expected)


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_database_access_does_not_redirect_to_lowercase_sibling(tmp_path, lowercase_identity, backend):
    """通过公开 API 写入，只能修改 URI 指定的 MyDB，不能接管 mydb。"""
    if os.name == 'nt':
        pytest.skip('Windows 大小写敏感目录由下方显式启用 fsutil 的原机用例验证')
    path = tmp_path / 'MyDB'
    sibling = tmp_path / 'mydb'
    sibling.mkdir()
    (sibling / 'keep.txt').write_bytes(b'unchanged')
    with Database(f'{backend}://{path}') as db:
        table = db.table('ticks', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 7}])
        assert table.select(to_dict=True) == [{'id': 7}]
    assert (path / 'ticks' / 'meta.sqlite3').is_file()
    assert sorted(child.name for child in sibling.iterdir()) == ['keep.txt']
    assert (sibling / 'keep.txt').read_bytes() == b'unchanged'


def mark_reparse(monkeypatch, path):
    """保留目录/普通文件类型，只模拟 Windows lstat 的重解析属性。"""
    original = Path.lstat

    def lstat(candidate, *args, **kwargs):
        value = original(candidate, *args, **kwargs)
        if str(candidate) == str(path):
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, 'lstat', lstat)


@pytest.mark.parametrize('name,is_directory', [
    ('junction', True),
    ('nested/junction', True),
    ('__iosql_generations__', True),
    ('payload.bin', False),
    ('meta.sqlite3-wal', False),
    ('.iosql.write.lock', False),
])
def test_snapshot_rejects_reparse_entries_before_filter_or_descent(tmp_path, monkeypatch, name, is_directory):
    """目录 junction 不是 symlink；被过滤的名字也必须先通过安全检查。"""
    source = tmp_path / 'source'
    source.mkdir()
    entry = source / name
    entry.parent.mkdir(parents=True, exist_ok=True)
    if is_directory:
        entry.mkdir()
        (entry / 'secret.txt').write_bytes(b'outside data')
    else:
        entry.write_bytes(b'outside data')
    mark_reparse(monkeypatch, entry)
    assert not entry.is_symlink()
    original_iterdir = Path.iterdir

    def iterdir(candidate):
        if str(candidate) == str(entry):
            raise AssertionError('检查重解析属性之前不得遍历目录')
        return original_iterdir(candidate)

    monkeypatch.setattr(Path, 'iterdir', iterdir)
    destination = tmp_path / 'snapshot'
    with pytest.raises(UnsafeOperation):
        copy_snapshot(source, destination)
    assert not (destination / name).exists()


def test_snapshot_rejects_reparse_root_before_creating_destination(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    mark_reparse(monkeypatch, source)
    destination = tmp_path / 'snapshot'
    with pytest.raises(UnsafeOperation):
        copy_snapshot(source, destination)
    assert not destination.exists()


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_backup_refuses_nested_reparse_directory(tmp_path, monkeypatch, backend):
    """完整有效库中的额外 junction 也不能通过备份入口带出库外内容。"""
    path = tmp_path / 'source'
    uri = f'{backend}://{path}'
    with Database(uri) as db:
        table = db.table('ticks', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 1}])
    junction = path / 'ticks' / 'outside'
    junction.mkdir()
    (junction / 'secret.txt').write_bytes(b'outside data')
    mark_reparse(monkeypatch, junction)
    destination = tmp_path / 'backup'
    with pytest.raises(UnsafeOperation):
        backup_snapshot(uri, destination)
    assert not (destination / 'ticks' / 'outside').exists()


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_migration_refuses_target_reparse_and_preserves_formal_view(tmp_path, monkeypatch, backend):
    """目标合并快照遇到重解析点时失败，正式目标及未选中的表不得变化。"""
    source_uri = f'{backend}://{tmp_path}/source'
    target_path = tmp_path / 'target'
    with Database(source_uri) as source, Database(f'{backend}://{target_path}') as target:
        source.table('ticks', pk=['id'], columns={'id': 'integer'}).write([{'id': 1}])
        target.table('keep', pk=['id'], columns={'id': 'integer'}).write([{'id': 99}])
        junction = target_path / 'keep' / 'outside'
        junction.mkdir()
        (junction / 'secret.txt').write_bytes(b'outside data')
        mark_reparse(monkeypatch, junction)
        with pytest.raises(UnsafeOperation):
            source.migrate_to(target.uri)
        assert target.table_names() == ['keep']
        assert target.table('keep').select(to_dict=True) == [{'id': 99}]
        assert (junction / 'secret.txt').read_bytes() == b'outside data'


@pytest.fixture
def windows_case_sensitive_parent(tmp_path):
    if os.name != 'nt':
        pytest.skip('需要 Windows 大小写敏感目录')
    parent = tmp_path / 'CaseParent'
    parent.mkdir()
    result = subprocess.run(
        ['fsutil.exe', 'file', 'setCaseSensitiveInfo', str(parent), 'enable'],
        capture_output=True, check=False,
    )
    if result.returncode:
        pytest.skip('当前 Windows 环境没有启用目录大小写敏感的能力或权限')
    return parent


@pytest.mark.parametrize('backend', ['sqlite', 'parquet'])
def test_windows_case_sensitive_database_uses_uri_path(windows_case_sensitive_parent, backend):
    """原机在同一父目录创建 MyDB/mydb，验证真实访问不会落到小写兄弟库。"""
    parent = windows_case_sensitive_parent
    path, sibling = parent / 'MyDB', parent / 'mydb'
    sibling.mkdir()
    (sibling / 'keep.txt').write_bytes(b'unchanged')
    with Database(f'{backend}://{path}') as db:
        table = db.table('ticks', pk=['id'], columns={'id': 'integer'})
        table.write([{'id': 7}])
        assert table.select(to_dict=True) == [{'id': 7}]
    assert not os.path.samefile(path, sibling)
    assert (path / 'ticks' / 'meta.sqlite3').is_file()
    assert sorted(child.name for child in sibling.iterdir()) == ['keep.txt']


@pytest.mark.skipif(os.name != 'nt', reason='需要 Windows 真实目录 junction')
@pytest.mark.parametrize('nested', [False, True])
def test_windows_snapshot_refuses_real_junction(tmp_path, nested):
    """mklink /J 不需要 symlink 特权，验证真正的库外目录联接不会被递归。"""
    source, outside = tmp_path / 'source', tmp_path / 'outside'
    source.mkdir()
    outside.mkdir()
    (outside / 'secret.txt').write_bytes(b'outside data')
    junction = source / 'nested' / 'junction' if nested else source / 'junction'
    junction.parent.mkdir(parents=True, exist_ok=True)
    # cmd 内建 mklink 没有可直接执行的二进制；路径由测试临时目录生成并加引号。
    result = subprocess.run(
        ['cmd.exe', '/d', '/c', f'mklink /J "{junction}" "{outside}"'],
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        assert not junction.is_symlink()
        assert junction.lstat().st_file_attributes & 0x400
        destination = tmp_path / 'snapshot'
        with pytest.raises(UnsafeOperation):
            copy_snapshot(source, destination)
        assert not (destination / junction.relative_to(source)).exists()
        assert (outside / 'secret.txt').read_bytes() == b'outside data'
    finally:
        junction.rmdir()
