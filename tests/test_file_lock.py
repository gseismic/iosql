"""Win32 调用契约、失败释放及真实文件租约回归。"""

import builtins
import ctypes
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from iosql import coordination, file_lock
from iosql.exception import DatabaseBusy


def fake_api(error=0, unlock_error=0):
    calls = []
    state = {'error': error}

    def lock(handle, flags, reserved, low, high, pointer):
        overlapped = ctypes.cast(pointer, ctypes.POINTER(file_lock._Overlapped)).contents
        calls.append(('lock', handle, flags, reserved, low, high, overlapped.Offset, overlapped.OffsetHigh))
        state['error'] = error
        return not error

    def unlock(handle, reserved, low, high, pointer):
        calls.append(('unlock', handle, reserved, low, high))
        state['error'] = unlock_error
        return not unlock_error

    return SimpleNamespace(lock=lock, unlock=unlock, handle=lambda fd: 2**36 + fd,
                           error=lambda: state['error'], calls=calls)


@pytest.mark.parametrize('exclusive', [False, True])
def test_win32_lock_flags_and_matching_unlock(monkeypatch, exclusive):
    api = fake_api()
    monkeypatch.setattr(file_lock, '_windows_api', lambda: api)
    lock = file_lock.WindowsFileLock(7)
    lock.acquire(exclusive)
    lock.release()
    assert api.calls == [('lock', 2**36 + 7, 3 if exclusive else 1, 0, 1, 0, 0, 0),
                         ('unlock', 2**36 + 7, 0, 1, 0)]


@pytest.mark.parametrize('error', [33, 5, 997])
def test_win32_lock_errors_are_not_silently_accepted(monkeypatch, error):
    api = fake_api(error=error)
    monkeypatch.setattr(file_lock, '_windows_api', lambda: api)
    lock = file_lock.WindowsFileLock(7)
    with pytest.raises(BlockingIOError if error == 33 else OSError):
        lock.acquire(True)


def test_win32_unlock_error_is_reported(monkeypatch):
    api = fake_api(unlock_error=5)
    monkeypatch.setattr(file_lock, '_windows_api', lambda: api)
    lock = file_lock.WindowsFileLock(7)
    lock.acquire(False)
    with pytest.raises(OSError):
        lock.release()


def test_win32_bindings_preserve_pointer_sized_handles(monkeypatch):
    class NativeFunction:
        def __call__(self, *args):
            return 1

    kernel = SimpleNamespace(
        LockFileEx=NativeFunction(), UnlockFileEx=NativeFunction(),
        CreateFileW=NativeFunction(), GetFileInformationByHandleEx=NativeFunction(),
        CloseHandle=NativeFunction(),
    )
    loaded = []

    def loader(name, **kwargs):
        loaded.append((name, kwargs))
        return kernel

    monkeypatch.setattr(ctypes, 'WinDLL', loader, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 0, raising=False)
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(
        get_osfhandle=lambda fd: 2**36 + fd, open_osfhandle=lambda handle, flags: 7,
    ))
    api = file_lock._windows_api.__wrapped__()
    assert loaded == [('kernel32', {'use_last_error': True})]
    assert api.handle(7) == 2**36 + 7
    assert kernel.LockFileEx.argtypes[0] is ctypes.c_void_p
    assert kernel.UnlockFileEx.argtypes[0] is ctypes.c_void_p
    assert len(kernel.LockFileEx.argtypes) == 6
    assert len(kernel.UnlockFileEx.argtypes) == 5
    # 新增打开接口也必须保留 64 位句柄，并使用宽字符文件名。
    assert kernel.CreateFileW.argtypes[0] is ctypes.c_wchar_p
    assert kernel.CreateFileW.restype is ctypes.c_void_p
    assert kernel.GetFileInformationByHandleEx.argtypes[0] is ctypes.c_void_p
    assert kernel.CloseHandle.argtypes == [ctypes.c_void_p]
    assert ctypes.sizeof(file_lock._FileAttributeTagInfo) == 8


def test_failed_portable_acquisition_closes_fd_without_unlock(tmp_path, monkeypatch):
    api = fake_api(error=33)
    monkeypatch.setattr(file_lock, '_windows_api', lambda: api)
    # 本例只验证字节锁失败的清理，使用真实描述符隔离原生打开接口的替身。
    monkeypatch.setattr(file_lock, 'open_lock_file',
                        lambda path: os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    original_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == 'fcntl':
            raise ImportError('模拟 Windows')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_fcntl)
    before = set(coordination._open_fds)
    coordinator = coordination.LocalCoordinator(coordination.resource(f'sqlite://{tmp_path}/source'))
    with pytest.raises(DatabaseBusy):
        coordinator.acquire(True, time.monotonic())
    assert coordinator.fd is None
    assert coordination._open_fds == before
    assert [call[0] for call in api.calls] == ['lock']


def test_portable_lock_timeout_preserves_deadline_and_closes_fd(tmp_path, monkeypatch):
    api = fake_api(error=33)
    monkeypatch.setattr(file_lock, '_windows_api', lambda: api)
    monkeypatch.setattr(file_lock, 'open_lock_file',
                        lambda path: os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    original_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == 'fcntl':
            raise ImportError('模拟 Windows')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_fcntl)
    ticks = iter([10.0, 10.02])
    slept = []
    monkeypatch.setattr(coordination, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=slept.append))
    coordinator = coordination.LocalCoordinator(coordination.resource(f'sqlite://{tmp_path}/source'))
    with pytest.raises(DatabaseBusy):
        coordinator.acquire(True, 10.015)
    assert slept == [0.01]
    assert coordinator.fd is None
    assert [call[0] for call in api.calls] == ['lock', 'lock']


def test_windows_directory_sync_does_not_open_directory_as_crt_file(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('Windows 不应通过 CRT 打开目录做 fsync')
    monkeypatch.setattr(coordination, 'os', SimpleNamespace(name='nt', open=unexpected))
    coordination.sync_directory(tmp_path)


@pytest.mark.parametrize('exclusive', [False, True])
def test_real_file_lock_shares_readers_and_excludes_maintenance(tmp_path, exclusive):
    source_uri = f'sqlite://{tmp_path}/source'
    observed = []

    def check():
        for requested in [False, True]:
            try:
                with coordination.lease(source_uri, exclusive=requested):
                    observed.append((requested, 'acquired'))
            except DatabaseBusy:
                observed.append((requested, 'busy'))

    with coordination.lease(source_uri, exclusive=exclusive):
        worker = threading.Thread(target=check)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
    assert observed == [(False, 'busy' if exclusive else 'acquired'), (True, 'busy')]
    with coordination.lease(source_uri, exclusive=True):
        pass
