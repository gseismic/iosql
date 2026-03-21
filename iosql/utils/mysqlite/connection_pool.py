import sqlite3
import threading
from contextlib import contextmanager
from typing import Dict, Any, Optional


class DatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10,
                 pragma_dict: Optional[Dict[str, Any]] = None, **kwargs):
        self.db_file = db_file
        self._max_connections = max_connections
        self._semaphore = threading.BoundedSemaphore(max_connections)
        self._thread_conns: Dict[int, sqlite3.Connection] = {}
        self._lock = threading.Lock()
        self._pragma_dict = pragma_dict or {}
        self._conn_kwargs = kwargs

    def _create_connection(self) -> sqlite3.Connection:
        conn_kwargs = {'timeout': 30.0}
        conn_kwargs.update(self._conn_kwargs)
        conn = sqlite3.connect(self.db_file, **conn_kwargs)
        conn.row_factory = sqlite3.Row
        for key, value in self._pragma_dict.items():
            if value:
                try:
                    conn.execute(f'PRAGMA {key}={value};')
                except sqlite3.OperationalError:
                    pass
        return conn

    @contextmanager
    def get_connection(self):
        tid = threading.get_ident()
        acquired = False
        try:
            with self._lock:
                if tid in self._thread_conns:
                    conn = self._thread_conns[tid]
                else:
                    self._semaphore.acquire()
                    acquired = True
                    if tid not in self._thread_conns:
                        self._thread_conns[tid] = self._create_connection()
                    conn = self._thread_conns[tid]
            yield conn
        finally:
            if acquired:
                self._semaphore.release()

    def force_close_all_connections(self):
        with self._lock:
            for tid, conn in list(self._thread_conns.items()):
                try:
                    conn.close()
                except Exception:
                    pass
                # Note: Do NOT release semaphore here
                # The semaphore is released when connections are normally closed
                # via the get_connection() finally block
                # Releasing here would cause "Semaphore released too many times"
            self._thread_conns.clear()