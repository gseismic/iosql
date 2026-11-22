"""显式检测和迁移低层 SQLite 文件中的旧 NumPy BLOB 主键。"""

import sqlite3
from collections.abc import Mapping
from uuid import uuid4

from .exception import BadArgument
from .sql import quoted_sql_text, sqlite_affinity


def primary_key_info(conn, table):
    columns = sorted(
        (item for item in conn.execute(f'PRAGMA table_info({table})') if item[5]),
        key=lambda item: item[5],
    )
    collations = {}
    for index in conn.execute(f'PRAGMA index_list({table})'):
        if index[3] != 'pk':
            continue
        key_index = quoted_sql_text(index[1])
        collations = {item[2]: item[4] for item in conn.execute(f'PRAGMA index_xinfo({key_index})')
                      if item[5]}
        break

    strict = any(row[5] for row in conn.execute(f'PRAGMA table_list({table})'))
    return [
        (name, 'BLOB' if strict and declared.upper() == 'ANY' else sqlite_affinity(declared),
         collations.get(name, 'BINARY'))
        for _, name, declared, *_ in columns
    ]


def _decode_numpy_key(np, value, dtype):
    raw = np.frombuffer(value, dtype=dtype, count=1)[0]
    if dtype.kind in 'iu':
        result = int(raw)
        if not -(2**63) <= result < 2**63:
            raise BadArgument('旧 NumPy 主键超出 SQLite INTEGER 的范围')
        return result
    if dtype.kind == 'b':
        if value not in (b'\x00', b'\x01'):
            raise BadArgument('BLOB 不是有效的 NumPy bool 字节')
        return bool(raw)
    result = float(raw)
    if raw != result:
        raise BadArgument('旧 NumPy 主键无法精确存为 SQLite REAL')
    return result


def migrate_numpy_blob_keys(conn, table_name, columns, where, dry_run):
    try:
        import numpy as np
    except ImportError as exc:
        raise BadArgument('检查旧 NumPy 主键需要安装 numpy') from exc

    if not isinstance(columns, Mapping) or not columns:
        raise BadArgument('columns 必须指定主键列及其原始 NumPy dtype')
    table = quoted_sql_text(table_name)
    info = primary_key_info(conn, table)
    names = [name for name, _, _ in info]
    if set(columns) - set(names):
        raise BadArgument('columns 只接受已存在的主键列')
    try:
        dtypes = {names.index(name): np.dtype(dtype) for name, dtype in columns.items()}
    except (ValueError, TypeError) as exc:
        raise BadArgument('columns 包含无效的 NumPy dtype') from exc
    if any(dtype.kind not in 'biuf' for dtype in dtypes.values()):
        raise BadArgument('旧 NumPy 主键迁移支持整数、浮点和布尔 dtype')
    if where is not None and (not isinstance(where, str) or not where.strip()):
        raise BadArgument('where 应为非空的 SQLite 条件')

    quoted = [quoted_sql_text(name) for name in names]
    fields = ', '.join(quoted)
    comparisons = [f'{name} COLLATE {quoted_sql_text(collation)}'
                   for name, (_, _, collation) in zip(quoted, info)]
    old_key = ' AND '.join(part + ' IS ?' for part in comparisons)
    new_key = ' AND '.join(part + ' = ?' for part in comparisons)
    matches = ' OR '.join(f"(typeof({quoted[i]}) = 'blob' AND length({quoted[i]}) = {dtype.itemsize})"
                          for i, dtype in dtypes.items())
    predicate = f'({matches})' if where is None else f'({matches}) AND ({where})'
    pending = []
    for row in conn.execute(f'SELECT {fields} FROM {table} WHERE {predicate}'):
        updates = {
            i: _decode_numpy_key(np, row[i], dtype) for i, dtype in dtypes.items()
            if isinstance(row[i], bytes) and len(row[i]) == dtype.itemsize
        }
        pending.append((tuple(row), updates))
    if not pending:
        return 0

    suffix = uuid4().hex
    stage = f'temp.iosql_keys_{suffix}'
    savepoint = f'iosql_numpy_keys_{suffix}'
    definitions = ', '.join(f'{field} {affinity} COLLATE {quoted_sql_text(collation)}'
                            for field, (_, affinity, collation) in zip(quoted, info))
    placeholders = ', '.join('?' for _ in names)
    conn.execute(f'SAVEPOINT {savepoint}')
    try:
        conn.execute(f'CREATE TABLE {stage} ({definitions}, UNIQUE ({fields}))')
        for old, updates in pending:
            proposed = tuple(updates.get(i, value) for i, value in enumerate(old))
            try:
                conn.execute(f'INSERT INTO {stage} VALUES ({placeholders})', proposed)
            except sqlite3.IntegrityError as exc:
                raise BadArgument('旧 NumPy BLOB 主键转换后相互冲突') from exc
            if conn.execute(f'SELECT 1 FROM {table} WHERE {new_key} LIMIT 1', proposed).fetchone():
                raise BadArgument('旧 NumPy BLOB 主键转换后与已有主键冲突')
            if conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {old_key}', old).fetchone()[0] != 1:
                raise BadArgument('存在无法唯一识别的旧 NumPy BLOB 主键')
            for i, value in updates.items():
                if info[i][1] == 'REAL' and float(value) != value:
                    raise BadArgument('旧 NumPy 整数主键无法精确存为声明的 SQLite REAL')

        if not dry_run:
            for old, updates in pending:
                assignments = ', '.join(f'{quoted[i]} = ?' for i in updates)
                cursor = conn.execute(
                    f'UPDATE OR ABORT {table} SET {assignments} WHERE {old_key}',
                    (*updates.values(), *old),
                )
                if cursor.rowcount != 1:
                    raise BadArgument('迁移期间旧 NumPy 主键发生变化')
        conn.execute(f'DROP TABLE {stage}')
    except Exception:
        conn.execute(f'ROLLBACK TO {savepoint}')
        raise
    finally:
        conn.execute(f'RELEASE {savepoint}')
    return len(pending)
