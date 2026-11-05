"""只读识别目录库的存储后端，供枚举和破坏性操作共用。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


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
                actual = parquet_conn.read_parquet(str(path)).count('*').fetchone()[0]
            else:
                with closing(duckdb.connect(config={'threads': 1})) as conn:
                    actual = conn.read_parquet(str(path)).count('*').fetchone()[0]
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
    """严格检查目录内容；空库没有身份标记，仅在明确允许时接受。"""
    db_path = Path(path)
    if db_path.is_symlink() or not db_path.is_dir():
        return False
    try:
        from ..coordination import physical_path
        db_path = physical_path(db_path)
        children = list(db_path.iterdir())
        if not children:
            return allow_empty
        if any(child.is_symlink() for child in children):
            return False
        directories = [child for child in children if child.is_dir()
                       and child.name not in ('__iosql_migrations__', '__iosql_generations__')]
        if not directories:
            return False
        valid_tables = [child for child in directories
                        if is_iosql_table(child, deep=deep, backend=backend)]
        if not valid_tables:
            return allow_empty and all(is_iosql_scaffold(child) for child in directories)
        # 库根目录的普通文件允许随完整备份恢复；普通子目录不能伪装成表。
        return all(child in valid_tables or is_iosql_scaffold(child) for child in directories)
    except OSError:
        return False
