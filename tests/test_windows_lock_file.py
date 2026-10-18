"""验证 Win32 按句柄拒绝重解析点、失败清理及锁文件替换防护。"""

import ctypes
import os
import time
from types import SimpleNamespace

import pytest

from iosql import coordination, file_lock
from iosql.exception import UnsafeOperation


def fake_open_api(*, attributes=0x80, tag=0, create_error=0, info_error=0,
                  convert_error=None):
    """记录真实调用顺序，用任意重解析标签验证检查不依赖符号链接类型。"""
    calls = []
    handle = 2**36 + 7

    def create(*args):
        calls.append(('create', *args))
        return ctypes.c_void_p(-1).value if create_error else handle

    def info(opened, kind, pointer, size):
        calls.append(('info', opened, kind, size))
        value = ctypes.cast(pointer, ctypes.POINTER(file_lock._FileAttributeTagInfo)).contents
        value.FileAttributes = attributes
        value.ReparseTag = tag
        return not info_error

    def open_fd(opened, flags):
        calls.append(('open_fd', opened, flags))
        if convert_error is not None:
            raise convert_error
        return 17

    def close(opened):
        calls.append(('close', opened))
        return 1

    return SimpleNamespace(create=create, info=info, open_fd=open_fd, close=close,
                           error=lambda: create_error or info_error, calls=calls)


def use_windows_api(monkeypatch, api):
    # 只替换当前模块的 os 引用，避免改变 pytest/pathlib 所见的运行平台。
    monkeypatch.setattr(file_lock, 'os', SimpleNamespace(
        name='nt', fspath=os.fspath, O_RDWR=os.O_RDWR, O_BINARY=0x8000, O_NOINHERIT=0x80,
    ))
    monkeypatch.setattr(file_lock, '_windows_api', lambda: api)


def test_windows_open_checks_handle_before_transferring_ownership(tmp_path, monkeypatch):
    """以不跟随模式打开且不共享删除；成功后只让 CRT 拥有原生句柄。"""
    api = fake_open_api()
    use_windows_api(monkeypatch, api)
    path = tmp_path / '库锁.lock'
    assert file_lock.open_lock_file(path) == 17
    assert api.calls == [
        ('create', str(path), 0xC0000000, 3, None, 4, 0x00200000, None),
        ('info', 2**36 + 7, 9, 8),
        ('open_fd', 2**36 + 7, os.O_RDWR | 0x8000 | 0x80),
    ]


@pytest.mark.parametrize('attributes,tag', [
    (0x400, 0xA000000C),  # 符号链接。
    (0x410, 0xA0000003),  # 挂载点或目录联接。
    (0x420, 0x9000001A),  # 云文件类重解析点。
    (0x400, 0),          # 即便标签未知也必须拒绝。
])
def test_windows_open_rejects_every_reparse_attribute(tmp_path, monkeypatch, attributes, tag):
    api = fake_open_api(attributes=attributes, tag=tag)
    use_windows_api(monkeypatch, api)
    with pytest.raises(UnsafeOperation, match='重解析点'):
        file_lock.open_lock_file(tmp_path / 'source.lock')
    assert [call[0] for call in api.calls] == ['create', 'info', 'close']
    assert api.calls[-1] == ('close', 2**36 + 7)


def test_windows_open_failure_does_not_close_invalid_handle(tmp_path, monkeypatch):
    api = fake_open_api(create_error=5)
    use_windows_api(monkeypatch, api)
    path = tmp_path / 'source.lock'
    with pytest.raises(OSError) as error:
        file_lock.open_lock_file(path)
    assert error.value.errno == 5
    assert error.value.filename == str(path)
    assert [call[0] for call in api.calls] == ['create']


def test_windows_attribute_failure_closes_handle_and_refuses_lock(tmp_path, monkeypatch):
    api = fake_open_api(info_error=5)
    use_windows_api(monkeypatch, api)
    with pytest.raises(OSError, match='读取库锁文件属性失败'):
        file_lock.open_lock_file(tmp_path / 'source.lock')
    assert [call[0] for call in api.calls] == ['create', 'info', 'close']


@pytest.mark.parametrize('error', [OSError('CRT 转换失败'), KeyboardInterrupt()])
def test_windows_fd_conversion_failure_closes_native_handle(tmp_path, monkeypatch, error):
    api = fake_open_api(convert_error=error)
    use_windows_api(monkeypatch, api)
    with pytest.raises(type(error)):
        file_lock.open_lock_file(tmp_path / 'source.lock')
    assert [call[0] for call in api.calls] == ['create', 'info', 'open_fd', 'close']
    assert api.calls[-1] == ('close', 2**36 + 7)


@pytest.mark.parametrize('exclusive', [False, True])
def test_coordinator_rejects_reparse_before_registering_or_locking_fd(tmp_path, monkeypatch, exclusive):
    """共享和独占租约都必须在登记描述符或尝试锁定之前拒绝重解析点。"""
    api = fake_open_api(attributes=0x400)
    use_windows_api(monkeypatch, api)

    def unexpected_lock(*args):
        raise AssertionError('不应锁定重解析点')

    monkeypatch.setattr(file_lock, 'FileLock', unexpected_lock)
    before = set(coordination._open_fds)
    coordinator = coordination.LocalCoordinator(coordination.resource(f'sqlite://{tmp_path}/source'))
    with pytest.raises(UnsafeOperation, match='重解析点'):
        coordinator.acquire(exclusive, time.monotonic())
    assert coordinator.fd is None
    assert coordination._open_fds == before
    assert [call[0] for call in api.calls] == ['create', 'info', 'close']


def test_real_lock_file_preserves_content_and_is_not_inherited(tmp_path):
    """已有锁文件不能被截断，转换后的描述符不能泄漏给子进程。"""
    path = tmp_path / '库锁.lock'
    path.write_bytes(b'preserve')
    fd = file_lock.open_lock_file(path)
    try:
        assert not os.get_inheritable(fd)
        assert os.read(fd, 8) == b'preserve'
    finally:
        os.close(fd)
    assert path.read_bytes() == b'preserve'


@pytest.mark.parametrize('target_exists', [False, True])
@pytest.mark.parametrize('exclusive', [False, True])
def test_real_coordinator_refuses_lock_symlink(tmp_path, target_exists, exclusive):
    """同时验证正常和悬空链接；拒绝过程中不得创建或修改链接目标。"""
    item = coordination.resource(f'sqlite://{tmp_path}/source')
    item.control_dir.mkdir()
    path = item.control_dir / (item.token + '.lock')
    target = tmp_path / 'target'
    if target_exists:
        target.write_bytes(b'preserve')
    try:
        path.symlink_to(target)
    except OSError as error:
        if os.name == 'nt' and error.winerror == 1314:
            pytest.skip('当前 Windows 用户缺少创建符号链接权限')
        raise
    before = set(coordination._open_fds)
    coordinator = coordination.LocalCoordinator(item)
    with pytest.raises(UnsafeOperation if os.name == 'nt' else OSError):
        coordinator.acquire(exclusive, time.monotonic())
    assert coordinator.fd is None
    assert coordination._open_fds == before
    assert path.is_symlink()
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_bytes() == b'preserve'


@pytest.mark.skipif(os.name != 'nt', reason='需要 Windows 文件共享删除语义')
@pytest.mark.parametrize('exclusive', [False, True])
def test_windows_held_lock_file_cannot_be_deleted_or_replaced(tmp_path, exclusive):
    """原机验证共享删除被禁止，释放租约之后路径才可被替换。"""
    item = coordination.resource(f'sqlite://{tmp_path}/source')
    path = item.control_dir / (item.token + '.lock')
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'replacement')
    coordinator = coordination.LocalCoordinator(item)
    coordinator.acquire(exclusive, time.monotonic())
    try:
        assert not os.get_inheritable(coordinator.fd)
        with pytest.raises(OSError):
            path.unlink()
        with pytest.raises(OSError):
            os.replace(replacement, path)
    finally:
        coordinator.release()
    os.replace(replacement, path)
    assert path.read_bytes() == b'replacement'
