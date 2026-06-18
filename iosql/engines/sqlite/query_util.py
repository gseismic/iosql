from dataclasses import dataclass
from typing import Any, List, Sequence

from ...exception import BadArgument


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


def build_select_fields_sql(fields):
    if fields is None:
        return '*'
    if isinstance(fields, (tuple, list)):
        return ','.join(_select_identifier_field_sql(field) for field in fields)
    if isinstance(fields, str):
        text = fields.strip(' ').strip(',')
        if not text:
            raise BadArgument('select fields must not be empty')
        return text
    raise BadArgument('BadFieldType')


def build_case_subquery(case, column_types):
    """Build a parameterized SQL fragment for one Case."""
    field = case.field
    op = case.op
    value = case.value['value']
    _column_type(column_types, field)
    field_sql = _quote_sql_identifier(field)

    if op == 'between_and':
        assert isinstance(value, (list, tuple))
        return SqlQuery(f'{field_sql} between ? and ?', [value[0], value[1]])

    if op == 'in':
        assert isinstance(value, (list, tuple))
        placeholders = ','.join(['?'] * len(value))
        return SqlQuery(f'{field_sql} in ({placeholders})', list(value))

    if case.value['type'] == 'variable':
        assert isinstance(value, str)
        _column_type(column_types, value)
        rhs = _quote_sql_identifier(value)
        params = []
    else:
        rhs = '?'
        params = [value]

    try:
        op_sql = _OP_SQL[op]
    except KeyError as e:
        raise BadArgument(f'Unsupported op `{op}`') from e
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
        item = build_and_cases_subquery(cases, column_types)
        if item.sql:
            sqls.append(f'({item.sql})')
            params.extend(item.params)
    if not sqls:
        return SqlQuery('', [])
    return SqlQuery(' or '.join(sqls), params)


def build_select_cases_query(part_table_name, fields, cases, column_types):
    field_str = build_select_fields_sql(fields)
    sql = f'select {field_str} from {part_table_name} '
    where = build_and_cases_subquery(cases, column_types)
    if where.sql:
        sql += f' where {where.sql}'
    return SqlQuery(sql + ';', where.params)


def build_select_or_cases_query(part_table_name, fields, or_cases, column_types):
    field_str = build_select_fields_sql(fields)
    sql = f'select {field_str} from {part_table_name} '
    where = build_or_cases_subquery(or_cases, column_types)
    if where.sql:
        sql += f' where {where.sql}'
    return SqlQuery(sql + ';', where.params)


def build_select_cases_sql(part_table_name, fields, cases, column_types):
    query = build_select_cases_query(part_table_name, fields, cases, column_types)
    if query.params:
        raise BadArgument('build_select_cases_sql cannot inline parameterized values')
    return query.sql


def build_delete_query(part_table_name, cases, column_types):
    """Build DELETE SQL from cases."""
    sql = f'DELETE FROM {part_table_name} '
    where = build_and_cases_subquery(cases, column_types)
    if where.sql:
        sql += f' where {where.sql}'
    return SqlQuery(sql + ';', where.params)


def build_delete_sql(part_table_name, cases, column_types):
    query = build_delete_query(part_table_name, cases, column_types)
    if query.params:
        raise BadArgument('build_delete_sql cannot inline parameterized values')
    return query.sql
