import os
import sqlite3
import threading
import queue
import weakref
import time
from contextlib import contextmanager
from typing import Dict, Any, Optional
from urllib.parse import parse_qs, urlsplit

from ...exception import BadArgument as IosqlBadArgument
from .exception import BadArgument


_DEFAULT_MAX_PROCESS_CONNECTIONS = 32
_query_deadline = threading.local()


@contextmanager
def connection_deadline(deadline):
    """为当前一次迭代推进继承查询截止时间，不能跨 yield 污染其他请求。"""
    previous = getattr(_query_deadline, 'value', None)
    _query_deadline.value = min(previous, deadline) if previous is not None else deadline
    try:
        yield
    finally:
        _query_deadline.value = previous


class _ProcessConnectionBudget:
    """进程内所有 mysqlite 连接池共享的真实连接预算。"""

    def __init__(self):
        self._condition = threading.Condition()
        self._pools = weakref.WeakKeyDictionary()
        # 非 mysqlite 池的外部 SQLite 连接（例如有序跨分区 raw 读连接）。
        # value=(声明的进程上限, 预留数量)，预留期间计入同一进程预算。
        self._external_reservations = {}
        self._in_use = 0
        self._pid = os.getpid()
        if hasattr(os, 'register_at_fork'):
            os.register_at_fork(after_in_child=self._after_fork_in_child)

    def _after_fork_in_child(self) -> None:
        """在子进程用户代码运行前重建所有池，避免首个访问并发竞争。"""
        self._ensure_current_process()

    def _ensure_current_process(self) -> bool:
        """子进程首次使用时重建进程级同步状态和所有已注册连接池。"""
        current_pid = os.getpid()
        if current_pid == self._pid:
            return False

        # fork 后不能获取父进程遗留的 Condition/池锁：其持有线程可能已不存在。
        # 子进程只有 fork 调用线程继续运行，可直接替换同步原语并重置全部池。
        pools = list(self._pools.keys())
        self._condition = threading.Condition()
        self._external_reservations = {}
        self._in_use = 0
        self._pid = current_pid
        for pool in pools:
            pool._reset_after_fork_state(current_pid)
        return True

    def register(self, pool, limit: int) -> None:
        self._ensure_current_process()
        with self._condition:
            self._pools[pool] = limit
            self._condition.notify_all()

    def unregister(self, pool) -> None:
        self._ensure_current_process()
        with self._condition:
            self._pools.pop(pool, None)
            self._condition.notify_all()

    def _effective_limit_locked(self, requester=None, external_limit=None) -> int:
        """计算进程连接预算。

        - 有活跃池（正在 checkout，含 requester）时取活跃池声明上限的最小值：
          活跃的保守池仍需被尊重，否则会超发连接。
        - 全部空闲时取所有池声明上限的最大值：空闲的保守临时池不应永久压缩
          全局预算（旧实现恒取 min()，一个临时小池会拖垮整个进程）。
        """
        pools = self._pool_items()
        limits = [
            limit
            for pool, limit in pools
            if pool is requester or pool._checked_out > 0
        ]
        limits.extend(
            limit
            for limit, _ in self._external_reservations.values()
        )
        if external_limit is not None:
            limits.append(int(external_limit))
        if limits:
            return min(limits)
        all_limits = [limit for _, limit in pools]
        return max(all_limits) if all_limits else _DEFAULT_MAX_PROCESS_CONNECTIONS

    def _pool_items(self):
        """先复制底层弱引用映射，再解引用；GC/析构注销不会修改正在遍历的字典。"""
        snapshot = self._pools.data.copy()
        items = []
        for reference, limit in snapshot.items():
            pool = reference()
            if pool is not None:
                items.append((pool, limit))
        return items

    def reset_after_fork(self, pool) -> None:
        """兼容池级入口；进程预算由 ``_ensure_current_process`` 一次性重建。"""
        self._ensure_current_process()
        if pool._pid != os.getpid():
            pool._reset_after_fork_state(os.getpid())
        with self._condition:
            self._pools[pool] = pool._max_process_connections
            self._condition.notify_all()

    def acquire(self, requester) -> None:
        self._ensure_current_process()
        while True:
            deadline = getattr(_query_deadline, 'value', None)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('等待连接预算超过 timeout_ms')
            with self._condition:
                if requester._closed:
                    raise RuntimeError('connection pool is closed')
                limit = self._effective_limit_locked(requester)
                if self._in_use < limit:
                    self._in_use += 1
                    return
                pools = [pool for pool, _ in self._pool_items()]

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
                limit = self._effective_limit_locked(requester)
                if self._external_reservations and self._in_use >= limit:
                    # 流在消费者返回前不会释放预留；继续等待可能永久自锁。
                    raise IosqlBadArgument(
                        '连接配额被流式查询占满；请先关闭读取流，'
                        '或提高 max_process_connections'
                    )
                remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0.1
                self._condition.wait(timeout=min(0.1, remaining))

    def release(self) -> None:
        self._ensure_current_process()
        with self._condition:
            if self._in_use > 0:
                self._in_use -= 1
            self._condition.notify_all()

    def reserve_external(self, count: int, max_process_connections: int):
        """为不属于连接池的连接一次性预留进程预算。

        预留数量必须一次性满足，避免 k 路归并打开一半连接后才发现预算不足。
        先回收其他池的空闲连接；若仍不能立即满足，则直接失败，避免有序迭代器自锁。
        """
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise BadArgument('external connection count must be a positive integer')
        if (
            isinstance(max_process_connections, bool)
            or not isinstance(max_process_connections, int)
            or max_process_connections <= 0
        ):
            raise BadArgument('max_process_connections must be a positive integer')

        self._ensure_current_process()

        while True:
            with self._condition:
                limit = self._effective_limit_locked(
                    external_limit=max_process_connections,
                )
                if count > limit:
                    raise BadArgument(
                        f'ordered stream needs {count} raw connections, '
                        f'exceeding process connection budget {limit}; '
                        'reduce the ordered partition range or increase '
                        'max_process_connections'
                    )
                if self._in_use + count <= limit:
                    token = object()
                    self._external_reservations[token] = (
                        max_process_connections,
                        count,
                    )
                    self._in_use += count
                    return token, count
                pools = [pool for pool, _ in self._pool_items()]

            reclaimed = False
            for pool in pools:
                if pool._close_one_idle_connection_for_budget():
                    reclaimed = True
                    break
            if reclaimed:
                continue

            raise BadArgument(
                f'ordered stream needs {count} raw connections, but only '
                f'{max(0, limit - self._in_use)} are available immediately '
                f'within process connection budget {limit}; '
                'close the active connection or reduce the ordered partition range'
            )

    def release_external(self, reservation) -> None:
        """释放 ``reserve_external`` 返回的进程预算预留。"""
        if reservation is None:
            return
        self._ensure_current_process()
        token, _ = reservation
        with self._condition:
            entry = self._external_reservations.pop(token, None)
            if entry is not None:
                self._in_use = max(0, self._in_use - entry[1])
            self._condition.notify_all()

    def status(self) -> Dict[str, int]:
        self._ensure_current_process()
        with self._condition:
            return {
                'max_process_connections': self._effective_limit_locked(),
                'open_process_connections': self._in_use,
                'registered_pools': len(self._pools),
            }


_PROCESS_CONNECTION_BUDGET = _ProcessConnectionBudget()


def get_process_connection_budget_status() -> Dict[str, int]:
    return _PROCESS_CONNECTION_BUDGET.status()


def reserve_process_connections(count: int, max_process_connections: int):
    """为外部 SQLite 连接预留与 mysqlite 池共享的进程预算。"""
    try:
        return _PROCESS_CONNECTION_BUDGET.reserve_external(
            count,
            max_process_connections,
        )
    except BadArgument as exc:
        raise IosqlBadArgument(str(exc)) from exc


def release_process_connections(reservation) -> None:
    """释放外部 SQLite 连接的进程预算预留。"""
    _PROCESS_CONNECTION_BUDGET.release_external(reservation)


class DatabaseConnectionPool:
    def __init__(self, db_file: str, max_connections: int = 10,
                 pragma_dict: Optional[Dict[str, Any]] = None,
                 max_process_connections: int = _DEFAULT_MAX_PROCESS_CONNECTIONS,
                 **kwargs):
        if int(max_connections) <= 0:
            raise ValueError('max_connections must be positive')
        if int(max_process_connections) <= 0:
            raise ValueError('max_process_connections must be positive')
        # 内存 URI 的连接共享范围和生命周期依赖 URI/cache 配置，不纳入连接池契约。
        db_file_text = str(db_file)
        is_memory_uri = False
        if kwargs.get('uri') and db_file_text.lower().startswith('file:'):
            query = parse_qs(urlsplit(db_file_text).query)
            is_memory_uri = any(
                value.lower() == 'memory'
                for value in query.get('mode', [])
            )
        if (
            db_file_text == ':memory:'
            or db_file_text.lower().startswith('file::memory:')
            or is_memory_uri
        ):
            raise BadArgument(
                '连接池不支持内存数据库（包括 :memory: 和 mode=memory URI）：'
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
        self._checked_out_connections: Dict[int, sqlite3.Connection] = {}
        self._inherited_connections: Dict[int, sqlite3.Connection] = {}
        self._connections: Dict[int, sqlite3.Connection] = {}
        self._connection_pragma_values: Dict[int, Dict[str, str]] = {}
        self._lock = threading.Lock()
        self._pragma_dict = dict(pragma_dict or {})
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

    @staticmethod
    def _format_pragma_value(value: Any) -> Optional[str]:
        """将 PRAGMA 值转成 SQL 字面值；None/空串表示不设置。"""
        if value is None:
            return None
        if isinstance(value, bool):
            return '1' if value else '0'
        value = str(value)
        return value if value else None

    def _create_connection(self) -> sqlite3.Connection:
        _PROCESS_CONNECTION_BUDGET.acquire(self)
        conn_kwargs = {'timeout': 30.0}
        conn_kwargs.update(self._conn_kwargs)
        conn_kwargs.setdefault('check_same_thread', False)
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, **conn_kwargs)
            conn.row_factory = sqlite3.Row
            with self._lock:
                pragma_dict = dict(self._pragma_dict)
            applied = {}
            for key, value in pragma_dict.items():
                value = self._format_pragma_value(value)
                if value is None:
                    continue
                try:
                    conn.execute(f'PRAGMA {key}={value};')
                    applied[key] = value
                except sqlite3.OperationalError:
                    # 与旧行为一致：不支持或暂时无法应用的 PRAGMA 不阻止连接创建。
                    pass
            with self._lock:
                self._connections[id(conn)] = conn
                self._connection_pragma_values[id(conn)] = applied
            return conn
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
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
            tracked = False
            with self._lock:
                connection_id = id(conn)
                tracked = self._connections.pop(connection_id, None) is not None
                self._connection_pragma_values.pop(connection_id, None)
                if self._checked_out_connections.pop(connection_id, None) is not None:
                    self._checked_out = max(0, self._checked_out - 1)
                if tracked and self._created_connections > 0:
                    self._created_connections -= 1
            if tracked:
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
        was_forked = current_pid != self._pid
        _PROCESS_CONNECTION_BUDGET._ensure_current_process()
        if self._pid != current_pid:
            self._reset_after_fork_state(current_pid)
        return was_forked

    def _reset_after_fork_state(self, current_pid: int) -> None:
        """不获取父进程锁，重建池状态并隔离父进程连接租约。"""
        old_available = self._available
        previously_inherited = set(self._inherited_connections)
        inherited = dict(self._inherited_connections)
        inherited.update(self._connections)

        # fork 可能发生在其他线程持有 queue/池锁时，因此只读旧队列的 deque，
        # 不调用旧队列方法，也不获取任何从父进程继承的同步原语。
        idle = list(old_available.queue)
        idle_ids = {id(conn) for conn in idle}
        active_ids = set(self._checked_out_connections)
        active_ids.update(set(self._connections) - idle_ids)
        for conn in idle:
            inherited[id(conn)] = conn

        self._lock = threading.Lock()
        self._available = queue.LifoQueue(maxsize=self._max_connections)
        self._lease_local = threading.local()
        self._created_connections = 0
        self._checked_out = 0
        self._checked_out_connections = {}
        self._connections = {}
        self._connection_pragma_values = {}
        self._inherited_connections = inherited
        self._pid = current_pid

        # 空闲连接没有仍在执行的调用方，可以立即关闭；已 checkout 的连接保留到
        # 旧租约归还时再关闭，避免影响 fork 调用栈中仍持有的连接对象。
        for connection_id, conn in list(inherited.items()):
            if connection_id in active_ids or connection_id in previously_inherited:
                continue
            try:
                conn.close()
            except Exception:
                pass
            self._inherited_connections.pop(connection_id, None)

    def _close_inherited_connection(self, conn: sqlite3.Connection) -> None:
        """关闭父进程遗留连接，不触碰当前子进程的连接计数和预算。"""
        with self._lock:
            self._inherited_connections.pop(id(conn), None)
        try:
            conn.close()
        except Exception:
            pass

    def _apply_configured_pragmas_locked(self, conn: sqlite3.Connection) -> None:
        """在池锁内将当前配置补齐到即将归还的连接。"""
        connection_id = id(conn)
        applied = self._connection_pragma_values.setdefault(connection_id, {})
        for key, raw_value in self._pragma_dict.items():
            value = self._format_pragma_value(raw_value)
            if value is None or applied.get(key) == value:
                continue
            try:
                conn.execute(f'PRAGMA {key}={value};')
                applied[key] = value
            except sqlite3.Error:
                # PRAGMA 失败沿用创建连接和空闲连接路径的 best-effort 语义。
                pass

    def _checkout_connection(self) -> sqlite3.Connection:
        while True:
            deadline = getattr(_query_deadline, 'value', None)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('等待文件连接超过 timeout_ms')
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
                        remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0.1
                        conn = self._available.get(timeout=min(0.1, remaining))
                    except queue.Empty:
                        continue

            if self._is_connection_alive(conn):
                should_close = False
                with self._lock:
                    if self._closed:
                        should_close = True
                    else:
                        self._checked_out += 1
                        self._checked_out_connections[id(conn)] = conn
                if should_close:
                    self._close_connection(conn)
                    raise RuntimeError('connection pool is closed')
                return conn

            self._close_connection(conn)

    def _return_connection(self, conn: sqlite3.Connection) -> None:
        self._ensure_same_process()
        connection_id = id(conn)
        with self._lock:
            if connection_id in self._inherited_connections:
                inherited = True
                tracked = False
                should_close = True
            else:
                inherited = False
                tracked = self._checked_out_connections.pop(connection_id, None) is not None
                if tracked:
                    self._checked_out = max(0, self._checked_out - 1)
                should_close = self._closed

        if inherited:
            self._close_inherited_connection(conn)
            return
        if not tracked:
            # 重复归还不能把同一个连接重复放入可用队列。
            return

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
                self._apply_configured_pragmas_locked(conn)
                try:
                    self._available.put_nowait(conn)
                    should_close = False
                except queue.Full:
                    should_close = True
        if should_close:
            self._close_connection(conn)

    def _close_one_idle_connection_for_budget(self) -> bool:
        self._ensure_same_process()
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
        self._ensure_same_process()
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
        self._ensure_same_process()
        idle = []
        inherited = []
        with self._lock:
            self._closed = True
            while True:
                try:
                    idle.append(self._available.get_nowait())
                except queue.Empty:
                    break
            inherited = list(self._inherited_connections.values())
            self._inherited_connections.clear()

        for conn in idle:
            self._close_connection(conn)
        for conn in inherited:
            try:
                conn.close()
            except Exception:
                pass
        _PROCESS_CONNECTION_BUDGET.unregister(self)

    def get_pool_status(self) -> Dict[str, Any]:
        self._ensure_same_process()
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
        """更新 PRAGMA 配置，并应用到空闲连接；借出连接在归还时更新。"""
        self._ensure_same_process()
        pragma_value = self._format_pragma_value(value)
        with self._lock:
            if self._closed:
                return
            self._pragma_dict[key] = value
            idle = []
            while True:
                try:
                    idle.append(self._available.get_nowait())
                except queue.Empty:
                    break
            for conn in idle:
                if pragma_value is not None:
                    try:
                        conn.execute(f'PRAGMA {key}={pragma_value};')
                        self._connection_pragma_values.setdefault(id(conn), {})[key] = pragma_value
                    except sqlite3.Error:
                        pass
                # 已排空队列，放回不会 Full（put 均在同锁内进行）
                self._available.put_nowait(conn)

    def is_pool_healthy(self) -> bool:
        """池状态是否健康：已关闭、计数为负或超出上限都视为不健康。"""
        self._ensure_same_process()
        with self._lock:
            if self._closed:
                return False
            if self._created_connections < 0 or self._checked_out < 0:
                return False
            if self._created_connections > self._max_connections:
                return False
            return self._available.qsize() <= self._max_connections
