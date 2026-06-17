import sqlite3
import threading
import queue
from contextlib import contextmanager
from typing import Dict, Any, Optional


class DatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10,
                 pragma_dict: Optional[Dict[str, Any]] = None, **kwargs):
        if int(max_connections) <= 0:
            raise ValueError('max_connections must be positive')
        self.db_file = db_file
        self._max_connections = int(max_connections)
        self._available: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(
            maxsize=self._max_connections
        )
        self._created_connections = 0
        self._checked_out = 0
        self._lock = threading.Lock()
        self._pragma_dict = pragma_dict or {}
        self._conn_kwargs = kwargs
        self._closed = False

    def _create_connection(self) -> sqlite3.Connection:
        conn_kwargs = {'timeout': 30.0}
        conn_kwargs.update(self._conn_kwargs)
        conn_kwargs.setdefault('check_same_thread', False)
        conn = sqlite3.connect(self.db_file, **conn_kwargs)
        conn.row_factory = sqlite3.Row
        for key, value in self._pragma_dict.items():
            if value:
                try:
                    conn.execute(f'PRAGMA {key}={value};')
                except sqlite3.OperationalError:
                    pass
        return conn

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
                with self._lock:
                    if self._closed:
                        self._close_connection(conn)
                        self._decrement_created()
                        raise RuntimeError('connection pool is closed')
                    self._checked_out += 1
                return conn

            self._close_connection(conn)
            self._decrement_created()

    def _return_connection(self, conn: sqlite3.Connection) -> None:
        should_close = False
        with self._lock:
            if self._checked_out > 0:
                self._checked_out -= 1
            should_close = self._closed

        if should_close or not self._is_connection_alive(conn):
            self._close_connection(conn)
            self._decrement_created()
            return

        try:
            self._available.put_nowait(conn)
        except queue.Full:
            self._close_connection(conn)
            self._decrement_created()

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
            self._decrement_created()

    def get_pool_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'max_connections': self._max_connections,
                'created_connections': self._created_connections,
                'available_connections': self._available.qsize(),
                'checked_out_connections': self._checked_out,
                'closed': self._closed,
                'db_file': self.db_file,
            }

    def is_pool_healthy(self) -> bool:
        status = self.get_pool_status()
        return status['created_connections'] <= status['max_connections']
