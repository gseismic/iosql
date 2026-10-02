import numbers
from collections import OrderedDict
from operator import itemgetter
from typing import Dict, List, Tuple, Union, Optional, Any

from .exception import DuplicateColumn, PrimaryKeyNotInColumn

try:
    import numpy as np
    _has_np = True
except ImportError:  # pragma: no cover - numpy 可选依赖
    _has_np = False

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


def sql_literal(value: Any) -> str:
    if value is None:
        return 'NULL'
    # bytes 必须走 blob 字面量，直接 str(bytes) 会得到 b'...' 字面量
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _quote_columns(columns: List[str]) -> List[str]:
    return [quoted_sql_text(str(column)) for column in columns]


def _merge_sqlite_type(current: Optional[str], incoming: Optional[str]) -> Optional[str]:
    if current is None:
        return incoming
    if incoming is None or incoming == current:
        return current
    numeric = {'integer', 'float'}
    if current in numeric and incoming in numeric:
        return 'float'
    return 'text'


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
                fields[k] = _merge_sqlite_type(cur_type, type_)
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
        row = item.copy()
        # 因为是union后的
        # 只有长度不同的才可能有字段missing
        if n_fields != len(row):
            for f in field_keys:
                if f not in row:
                    row[f] = defaults.get(f)
        list_values.append([row.get(k) for k in field_keys])
    return fields, list_values


def getarg_diclist_insertmany_known_fields(
    data: List[Dict[str, Any]],
    defaults: Dict[str, Any],
) -> Optional[Tuple[OrderedDict[str, Optional[str]], List[List[Any]]]]:
    """固定表结构的批量参数快速路径。

    如果批次出现未知字段则返回 None，让调用方回退到字段 union 和 schema evolve 路径。
    """
    if not defaults:
        return None

    fields = OrderedDict((field, None) for field in defaults)
    default_keys = defaults.keys()
    if all(
        len(item) == len(defaults) and item.keys() == default_keys
        for item in data
    ):
        if len(fields) == 1:
            field = next(iter(fields))
            return fields, [(item[field],) for item in data]
        getter = itemgetter(*fields)
        return fields, list(map(getter, data))

    for item in data:
        if any(field not in defaults for field in item):
            return None

    field_names = list(fields)
    list_values = [
        [
            item[field] if field in item else defaults[field]
            for field in field_names
        ]
        for item in data
    ]
    return fields, list_values


def group_rows_by_keys(data: List[Dict[str, Any]]) -> List[Tuple[List[str], List[List[Any]]]]:
    """按连续相同的 key 集合分组，返回 [(字段列表, 参数列表), ...]。

    行中缺失的列不再由 Python 侧回填 DEFAULT，而是直接从语句中省略，
    交由 SQLite 求值列默认值（含 CURRENT_TIMESTAMP / (datetime('now')) 等表达式）。
    这样既保持默认值语义，也保留异构行的输入顺序，避免 REPLACE 顺序变化。
    """
    groups: List[Tuple[List[str], List[List[Any]]]] = []
    previous_key = None
    for item in data:
        key = frozenset(item.keys())
        if not groups or key != previous_key:
            groups.append((list(item.keys()), []))
        fields, values = groups[-1]
        values.append([item[field] for field in fields])
        previous_key = key
    return groups


def suggest_value_type(v: Any) -> str:
    if v is None:
        return None
    # numpy 标量统一转换为其 Python 原生类型再判定
    if _has_np and isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, bool):
        return 'integer'
    if isinstance(v, numbers.Integral):
        return 'integer'
    if isinstance(v, numbers.Real):
        return 'float'
    if isinstance(v, (bytes, bytearray, memoryview)):
        return 'blob'
    return 'text'


def suggest_columns(field_types: Dict[str, str], **kwargs) -> List[Dict[str, Any]]:
    return [
        {
            'name': str(k),
            'type': type_ or 'text',
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
    return (
        "SELECT "
        f"{columns} FROM sqlite_master WHERE type='table' "
        f"AND name={sql_literal(table_name)};"
    )


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
    if not columns:
        raise ValueError('getsql_create_table: columns 不能为空')
    pk = pk or []
    pk = pk if isinstance(pk, (list, tuple)) else [pk]
    
    create_keyword = 'CREATE TABLE IF NOT EXISTS' if if_not_exists else 'CREATE TABLE'
    sql_parts = [f'{create_keyword} {quoted_sql_text(table_name)} (']

    fields = [item['name'] for item in columns]
    duplicates = sorted({f for f in fields if fields.count(f) > 1})
    if duplicates:
        raise DuplicateColumn(f'columns 定义存在重复列: {duplicates}')

    if pk and not set(pk).issubset(set(fields)):
        extra_pk = set(pk) - set(fields)
        raise PrimaryKeyNotInColumn(f'pk 不在 columns 中: {sorted(extra_pk)}')

    pk_set = set(pk)
    for item in columns:
        column_parts = [f"\t{quoted_sql_text(item['name'])} {item['type'] or 'text'}"]
        
        if item.get('not_null', False) or item['name'] in pk_set:
            column_parts.append('NOT NULL')
        if item.get('unique', False):
            column_parts.append('UNIQUE')
        if item.get('default') is not None:
            column_parts.append(f'DEFAULT {sql_literal(item["default"])}')
            
        sql_parts.append(' '.join(column_parts) + ',')

    if pk:
        sql_parts.append(f'\tPRIMARY KEY ({",".join(_quote_columns(pk))})')
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
    index_name = f'{table_name}_{column}_index'
    return (
        f'{base_sql} {quoted_sql_text(index_name)} '
        f'on {quoted_sql_text(table_name)} ({quoted_sql_text(column)}{order_clause})'
    )


def getsubsql_insert(ignore: bool, replace: bool) -> str:
    if ignore and replace:
        raise ValueError('ignore 与 replace 不能同时为 True')
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
    key_string = ','.join(_quote_columns(keys))
    placeholders = ','.join(['?'] * len(values))
    sql = f'{action} {quoted_sql_text(table_name)} ({key_string}) values ({placeholders})'
    return sql, values


def getsqlarg_dict_insertmany(table_name: str, fields: List[str], replace: bool = False, ignore: bool = False) -> str:
    action = getsubsql_insert(replace=replace, ignore=ignore)
    field_string = ','.join(_quote_columns(fields))
    placeholders = ','.join(['?'] * len(fields))
    return f'{action} {quoted_sql_text(table_name)} ({field_string}) values ({placeholders})'


def getsql_add_column(table_name: str, column_name: str, definition: str) -> str:
    # https://www.techonthenet.com/sqlite/tables/alter_table.php
    # Modify column in table
    # You can not use the ALTER TABLE statement to modify a column in SQLite. 
    # Instead you will need to rename the table, create a new table, and copy the data into the new table.
    return (
        f'ALTER TABLE {quoted_sql_text(table_name)} '
        f'ADD {quoted_sql_text(column_name)} {definition};'
    )
