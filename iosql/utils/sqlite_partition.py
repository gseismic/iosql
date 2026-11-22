"""目录表按 SQLite 落盘值计算地址，调用者的行和 SQL 参数保持原样。"""

import sqlite3
from contextlib import ExitStack, closing
from itertools import islice

from ..exception import BadArgument
from .column import suggest_sqlite_column_types
from .mysqlite.sql import normalize_sqlite_value, sqlite_affinity
from .sqlite_schema import sqlite_column_rules


def sqlite_partition_column(rows, field, column_info, pk, n_suggest_check=None):
    if field in column_info:
        declaration = str(column_info[field].get('type') or 'TEXT')
        inferred = False
    else:
        limit = n_suggest_check or len(rows)
        samples = ({field: row[field]} for row in islice(rows, limit))
        declaration = suggest_sqlite_column_types(samples, n_check=limit)[field]
        inferred = True
    if declaration.isascii() and declaration.isalpha():
        # 普通类型名可直接求亲和性。带空格、列约束等声明交给 SQLite。
        affinity = sqlite_affinity(declaration)
    else:
        affinity, _ = sqlite_column_rules(column_info, pk, (field,))[field]
    return declaration, affinity, inferred


def sqlite_partition_values(rows, field, affinity, *, reject_inexact=False):
    """原生同亲和性参数只读分区列，其他值按实际插入规则求值。"""
    with ExitStack() as stack:
        conn = None
        for row in rows:
            value = normalize_sqlite_value(row[field])
            if type(value) is int and not -(2**63) <= value < 2**63:
                raise OverflowError('Python int too large to convert to SQLite INTEGER')
            if type(value) is int and affinity == 'REAL':
                stored = float(value)
                if reject_inexact and stored != value:
                    raise BadArgument(
                        f'推断为 REAL 时分区主键 `{field}` 的值 {value} 会丢失整数精度。'
                        '请显式声明 INTEGER，或使用能精确保存主键的类型'
                    )
                value = stored
            elif not (
                value is None
                or isinstance(value, bool)
                or (type(value) in (int, float) and affinity in ('INTEGER', 'NUMERIC', 'REAL', 'BLOB'))
                or (type(value) is str and affinity in ('TEXT', 'BLOB'))
            ):
                if conn is None:
                    conn = stack.enter_context(closing(sqlite3.connect(':memory:')))
                    conn.execute(f'CREATE TABLE routing (value {affinity})')
                conn.execute('INSERT INTO routing VALUES (?)', (value,))
                value = conn.execute('SELECT value FROM routing').fetchone()[0]
                conn.execute('DELETE FROM routing')
            yield value


def sqlite_partition_keys(rows, partition, *, column_info, pk):
    field = partition.TS_COL
    _, affinity, inferred = sqlite_partition_column(rows, field, column_info, pk)
    values = sqlite_partition_values(rows, field, affinity, reject_inexact=inferred and field in pk)
    with closing(values):
        for row, value in zip(rows, values):
            yield partition.get_key(row if value is row[field] else {field: value})
