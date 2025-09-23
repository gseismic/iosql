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
    
    def _is_connection_alive(self, conn: sqlite3.Connection) -> bool:
        """检查连接是否仍然有效
        
        在长时间运行的应用中，SQLite连接可能会因为以下原因失效：
        1. 数据库文件被删除或移动
        2. 文件系统权限变更
        3. 网络存储断开（如果数据库在网络驱动器上）
        4. 进程长时间空闲导致的连接超时
        
        通过执行简单的SELECT 1查询来验证连接是否仍然可用，
        这比直接使用可能已失效的连接更安全。
        """
        try:
            conn.execute('SELECT 1')
            return True
        except sqlite3.Error:
            return False
    
    @contextmanager
    def get_connection(self):
        conn = None
        try:
            # 尝试从池中获取连接
            conn = self._pool.get_nowait()
            # 检查连接是否仍然有效
            # 这一步很重要：池中的连接可能因为长时间未使用或外部因素而失效
            if not self._is_connection_alive(conn):
                conn.close()  # 关闭失效的连接
                conn = self._create_connection()  # 创建新的有效连接
        except queue.Empty:
            # 如果池为空且未达到最大连接数，创建新连接
            with self._lock:
                if self._created_connections < self.max_connections:
                    conn = self._create_connection()
                    self._created_connections += 1
                else:
                    # 等待可用连接，这里也需要检查连接健康状态
                    conn = self._pool.get()
                    # 对从阻塞获取的连接也进行健康检查
                    if not self._is_connection_alive(conn):
                        conn.close()
                        conn = self._create_connection()
        
        try:
            yield conn
        finally:
            # 将连接返回池中
            # 只有健康的连接才放回池中，避免污染连接池
            if self._is_connection_alive(conn):
                try:
                    self._pool.put_nowait(conn)
                except queue.Full:
                    # 池已满，关闭多余连接
                    conn.close()
                    with self._lock:
                        self._created_connections -= 1
            else:
                # 连接已失效，直接关闭并减少计数
                conn.close()
                with self._lock:
                    self._created_connections -= 1