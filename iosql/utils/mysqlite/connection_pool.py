import os
import sqlite3
import threading
import queue
import weakref
from contextlib import contextmanager
from typing import Dict, Any, Optional

from .exception import BadArgument


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

    def _effective_limit_locked(self, requester=None) -> int:
        """计算进程连接预算。

        - 有活跃池（正在 checkout，含 requester）时取活跃池声明上限的最小值：
          活跃的保守池仍需被尊重，否则会超发连接。
        - 全部空闲时取所有池声明上限的最大值：空闲的保守临时池不应永久压缩
          全局预算（旧实现恒取 min()，一个临时小池会拖垮整个进程）。
        """
        limits = [
            limit
            for pool, limit in self._pools.items()
            if pool is requester or pool._checked_out > 0
        ]
        if limits:
            return min(limits)
        all_limits = list(self._pools.values())
        return max(all_limits) if all_limits else _DEFAULT_MAX_PROCESS_CONNECTIONS

    def reset_after_fork(self, pool) -> None:
        """fork 后子进程重建预算状态，丢弃从父进程继承的连接计数。"""
        with self._condition:
            self._in_use = 0
            self._pools[pool] = pool._max_process_connections
            self._condition.notify_all()

    def acquire(self, requester) -> None:
        while True:
            with self._condition:
                if requester._closed:
                    raise RuntimeError('connection pool is closed')
                limit = self._effective_limit_locked(requester)
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
        # 池中的每个连接都会独立打开 ':memory:'，数据互不可见，因此直接拒绝
        if str(db_file) == ':memory:' or str(db_file).startswith('file::memory:'):
            raise BadArgument(
                '连接池不支持 :memory: 数据库：每个连接会得到独立的内存库；'
                '请改用文件数据库，或直接用 sqlite3.connect(":memory:")'
            )
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
        # 记录创建进程 pid 与本线程当前持有的租约（支持同线程重入）
        self._pid = os.getpid()
        self._lease_local = threading.local()
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

    def _ensure_same_process(self) -> bool:
        """fork 检测：pid 变化说明连接是从父进程继承的，必须整体重建。

        Returns:
            bool: 是否刚刚执行了跨进程重置。
        """
        current_pid = os.getpid()
        if current_pid == self._pid:
            return False
        self._reset_after_fork()
        self._pid = current_pid
        return True

    def _reset_after_fork(self) -> None:
        """子进程中丢弃父进程继承的连接与计数，重新开始。"""
        idle = []
        with self._lock:
            while True:
                try:
                    idle.append(self._available.get_nowait())
                except queue.Empty:
                    break
            self._created_connections = 0
            self._checked_out = 0
        for conn in idle:
            try:
                conn.close()
            except Exception:
                pass
        _PROCESS_CONNECTION_BUDGET.reset_after_fork(self)

    def _checkout_connection(self) -> sqlite3.Connection:
        while True:
            self._ensure_same_process()
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
        # 子进程里归还父进程继承的连接：直接丢弃，不能放回池中
        if self._ensure_same_process():
            self._close_connection(conn)
            return

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

        # 校验期间池可能已被关闭：必须在锁内复查并决定是否放回
        with self._lock:
            if self._closed:
                should_close = True
            else:
                try:
                    self._available.put_nowait(conn)
                    should_close = False
                except queue.Full:
                    should_close = True
        if should_close:
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
        """获取连接上下文；同线程可重入（嵌套时复用同一连接）。

        嵌套场景（如 max_connections=1 时在外层连接中调用需要连接的 API）
        若每次都从池中重新 checkout 会自锁，这里按线程维护带引用计数的租约。
        """
        lease = getattr(self._lease_local, 'lease', None)
        if lease is not None:
            if self._closed:
                raise RuntimeError('connection pool is closed')
            lease['depth'] += 1
            try:
                yield lease['conn']
            finally:
                lease['depth'] -= 1
            return

        conn = self._checkout_connection()
        lease = {'conn': conn, 'depth': 1}
        self._lease_local.lease = lease
        try:
            yield conn
        finally:
            lease['depth'] -= 1
            self._lease_local.lease = None
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

    def apply_pragma_to_idle(self, key: str, value: Any) -> None:
        """把 PRAGMA 应用到当前空闲连接；使用中的连接不受影响（等归还或新建时生效）。"""
        with self._lock:
            if self._closed:
                return
            idle = []
            while True:
                try:
                    idle.append(self._available.get_nowait())
                except queue.Empty:
                    break
            for conn in idle:
                try:
                    conn.execute(f'PRAGMA {key}={value};')
                except sqlite3.Error:
                    pass
                # 已排空队列，放回不会 Full（put 均在同锁内进行）
                self._available.put_nowait(conn)

    def is_pool_healthy(self) -> bool:
        """池状态是否健康：已关闭、计数为负或超出上限都视为不健康。"""
        with self._lock:
            if self._closed:
                return False
            if self._created_connections < 0 or self._checked_out < 0:
                return False
            if self._created_connections > self._max_connections:
                return False
            return self._available.qsize() <= self._max_connections
