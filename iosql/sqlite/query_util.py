from ..exception import BadArgument


def _quote_sql_value(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    value = str(value).replace('"', '""')
    return f'"{value}"'


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


def _case_value_needs_quote(case, column_types):
    if case.value['type'] == 'variable':
        return False

    type_ = _column_type(column_types, case.field)
    return type_ not in [int, float, bool, 'integer', 'float', 'bool']


def _format_case_value(value, to_quoted_str):
    if to_quoted_str:
        return _quote_sql_value(value)
    if isinstance(value, bool):
        return int(value)
    return value


def build_case_subsql(case, column_types):
    '''Create subsql by case
    '''
    field = case.field
    op = case.op
    value = case.value['value']
    field_sql = _quote_sql_identifier(field)
    to_quoted_str = _case_value_needs_quote(case, column_types)

    if op in ['between_and', 'in']:
        assert isinstance(value, (list, tuple))
        _value = [_format_case_value(v, to_quoted_str) for v in value]
        if op == 'between_and':
            subsql = f' between {_value[0]} and {_value[1]}'
        elif op == 'in':
            value_sql = ','.join(str(v) for v in _value)
            subsql = f' in ({value_sql})'
    else:
        if case.value['type'] == 'variable':
            assert isinstance(value, str)
            value = _quote_sql_identifier(value)
        else:
            value = _format_case_value(value, to_quoted_str)
        if op in ['gt', '>']:
            subsql = f' > {value}'
        elif op in ['ge', '>=']:
            subsql = f' >= {value}'
        elif op in ['lt', '<']:
            subsql = f' < {value}'
        elif op in ['le', '<=']:
            subsql = f' <= {value}'
        elif op in ['eq', '==', '=']:
            subsql = f' == {value}'
        elif op in ['neq', '!=']:
            subsql = f' != {value}'
        else:
            raise BadArgument(f'Unsupported op `{op}`')

    return f'{field_sql} {subsql} '


def build_and_cases_subsql(cases, column_types):
    # case [] OK
    # [@2021-11-27 22:19:08] cc
    if cases:
        sqls = [build_case_subsql(case, column_types) for case in cases]
        return ' where ' + ' and '.join(sqls)
    else:
        return ''


def build_select_cases_sql(part_table_name, fields, cases, column_types):
    # 允许 cases 为None []
    # [@2021-11-27 22:18:58] cc
    if isinstance(fields, (tuple, list)):
        field_str = ','.join(fields)
    elif isinstance(fields, str):
        field_str = fields.strip(' ').strip(',')
    else:
        raise Exception('BadFieldType')
    sql = f'select {field_str} from {part_table_name} '
    subsql = build_and_cases_subsql(cases, column_types)
    return sql + subsql + ';'


def build_delete_sql(part_table_name, cases, column_types):
    """Build DELETE SQL from cases."""
    sql = f'DELETE FROM {part_table_name} '
    subsql = build_and_cases_subsql(cases, column_types)
    return sql + subsql + ';'
