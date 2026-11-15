"""目录库身份识别和存储后端一致性保护。"""

import json
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from ..exception import DatabaseBusy, UnsafeOperation


# __ 前缀已保留给内部对象，避免与历史合法表名冲突。
_ENGINE_MARKER = '__iosql_database__.json'
_ENGINE_MARKER_VERSION = 1
_LOCAL_ENGINES = frozenset(('sqlite', 'parquet'))
_engine_local = threading.local()
_engine_fds = set()
_engine_fds_lock = threading.Lock()


def _engine_after_fork():
    """子进程关闭继承的锁描述符，但不解锁父进程持有的锁。"""
    global _engine_local, _engine_fds, _engine_fds_lock
    for fd in _engine_fds:
        try:
            os.close(fd)
        except OSError:
            pass
    _engine_local = threading.local()
    _engine_fds = set()
    _engine_fds_lock = threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_engine_after_fork)


@contextmanager
def storage_engine_lock(uri, *, exclusive, deadline=None):
    """共享锁覆盖写入，独占锁覆盖表构造和识别；deadline 为单调时钟绝对时间。"""
    from ..coordination import ensure_directory, resource
    from ..file_lock import FileLock, open_lock_file

    item = resource(uri)
    if item.backend not in ('sqlite', 'sqlite3', 'parquet'):
        yield
        return
    held = getattr(_engine_local, 'held', None)
    if held is None:
        held = _engine_local.held = {}
    entry = held.get(item.key)
    if entry is not None:
        if exclusive and not entry['exclusive']:
            raise UnsafeOperation('存储类型扫描不能升级正在持有的共享锁')
        entry['depth'] += 1
        try:
            yield
        finally:
            entry['depth'] -= 1
        return
    ensure_directory(item.control_dir)
    lock_path = item.control_dir / (item.token + '.engine.lock')
    pid = os.getpid()
    fd = open_lock_file(lock_path)
    with _engine_fds_lock:
        _engine_fds.add(fd)
    lock = None
    try:
        lock = FileLock(fd)
        if deadline is None:
            deadline = time.monotonic() + 30.0
        while True:
            try:
                lock.acquire(exclusive)
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabaseBusy('等待数据库存储类型锁超时') from exc
                time.sleep(min(0.01, remaining))
        held[item.key] = {'exclusive': exclusive, 'depth': 1}
        try:
            yield
        finally:
            held.pop(item.key, None)
    finally:
        # fork 子进程已在回调中关闭继承的描述符；它可能已被重新使用。
        if pid == os.getpid():
            try:
                if lock is not None:
                    lock.release()
            finally:
                with _engine_fds_lock:
                    _engine_fds.discard(fd)
                os.close(fd)


def _marker_engine(root):
    marker = Path(root) / _ENGINE_MARKER
    if marker.is_symlink():
        raise UnsafeOperation(f'库类型标记不能是符号链接: {marker}')
    try:
        with marker.open(encoding='utf-8') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise UnsafeOperation(f'库类型标记无法读取: {marker}') from exc
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value.get('version') != _ENGINE_MARKER_VERSION
            or not isinstance(value.get('engine'), str)
            or value.get('engine') not in _LOCAL_ENGINES):
        raise UnsafeOperation(f'库类型标记无效或版本不受支持: {marker}')
    return value['engine']


def _manifest_engine(table_path):
    """只读取表级身份；旧版未标注类型的有效 manifest 按 SQLite 识别。"""
    meta = table_path / 'meta.sqlite3'
    if meta.is_symlink():
        raise UnsafeOperation(f'表元数据不能是符号链接: {meta}')
    if not meta.is_file():
        return None
    try:
        with closing(sqlite3.connect(f'{meta.resolve().as_uri()}?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT meta FROM meta WHERE table_name = ?',
                               (table_path.name,)).fetchone()
        if row is None:
            raise ValueError('missing table manifest')
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValueError('invalid table manifest')
        backend = value.get('storage_engine', 'sqlite')
        if backend not in _LOCAL_ENGINES or not is_iosql_table(table_path, backend=backend):
            raise ValueError('invalid storage engine or table manifest')
        return backend
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        if is_iosql_scaffold(table_path):
            return None
        raise UnsafeOperation(f'无法识别表的存储类型: {table_path}') from exc


def detect_storage_engine(root, *, verify_tables=True):
    """只读识别物理库；不读取业务数据，也不把未知或混合库当作空库。"""
    root = Path(root)
    if not root.exists():
        return None
    if not root.is_dir():
        raise UnsafeOperation(f'数据库路径不是目录: {root}')
    marker_engine = _marker_engine(root)
    if marker_engine is not None and not verify_tables:
        return marker_engine
    engines = set()
    foreign = []
    for child in root.iterdir():
        if child.name.startswith('__'):
            continue
        if child.is_symlink():
            raise UnsafeOperation(f'数据库内容不能是符号链接: {child}')
        if not child.is_dir():
            continue
        engine = _manifest_engine(child)
        if engine is not None:
            engines.add(engine)
        elif not is_iosql_scaffold(child) and any(child.iterdir()):
            foreign.append(child)
    if foreign:
        # 已有库中可能有不属于 iosql 的普通目录；但疑似表的数据目录缺少
        # manifest 时不能静默忽略，完全未知的目录也不能被接管为新库。
        suspect = next((child for child in foreign if os.path.lexists(child / 'data')), None)
        if suspect is not None or (not engines and marker_engine is None):
            raise UnsafeOperation(f'目录中存在无法识别的表或其他目录: {suspect or foreign[0]}')
    if marker_engine is not None:
        engines.add(marker_engine)
    if len(engines) > 1:
        raise UnsafeOperation(f'数据库混有不同存储类型: {root}')
    return next(iter(engines), None)


def _check_engine(actual, expected, root):
    expected = 'sqlite' if expected in ('sqlite', 'sqlite3') else expected
    if actual is not None and actual != expected:
        raise UnsafeOperation(
            f'数据库存储类型不匹配: {root}；实际为 {actual}，请求为 {expected}；'
            '请使用实际类型打开，或迁移到新的目标库'
        )


def _write_marker(root, engine):
    from ..coordination import atomic_json
    atomic_json(Path(root) / _ENGINE_MARKER,
                {'version': _ENGINE_MARKER_VERSION, 'engine': engine})


def ensure_storage_engine(uri, *, claim=False):
    """在库租约内串行确认类型；claim 时为旧库或新库持久化类型。"""
    from ..coordination import physical_root, resource

    item = resource(uri)
    if item.backend not in ('sqlite', 'sqlite3', 'parquet'):
        return
    expected = 'sqlite' if item.backend in ('sqlite', 'sqlite3') else 'parquet'
    root = physical_root(uri)
    marker = _marker_engine(root)
    if marker is not None:
        _check_engine(marker, expected, root)
        return
    with storage_engine_lock(uri, exclusive=True):
        root = physical_root(uri)
        actual = detect_storage_engine(root, verify_tables=False)
        _check_engine(actual, expected, root)
        if actual is None or (claim and _marker_engine(root) is None):
            # 未知库必须核对全部表，而不是把已有业务目录覆盖成新库。
            actual = detect_storage_engine(root, verify_tables=True)
            _check_engine(actual, expected, root)
            if actual is None and root.exists():
                for child in root.iterdir():
                    if child.name in (_ENGINE_MARKER, '__generation__.json',
                                      '__iosql_generations__', '__iosql_migrations__'):
                        continue
                    if child.is_dir() and (not any(child.iterdir()) or is_iosql_scaffold(child)):
                        continue
                    raise UnsafeOperation(f'不能初始化含未知内容的数据库目录: {child}')
            _write_marker(root, expected)


def is_iosql_table(path, *, deep=False, backend='sqlite'):
    """检查表级 manifest；deep 时还检查引用的数据文件。"""
    table_path = Path(path)
    meta_path = table_path / "meta.sqlite3"
    if table_path.is_symlink() or meta_path.is_symlink():
        return False
    if not table_path.is_dir() or not meta_path.is_file():
        return False
    try:
        # mode=ro 保证发现过程不会在普通目录内创建数据库。
        with closing(_DataFileValidator(backend)) as validator, \
                closing(sqlite3.connect(f"{meta_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            if deep and conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            row = conn.execute(
                "SELECT meta FROM meta WHERE table_name = ?", (table_path.name,)
            ).fetchone()
            if row is None:
                return False
            manifest = json.loads(row[0])
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("column_info"), (dict, list)
            ) or not isinstance(manifest.get("pk"), list):
                return False
            if manifest.get('storage_engine', 'sqlite') != backend:
                return False
            if deep:
                data_dir = table_path / "data"
                if data_dir.is_symlink() or (data_dir.exists() and not data_dir.is_dir()):
                    return False
                has_parts = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='table_part'"
                ).fetchone()
                parts = conn.execute(
                    "SELECT part FROM table_part WHERE table_name = ?", (table_path.name,)
                ).fetchall() if has_parts else []
                # 新布局把可见 segment 记在表级 manifest；复制/重命名后
                # 旧绝对 path 可能过期，以 part_key 还原目标文件。
                has_segments = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_iosql_segments'"
                ).fetchone()
                segmented_parts = set()
                if has_segments:
                    for part, part_key, row_count in conn.execute(
                        'SELECT logical_part, part_key, row_count FROM "_iosql_segments" '
                        'WHERE table_name = ?', (table_path.name,)
                    ):
                        segmented_parts.add(str(part))
                        suffix = '.parquet' if backend == 'parquet' else '.sqlite3'
                        candidate = data_dir / f"{part_key}{suffix}"
                        if not part_key or candidate.parent != data_dir or not validator.valid(
                            candidate, expected_rows=row_count,
                        ):
                            return False
                for (part,) in parts:
                    if str(part) not in segmented_parts:
                        suffix = '.parquet' if backend == 'parquet' else '.sqlite3'
                        candidate = data_dir / f"{part}{suffix}"
                        if candidate.parent != data_dir or not validator.valid(candidate):
                            return False
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return False
    except Exception as exc:
        # 连接关闭失败与原来逐文件连接关闭失败一样，均视为校验失败。
        if backend == 'parquet':
            try:
                import duckdb
            except ImportError:
                pass
            else:
                if isinstance(exc, duckdb.Error):
                    return False
        raise


class _DataFileValidator:
    """在一次表级检查内复用 DuckDB 连接，不共享到其他调用或线程。"""

    def __init__(self, backend):
        self.backend = backend
        self._parquet_conn = None

    def valid(self, path, *, expected_rows=None):
        if self.backend != 'parquet':
            return _valid_data_file(path, expected_rows=expected_rows, backend=self.backend)
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return False
        if self._parquet_conn is None:
            try:
                import duckdb
                self._parquet_conn = duckdb.connect(config={'threads': 1})
            except (ImportError, OSError, ValueError):
                return False
            except duckdb.Error:
                return False
        return _valid_data_file(path, expected_rows=expected_rows, backend=self.backend,
                                parquet_conn=self._parquet_conn)

    def close(self):
        if self._parquet_conn is not None:
            self._parquet_conn.close()


def _valid_data_file(path, *, expected_rows=None, backend='sqlite', parquet_conn=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    if backend == 'parquet':
        try:
            import duckdb
            if parquet_conn is not None:
                actual = parquet_conn.read_parquet(str(path)).count('*').fetchall()[0][0]
            else:
                with closing(duckdb.connect(config={'threads': 1})) as conn:
                    actual = conn.read_parquet(str(path)).count('*').fetchall()[0][0]
            return expected_rows is None or actual == int(expected_rows)
        except (ImportError, OSError, ValueError):
            return False
        except duckdb.Error:
            return False
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                return False
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='data'"
            ).fetchone():
                return False
            if expected_rows is not None:
                actual = conn.execute('SELECT COUNT(*) FROM "data"').fetchone()[0]
                if int(expected_rows) != actual:
                    return False
            return True
    except (OSError, sqlite3.Error):
        return False


def iosql_table_names(path, *, backend='sqlite'):
    """仅列出有真实表级 manifest 的子目录。"""
    from ..coordination import physical_path
    db_path = physical_path(path)
    if not db_path.is_dir():
        return []
    return sorted(
        child.name for child in db_path.iterdir()
        if child.is_dir() and not child.name.startswith("__")
        and is_iosql_table(child, backend=backend)
    )


def is_iosql_scaffold(path):
    """识别尚未首次写入的空表目录，不接受含业务文件的普通目录。"""
    table_path = Path(path)
    allowed = {"meta.sqlite3", "meta.sqlite3-wal", "meta.sqlite3-shm", ".iosql.write.lock"}
    try:
        children = list(table_path.iterdir())
        if not children or any(child.is_symlink() or child.name not in allowed for child in children):
            return False
        meta_path = table_path / "meta.sqlite3"
        if not meta_path.is_file():
            return False
        with closing(sqlite3.connect(f"{meta_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone() is None
    except (OSError, sqlite3.Error):
        return False


def is_iosql_database(path, *, allow_empty=False, deep=False, backend='sqlite'):
    """严格检查目录内容；带有效身份标记的空库也属于完整的库。"""
    db_path = Path(path)
    if db_path.is_symlink() or not db_path.is_dir():
        return False
    try:
        from ..coordination import physical_path
        db_path = physical_path(db_path)
        children = list(db_path.iterdir())
        if not children:
            return allow_empty
        marker_engine = _marker_engine(db_path)
        if marker_engine is not None and marker_engine != backend:
            return False
        if any(child.is_symlink() for child in children):
            return False
        directories = [child for child in children if child.is_dir()
                       and child.name not in ('__iosql_migrations__', '__iosql_generations__')]
        if not directories:
            return marker_engine is not None
        valid_tables = [child for child in directories
                        if is_iosql_table(child, deep=deep, backend=backend)]
        if not valid_tables:
            return ((allow_empty or marker_engine is not None)
                    and all(is_iosql_scaffold(child) for child in directories))
        # 库根目录的普通文件允许随完整备份恢复；普通子目录不能伪装成表。
        return all(child in valid_tables or is_iosql_scaffold(child) for child in directories)
    except OSError:
        return False
