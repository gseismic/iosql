"""由 SQLite 解析完整表结构，供目录表的路由和比较使用。"""

import sqlite3
from contextlib import closing
from functools import lru_cache

from ..exception import BadArgument
from .mysqlite.sql import getsql_create_table, quoted_sql_text, sqlite_affinity


@lru_cache(maxsize=256)
def _parse_schema(sql, fields):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute(sql)
        columns = list(conn.execute('PRAGMA table_xinfo("data")'))
        rules = {}
        for column in columns:
            name, type_name = column[1:3]
            collation = 'BINARY'
            if name in fields:
                conn.execute(f'CREATE INDEX key_index ON "data" ({quoted_sql_text(name)})')
                collation = conn.execute('PRAGMA index_xinfo(key_index)').fetchone()[4].upper()
                conn.execute('DROP INDEX key_index')
            rules[name] = sqlite_affinity(type_name), collation
        return rules


def sqlite_schema_rules(column_info, pk, fields=()):
    """用与真实建表一致的 DDL 校验结构，读取指定列的亲和性和 collation。"""
    sql, _ = getsql_create_table('data', list(column_info.values()), pk, if_not_exists=False)
    rules = _parse_schema(sql, tuple(fields))
    if list(rules) != list(column_info):
        raise BadArgument('SQLite 列声明与请求的列名不一致')
    return rules


def sqlite_column_rules(column_info, pk, fields):
    fields = tuple(fields)
    if not fields:
        return {}
    try:
        return sqlite_schema_rules(column_info, pk, fields)
    except sqlite3.Error as exc:
        raise BadArgument(f'无法确认 SQLite 表的列声明: {exc}') from exc
