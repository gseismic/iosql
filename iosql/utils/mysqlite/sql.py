from collections import OrderedDict
from typing import Dict, List, Tuple, Union, Optional, Any

"""
columns = [
    {'name': 'name', 'dtype': 'text', 'index': False, 'unique': False, 'nullable': True}
]

ChangeLog
    2020-04-20 16:30:41 ignore_if_exists -> default=False
    2020-04-20 16:36:36 删除了不必要的函数

2020-03-28 01:58:46 from sqliteutil_v2.py
"""

def quoted_sql_text(s: str) -> str:
    return f'"{s.replace(chr(34), chr(34) + chr(34))}"'


def _union_fields_of_dictlist(data: List[Dict[str, Any]]) -> OrderedDict[str, Optional[str]]:
    '''扫描全部字段，获得key union 和type
        && 要求获得的type不能为None
    '''
    fields = OrderedDict()
    for item in data:
        for k, v in item.items():
            type_ = suggest_value_type(v)
            if k in fields:
                cur_type = fields[k]
                # 如果已经不为None，则
                if cur_type is None:
                    fields[k] = type_ 
                else:
                    # Allow type coercion for numeric types
                    if type_ == 'float' and cur_type == 'text':
                        # Keep the more specific type (float)
                        pass
                    elif type_ == 'text' and cur_type == 'float':
                        # Keep the more specific type (float)
                        pass
                    elif type_ == 'integer' and cur_type == 'float':
                        # Keep the more specific type (float)
                        pass
                    elif type_ == 'float' and cur_type == 'integer':
                        # Keep the more specific type (float)
                        pass
                    else:
                        assert type_ is None or type_ == cur_type, f'{k, type_, cur_type, v=}, {item=}'
            else:
                # add field
                fields[k] = type_ 

    return fields


def getarg_diclist_insertmany(data: List[Dict[str, Any]], defaults: Dict[str, Any]) -> Tuple[OrderedDict[str, Optional[str]], List[List[Any]]]:
    '''column_type
    list_values
    '''
    # dict 的键顺序可能还不一样
    list_values = []
    fields = _union_fields_of_dictlist(data)
    n_fields = len(fields)
    field_keys = list(fields.keys())
    
    for item in data:
        # 因为是union后的
        # 只有长度不同的才可能有字段missing
        if n_fields != len(item):
            for f in field_keys:
                if f not in item:
                    item[f] = defaults.get(f)
        list_values.append([item[k] for k in field_keys])
    return fields, list_values


def suggest_value_type(v: Any) -> str:
    if isinstance(v, float):
        return 'float'
    elif isinstance(v, (int, bool)):
        return 'integer'
    elif isinstance(v, bytes):
        return 'blob'
    else:
        return 'text'


def suggest_columns(field_types: Dict[str, str], **kwargs) -> List[Dict[str, Any]]:
    return [
        {
            'name': str(k),
            'type': type_,
            'default': None,
            'unique': False,
            'not_null': False,
            'dtype': None,
            'comment': ''
        }
        for k, type_ in field_types.items()
    ]


def suggest_data_columns(dic: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            'name': str(k),
            'type': suggest_value_type(v),
            'default': None,
            'unique': False,
            'not_null': False,
            'dtype': None,
            'comment': ''
        }
        for k, v in dic.items()
    ]


def getsql_query_table(table_name: str, name_only: bool = False) -> str:
    columns = "*" if name_only else "name"
    return f"SELECT {columns} FROM sqlite_master WHERE type='table' AND name='{table_name}';"


def getsql_create_table(table_name: str, columns: List[Dict[str, Any]], pk: Union[List[str], str, None], if_not_exists: bool = True) -> Tuple[str, Dict[str, Any]]:
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
    pk = pk or []
    pk = pk if isinstance(pk, (list, tuple)) else [pk]
    
    create_keyword = 'CREATE TABLE IF NOT EXISTS' if if_not_exists else 'CREATE TABLE'
    sql_parts = [f'{create_keyword} {table_name} (']

    fields = [item['name'] for item in columns]
    if len(fields) != len(set(fields)):
        raise Exception('DuplicatedColumn')

    if pk and not set(pk).issubset(set(fields)):
        extra_pk = set(pk) - set(fields)
        raise Exception(f'PrimaryKeyNotInColumn: {extra_pk}')

    pk_set = set(pk)
    for item in columns:
        column_parts = [f"\t{item['name']} {item['type']}"]
        
        if item.get('not_null', False) or item['name'] in pk_set:
            column_parts.append('NOT NULL')
        if item.get('unique', False):
            column_parts.append('UNIQUE')
        if item.get('default'):
            column_parts.append(f'DEFAULT {item["default"]}')
            
        sql_parts.append(' '.join(column_parts) + ',')

    if pk:
        sql_parts.append(f'\tPRIMARY KEY ({",".join(pk)})')
    else:
        # Remove trailing comma from last column
        sql_parts[-1] = sql_parts[-1].rstrip(',')
        
    sql_parts.append(')')
    sql = '\n'.join(sql_parts)

    meta = {'table_name': table_name, 'columns': columns, 'pk': pk}
    return sql, meta


def getsql_create_index(table_name: str, column: str, order: str = 'ASC', if_not_exists: bool = True) -> str:
    base_sql = 'CREATE INDEX IF NOT EXISTS' if if_not_exists else 'CREATE INDEX'
    order_clause = f' {order}' if order else ''
    return f'{base_sql} {table_name}_{column}_index on {table_name} ({column}{order_clause})'


def getsubsql_insert(ignore: bool, replace: bool) -> str:
    assert not (ignore and replace)
    if replace:
        return 'replace into'
    elif ignore:
        return 'insert or ignore into'
    else:
        return 'insert into'


def getsqlarg_dict_insert(table_name: str, kv: Dict[str, Any], replace: bool = False, ignore: bool = False) -> Tuple[str, None]:
    action = getsubsql_insert(replace=replace, ignore=ignore)
    keys = list(kv.keys())
    values = list(kv.values())
    key_string = ','.join(str(key) for key in keys)
    value_string = ','.join(f'"{v}"' for v in values)
    sql = f'{action} {table_name} ({key_string}) values ({value_string})'
    return sql, None


def getsqlarg_dict_insertmany(table_name: str, fields: List[str], replace: bool = False, ignore: bool = False) -> str:
    action = getsubsql_insert(replace=replace, ignore=ignore)
    field_string = ','.join(str(key) for key in fields)
    placeholders = ','.join(['?'] * len(fields))
    return f'{action} {table_name} ({field_string}) values ({placeholders})'


def getsql_add_column(table_name: str, column_name: str, definition: str) -> str:
    # https://www.techonthenet.com/sqlite/tables/alter_table.php
    # Modify column in table
    # You can not use the ALTER TABLE statement to modify a column in SQLite. 
    # Instead you will need to rename the table, create a new table, and copy the data into the new table.
    return f'ALTER TABLE {table_name} ADD {column_name} {definition};'


def getsql_query_all_index(table_name: str) -> str:
    return (f"SELECT name FROM sqlite_master "
            f"WHERE type == 'index' AND "
            f"tbl_name == '{table_name}'")


def getsql_drop_index(index: str) -> str:
    return f'drop index {index}'


def getsql_drop_table(table_name: str) -> str:
    return f'drop table if exists {table_name}'


def getsql_dict_update(table_name: str, kv: Dict[str, Any], where: str) -> str:
    kv_string = ','.join(
        f'{k}="{v}"' if isinstance(v, str) else f'{k}="{v}"'
        for k, v in kv.items()
    )
    sql = f'update {table_name} set {kv_string}'
    if where:
        sql += f' where {where}'
    return sql
