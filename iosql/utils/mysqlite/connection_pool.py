import sqlite3
import threading
import queue
import weakref
from contextlib import contextmanager
from typing import Dict, Any, Optional


_DEFAULT_MAX_PROCESS_CONNECTIONS = 32


class _ProcessConnectionBudget:
    """进程内所有 mysqlite 连接池共享的真实连接预算。"""

    def __init__(self):
        self._condition = threading.Condition()
        self._pools = weakref.WeakKeyDictionary()
        self._in_use = 0

    def register(self, pool, limit: int) -> None:
        with self._condition:
            self._pools[pool] = limit
            self._condition.notify_all()

    def unregister(self, pool) -> None:
        with self._condition:
            self._pools.pop(pool, None)
            self._condition.notify_all()

    def _effective_limit_locked(self) -> int:
        limits = list(self._pools.values())
        return min(limits) if limits else _DEFAULT_MAX_PROCESS_CONNECTIONS

    def acquire(self, requester) -> None:
        while True:
            with self._condition:
                if requester._closed:
                    raise RuntimeError('connection pool is closed')
                limit = self._effective_limit_locked()
                if self._in_use < limit:
                    self._in_use += 1
                    return
                pools = list(self._pools)

            reclaimed = False
            for pool in pools:
                if pool is requester:
                    continue
                if pool._close_one_idle_connection_for_budget():
                    reclaimed = True
                    break
            if reclaimed:
                continue

            with self._condition:
                self._condition.wait(timeout=0.1)

    def release(self) -> None:
        with self._condition:
            if self._in_use > 0:
                self._in_use -= 1
            self._condition.notify_all()

    def status(self) -> Dict[str, int]:
        with self._condition:
            return {
                'max_process_connections': self._effective_limit_locked(),
                'open_process_connections': self._in_use,
                'registered_pools': len(self._pools),
            }


_PROCESS_CONNECTION_BUDGET = _ProcessConnectionBudget()


def get_process_connection_budget_status() -> Dict[str, int]:
    return _PROCESS_CONNECTION_BUDGET.status()


class DatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10,
                 pragma_dict: Optional[Dict[str, Any]] = None,
                 max_process_connections: int = _DEFAULT_MAX_PROCESS_CONNECTIONS,
                 **kwargs):
        if int(max_connections) <= 0:
            raise ValueError('max_connections must be positive')
        if int(max_process_connections) <= 0:
            raise ValueError('max_process_connections must be positive')
        self.db_file = db_file
        self._max_connections = int(max_connections)
        self._max_process_connections = int(max_process_connections)
        self._available: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=self._max_connections
        )
        self._created_connections = 0
        self._checked_out = 0
        self._lock = threading.Lock()
        self._pragma_dict = pragma_dict if pragma_dict is not None else {}
        self._conn_kwargs = kwargs
        self._closed = False
        _PROCESS_CONNECTION_BUDGET.register(
            self,
            self._max_process_connections,
        )

    def __del__(self):
        try:
            self.force_close_all_connections()
        except Exception:
            pass

    def _create_connection(self) -> sqlite3.Connection:
        _PROCESS_CONNECTION_BUDGET.acquire(self)
        conn_kwargs = {'timeout': 30.0}
        conn_kwargs.update(self._conn_kwargs)
        conn_kwargs.setdefault('check_same_thread', False)
        try:
            conn = sqlite3.connect(self.db_file, **conn_kwargs)
            conn.row_factory = sqlite3.Row
            for key, value in self._pragma_dict.items():
                if value:
                    try:
                        conn.execute(f'PRAGMA {key}={value};')
                    except sqlite3.OperationalError:
                        pass
            return conn
        except Exception:
            _PROCESS_CONNECTION_BUDGET.release()
            raise

    def _is_connection_alive(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute('SELECT 1')
            return True
        except sqlite3.Error:
            return False

    def _close_connection(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except Exception:
            pass
        finally:
            self._decrement_created()
            _PROCESS_CONNECTION_BUDGET.release()

    def _decrement_created(self) -> None:
        with self._lock:
            if self._created_connections > 0:
                self._created_connections -= 1

    def _new_connection_if_allowed(self) -> Optional[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError('connection pool is closed')
            if self._created_connections >= self._max_connections:
                return None
            self._created_connections += 1
        try:
            return self._create_connection()
        except Exception:
            self._decrement_created()
            raise

    def _checkout_connection(self) -> sqlite3.Connection:
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError('connection pool is closed')

            try:
                conn = self._available.get_nowait()
            except queue.Empty:
                conn = self._new_connection_if_allowed()
                if conn is None:
                    try:
                        conn = self._available.get(timeout=0.1)
                    except queue.Empty:
                        continue

            if self._is_connection_alive(conn):
                should_close = False
                with self._lock:
                    if self._closed:
                        should_close = True
                    else:
                        self._checked_out += 1
                if should_close:
                    self._close_connection(conn)
                    raise RuntimeError('connection pool is closed')
                return conn

            self._close_connection(conn)

    def _return_connection(self, conn: sqlite3.Connection) -> None:
        should_close = False
        with self._lock:
            if self._checked_out > 0:
                self._checked_out -= 1
            should_close = self._closed

        if should_close:
            self._close_connection(conn)
            return

        try:
            conn.rollback()
        except sqlite3.Error:
            pass

        if not self._is_connection_alive(conn):
            self._close_connection(conn)
            return

        try:
            self._available.put_nowait(conn)
        except queue.Full:
            self._close_connection(conn)

    def _close_one_idle_connection_for_budget(self) -> bool:
        try:
            conn = self._available.get_nowait()
        except queue.Empty:
            return False
        self._close_connection(conn)
        return True

    @contextmanager
    def get_connection(self):
        conn = self._checkout_connection()
        try:
            yield conn
        finally:
            self._return_connection(conn)

    def force_close_all_connections(self):
        idle = []
        with self._lock:
            self._closed = True
            while True:
                try:
                    idle.append(self._available.get_nowait())
                except queue.Empty:
                    break

        for conn in idle:
            self._close_connection(conn)
        _PROCESS_CONNECTION_BUDGET.unregister(self)

    def get_pool_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'max_connections': self._max_connections,
                'max_process_connections': self._max_process_connections,
                'created_connections': self._created_connections,
                'available_connections': self._available.qsize(),
                'checked_out_connections': self._checked_out,
                'closed': self._closed,
                'db_file': self.db_file,
            }

    def is_pool_healthy(self) -> bool:
        status = self.get_pool_status()
        return status['created_connections'] <= status['max_connections']
