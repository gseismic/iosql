from collections import OrderedDict
"""
columns = [
    {'name': 'name', 'dtype': 'text', 'index': False, 'unique': False, 'nullable': True}
]

ChangeLog
    2020-04-20 16:30:41 ignore_if_exists -> default=False
    2020-04-20 16:36:36 删除了不必要的函数

2020-03-28 01:58:46 from sqliteutil_v2.py
"""

def quoted_sql_text(s):
    return '"' + s.replace('"', '""') + '"'


def _union_fields_of_dictlist(data):
    '''扫描全部字段，获得key union 和type
    '''
    fields = OrderedDict()
    _set_fields = set()
    for item in data:
        new = [k for k in item if k not in _set_fields]
        if new:
            _set_fields |= set(new)
            for f in new:
                fields[f] = suggest_value_type(item[f])
            # fields.extend(new)
    return fields


def getarg_diclist_insertmany(data, defaults):
    '''column_type
    list_values
    '''
    # dict 的键顺序可能还不一样
    list_values = []
    fields = _union_fields_of_dictlist(data)
    n_fields = len(fields)
    for item in data:
        # 因为是union后的
        # 只有长度不同的才可能有字段missing
        if n_fields != len(item):
            for f in fields:
                if f not in item:
                    item[f] = defaults.get(f)
        list_values.append([item[k] for k in fields])
    return fields, list_values


def suggest_value_type(v):
    type_ = None
    if isinstance(v, float):
        type_ = 'float'
    elif isinstance(v, (int, bool)):
        type_ = 'integer'
    elif isinstance(v, (bytes)):
        type_ = 'blob'
    else:
        type_ = 'text'
    return type_


def suggest_columns(field_types, **kwargs):
    columns = []
    for k, type_ in field_types.items():
        # type_ = suggest_value_type(v)
        columns.append({
            'name': str(k),
            'type': type_,
            'default': None,
            'unique': False,
            'not_null': False,
            'dtype': None,
            'comment': ''
        })
    return columns


def suggest_data_columns(dic):
    columns = []
    for k, v in dic.items():
        type_ = suggest_value_type(v)
        columns.append({
            'name': str(k),
            'type': type_,
            'default': None,
            'unique': False,
            'not_null': False,
            'dtype': None,
            'comment': ''
        })
    return columns


def getsql_query_table(table_name, name_only=False):
    if name_only:
        sql = f"SELECT * FROM sqlite_master WHERE type='table' AND name='{table_name}';"
    else:
        sql = f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}';"
    return sql


def getsql_create_table(table_name, columns, pk, if_not_exists=True):
    '''
    [{
        'name': 'column1',
        'type': 'text'
        'default': default,
        'unique': False
        'not_null': not_null,
        'dtype': 'datetime'
        'comment': '姓名'
    }]
    '''
    # [@2021-11-27 03:23:54] pass
    pk = pk or []
    pk = pk if isinstance(pk, (list, tuple)) else [pk]
    if if_not_exists:
        sql = 'CREATE TABLE IF NOT EXISTS %s \n(\n' % table_name
    else:
        sql = 'CREATE TABLE %s(\n' % table_name

    fields = [item['name'] for item in columns]
    if len(fields) != len(set(fields)):
        raise Exception('DuplicatedColumn')

    if pk and not set(pk).issubset(set(fields)):
        extra_pk = set(pk) - set(fields)
        raise Exception(f'PrimaryKeyNotInColumn: {extra_pk}')

    # pk = []
    for item in columns:
        default = item.get('default')
        unique = item.get('unique', False)
        not_null = item.get('not_null', False)
        sql += '\t%s %s' % (item['name'], item['type'])
        if not_null or item['name'] in pk:
            sql += ' NOT NULL'
        if unique:
            sql += ' UNIQUE'
        if default:
            sql += ' DEFAULT ' + str(default)
        sql += ',\n'
        #primary = item.get('primary', False)
        #if primary:
        #    pk.append(item['name'])

    if pk:
        sql += '\tPRIMARY KEY (' + ','.join(pk) + ')\n'
    else:
        sql = sql.rstrip(',\n')
    sql += ')'

    meta = {'table_name': table_name, 'columns': columns, 'pk': pk}
    return sql, meta


def getsql_create_index(table_name, column, order='ASC', if_not_exists=True):
    if if_not_exists:
        base_sql = 'CREATE INDEX IF NOT EXISTS'
    else:
        base_sql = 'CREATE INDEX'

    sql = '%s %s_%s_index on %s (%s ' % (base_sql, table_name, column, table_name, column)
    if order:
        sql += order
    sql += ')'

    return sql


def getsubsql_insert(ignore, replace):
    assert not (ignore and replace)
    if replace:
        insert = 'replace into'
    elif ignore:
        insert = 'insert or ignore into'
    else:
        insert = 'insert into'
    return insert


def getsqlarg_dict_insert(table_name, kv, replace=False, ignore=False):
    action = getsubsql_insert(replace=replace, ignore=ignore)
    keys = list(kv.keys())
    values = list(kv.values())
    key_string = ','.join([str(key)for key in keys])
    value_string = ','.join(['"%s"' % str(v) for v in values])
    sql = '%s %s (%s) values (%s)' % (action, table_name, 
                                      key_string, value_string)
    return sql, None


def getsqlarg_dict_insertmany(table_name, fields, replace=False, ignore=False):
    # 要求keys一致: 但并不检验
    action = getsubsql_insert(replace=replace, ignore=ignore)
    # fields = list(list_kv[0].keys()) # 仅用第一个
    field_string = ','.join([str(key) for key in fields])
    n_fields = len(fields)
    sql = '%s %s (%s) values (%s)' % (action, table_name, 
                                      field_string,
                                      ','.join(['?'] * n_fields))
    return sql


def getsql_add_column(table_name, column_name, definition):
    # https://www.techonthenet.com/sqlite/tables/alter_table.php
    # Modify column in table
    # You can not use the ALTER TABLE statement to modify a column in SQLite. 
    # Instead you will need to rename the table, create a new table, and copy the data into the new table.
    sql = f'ALTER TABLE {table_name} ADD {column_name} {definition};'
    return sql


################################################################################################
# NOT new test


def getsql_query_all_index(table_name):
    # 获取当前已经被创建的index
    sql = ("SELECT name FROM sqlite_master " 
           "WHERE type == 'index' AND "
           f"tbl_name == '{table_name}'"
          )
    # return [it[0] for it in data]
    return sql


def getsql_drop_index(index):
    return 'drop index %s' % index


def getsql_drop_table(table_name):
    return f'drop table if exists {table_name}'


def getsql_dict_update(table_name, kv, where):
    sql = 'update %s' % table_name 
    # fixed:  and --> ','
    kv_string = ','.join([
        ('%s="%s"' % (str(k), str(v))) if isinstance(v, str)
        else ('%s="%s"' % (str(k), v)) 
        for k, v in kv.items()
    ])
    sql += ' set ' + kv_string
    if where:
        sql += ' where ' + where
    # self.execute(sql, commit=commit)
