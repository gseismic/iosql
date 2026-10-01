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
        with closing(sqlite3.connect(f"{meta_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
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
                if not data_dir.is_dir() or data_dir.is_symlink():
                    return False
                parts = conn.execute(
                    "SELECT part FROM table_part WHERE table_name = ?", (table_path.name,)
                ).fetchall()
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
                        if not part_key or candidate.parent != data_dir or not _valid_data_file(
                            candidate, expected_rows=row_count, backend=backend,
                        ):
                            return False
                for (part,) in parts:
                    if str(part) not in segmented_parts:
                        suffix = '.parquet' if backend == 'parquet' else '.sqlite3'
                        candidate = data_dir / f"{part}{suffix}"
                        if candidate.parent != data_dir or not _valid_data_file(candidate, backend=backend):
                            return False
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return False


def _valid_data_file(path, *, expected_rows=None, backend='sqlite'):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    if backend == 'parquet':
        try:
            import duckdb
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
    db_path = Path(path)
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


def is_iosql_database(path, *, allow_empty=False, deep=False):
    """严格检查目录内容；空库没有身份标记，仅在明确允许时接受。"""
    db_path = Path(path)
    if db_path.is_symlink() or not db_path.is_dir():
        return False
    try:
        children = list(db_path.iterdir())
        if not children:
            return allow_empty
        if any(child.is_symlink() for child in children):
            return False
        directories = [child for child in children if child.is_dir()]
        if not directories:
            return False
        valid_tables = [child for child in directories if is_iosql_table(child, deep=deep)]
        if not valid_tables:
            return False
        # 库根目录的普通文件允许随完整备份恢复；普通子目录不能伪装成表。
        return all(child in valid_tables or is_iosql_scaffold(child) for child in directories)
    except OSError:
        return False
