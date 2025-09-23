# 在 database.py 中添加连接池
import threading
import queue
import sqlite3
from contextlib import contextmanager

class DatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10, **kwargs):
        self.db_file = db_file
        self.max_connections = max_connections
        self.kwargs = kwargs
        self._pool = queue.Queue(maxsize=max_connections)
        self._created_connections = 0
        self._lock = threading.Lock()
        
    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, **self.kwargs)
        conn.row_factory = sqlite3.Row
        return conn
    
    @contextmanager
    def get_connection(self):
        conn = None
        try:
            # 尝试从池中获取连接
            conn = self._pool.get_nowait()
        except queue.Empty:
            # 如果池为空且未达到最大连接数，创建新连接
            with self._lock:
                if self._created_connections < self.max_connections:
                    conn = self._create_connection()
                    self._created_connections += 1
                else:
                    # 等待可用连接
                    conn = self._pool.get()
        
        try:
            yield conn
        finally:
            # 将连接返回池中
            try:
                self._pool.put_nowait(conn)
            except queue.Full:
                conn.close()
                with self._lock:
                    self._created_connections -= 1