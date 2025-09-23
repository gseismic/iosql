# 优化版连接池 - 解决长时间卡顿问题
import threading
import queue
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

class OptimizedDatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10, 
                 connection_timeout: float = 30.0, 
                 health_check_interval: float = 300.0, **kwargs):
        self.db_file = db_file
        self.max_connections = max_connections
        self.connection_timeout = connection_timeout  # 连接获取超时
        self.health_check_interval = health_check_interval  # 健康检查间隔
        self.kwargs = kwargs
        self._pool = queue.Queue(maxsize=max_connections)
        self._created_connections = 0
        self._lock = threading.RLock()  # 使用可重入锁
        self._last_health_check = {}  # 记录每个连接的最后健康检查时间
        
    def _create_connection(self) -> sqlite3.Connection:
        """创建新的数据库连接"""
        conn = sqlite3.connect(self.db_file, **self.kwargs)
        conn.row_factory = sqlite3.Row
        # 设置连接超时和其他优化参数
        conn.execute('PRAGMA journal_mode=WAL')  # 启用WAL模式提高并发性能
        conn.execute('PRAGMA synchronous=NORMAL')  # 平衡性能和安全性
        conn.execute('PRAGMA cache_size=10000')  # 增加缓存大小
        conn.execute('PRAGMA temp_store=memory')  # 临时表存储在内存中
        return conn
    
    def _is_connection_alive(self, conn: sqlite3.Connection, force_check: bool = False) -> bool:
        """检查连接是否仍然有效
        
        Args:
            conn: 数据库连接
            force_check: 是否强制检查（忽略时间间隔）
        
        优化点：
        1. 添加时间间隔控制，避免频繁检查
        2. 使用更轻量的检查方式
        """
        conn_id = id(conn)
        current_time = time.time()
        
        # 如果不是强制检查且距离上次检查时间不足间隔，直接返回True
        if not force_check and conn_id in self._last_health_check:
            if current_time - self._last_health_check[conn_id] < self.health_check_interval:
                return True
        
        try:
            # 使用更轻量的检查方式
            conn.execute('SELECT 1').fetchone()
            self._last_health_check[conn_id] = current_time
            return True
        except sqlite3.Error:
            # 清理失效连接的记录
            self._last_health_check.pop(conn_id, None)
            return False
    
    @contextmanager
    def get_connection(self, timeout: Optional[float] = None):
        """获取数据库连接的上下文管理器
        
        Args:
            timeout: 获取连接的超时时间，None表示使用默认超时
        """
        if timeout is None:
            timeout = self.connection_timeout
            
        conn = None
        start_time = time.time()
        
        try:
            # 尝试从池中获取连接
            try:
                conn = self._pool.get_nowait()
                # 只在必要时检查连接健康状态
                if not self._is_connection_alive(conn):
                    conn.close()
                    conn = self._create_connection()
            except queue.Empty:
                # 池为空，尝试创建新连接或等待
                with self._lock:
                    if self._created_connections < self.max_connections:
                        conn = self._create_connection()
                        self._created_connections += 1
                    else:
                        # 使用超时等待而不是无限期阻塞
                        remaining_timeout = timeout - (time.time() - start_time)
                        if remaining_timeout <= 0:
                            raise TimeoutError(f"获取数据库连接超时 ({timeout}秒)")
                        
                        try:
                            conn = self._pool.get(timeout=remaining_timeout)
                            # 对等待获取的连接进行健康检查
                            if not self._is_connection_alive(conn, force_check=True):
                                conn.close()
                                # 如果连接失效，尝试创建新连接
                                if self._created_connections < self.max_connections:
                                    conn = self._create_connection()
                                    self._created_connections += 1
                                else:
                                    raise RuntimeError("无法获取有效的数据库连接")
                        except queue.Empty:
                            raise TimeoutError(f"获取数据库连接超时 ({timeout}秒)")
            
            yield conn
            
        except Exception as e:
            # 如果在获取或使用连接时出错，确保连接被正确处理
            if conn:
                try:
                    conn.close()
                except:
                    pass
                with self._lock:
                    self._created_connections = max(0, self._created_connections - 1)
            raise e
            
        finally:
            # 将连接返回池中
            if conn:
                try:
                    # 只有健康的连接才放回池中
                    if self._is_connection_alive(conn):
                        try:
                            self._pool.put_nowait(conn)
                        except queue.Full:
                            # 池已满，关闭多余连接
                            conn.close()
                            with self._lock:
                                self._created_connections = max(0, self._created_connections - 1)
                    else:
                        # 连接已失效，直接关闭
                        conn.close()
                        with self._lock:
                            self._created_connections = max(0, self._created_connections - 1)
                except Exception:
                    # 确保连接被关闭
                    try:
                        conn.close()
                    except:
                        pass
                    with self._lock:
                        self._created_connections = max(0, self._created_connections - 1)
    
    def get_pool_stats(self) -> dict:
        """获取连接池统计信息"""
        with self._lock:
            return {
                'pool_size': self._pool.qsize(),
                'created_connections': self._created_connections,
                'max_connections': self.max_connections,
                'active_connections': self._created_connections - self._pool.qsize()
            }
    
    def close_all(self):
        """关闭所有连接"""
        with self._lock:
            # 关闭池中的所有连接
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except queue.Empty:
                    break
            
            self._created_connections = 0
            self._last_health_check.clear()