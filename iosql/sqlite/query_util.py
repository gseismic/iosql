# from ..query import Where


def build_case_subsql(case, column_types):
    '''Create subsql by case
    '''
    field = case.field
    op = case.op
    value = case.value['value']
    # 以系统的指示为准
    # 用户提供的int类型, 1235, 系统中完全可能是str
    if case.value['type'] == 'const':
        if column_types[field] in [int, float, bool, 'integer', 'float', 'bool']:
            to_quoted_str = False
            value = value
        else:
            to_quoted_str = True
    elif case.value['type'] == 'variable':
        to_quoted_str = False
        assert isinstance(value, str)

    if op in ['between_and', 'in']:
        assert isinstance(value, (list, tuple))
        if to_quoted_str:
            _value = [f'"{str(v)}"' for v in value]
        else:
            _value = value
        if op == 'between_and':
            subsql = (f' between {_value[0]} and {_value[1]}')      
        # elif op == 'range':
            # subsql = (f' {_value[0]} and {_value[1]}')
        elif op == 'in':
            subsql = f' in {_value[0]} and {_value[1]}'
    else:
        if to_quoted_str:
            # [@2021-12-01 17:16:32] FIXED
            # value = '"{str(value)}"'
            value = f'"{str(value)}"'
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

    return f'{field} {subsql} '


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
