import re
from dataclasses import dataclass
from typing import Any, List, Sequence

from ...exception import BadArgument


# 纯列名白名单：字符串 fields 只允许列名列表，表达式一律拒绝，
# 防止把 SQL 语法原样拼进 SELECT（old S-M5 / P1-1）
_SQL_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')


def _quote_sql_identifier(name):
    name = str(name).replace('"', '""')
    return f'"{name}"'


def _column_type(column_types, field):
    try:
        type_ = column_types[field]
    except KeyError as e:
        raise BadArgument(f'Unknown field `{field}`') from e
    if isinstance(type_, str):
        return type_.lower()
    return type_


@dataclass(frozen=True)
class SqlQuery:
    sql: str
    params: List[Any]


_OP_SQL = {
    'gt': '>',
    '>': '>',
    'ge': '>=',
    '>=': '>=',
    'lt': '<',
    '<': '<',
    'le': '<=',
    '<=': '<=',
    'eq': '=',
    '==': '=',
    '=': '=',
    'neq': '!=',
    '!=': '!=',
}


def _select_identifier_field_sql(field):
    text = str(field).strip()
    if not text:
        raise BadArgument('select field is empty')
    if text == '*':
        return text
    return _quote_sql_identifier(text)


def _select_string_field_sql(item, original):
    """字符串 fields 的单个片段：只接受列名（可带双引号），统一加引号。"""
    text = item.strip()
    if not text:
        raise BadArgument(f'select fields `{original}` 含空列名')
    if text == '*':
        return text
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        inner = text[1:-1].replace('""', '"').strip()
        if not inner:
            raise BadArgument(f'select fields `{original}` 含空列名')
        return _quote_sql_identifier(inner)
    if not _SQL_IDENTIFIER_RE.match(text):
        raise BadArgument(
            f'select fields `{original}` 不是纯列名列表（不允许 SQL 语法）；'
            '修复: 用逗号分隔的列名，表达式/聚合请改用 query()/explain_select()'
        )
    return _quote_sql_identifier(text)


def build_select_fields_sql(fields):
    if fields is None:
        return '*'
    if isinstance(fields, (tuple, list)):
        return ','.join(_select_identifier_field_sql(field) for field in fields)
    if isinstance(fields, str):
        text = fields.strip(' ').strip(',')
        if not text:
            raise BadArgument('select fields must not be empty')
        items = text.split(',')
        if len(items) == 1 and items[0].strip() == '*':
            return '*'
        if any(item.strip() == '*' for item in items):
            raise BadArgument(
                f'select fields `{fields}` 中通配符 `*` 必须单独使用'
            )
        return ','.join(
            _select_string_field_sql(item, fields) for item in items
        )
    raise BadArgument('BadFieldType')


def build_case_subquery(case, column_types):
    """Build a parameterized SQL fragment for one Case."""
    field = case.field
    op = case.op
    value = case.value['value']
    _column_type(column_types, field)
    field_sql = _quote_sql_identifier(field)

    if op == 'between_and':
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise BadArgument(
                'op `between` 需要长度为 2 的列表值，'
                '示例: {"field": "ts", "op": "between", "value": [1, 10]}'
            )
        return SqlQuery(f'{field_sql} between ? and ?', [value[0], value[1]])

    if op == 'in':
        if not isinstance(value, (list, tuple)) or not value:
            raise BadArgument(
                'op `in` 需要非空列表值，'
                '示例: {"field": "symbol", "op": "in", "value": ["AAPL", "MSFT"]}'
            )
        placeholders = ','.join(['?'] * len(value))
        return SqlQuery(f'{field_sql} in ({placeholders})', list(value))

    if case.value['type'] == 'variable':
        if not isinstance(value, str):
            raise BadArgument('variable 类型条件值必须是字段名字符串')
        _column_type(column_types, value)
        rhs = _quote_sql_identifier(value)
        params = []
    else:
        rhs = '?'
        params = [value]

    try:
        op_sql = _OP_SQL[op]
    except KeyError as e:
        raise BadArgument(
            f"不支持的 op `{op}`。"
            "合法 op: ==, !=, >, >=, <, <=, between, in。"
            '示例: {"field": "ts", "op": "between", "value": [1, 10]}'
        ) from e
    return SqlQuery(f'{field_sql} {op_sql} {rhs}', params)


def build_and_cases_subquery(cases, column_types):
    if not cases:
        return SqlQuery('', [])

    sqls = []
    params = []
    for case in cases:
        item = build_case_subquery(case, column_types)
        sqls.append(item.sql)
        params.extend(item.params)
    return SqlQuery(' and '.join(sqls), params)


def build_or_cases_subquery(or_cases: Sequence[Sequence], column_types):
    if not or_cases:
        return SqlQuery('', [])

    sqls = []
    params = []
    for cases in or_cases:
        if not cases:
            # 空 AND 组恒真；OR 中只要有一组恒真，整体恒真。
            # 与 select 路径“空条件=全集”保持一致，禁止静默丢弃该组
            return SqlQuery('', [])
        item = build_and_cases_subquery(cases, column_types)
        sqls.append(f'({item.sql})')
        params.extend(item.params)
    if not sqls:
        return SqlQuery('', [])
    return SqlQuery(' or '.join(sqls), params)


def _build_select_with_where(part_table_name, field_sql, where,
                             order_by_sql=None, limit=None):
    sql = f'select {field_sql} from {part_table_name} '
    if where.sql:
        sql += f' where {where.sql}'
    if order_by_sql:
        sql += f' order by {order_by_sql}'
    params = list(where.params)
    if limit is not None:
        sql += ' limit ?'
        params.append(int(limit))
    return SqlQuery(sql + ';', params)


def build_select_cases_query(part_table_name, fields, cases, column_types,
                             order_by_sql=None, limit=None):
    where = build_and_cases_subquery(cases, column_types)
    return _build_select_with_where(
        part_table_name,
        build_select_fields_sql(fields),
        where,
        order_by_sql=order_by_sql,
        limit=limit,
    )


def build_select_or_cases_query(part_table_name, fields, or_cases, column_types,
                                order_by_sql=None, limit=None):
    where = build_or_cases_subquery(or_cases, column_types)
    return _build_select_with_where(
        part_table_name,
        build_select_fields_sql(fields),
        where,
        order_by_sql=order_by_sql,
        limit=limit,
    )


def build_select_count_query(part_table_name, cases, column_types):
    """内部计数查询：字段为固定的 COUNT(*)，不经过用户 fields 白名单。"""
    where = build_and_cases_subquery(cases, column_types)
    return _build_select_with_where(part_table_name, 'COUNT(*)', where)


def build_select_exists_query(part_table_name, cases, column_types):
    """内部存在性查询：字段为固定字面量 1，限制 1 行。"""
    where = build_and_cases_subquery(cases, column_types)
    return _build_select_with_where(
        part_table_name,
        '1',
        where,
        limit=1,
    )


def build_delete_query(part_table_name, cases, column_types):
    """Build DELETE SQL from cases."""
    sql = f'DELETE FROM {part_table_name} '
    where = build_and_cases_subquery(cases, column_types)
    if where.sql:
        sql += f' where {where.sql}'
    return SqlQuery(sql + ';', where.params)
