"""跨平台文件租约；Windows 与 POSIX 均支持共享和独占。"""

from __future__ import annotations

import ctypes
import errno
import os
from functools import lru_cache
from types import SimpleNamespace

from .exception import UnsafeOperation


class _FileAttributeTagInfo(ctypes.Structure):
    """已打开句柄的属性与重解析标签，用于拒绝所有重解析点。"""

    _fields_ = [('FileAttributes', ctypes.c_uint32), ('ReparseTag', ctypes.c_uint32)]


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
        ('Offset', ctypes.c_uint32), ('OffsetHigh', ctypes.c_uint32),
        ('hEvent', ctypes.c_void_p),
    ]


@lru_cache(maxsize=1)
def _windows_api():
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signature = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                 ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_Overlapped)]
    kernel.LockFileEx.argtypes = signature
    kernel.LockFileEx.restype = ctypes.c_int
    kernel.UnlockFileEx.argtypes = signature[:1] + signature[2:]
    kernel.UnlockFileEx.restype = ctypes.c_int
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return SimpleNamespace(lock=kernel.LockFileEx, unlock=kernel.UnlockFileEx,
                           handle=msvcrt.get_osfhandle, error=ctypes.get_last_error,
                           create=kernel.CreateFileW, info=kernel.GetFileInformationByHandleEx,
                           close=kernel.CloseHandle, open_fd=msvcrt.open_osfhandle)


def _open_windows_lock_file(path):
    """检查句柄后才交给 CRT，避免路径检查与打开之间的替换竞态。"""
    api = _windows_api()
    path = os.fspath(path)
    # GENERIC_READ | GENERIC_WRITE、OPEN_ALWAYS、FILE_FLAG_OPEN_REPARSE_POINT。
    # 仅共享读写（1 | 2），不共享删除，以阻止持锁期间删除或重命名锁文件。
    handle = api.create(path, 0xC0000000, 3, None, 4, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(api.error(), 'CreateFileW 打开库锁文件失败', path)
    try:
        info = _FileAttributeTagInfo()
        if not api.info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # FileAttributeTagInfo。
            raise OSError(api.error(), '读取库锁文件属性失败', path)
        if info.FileAttributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT，不只拒绝符号链接。
            raise UnsafeOperation('库锁文件不能是重解析点')
        flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)
        # 转换成功后描述符拥有句柄，统一由 os.close 关闭。
        return api.open_fd(handle, flags)
    except BaseException:
        api.close(handle)
        raise


def open_lock_file(path):
    """安全打开库锁文件；Windows 检查句柄，POSIX 拒绝跟随符号链接。"""
    if os.name == 'nt':
        return _open_windows_lock_file(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_CLOEXEC', 0)
    return os.open(path, flags, 0o600)


class WindowsFileLock:
    def __init__(self, fd):
        self.api = _windows_api()
        self.handle = self.api.handle(fd)
        self.overlapped = _Overlapped()

    def acquire(self, exclusive):
        # FAIL_IMMEDIATELY；未设置 EXCLUSIVE_LOCK 时为真正的共享锁。
        flags = 1 | (2 if exclusive else 0)
        if not self.api.lock(self.handle, flags, 0, 1, 0, ctypes.byref(self.overlapped)):
            error = self.api.error()
            if error == 33:  # ERROR_LOCK_VIOLATION
                raise BlockingIOError(errno.EAGAIN, '库文件锁正被其他操作持有')
            raise OSError(error, 'LockFileEx 获取库文件锁失败')

    def release(self):
        if not self.api.unlock(self.handle, 0, 1, 0, ctypes.byref(self.overlapped)):
            raise OSError(self.api.error(), 'UnlockFileEx 释放库文件锁失败')


class FileLock:
    """单个已打开 FD 的非阻塞锁；只释放本实例成功取得的锁。"""

    def __init__(self, fd):
        self.fd = fd
        self.locked = False
        try:
            import fcntl
        except ImportError:
            self.fcntl = None
            self.windows = WindowsFileLock(fd)
        else:
            self.fcntl = fcntl
            self.windows = None

    def acquire(self, exclusive):
        if self.fcntl is None:
            self.windows.acquire(exclusive)
        else:
            mode = self.fcntl.LOCK_EX if exclusive else self.fcntl.LOCK_SH
            self.fcntl.flock(self.fd, mode | self.fcntl.LOCK_NB)
        self.locked = True

    def release(self):
        if not self.locked:
            return
        try:
            if self.fcntl is None:
                self.windows.release()
            else:
                self.fcntl.flock(self.fd, self.fcntl.LOCK_UN)
        finally:
            self.locked = False
