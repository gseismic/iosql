"""Parquet 写入的内部列式导入；不改变不受支持输入的既有转换语义。"""

from __future__ import annotations

import math


_MIN_ROWS = 128
_EXACT_FLOAT_INTEGER = 1 << 53
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _optional_arrow():
    try:
        import pyarrow
    except ImportError:
        return None
    return pyarrow


def _compatible(values, sql_type):
    """仅接受批量绑定与逐值绑定有相同表示的内建类型。"""
    for value in values:
        if value is None:
            continue
        if sql_type == 'BIGINT':
            if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
                return False
        elif sql_type == 'DOUBLE':
            if type(value) is float:
                # DuckDB 的 Python 列表绑定会把 NaN 转成 NULL。
                if not math.isfinite(value):
                    return False
            elif type(value) is not int or abs(value) > _EXACT_FLOAT_INTEGER:
                return False
        elif sql_type == 'VARCHAR':
            if type(value) is not str:
                return False
        elif sql_type == 'BLOB':
            if type(value) is not bytes:
                return False
        else:
            return False
    return True


def try_insert(conn, rows, columns, sql_types, fields):
    """尝试无冲突覆盖的列式 INSERT；无法证明等价时返回 False。

    调用方持有事务和连接。此方法只在开始 INSERT 前回退；SQL 的约束错误
    必须由调用方正常处理。数据规模受调用方批次大小约束。
    """
    if len(rows) < _MIN_ROWS:
        return False
    vectors = [[row.get(column) for row in rows] for column in columns]
    if any(not _compatible(values, sql_type)
           for values, sql_type in zip(vectors, sql_types)):
        return False

    pa = _optional_arrow()
    if pa is not None:
        arrow_types = {
            'BIGINT': pa.int64(), 'DOUBLE': pa.float64(),
            'VARCHAR': pa.string(), 'BLOB': pa.binary(),
        }
        try:
            arrays = [pa.array(values, type=arrow_types[sql_type], from_pandas=False)
                      for values, sql_type in zip(vectors, sql_types)]
            batch = pa.Table.from_arrays(arrays, names=list(columns))
        except (pa.ArrowException, UnicodeError, OverflowError):
            return False
        conn.register('__iosql_bulk_input', batch)
        conn.execute(f'INSERT INTO data ({fields}) SELECT * FROM __iosql_bulk_input')
    else:
        expressions = ', '.join(f'UNNEST(?::{sql_type}[])' for sql_type in sql_types)
        conn.execute(f'INSERT INTO data ({fields}) SELECT {expressions}', vectors)
    return True
