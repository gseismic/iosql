"""主 API 的库级租约；表级写锁继续保护日常复合写入。"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import math
import os
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .exception import (
    BadArgument, DatabaseBusy, MigrationLockTimeout,
    MigrationRecoveryRequired, UnsafeOperation, SchemaConflict,
)
from .utils.schema import parse_db_schema
from .utils.mysqlite.exception import NoSuchColumn


_FORMAT = 'iosql-coordination-v1'
_local = threading.local()
_open_fds = set()
_fds_lock = threading.Lock()
_UNSET = object()


def _after_fork():
    """子进程只关闭继承的描述符，不能 LOCK_UN 解掉父进程的锁。"""
    global _local, _open_fds, _fds_lock
    postgresql = sys.modules.get('iosql.coordination_postgresql')
    if postgresql is not None:
        postgresql.after_fork()
    for fd in _open_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    _open_fds = set()
    _fds_lock = threading.Lock()
    _local = threading.local()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


@dataclass(frozen=True)
class Resource:
    key: str
    uri: str
    backend: str
    path: Path | None = None

    @property
    def control_dir(self):
        return self.path.parent / '.iosql-locks'

    @property
    def token(self):
        return hashlib.sha256(self.key.encode()).hexdigest()

    @property
    def state_path(self):
        return self.control_dir / (self.token + '.state.json')


def resource(uri):
    schema, options = parse_db_schema(uri)
    if schema in ('sqlite', 'sqlite3', 'parquet'):
        if options.get('memory'):
            return Resource('memory:' + uri, uri, 'memory')
        path = Path(os.path.realpath(os.path.join(
            options.get('path') or '', options.get('db') or '',
        )))
        # 身份规范化不能改写实际路径，Windows 目录可以启用大小写敏感。
        key = 'local:' + os.path.normcase(str(path))
        return Resource(key, uri, schema, path)
    if schema == 'postgresql':
        parsed = urlsplit(uri)
        # 密码、用户名与 search_path 不改变同一数据库的协调锁。
        key = 'postgresql:{}:{}/{}'.format(
            (parsed.hostname or '').lower(), parsed.port or 5432,
            parsed.path.strip('/'),
        )
        return Resource(key, uri, schema)
    raise BadArgument('该后端没有库级协调协议')


def timeout_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadArgument('lock_timeout 必须是有限的非负秒数')
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise BadArgument('lock_timeout 必须是有限的非负秒数')
    return value


def sync_directory(path):
    if os.name == 'nt':
        # Windows 没有可直接等价替代 POSIX 目录 fsync 的接口。
        # 文件本身仍 fsync；不把此平台的原子替换描述为断电持久保证。
        return
    fd = os.open(os.fspath(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_directory(path):
    """新目录的父目录项也必须持久化，不能只同步目录内的文件。"""
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if missing:
        path.mkdir(parents=True, exist_ok=True)
        for created in reversed(missing):
            sync_directory(created.parent)


def atomic_json(path, value):
    """完成状态和 generation 必须包含在同一次原子替换中。"""
    path = Path(path)
    ensure_directory(path.parent)
    if path.is_symlink() or path.parent.is_symlink():
        raise UnsafeOperation('协调记录不能使用符号链接')
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class LocalCoordinator:
    def __init__(self, item):
        self.resource = item
        self.fd = None
        self.file_lock = None
        self.pid = os.getpid()

    def acquire(self, exclusive, deadline):
        from .file_lock import FileLock, open_lock_file

        directory = self.resource.control_dir
        if directory.is_symlink():
            raise UnsafeOperation('库锁目录不能是符号链接')
        ensure_directory(directory)
        path = directory / (self.resource.token + '.lock')
        self.fd = open_lock_file(path)
        with _fds_lock:
            _open_fds.add(self.fd)
        try:
            self.file_lock = FileLock(self.fd)
            while True:
                try:
                    self.file_lock.acquire(exclusive)
                    return
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DatabaseBusy('库正在迁移或仍有活跃操作，请稍后重试')
                    time.sleep(min(0.01, remaining))
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.pid != os.getpid():
            self.fd = None
            return
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if self.file_lock is not None:
                self.file_lock.release()
        finally:
            self.file_lock = None
            with _fds_lock:
                _open_fds.discard(fd)
            os.close(fd)

    def state(self):
        path = self.resource.state_path
        if path.is_symlink():
            raise MigrationRecoveryRequired('库控制记录是符号链接，拒绝访问')
        try:
            with path.open(encoding='utf-8') as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return {'generation': None, 'transaction': None}
        except (OSError, ValueError) as exc:
            raise MigrationRecoveryRequired('库控制记录无法读取，拒绝访问') from exc
        if (not isinstance(value, dict) or value.get('format') != _FORMAT
                or value.get('identity') != self.resource.key):
            raise MigrationRecoveryRequired('库控制记录身份不匹配，拒绝访问')
        transaction = value.get('transaction')
        if ((value.get('generation') is not None and not isinstance(value['generation'], str))
                or transaction is not None and (not isinstance(transaction, dict)
                or transaction.get('state') not in ('COMMIT_DECIDED', 'COMMITTED'))):
            raise MigrationRecoveryRequired('库控制记录格式非法，拒绝访问')
        _validated_root(self.resource.path, value.get('physical_root'))
        if value.get('physical_root') is not None and Path(value['physical_root']).name != value.get('generation'):
            raise MigrationRecoveryRequired('物理代际与 generation 不一致，拒绝访问')
        return value

    def write_state(self, generation, transaction=None, *, physical_root=_UNSET,
                    previous_state=_UNSET):
        previous = self.state() if previous_state is _UNSET else previous_state
        if physical_root is _UNSET:
            physical_root = previous.get('physical_root')
        completed = previous.get('transaction') or {}
        if completed.get('state') == 'COMMITTED':
            operation_id = completed.get('operation_id')
            if (not isinstance(operation_id, str) or len(operation_id) != 32
                    or any(c not in '0123456789abcdef' for c in operation_id)
                    or previous.get('generation') != operation_id):
                raise MigrationRecoveryRequired('已提交任务身份不合法，拒绝覆盖控制记录')
            archive = self.resource.control_dir / (self.resource.token + '.migrations')
            atomic_json(archive / (operation_id + '.json'), previous)
        atomic_json(self.resource.state_path, {
            'format': _FORMAT, 'identity': self.resource.key,
            'generation': generation, 'transaction': transaction,
            'physical_root': physical_root,
            'namespace_revision': previous.get('namespace_revision'),
        })
        entry = _held().get(self.resource.key)
        if entry is not None:
            entry.generation = generation

    def recover(self, state):
        from .migration import recover_local
        recover_local(self, state)


def physical_path(path):
    """调用方须持有逻辑库租约；物理代际路径不复用。"""
    path = Path(os.path.realpath(path))
    item = Resource('local:' + os.path.normcase(str(path)), '', 'sqlite', path)
    state = LocalCoordinator(item).state()
    return _validated_root(path, state.get('physical_root'))


def _validated_root(path, relative):
    if relative is None:
        return path
    parts = Path(relative).parts if isinstance(relative, str) else ()
    if (len(parts) != 2 or parts[0] != '__iosql_generations__'
            or len(parts[1]) != 32 or any(c not in '0123456789abcdef' for c in parts[1])):
        raise MigrationRecoveryRequired('库物理代际记录非法，拒绝访问')
    root = path.joinpath(*parts)
    if root.parent.is_symlink() or root.is_symlink() or not root.is_dir():
        raise MigrationRecoveryRequired('库的已发布物理代际缺失或不是普通目录')
    marker = root / '__generation__.json'
    try:
        if marker.is_symlink():
            raise ValueError('symlink')
        with marker.open(encoding='utf-8') as handle:
            identity = json.load(handle)
        if identity.get('operation_id') != parts[1]:
            raise ValueError('identity')
    except (OSError, ValueError, AttributeError) as exc:
        raise MigrationRecoveryRequired('物理代际身份无法确认，拒绝访问') from exc
    return root


def physical_root(uri):
    return physical_path(resource(uri).path)


@dataclass
class _Entry:
    coordinator: object
    exclusive: bool
    generation: str | None
    references: int = 1
    namespace_revision: str | None = None  # 同一物理代内重命名引起的缓存失效版本。


def _held():
    if not hasattr(_local, 'held'):
        _local.held = {}
    return _local.held


def _coordinator(item):
    if item.backend == 'postgresql':
        from .coordination_postgresql import PostgreSQLCoordinator
        return PostgreSQLCoordinator(item)
    return LocalCoordinator(item)


def needs_recovery(state):
    transaction = state.get('transaction')
    return bool(transaction and transaction.get('state') in ('COMMIT_DECIDED', 'PG_PENDING'))


def _release_entry(held, key, entry):
    entry.references -= 1
    if not entry.references:
        held.pop(key, None)
        try:
            entry.coordinator.release()
        finally:
            if not held:
                callbacks = getattr(_local, 'cleanup', [])
                _local.cleanup = []
                for callback in callbacks:
                    callback()


def after_leases(callback):
    """嵌套维护操作也必须等最外层库锁释放后再清理。"""
    if _held():
        if not hasattr(_local, 'cleanup'):
            _local.cleanup = []
        _local.cleanup.append(callback)
    else:
        callback()


@contextmanager
def lease(uri, *, exclusive=False, timeout=0.0, recover=True):
    """同线程复用租约；跨进程锁保持到最外层操作结束。"""
    item = uri if isinstance(uri, Resource) else resource(uri)
    if item.backend == 'memory':
        yield None
        return
    held = _held()
    existing = held.get(item.key)
    if existing is not None:
        if exclusive and not existing.exclusive:
            raise UnsafeOperation('当前线程仍持有读取流或共享租约，请先关闭流再迁移')
        existing.references += 1
        try:
            yield existing
        finally:
            _release_entry(held, item.key, existing)
        return
    deadline = time.monotonic() + timeout_value(timeout)
    coordinator = _coordinator(item)
    coordinator.acquire(exclusive, deadline)
    entry = None
    try:
        try:
            state = coordinator.state()
            if needs_recovery(state):
                if not recover:
                    raise MigrationRecoveryRequired(
                        '源库需要先恢复，预览不会自动执行恢复',
                        operation_id=state['transaction'].get('operation_id'))
                if not exclusive:
                    coordinator.release()
                    coordinator.acquire(True, deadline)
                    exclusive = True
                    state = coordinator.state()
                # 先登记独占租约，恢复内部不能再申请共享锁而降级。
                held[item.key] = _Entry(coordinator, True, state.get('generation'))
                if needs_recovery(state):
                    coordinator.recover(state)
                    state = coordinator.state()
        except MigrationRecoveryRequired as exc:
            exc.recovery_path = (str(item.state_path) if item.path is not None
                                 else '__iosql_control.migrations')
            raise
        entry = _Entry(coordinator, exclusive, state.get('generation'))
        entry.namespace_revision = state.get('namespace_revision')
        held[item.key] = entry
        yield entry
    finally:
        if entry is not None:
            _release_entry(held, item.key, entry)
        else:
            held.pop(item.key, None)
            coordinator.release()


@contextmanager
def exclusive_resources(uris, timeout=30.0):
    deadline = time.monotonic() + timeout_value(timeout)
    items = {resource(uri).key: uri for uri in uris}
    try:
        with ExitStack() as stack:
            entries = {}
            for key, uri in sorted(items.items()):
                entries[key] = stack.enter_context(lease(
                    uri, exclusive=True, timeout=max(0.0, deadline - time.monotonic()),
                ))
            yield entries
    except DatabaseBusy as exc:
        raise MigrationLockTimeout('等待迁移独占锁超过 lock_timeout') from exc


def _object_uri(owner, args=(), kwargs=None):
    if hasattr(owner, '_table'):
        owner = owner._table
    uri = getattr(owner, '_uri', None)
    if uri is None:
        uri = (kwargs or {}).get('uri') or (args[0] if args else None)
    return owner, uri


def _refresh(owner, generation, namespace_revision=None):
    """已打开对象也必须观察发布版本，不能继续读旧 inode 的连接。"""
    lock = owner.__dict__.setdefault('_iosql_refresh_lock', threading.RLock())
    with lock:
        missing = object()
        previous = getattr(owner, '_iosql_generation', missing)
        previous_revision = getattr(owner, '_iosql_namespace_revision', missing)
        owner._iosql_generation = generation
        owner._iosql_namespace_revision = namespace_revision
        if previous is missing or (previous == generation and previous_revision == namespace_revision):
            return
        refresh = getattr(owner, '_reload_after_migration', None)
        if refresh is not None:
            try:
                refresh()
            except BaseException:
                owner._iosql_generation = previous
                owner._iosql_namespace_revision = previous_revision
                raise


def invalidate_namespace(uri):
    """重命名在独占租约内发布失效版本，不改变物理代际指针。"""
    item = resource(uri)
    entry = _held().get(item.key)
    if entry is None or not entry.exclusive:
        raise UnsafeOperation('命名空间发布需要库独占租约')
    state = entry.coordinator.state()
    revision = uuid.uuid4().hex
    state.update(format=_FORMAT, identity=item.key, namespace_revision=revision)
    atomic_json(item.state_path, state)
    entry.namespace_revision = revision


@contextmanager
def operation(owner, *, uri=None, recover=True, refresh=True):
    target, derived = _object_uri(owner)
    uri = uri or derived
    if uri is None:
        yield
        return
    cached = getattr(target, '_iosql_resource', None)
    if cached is None or cached.uri != uri:
        cached = resource(uri)
        target._iosql_resource = cached
    with lease(cached, recover=recover) as entry:
        if entry is not None and refresh:
            _refresh(target, entry.generation, entry.namespace_revision)
        yield


def guarded(function):
    if getattr(function, '_iosql_guarded', False):
        return function
    if inspect.isgeneratorfunction(function):
        @functools.wraps(function)
        def generator(self, *args, **kwargs):
            from .utils.mysqlite.connection_pool import connection_deadline
            timeout = kwargs.get('timeout_ms')
            valid_timeout = (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                             and math.isfinite(timeout) and timeout > 0)
            deadline = time.monotonic() + timeout / 1000 if valid_timeout else None
            with ExitStack() as scope:
                with connection_deadline(deadline) if deadline is not None else nullcontext():
                    scope.enter_context(operation(self))
                iterator = function(self, *args, **kwargs)
                try:
                    if deadline is None:
                        yield from iterator
                    else:
                        while True:
                            try:
                                with connection_deadline(deadline):
                                    value = next(iterator)
                            except StopIteration:
                                break
                            yield value
                finally:
                    iterator.close()
        wrapped = generator
    else:
        @functools.wraps(function)
        def regular(self, *args, **kwargs):
            preflight = getattr(self, '_preflight_public_call', None)
            if preflight is not None:
                preflight(function.__name__, args, kwargs)
            closing = function.__name__ == 'close'
            connecting = function.__name__ == 'connect'
            if closing and ((hasattr(self, '_tables') and not self._tables)
                            or (hasattr(self, '_conn') and self._conn is None)):
                if getattr(self, '_uri_schema', None) == 'postgresql':
                    from .coordination_postgresql import close_idle_controls
                    close_idle_controls(self._uri)
                return function(self, *args, **kwargs)
            with operation(self, refresh=not (closing or connecting)):
                try:
                    result = function(self, *args, **kwargs)
                except NoSuchColumn as exc:
                    raise SchemaConflict(f'物理表缺少逻辑契约中的列: {exc}') from exc
                if closing and not getattr(self, '_iosql_invalidating', False):
                    self._iosql_explicitly_closed = True
                    if getattr(self, '_uri_schema', None) == 'postgresql':
                        from .coordination_postgresql import close_idle_controls
                        close_idle_controls(self._uri)
                        entry = _held().get(self._iosql_resource.key)
                        if entry is not None:
                            entry.coordinator.reusable = False
                elif connecting:
                    self._iosql_explicitly_closed = False
                return result
        wrapped = regular
    wrapped._iosql_guarded = True
    return wrapped


def guard_query(cls):
    """查询对象的编译和消费也在同一租约内，覆盖惰性 SQL。"""
    for name, function in list(vars(cls).items()):
        if not name.startswith('_') and inspect.isfunction(function):
            if name in ('iter_rows', 'iter_batches') and not inspect.isgeneratorfunction(function):
                def as_generator(method):
                    @functools.wraps(method)
                    def generate(self, *args, **kwargs):
                        iterator = iter(method(self, *args, **kwargs))
                        try:
                            yield from iterator
                        finally:
                            close = getattr(iterator, 'close', None)
                            if close is not None:
                                close()
                    return generate
                function = as_generator(function)
            setattr(cls, name, guarded(function))
    return cls


def _renaming(function):
    @functools.wraps(function)
    def renamed(self, destination, *args, **kwargs):
        uris = [self._uri]
        resolve = getattr(self, '_resolve_rename_database_target', None)
        if resolve is not None:
            target = resolve(destination)
            from .database import _forbidden_rename_target_reason
            reason = _forbidden_rename_target_reason(target)
            if reason:
                raise UnsafeOperation(f'rename target must not be a {reason}: {target}')
            uris.append('sqlite://' + str(target))
        with exclusive_resources(uris, timeout=0.0):
            with operation(self):
                return function(self, destination, *args, **kwargs)
    return renamed


def _renaming_table(function):
    @functools.wraps(function)
    def renamed(self, *args, **kwargs):
        with exclusive_resources([self._uri], timeout=0.0):
            with operation(self):
                return function(self, *args, **kwargs)
    return renamed


def _constructing(function):
    @functools.wraps(function)
    def constructed(self, *args, **kwargs):
        uri = kwargs.get('uri') or (args[0] if args else getattr(self, '_uri', None))
        if uri is None or resource(uri).backend == 'postgresql':
            # PG 构造器只保存参数，实际 SQL 调用才需要连接与锁。
            result = function(self, *args, **kwargs)
        else:
            with operation(self, uri=uri):
                result = function(self, *args, **kwargs)
        self._iosql_constructor = (args, dict(kwargs))
        self._iosql_explicitly_closed = False
        return result
    constructed._iosql_constructed = True
    return constructed


def guard_table_class(cls):
    """在引擎继承边界接入所有公共方法，避免新增方法漏掉屏障。"""
    excluded = {'migrate_to', 'save_as', 'repartition_to', 'rename'}
    methods = {}
    for base in reversed(cls.__mro__):
        methods.update(vars(base))
    for name, function in methods.items():
        if (not name.startswith('_') and name not in excluded
                and inspect.isfunction(function)):
            setattr(cls, name, guarded(function))
    for name, decorator in (('rename', _renaming),):
        function = cls.__dict__.get(name)
        if inspect.isfunction(function):
            setattr(cls, name, decorator(function))
    constructor = cls.__dict__.get('__init__')
    if constructor is not None and not getattr(constructor, '_iosql_constructed', False):
        cls.__init__ = _constructing(constructor)


def guard_database_class(cls):
    for name, function in list(vars(cls).items()):
        if name in ('migrate_to', 'save_as', 'to_postgresql', '__init__'):
            continue
        if (not name.startswith('_') or name == '__getitem__') and inspect.isfunction(function):
            wrapper = _renaming if name == 'rename' else (
                _renaming_table if name == 'rename_table' else guarded)
            setattr(cls, name, wrapper(function))
    return cls
