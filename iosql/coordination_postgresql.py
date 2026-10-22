"""PostgreSQL 库级 advisory 锁与持久迁移状态。"""

from __future__ import annotations

import hashlib
import math
import os
import time
import weakref
import threading
import atexit
from collections import OrderedDict
from dataclasses import dataclass

from .exception import DatabaseBusy, MigrationRecoveryRequired


DATABASE_LOCK = 0x494F53514C4D4947
CONTROL_SCHEMA = '__iosql_control'
_coordinators = weakref.WeakSet()
_idle = OrderedDict()  # URI -> 已解锁的控制会话；所有身份总计最多 8 条。
_idle_lock = threading.RLock()


@dataclass
class _ControlSession:
    """绑定连接及已确认的目录状态，避免复用会话时重查稳定的存在性。"""
    connection: object
    control_exists: bool = False  # 只缓存“已存在”；未确认时必须在取锁后重新查询。


def close_idle_controls(uri=None):
    """显式关闭所属 URI 的空闲会话，进程退出时清空全部。"""
    with _idle_lock:
        keys = list(_idle) if uri is None else [key for key in _idle if key == uri]
        connections = [_idle.pop(key).connection for key in keys]
    for connection in connections:
        connection.close()


atexit.register(close_idle_controls)


def _detach_in_child(connection):
    """关闭子进程的 socket 副本，PQfinish 不能影响父进程会话。"""
    if connection is None or connection.closed:
        return
    fd = None
    try:
        fd = connection.fileno()
        null_fd = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(null_fd, fd)
        finally:
            os.close(null_fd)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    finally:
        connection.close()


def after_fork():
    """子进程不能通过继承的 libpq 会话解除父进程锁。"""
    global _idle, _idle_lock
    idle = [session.connection for session in _idle.values()]
    _idle = OrderedDict()
    _idle_lock = threading.RLock()
    for connection in idle:
        _detach_in_child(connection)
    for coordinator in list(_coordinators):
        connection = coordinator.connection
        if connection is None or connection.closed:
            continue
        # 先用 /dev/null 替换子进程的 socket FD，PQfinish 不会发送 Terminate 给父会话。
        fd = None
        try:
            fd = connection.fileno()
            null_fd = os.open(os.devnull, os.O_RDWR)
            try:
                os.dup2(null_fd, fd)
            finally:
                os.close(null_fd)
        except Exception:
            # 子进程此时没有其他运行线程，立即关闭旧 FD，避免 PQfinish 发送协议消息。
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        finally:
            connection.close()
            coordinator.connection = None


def task_lock(operation_id):
    return int.from_bytes(hashlib.sha256(('iosql-task:' + operation_id).encode()).digest()[:8],
                          'big', signed=True)


def connect_control(uri, timeout=30.0):
    import psycopg2
    options = {'application_name': 'iosql-coordination'}
    if 'connect_timeout' not in psycopg2.extensions.parse_dsn(uri):
        options['connect_timeout'] = max(1, math.ceil(timeout))
    connection = psycopg2.connect(uri, **options)
    connection.autocommit = True
    return connection


class PostgreSQLCoordinator:
    def __init__(self, resource):
        self.resource = resource
        self.connection = None
        self.exclusive = False
        self.reusable = True  # 只有确认解锁的 autocommit 会话允许回到缓存。
        _coordinators.add(self)

    def acquire(self, exclusive, deadline):
        with _idle_lock:
            session = _idle.pop(self.resource.uri, None)
        self.connection = session.connection if session is not None else None
        self._control_exists = True if session is not None and session.control_exists else None
        reused = self.connection is not None
        if self.connection is not None and self.connection.closed:
            self.connection = None
            self._control_exists = None
        if self.connection is None:
            self.connection = connect_control(self.resource.uri, max(0.0, deadline - time.monotonic()))
        self.exclusive = exclusive
        function = 'pg_try_advisory_lock' if exclusive else 'pg_try_advisory_lock_shared'
        try:
            with self.connection.cursor() as cursor:
                while True:
                    cursor.execute(f'SELECT {function}(%s)', (DATABASE_LOCK,))
                    if cursor.fetchone()[0]:
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DatabaseBusy('PostgreSQL 库正在迁移或仍有活跃操作')
                    time.sleep(min(0.01, remaining))
        except BaseException as exc:
            self.reusable = False
            self.release()
            import psycopg2
            if reused and isinstance(exc, psycopg2.Error):
                # 空闲会话断连时还没有业务副作用，丢弃旧会话后只重连一次。
                self.reusable = True
                return self.acquire(exclusive, deadline)
            raise

    def release(self):
        connection, self.connection = self.connection, None
        if connection is None or connection.closed:
            return
        function = 'pg_advisory_unlock' if self.exclusive else 'pg_advisory_unlock_shared'
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT {function}(%s)', (DATABASE_LOCK,))
                unlocked = cursor.fetchone()[0]
        except Exception:
            # 关闭专用会话同样释放锁，断连不能掩盖已经确认的业务提交。
            unlocked = False
        if not unlocked or not self.reusable or not connection.autocommit:
            connection.close()
            return
        with _idle_lock:
            previous = _idle.pop(self.resource.uri, None)
            _idle[self.resource.uri] = _ControlSession(connection, self._control_exists is not None)
            retired = []
            while len(_idle) > 8:
                retired.append(_idle.popitem(last=False)[1].connection)
        for old in ([previous.connection] if previous is not None else []) + retired:
            old.close()

    def state(self):
        import psycopg2
        with self.connection.cursor() as cursor:
            if self._control_exists is None:
                # 必须是取锁后的新语句快照；与取锁 SQL 合并会漏掉刚提交的首次发布。
                cursor.execute("SELECT to_regclass('__iosql_control.database_state')")
                if cursor.fetchone()[0] is None:
                    return {'generation': None, 'transaction': None}
                self._control_exists = True
            try:
                cursor.execute('SELECT generation, operation_id FROM __iosql_control.database_state '
                               'WHERE singleton = TRUE')
            except psycopg2.errors.UndefinedTable:
                self._control_exists = None
                return self.state()
            row = cursor.fetchone()
            if row is None:
                return {'generation': None, 'transaction': None}
            generation, operation_id = row
            return {
                'generation': generation,
                'transaction': ({'state': 'PG_PENDING', 'operation_id': operation_id}
                                if operation_id else None),
            }

    def ensure_control(self):
        with self.connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA IF NOT EXISTS __iosql_control')
            cursor.execute('''CREATE TABLE IF NOT EXISTS __iosql_control.database_state (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                generation TEXT, operation_id TEXT)''')
            cursor.execute('''INSERT INTO __iosql_control.database_state(singleton)
                VALUES (TRUE) ON CONFLICT DO NOTHING''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS __iosql_control.migrations (
                operation_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                staging_schema TEXT NOT NULL)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS __iosql_control.table_contracts (
                schema_name TEXT NOT NULL, table_name TEXT NOT NULL,
                relation_oid OID NOT NULL, contract JSONB NOT NULL,
                PRIMARY KEY (schema_name, table_name))''')
        self._control_exists = True

    def begin_operation(self, operation_id, staging_schema):
        self.ensure_control()
        self.connection.autocommit = False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute('INSERT INTO __iosql_control.migrations VALUES (%s, %s, %s)',
                               (operation_id, 'preparing', staging_schema))
                cursor.execute('UPDATE __iosql_control.database_state SET operation_id=%s '
                               'WHERE singleton=TRUE', (operation_id,))
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self.connection.autocommit = True

    def recover(self, state):
        operation_id = state['transaction']['operation_id']
        key = task_lock(operation_id)
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', (key,))
            if not cursor.fetchone()[0]:
                raise DatabaseBusy('原迁移事务尚未结束，暂不能判断 PostgreSQL 提交结果')
            try:
                # 原事务的任务锁释放后，再用新的语句快照确认；不能提前猜测回滚。
                cursor.execute('SELECT state FROM __iosql_control.migrations WHERE operation_id=%s',
                               (operation_id,))
                row = cursor.fetchone()
                if row is None:
                    raise MigrationRecoveryRequired('迁移状态缺失，拒绝猜测 PostgreSQL 提交结果',
                                                    operation_id=operation_id)
                if row[0] == 'committed':
                    cursor.execute('UPDATE __iosql_control.database_state '
                                   'SET generation=%s, operation_id=NULL '
                                   'WHERE singleton=TRUE AND operation_id=%s',
                                   (operation_id, operation_id))
                else:
                    # 原事务已经结束而无 committed：正式变更已由 PostgreSQL 回滚。
                    self.connection.autocommit = False
                    try:
                        cursor.execute("UPDATE __iosql_control.migrations SET state='aborted' "
                                       'WHERE operation_id=%s', (operation_id,))
                        cursor.execute('UPDATE __iosql_control.database_state SET operation_id=NULL '
                                       'WHERE singleton=TRUE AND operation_id=%s', (operation_id,))
                        self.connection.commit()
                    except BaseException:
                        self.connection.rollback()
                        raise
                    finally:
                        self.connection.autocommit = True
            finally:
                cursor.execute('SELECT pg_advisory_unlock(%s)', (key,))
