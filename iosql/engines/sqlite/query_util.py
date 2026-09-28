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


def _bucket_require_column(column_types, col):
    """分桶聚合列存在性校验：与 where 条件同级的 Unknown field 语义。"""
    if not column_types or col in column_types:
        return
    raise BadArgument(f'Unknown field `{col}`')


def build_bucket_stats_query(part_table_name, x_col, value_cols,
                             or_and_cases, column_types):
    """分桶聚合的探测查询（PLAN-070）：x 列空值数/范围/类型分布 + 数值列脏数据计数。

    与 build_select_count_query 同一信任级别：列名经存在性校验后加引号，
    聚合表达式为固定字面量，不经过用户 fields 白名单路径。
    or_and_cases 统一走 build_or_cases_subquery：单 AND 组/多 OR 组/空条件
    三种形态与 select 路径语义一致。
    """
    _bucket_require_column(column_types, x_col)
    x_sql = _quote_sql_identifier(x_col)
    trimmed = f'TRIM({x_sql})'
    unsigned = f"(CASE WHEN SUBSTR({trimmed},1,1)='-' THEN SUBSTR({trimmed},2) ELSE {trimmed} END)"
    normalized = f"LTRIM({unsigned}, '0')"
    digit = (
        f"({unsigned} <> '' AND {unsigned} NOT GLOB '*[^0-9]*'"
        f" AND {trimmed} <> '')"
    )
    overflow = (
        f'(LENGTH({normalized}) > 19 OR (LENGTH({normalized}) = 19 AND '
        f"{normalized} > CASE WHEN SUBSTR({trimmed},1,1)='-' "
        f"THEN '9223372036854775808' ELSE '9223372036854775807' END))"
    )
    select_items = [
        f'COUNT({x_sql})',
        f'MIN({x_sql})',
        f'MAX({x_sql})',
        # 数值行数 / 纯数字文本行数：无约束类型下的 x 模式分类依据
        f"SUM(CASE WHEN typeof({x_sql}) IN ('integer','real')"
        f' THEN 1 ELSE 0 END)',
        f"SUM(CASE WHEN typeof({x_sql})='text' AND {digit} THEN 1 ELSE 0 END)",
        f'MIN(CASE WHEN {digit} THEN CAST({trimmed} AS INTEGER) END)',
        f'MAX(CASE WHEN {digit} THEN CAST({trimmed} AS INTEGER) END)',
        f'SUM(CASE WHEN {digit} AND {overflow} THEN 1 ELSE 0 END)',
    ]
    for col in value_cols:
        _bucket_require_column(column_types, col)
        c_sql = _quote_sql_identifier(col)
        # 数值列非空但非数值的行数：SUM 会把脏值静默强转 0，靠计数透出
        select_items.append(
            f"SUM(CASE WHEN {c_sql} IS NOT NULL"
            f" AND typeof({c_sql}) NOT IN ('integer','real')"
            f' THEN 1 ELSE 0 END)'
        )
    where = build_or_cases_subquery(or_and_cases, column_types)
    sql = f'select {", ".join(select_items)} from {part_table_name}'
    if where.sql:
        sql += f' where {where.sql}'
    return SqlQuery(sql + ';', list(where.params))


def build_bucket_agg_query(part_table_name, x_col, value_cols,
                           or_and_cases, column_types,
                           x_text_mode, x_min, width):
    """分桶聚合查询（PLAN-070）：GROUP BY 桶下标，返回每桶 SUM/COUNT。

    桶下标 = floor((x - x_min) / width)：CAST 向零截断，非负值域上与
    Python floor 一致；text 模式先 CAST 成整数（YYYYMMDD 等纯数字文本）。
    结果行形如 (bucket_idx, sum1, count1, sum2, count2, ...)。
    """
    _bucket_require_column(column_types, x_col)
    x_sql = _quote_sql_identifier(x_col)
    if x_text_mode:
        params = [int(str(x_min).strip()), width]
        bucket_expr = f'(CAST({x_sql} AS INTEGER) - ?) / ?'
    else:
        params = [x_min, width]
        bucket_expr = f'({x_sql} - ?) / ?'
    select_items = [f'CAST({bucket_expr} AS INTEGER)']
    for col in value_cols:
        _bucket_require_column(column_types, col)
        c_sql = _quote_sql_identifier(col)
        select_items.append(f'SUM({c_sql})')
        select_items.append(f'COUNT({c_sql})')
    where = build_or_cases_subquery(or_and_cases, column_types)
    sql = f'select {", ".join(select_items)} from {part_table_name}'
    if where.sql:
        sql += f' where {where.sql} and {x_sql} IS NOT NULL'
    else:
        sql += f' where {x_sql} IS NOT NULL'
    sql += ' group by 1'
    return SqlQuery(sql + ';', params + list(where.params))
