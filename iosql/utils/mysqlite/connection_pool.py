# 在 database.py 中添加连接池
import threading
import queue
import sqlite3
from contextlib import contextmanager
from .logger import logger 

class DatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10, pragma_dict: dict = None, **kwargs):
        self.db_file = db_file
        self.max_connections = max_connections
        self.kwargs = kwargs
        self.pragma_dict = pragma_dict or {}
        self._pool = queue.Queue(maxsize=max_connections)
        self._created_connections = 0
        self._lock = threading.RLock()
        self._pragma_lock = threading.Lock()  # 专门用于PRAGMA设置的锁
        self._pragma_applied_connections = set()  # 跟踪已应用PRAGMA的连接
        
    def _create_connection(self) -> sqlite3.Connection:
        # 在连接参数中设置一些基本的PRAGMA
        kwargs = self.kwargs.copy()
        kwargs.setdefault('timeout', 30.0)  # 设置超时时间
        
        conn = sqlite3.connect(self.db_file, **kwargs)
        conn.row_factory = sqlite3.Row
        
        # 只设置最关键的PRAGMA，避免锁定问题
        try:
            conn.execute('PRAGMA journal_mode=WAL;')
            conn.execute('PRAGMA busy_timeout=30000;')
        except sqlite3.OperationalError:
            # 如果设置失败，继续使用连接
            pass
            
        return conn
    
    def _apply_pragma_to_connection(self, conn: sqlite3.Connection) -> None:
        """为单个连接应用PRAGMA设置"""
        # 使用专门的锁来确保PRAGMA设置的原子性
        with self._pragma_lock:
            cursor = conn.cursor()
            try:
                for key, value in self.pragma_dict.items():
                    cursor.execute(f'PRAGMA {key}={value};')
            finally:
                cursor.close()
    
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
    
    def force_close_all_connections(self) -> None:
        """强制关闭连接池中的所有连接
        
        这个方法用于紧急情况下的资源清理，比如：
        1. 应用异常退出时的清理
        2. 防止文件句柄泄露
        3. 内存压力下的强制清理
        
        注意：调用此方法后，连接池将不可用，需要重新创建Database实例
        """
        logger.info(f'强制关闭连接池中的所有连接 (当前连接数: {self._created_connections})')
        
        with self._lock:
            # 关闭池中的所有连接
            closed_count = 0
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    try:
                        conn.close()
                        closed_count += 1
                    except sqlite3.Error as e:
                        logger.warning(f'关闭连接时出错: {e}')
                except queue.Empty:
                    break
            
            # 重置连接计数
            self._created_connections = 0
            
        logger.info(f'已强制关闭 {closed_count} 个连接')
    
    def get_pool_status(self) -> dict:
        """获取连接池状态信息
        
        Returns:
            dict: 包含连接池状态信息的字典
        """
        with self._lock:
            return {
                'max_connections': self.max_connections,
                'created_connections': self._created_connections,
                'available_connections': self._pool.qsize(),
                'db_file': self.db_file
            }
    
    def is_pool_healthy(self) -> bool:
        """检查连接池是否健康
        
        Returns:
            bool: True表示连接池健康，False表示可能存在问题
        """
        status = self.get_pool_status()
        # 检查是否有连接泄露（创建数远大于可用数）
        if status['created_connections'] > status['max_connections']:
            return False
        # 检查是否有太多连接在使用中
        in_use = status['created_connections'] - status['available_connections']
        if in_use > status['max_connections'] * 0.8:  # 80%阈值
            logger.warning(f'连接池使用率过高: {in_use}/{status["max_connections"]}')
        return True

    @contextmanager
    def get_connection(self):
        conn = None
        try:
            # 尝试从池中获取连接
            logger.debug('get_connection')
            conn = self._pool.get_nowait()
            # 检查连接是否仍然有效 
            # 这一步很重要：池中的连接可能因为长时间未使用或外部因素而失效
            if not self._is_connection_alive(conn):
                logger.debug('create new connection ...')
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