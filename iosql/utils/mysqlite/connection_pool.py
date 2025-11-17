# connection_pool.py
import os
import time
import sqlite3
import threading
import queue
from contextlib import contextmanager
from typing import Optional, Dict, Any
from .logger import logger

class DatabaseConnectionPool:
    """
    改进的 SQLite 连接池（线程绑定实现）
    设计目标：
    - 每个线程保持自己的 sqlite3.Connection（避免跨线程复用）
    - 全局连接数量受限（Semaphore）
    - 连接健康检查与自动重建
    - 兼容原有 get_connection() 上下文管理器接口
    """

    def __init__(self, db_file: str, max_connections: int = 10, pragma_dict: Optional[Dict[str, Any]] = None, **kwargs):
        self.db_file = db_file
        self.max_connections = int(max_connections) if max_connections and max_connections > 0 else 10
        self.kwargs = kwargs or {}
        self.pragma_dict = pragma_dict or {}

        # 限制并发创建连接 / 活跃连接数
        self._semaphore = threading.BoundedSemaphore(self.max_connections)

        # 线程 -> connection 映射（每线程独占）
        self._thread_conns = {}  # tid -> sqlite3.Connection
        self._thread_conn_meta = {}  # tid -> dict(meta: created_at, last_used, usage_count)
        self._global_lock = threading.RLock()

        # PRAGMA 应用锁（保证创建/重建时原子性）
        self._pragma_lock = threading.Lock()

        # 统计
        self._created_connections = 0

    # -------------------------
    # 内部工具函数
    # -------------------------
    def _ensure_db_dir(self):
        """确保数据库目录存在并可写"""
        db_dir = os.path.dirname(self.db_file) or '.'
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"无法创建数据库目录 {db_dir}: {e}")
            raise

    def _create_connection(self) -> sqlite3.Connection:
        """创建一个新的 sqlite3.Connection，并应用初始 PRAGMA"""
        # 在创建前先确保目录存在
        self._ensure_db_dir()

        kwargs = dict(self.kwargs)  # copy
        # 保证合理超时
        kwargs.setdefault('timeout', 30.0)
        # 使用默认的 check_same_thread=True（因为每个连接绑定到创建它的线程）
        # 如果用户特意在 kwargs 中传了 check_same_thread，则尊重之（不建议）
        try:
            conn = sqlite3.connect(self.db_file, **kwargs)
        except sqlite3.OperationalError as e:
            logger.error(f"创建 sqlite 连接失败: {e} (db_file={self.db_file})")
            raise

        conn.row_factory = sqlite3.Row

        # 尝试设置一些关键 PRAGMA（在创建时应用）
        try:
            with self._pragma_lock:
                # 如果用户没有设置 journal_mode，但是 pool 想要启用 wal，可在 pragma_dict 指定
                for key, value in self.pragma_dict.items():
                    try:
                        # 支持 boolean/str/int
                        if value is None or value == '':
                            conn.execute(f'PRAGMA {key};')
                        else:
                            conn.execute(f'PRAGMA {key}={value};')
                    except sqlite3.OperationalError as e:
                        # 某些 PRAGMA 在只读fs或特殊环境下可能失败
                        logger.debug(f"设置 PRAGMA {key} 失败: {e}")
                # 也保证 busy_timeout 最低值
                try:
                    conn.execute('PRAGMA busy_timeout=30000;')
                except sqlite3.Error:
                    pass
        except Exception as e:
            logger.debug(f"apply pragma exception: {e}")

        return conn

    def _is_connection_alive(self, conn: sqlite3.Connection) -> bool:
        """轻量级检查连接是否可用"""
        try:
            # 使用 execute 而非 cursor() 可减少开销
            conn.execute('SELECT 1')
            return True
        except Exception:
            return False

    def _recreate_connection_for_thread(self, tid: int):
        """为线程重新创建连接（关闭旧连接并创建新连接）"""
        # 关闭旧的（如果存在）
        with self._global_lock:
            old = self._thread_conns.pop(tid, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
                # 注意：_created_connections 不在此处减少，因为我们马上会创建新连接并保持计数不变

            # 创建新连接（不再检查 semaphore，因为原来的连接已持有 semaphore）
            conn = self._create_connection()
            self._thread_conns[tid] = conn
            meta = {
                'created_at': time.time(),
                'last_used': time.time(),
                'usage_count': 0
            }
            self._thread_conn_meta[tid] = meta
            return conn

    # -------------------------
    # 外部接口
    # -------------------------
    @contextmanager
    def get_connection(self):
        """
        Context manager 返回当前线程的连接（如果不存在则创建）
        语义与原来保持一致：使用方式 `with pool.get_connection() as conn:`
        """
        tid = threading.get_ident()
        conn = None
        created_here = False

        # 首先尝试复用当前线程已有连接
        with self._global_lock:
            if tid in self._thread_conns:
                conn = self._thread_conns[tid]
                meta = self._thread_conn_meta.get(tid)
                if meta:
                    meta['last_used'] = time.time()
                    meta['usage_count'] = meta.get('usage_count', 0) + 1

        # 如果没有，需创建新的连接（受 semaphore 限制）
        if conn is None:
            # 尝试获取信号量（如果已达到上限，将阻塞直到有释放）
            logger.debug(f"线程 {tid} 尝试获取数据库连接（最大 {self.max_connections}）")
            self._semaphore.acquire()  # 阻塞直到可用
            created_here = True
            with self._global_lock:
                # double-check（另一个同线程操作不太可能，但保持健壮性）
                if tid not in self._thread_conns:
                    conn = self._create_connection()
                    self._thread_conns[tid] = conn
                    self._thread_conn_meta[tid] = {
                        'created_at': time.time(),
                        'last_used': time.time(),
                        'usage_count': 1
                    }
                    self._created_connections += 1
                else:
                    conn = self._thread_conns[tid]
                    self._thread_conn_meta[tid]['usage_count'] += 1

        # 到这里，conn 必然非 None
        try:
            # 再次健康检查（保护长期存活但被外部因素破坏的连接）
            if not self._is_connection_alive(conn):
                logger.warning(f"线程 {tid} 的连接不可用，尝试重建")
                # 如果连接是我们自己创建的，占用的 semaphore 保持不变；否则这里仍要安全重建
                conn = self._recreate_connection_for_thread(tid)
            yield conn
        finally:
            # 只做使用计数的减少和 last_used 更新；连接保留给当前线程以供复用
            with self._global_lock:
                meta = self._thread_conn_meta.get(tid)
                if meta:
                    meta['last_used'] = time.time()
                    # usage_count 保持不低于 0
                    meta['usage_count'] = max(0, meta.get('usage_count', 1) - 1)

            # 如果这是新创建并且被立即释放（极端场景），我们仍然保留连接给线程复用，
            # 释放 semaphore 的职责交由 force_close_all_connections() 或空闲回收策略。
            # 说明：不自动释放 semaphore，这样能确保 max_connections 是同时活跃线程上限。
            pass

    def get_pool_status(self) -> dict:
        """返回状态信息"""
        with self._global_lock:
            active = len(self._thread_conns)
            available_slots = max(0, self.max_connections - active)
            return {
                'max_connections': self.max_connections,
                'created_connections': self._created_connections,
                'active_thread_connections': active,
                'available_slots': available_slots,
                'db_file': self.db_file
            }

    def is_pool_healthy(self) -> bool:
        """简单健康检查"""
        status = self.get_pool_status()
        # 如果创建数超过最大连接数，显然有异常
        if status['created_connections'] > status['max_connections']:
            return False
        # 如果活跃连接非常接近上限，发出警告
        if status['active_thread_connections'] > self.max_connections * 0.8:
            logger.warning(f'连接池使用率高: {status["active_thread_connections"]}/{self.max_connections}')
        return True

    def force_close_all_connections(self) -> None:
        """
        关闭并清理所有线程连接，释放 semaphore 计数
        调用者应保证不会在关闭后继续使用旧的 Database 实例（与 database.py 约定一致）
        """
        with self._global_lock:
            logger.info(f'强制关闭连接池所有连接 (total={len(self._thread_conns)})')
            closed = 0
            for tid, conn in list(self._thread_conns.items()):
                try:
                    try:
                        conn.close()
                    except Exception as e:
                        logger.debug(f"关闭连接时出错 (tid={tid}): {e}")
                finally:
                    closed += 1
                    # 释放 semaphore（每关闭一个连接释放一个 slot）
                    try:
                        self._semaphore.release()
                    except ValueError:
                        # 如果 semaphore 超额释放，会抛 ValueError，我们忽略
                        pass
                    # 清理字典
                    self._thread_conns.pop(tid, None)
                    self._thread_conn_meta.pop(tid, None)

            # reset created count
            self._created_connections = 0
            logger.info(f'已强制关闭 {closed} 个连接')

    # 可选：提供一个主动回收空闲连接的方法（未自动运行）
    def prune_idle_connections(self, idle_seconds: float = 300.0) -> int:
        """
        关闭指定空闲时间以上的连接，返回关闭数量
        这个操作是显式的（不会自动启动线程），需要调用方按需触发（例如定时任务）
        """
        now = time.time()
        closed = 0
        with self._global_lock:
            for tid, meta in list(self._thread_conn_meta.items()):
                last = meta.get('last_used', 0)
                usage = meta.get('usage_count', 0)
                if usage == 0 and (now - last) >= idle_seconds:
                    conn = self._thread_conns.pop(tid, None)
                    self._thread_conn_meta.pop(tid, None)
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        closed += 1
                        try:
                            self._semaphore.release()
                        except ValueError:
                            pass
                        self._created_connections = max(0, self._created_connections - 1)
        if closed:
            logger.info(f'回收 {closed} 个空闲连接')
        return closed
