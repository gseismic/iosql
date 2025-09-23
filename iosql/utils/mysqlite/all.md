# 说明

## 文件列表

文件列表如下:

1. `__init__.py`
2. `const.py`
3. `database.py`
4. `exception.py`
5. `logger.py`
6. `sql.py`
7. `table.py`
8. `version.py`

## 文件内容

各文件内容详见下方:

---

### __init__.py

文件路径: `__init__.py`

```python
from .database import Database
from .table import Table
from .version import __version__
```

### const.py

文件路径: `const.py`

```python
META_TABLE_NAME = '_meta'

_DEFAULT_PRAGMA = {
    "foreign_keys": "on",
    "journal_mode": "wal",
    "cache_size": "-10000" # 10M
}
```

### database.py

文件路径: `database.py`

```python
import time
import sqlite3
import logging
import copy
import threading
import contextlib
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from . import table
from .logger import logger
from .const import _DEFAULT_PRAGMA 


class DatabaseError(Exception):
    """数据库操作错误"""
    pass


class DatabaseConnectionError(DatabaseError):
    """数据库连接错误"""
    pass


class Database(object):
    """
    如果文件不存在，自动创建 

    定位: SQL 完备的工具，而非好用的工具 

    留意应对
    OSError: [Errno 24] Too many open files
    """
    
    def __init__(self, db_file: str, auto_commit: bool = True, verbose: int = 1,
                 pragma_dict: Optional[Dict[str, Any]] = None, wal: bool = True, 
                 timeout: float = 30.0, **kwargs):
        """
        初始化数据库实例
        
        Args:
            db_file: 数据库文件路径
            auto_commit: 自动提交事务
            verbose: 详细级别 (0=静默, 1=信息, 2=调试)
            pragma_dict: 自定义PRAGMA设置
            wal: 启用WAL模式
            timeout: 连接超时时间（秒）
            **kwargs: 其他连接参数
        """
        self.db_file = Path(db_file)
        self.verbose = verbose
        self.auto_commit = auto_commit
        self._kwargs = kwargs
        self._kwargs.setdefault('timeout', timeout)
        self._tables = {}
        self._conn: Optional[sqlite3.Connection] = None
        self._cursor: Optional[sqlite3.Cursor] = None
        self._lock = threading.RLock()  # 使用RLock支持嵌套锁定
        self._pragma_dict = pragma_dict or copy.deepcopy(_DEFAULT_PRAGMA)
        self._is_connected = False
        
        # 如果请求则启用WAL模式
        if wal:
            self.enable_wal()

    def enable_wal(self, enable: bool = True) -> None:
        """启用或禁用WAL模式"""
        if enable:
            self._pragma_dict['journal_mode'] = 'wal'
            if self._is_connected:
                self._ensure_pragma('journal_mode', 'wal')
        else:
            self._pragma_dict['journal_mode'] = 'delete'
            if self._is_connected:
                self._ensure_pragma('journal_mode', 'delete')

    def _pragma_one(self, key: str, value: Optional[str] = None, ensure: bool = False) -> None:
        """设置单个PRAGMA值"""
        value = value or ''
        if ensure:
            self._ensure_pragma(key, value)
        else:
            current_value = self._pragma_dict.get(key.lower(), '')
            if str(value).lower() != current_value.lower():
                self._pragma_dict[key.lower()] = str(value)
                if self._is_connected:
                    self._ensure_pragma(key, value)

    def pragma(self, key_or_dict: Union[str, Dict[str, Any]], value: Optional[str] = None, 
               ensure: bool = False) -> None:
        """设置PRAGMA值"""
        if isinstance(key_or_dict, dict):
            if value is not None:
                raise ValueError("Cannot specify value when key_or_dict is a dictionary")
            for k, v in key_or_dict.items():
                self._pragma_one(k, v, ensure=ensure)
        else:
            self._pragma_one(key_or_dict, value, ensure=ensure)

    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """上下文管理器退出，进行适当的清理"""
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            self.close()

    def __getitem__(self, table_name: str) -> 'table.Table':
        """通过名称获取表，如果不存在则创建"""
        if table_name not in self._tables:
            if self.verbose >= 2:
                logger.debug(f'创建表 {self.db_file}: {table_name}')
            _table = table.Table(self, table_name=table_name)
            self._tables[table_name] = _table
        return self._tables[table_name]

    def connect(self) -> None:
        """建立数据库连接，包含适当的错误处理"""
        with self._lock:
            if self._is_connected:
                if self.verbose >= 2:
                    logger.debug(f'已连接到 {self.db_file}')
                return

            try:
                # 确保目录存在
                db_dirname = self.db_file.parent
                if self.verbose >= 2:
                    logger.debug(f'数据库数据目录: {db_dirname}')
                
                if not db_dirname.exists():
                    if self.verbose >= 1:
                        logger.debug(f'创建数据库目录: {db_dirname} ...')
                    db_dirname.mkdir(parents=True, exist_ok=True)

                # 创建连接
                self._conn = sqlite3.connect(str(self.db_file), **self._kwargs)
                self._conn.row_factory = sqlite3.Row
                self._cursor = self._conn.cursor()
                self._is_connected = True
                
                # 应用PRAGMA设置
                self._ensure_all_pragma()
                
                if self.verbose >= 1:
                    logger.debug(f'已连接到 {self.db_file}')
                    
            except sqlite3.Error as e:
                self._cleanup_connection()
                raise DatabaseConnectionError(f"Failed to connect to {self.db_file}: {e}") from e
            except OSError as e:
                self._cleanup_connection()
                raise DatabaseConnectionError(f"Failed to create database directory: {e}") from e

    def _cleanup_connection(self) -> None:
        """清理连接资源"""
        try:
            if self._cursor:
                self._cursor.close()
        except Exception:
            pass
        finally:
            self._cursor = None
            
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        finally:
            self._conn = None
            
        self._is_connected = False

    def execute(self, sql: str) -> None:
        """执行SQL语句，包含适当的错误处理"""
        if self.verbose >= 2:
            logger.debug(f'{self.db_file}: 执行: {sql}')

        with self._lock:
            if not self._is_connected:
                self.connect()
            
            try:
                self._conn.execute(sql)
            except sqlite3.Error as e:
                logger.error(f"SQL execution failed: {sql}")
                raise DatabaseError(f"SQL execution failed: {e}") from e

    def commit(self) -> None:
        """提交当前事务"""
        with self._lock:
            if not self._is_connected:
                raise DatabaseError("Cannot commit: not connected")
            
            try:
                self._conn.commit()
                if self.verbose >= 2:
                    logger.debug(f'已提交事务 {self.db_file}')
            except sqlite3.Error as e:
                raise DatabaseError(f"Commit failed: {e}") from e

    def rollback(self) -> None:
        """回滚当前事务"""
        with self._lock:
            if not self._is_connected:
                return  # 无需回滚
            
            try:
                self._conn.rollback()
                if self.verbose >= 2:
                    logger.debug(f'已回滚事务 {self.db_file}')
            except sqlite3.Error as e:
                logger.warning(f"Rollback failed: {e}")

    def close(self) -> None:
        """关闭数据库连接"""
        with self._lock:
            self._cleanup_connection()
            if self.verbose >= 1:
                logger.debug(f'已关闭连接 {self.db_file}')

    def is_closed(self) -> bool:
        """检查连接是否已关闭"""
        return not self._is_connected

    def get_conn(self) -> sqlite3.Connection:
        """获取数据库连接，必要时进行连接"""
        with self._lock:
            if not self._is_connected:
                self.connect()
            return self._conn

    def get_cursor(self) -> sqlite3.Cursor:
        """获取数据库游标，必要时进行连接"""
        with self._lock:
            if not self._is_connected:
                self.connect()
            return self._cursor

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        return self.get_conn()

    @property
    def cursor(self) -> sqlite3.Cursor:
        """获取数据库游标"""
        return self.get_cursor()

    def __table_names(self) -> List[str]:
        """获取所有表名（已弃用，请使用get_all_tables）"""
        return self.get_all_tables()

    def get_all_tables(self) -> List[str]:
        """获取数据库中所有表名"""
        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
                table_names = [row[0] for row in cursor.fetchall()]
                return table_names
            except sqlite3.Error as e:
                raise DatabaseError(f"Failed to get table names: {e}") from e

    def get_table_indices(self, table_name: str, exclude_autoindex: bool = True) -> List[str]:
        """获取指定表的所有索引"""
        with self._lock:
            try:
                cursor = self.conn.cursor()
                sql = ("SELECT name FROM sqlite_master " 
                       "WHERE type = 'index' AND tbl_name = ?")
                cursor.execute(sql, (table_name,))
                indices = [row[0] for row in cursor.fetchall()]
                
                if exclude_autoindex:
                    indices = self._exclude_autoindex(indices)
                return indices
            except sqlite3.Error as e:
                raise DatabaseError(f"Failed to get indices for table {table_name}: {e}") from e

    def _exclude_autoindex(self, indices: List[str]) -> List[str]:
        """过滤掉自动生成的索引"""
        return [index for index in indices if 'autoindex' not in index.lower()]

    def get_all_indices(self, exclude_autoindex: bool = True) -> List[str]:
        """获取数据库中所有索引"""
        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('SELECT name FROM sqlite_master WHERE type="index"')
                indices = [row[0] for row in cursor.fetchall()]
                
                if exclude_autoindex:
                    indices = self._exclude_autoindex(indices)
                return indices
            except sqlite3.Error as e:
                raise DatabaseError(f"Failed to get all indices: {e}") from e

    def drop_table(self, table_name: Union[str, List[str], tuple]) -> None:
        """删除一个或多个表"""
        if isinstance(table_name, str):
            table_names = [table_name]
        elif isinstance(table_name, (list, tuple)):
            table_names = list(table_name)
        else:
            raise ValueError('table_name must be str, list, or tuple')

        with self._lock:
            for tbl in table_names:
                try:
                    sql = f'DROP TABLE IF EXISTS {tbl}'
                    if self.verbose >= 1:
                        logger.info(f'删除表: {sql}')
                    self.execute(sql)
                except sqlite3.Error as e:
                    raise DatabaseError(f"Failed to drop table {tbl}: {e}") from e

    def drop_index(self, index_or_indices: Union[str, List[str], tuple]) -> None:
        """删除一个或多个索引"""
        if isinstance(index_or_indices, (list, tuple)):
            indices = list(index_or_indices)
        elif isinstance(index_or_indices, str):
            indices = [index_or_indices]
        else:
            raise ValueError('index_or_indices must be str, list, or tuple')

        with self._lock:
            for index in indices:
                try:
                    sql = f'DROP INDEX IF EXISTS {index}'
                    if self.verbose >= 1:
                        logger.info(f'删除索引: {sql}')
                    self.execute(sql)
                except sqlite3.Error as e:
                    raise DatabaseError(f"Failed to drop index {index}: {e}") from e

    def drop_all_indices(self, exclude_autoindex: bool = True) -> None:
        """删除数据库中所有索引"""
        if self.verbose >= 1:
            logger.info('删除所有索引...')
        
        indices = self.get_all_indices(exclude_autoindex)
        if indices:
            self.drop_index(indices)
            if self.verbose >= 1:
                logger.info(f'已删除 {len(indices)} 个索引')

    def create_index(self, table_name: str, field: str, 
                     option: str = 'ASC', ignore_if_exists: bool = True) -> None:
        """在表字段上创建索引"""
        if ignore_if_exists:
            base_sql = 'CREATE INDEX IF NOT EXISTS'
        else:
            base_sql = 'CREATE INDEX'

        index_name = f'{table_name}_{field}_index'
        sql = f'{base_sql} {index_name} ON {table_name} ({field} {option})'
        
        if self.verbose >= 1:
            logger.info(f'创建索引: {sql}')
        
        try:
            self.execute(sql)
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to create index {index_name}: {e}") from e

    def _ensure_pragma(self, key: str, value: Optional[str] = None) -> None:
        """确保PRAGMA设置生效"""
        if value:
            self.execute(f'PRAGMA {key}={value};')
        else:
            self.execute(f'PRAGMA {key};')

    def _ensure_all_pragma(self) -> None:
        """确保所有PRAGMA设置生效"""
        for key, value in self._pragma_dict.items():
            self._ensure_pragma(key, value)
```

### exception.py

文件路径: `exception.py`

```python
from sqlite3 import OperationalError


class NoSuchTable(OperationalError):
    pass


class NoSuchColumn(OperationalError):
    pass


class DuplicateColumn(OperationalError):
    pass


class TableAlreadyExists(OperationalError):
    pass


class UnKnownOperationalError(OperationalError):
    pass
```

### logger.py

文件路径: `logger.py`

```python
import logging

logger = logging.getLogger("iosql")
```

### sql.py

文件路径: `sql.py`

```python
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
```

### table.py

文件路径: `table.py`

```python
import re
import math
import json
import sqlite3
from collections import OrderedDict
from typing import Dict, List, Optional, Union, Any, Tuple, TYPE_CHECKING

import arrow

from .exception import (
    NoSuchColumn, 
    NoSuchTable,
    TableAlreadyExists,
    DuplicateColumn
)

if TYPE_CHECKING:
    from . import database as my_database
from .sql import (
    getsql_create_table, 
    getsql_create_index, 
    getsubsql_insert,
    getsqlarg_dict_insert,
    getsqlarg_dict_insertmany,
    getsql_add_column,
    suggest_value_type,
    suggest_data_columns,
    getsql_query_table, quoted_sql_text,
    getarg_diclist_insertmany,
    suggest_columns
)
from .const import META_TABLE_NAME
from .logger import logger

# Pre-compiled regex patterns for better performance
_COLUMN_NAME_PATTERN = re.compile(r'no column named (\w+)')
_TABLE_NAME_PATTERN = re.compile(r'no such table: (\w+)')
_TABLE_EXISTS_PATTERN = re.compile(r'table (\w+) already exists')
_DUPLICATE_COLUMN_PATTERN = re.compile(r'duplicate column name: (\w+)')
_INDEX_COLUMN_PATTERN = re.compile(r'\((.*?)\)')

"""
是否关闭cursor 几乎没有影响: 只是关闭后不能用于查询
注意：
    不是长久的解决方案，最次也要用pewee/sqlalchemy: pewee 等不适合高速插入，用于读取可以
意义:
    基于dict，写入文件
    所有想用文件的地方，用sqlite替代

ChangeLog:
    2020-04-16 23:43:51 batch_size: default 5000
"""

class Table:
    """
    设计原则： 只有commit抛出的异常才catch
    定位: SQL 完备的工具，而非好用的工具
    """
    def __init__(self, database: "my_database.Database", table_name: str, db_options: Dict = None):
        '''
            数据安全最重要，auto_commit默认打开
            不要用uri, 需要手动关闭吗？ 这里不推荐使用
        '''
        if db_options is None:
            db_options = {}
        # Type check will be handled by type hints
        self._database = database
        self._table_name = table_name
        self.auto_commit = self.database.auto_commit
        self.verbose = self.database.verbose
        
        # Cache for frequently accessed properties
        self._table_info_cache: Optional[List[Dict]] = None
        self._default_values_cache: Optional[Dict[str, Any]] = None
        self._pk_cache: Optional[List[str]] = None
    
    def _invalidate_cache(self) -> None:
        """Invalidate cached table information when table structure changes"""
        self._table_info_cache = None
        self._default_values_cache = None
        self._pk_cache = None
    
    def _execute_with_fallback(self, sql: str, commit: Optional[bool], data: Dict[str, Any], 
                              ensure_table: bool, ensure_column: bool, **kwargs) -> None:
        """Execute SQL with fallback handling for missing table/column"""
        try:
            self.execute(sql, commit=commit)
        except NoSuchTable as e:
            if ensure_table:
                if self.verbose >= 1:
                    logger.info(f'Table {self._table_name} Not Exist, try-to-create by Data')
                self.create_table_like(data, **kwargs)
                # 创建了table，就不会报错 NoSuchColumn
                self.execute(sql, commit=commit)
            else:
                raise e
        except NoSuchColumn as e:
            if ensure_column:
                self.ensure_columns_like(data)
                self.execute(sql, commit=commit)
            else:
                raise e
        # self._fields = [item['name'] for item in self._table_fields]
        # 使用SqliteDatabase 因为可以自动维护conn
        # 同时考虑: 因为fetchall需要，所以设计一个SqliteTable只有一个
        # 固定的cursor
        # self.cursor = self._database.conn.cursor()
        # self._cursor = self.get_cursor()

    def pragma(self, key: str, value: Optional[str] = None) -> None:
        # https://www.programcreek.com/python/?CodeExample=set+sqlite+pragma
        value = value or ''
        if value:
            self.cursor.execute(f'PRAGMA {key}={value};')
        else:
            self.cursor.execute(f'PRAGMA {key};')

    def table_exists(self) -> bool:
        sql = getsql_query_table(self._table_name, name_only=True)
        self.execute(sql)
        # rv {'type': 'table', 'name': 'not_exist2', 'tbl_name': 'not_exist2', 'rootpage': 11, 'sql': 'CREATE TABLE not_exist2(\n\tid integer,\n\tname5 text,\n\tname6 text)'}
        rv = self.fetchone()
        return bool(rv)

    def create_table_like(self, dic: Dict[str, Any], columns: Optional[List[Dict]] = None, 
                          pk: Optional[Union[str, List[str]]] = None, exist_ok: bool = True, 
                          save_meta: bool = True, table_comment: Optional[str] = None,
                          exist_no_warn: bool = False, **kwargs) -> None:
        if columns is None:
            columns = suggest_data_columns(dic)
            if self.verbose >= 1:
                logger.info(f'Data suggest columns: {columns}')
        return self.create_table(columns, pk=pk, 
                                 exist_ok=exist_ok,
                                 save_meta=save_meta,
                                 table_comment=table_comment,
                                 exist_no_warn=exist_no_warn, **kwargs)

    def create_table(self, columns: List[Dict], pk: Optional[Union[str, List[str]]] = None, 
                     exist_ok: bool = True, save_meta: bool = True, 
                     table_comment: Optional[str] = None, exist_no_warn: bool = False,
                     **kwargs) -> None:
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
        table_created = False
        sql, meta = getsql_create_table(self._table_name, columns, pk, if_not_exists=False)
        try:
            self.execute(sql, commit=True)
            table_created = True
        except DuplicateColumn as e:
            logger.error(f'DuplicateColumn: `{e}`')
            raise e
        except TableAlreadyExists as e:
            if exist_ok:
                if not exist_no_warn:
                    logger.info(f'Table `{self._table_name}` already exist, Not created')
            else:
                raise e

        if not table_created or not save_meta:
            return

        # save meta
        # 有歧义的地方才带上`table_`
        meta_columns = [ 
            {'name': 'table_name', 'type': 'text', 'not_null': True},
            {'name': 'columns', 'type': 'text', 'not_null': True},
            {'name': 'pk', 'type': 'text'},
            {'name': 'table_comment', 'type': 'text'},
            {'name': 'author', 'type': 'text'},
            {'name': 'author_email', 'type': 'text'},
            {'name': 'created_at', 'type': 'text'},
            {'name': 'license', 'type': 'text'}
        ]
        meta_table_name = META_TABLE_NAME
        _sql, _ = getsql_create_table(
            meta_table_name, meta_columns, 
            pk=['table_name'],
            if_not_exists=True
        )
        self.execute(_sql, commit=True)

        __sql, _ = getsqlarg_dict_insert(
            meta_table_name, {
                'table_name': self._table_name,
                'columns': json.dumps(columns).replace('"', '""'),
                'pk': ','.join(pk) if isinstance(pk, (list,tuple)) else pk,
                'table_comment': table_comment,
                'author': kwargs.get('author'),
                'author_email': kwargs.get('author_email'),
                'created_at': arrow.now().format('YYYY-MM-DD HH:mm:ss'),
                'license': kwargs.get('license')
            },
            replace=True
        )
        self.execute(__sql, commit=True)
        logger.debug(f'Table {self._table_name} meta info Created.')

    def add_column(self, column_name: str, definition: str) -> None:
        # logger.info(f'Table {self._table_name}: Added Column `{column_name}: {type_}`')
        sql = getsql_add_column(self._table_name, column_name, definition)
        self.execute(sql, commit=True)
        self._invalidate_cache()

    def ensure_column(self, column_name: str, definition: str) -> bool:
        try:
            self.add_column(column_name, definition)
            return True
        except DuplicateColumn:
            pass
        return False

    def ensure_columns(self, columns: Dict[str, str]) -> None:
        for column_name, type_ in columns.items():
            is_added = self.ensure_column(column_name, definition=type_)
            if is_added and self.verbose >= 1:
                logger.info(f'{self._table_name}: Added column {column_name} {type_}')

    def ensure_columns_like(self, dic: Dict[str, Any]) -> None:
        for column_name, v in dic.items():
            try:
                type_ = suggest_value_type(v)
                self.add_column(column_name, type_)
                if self.verbose >= 1:
                    logger.info(f'{self._table_name}: Added column {column_name} {type_}')
            except DuplicateColumn:
                pass

    def create_index(self, column: str, order: str = 'asc', exist_ok: bool = False) -> None:
        sql = getsql_create_index(self._table_name, column, order=order,
                                  if_not_exists=exist_ok)
        if self.verbose >= 1:
            logger.info(f'Create Index on `{column}` {order}')
        self.execute(sql, commit=True)

    def _handle_error(self, e: Exception) -> None:
        str_e = str(e)
        if 'no column named' in str_e:
            match = _COLUMN_NAME_PATTERN.search(str_e)
            if match:
                raise NoSuchColumn(match.group(1))
        elif 'no such table' in str_e:
            match = _TABLE_NAME_PATTERN.search(str_e)
            if match:
                raise NoSuchTable(match.group(1))
        elif 'already exists' in str_e:
            match = _TABLE_EXISTS_PATTERN.search(str_e)
            if match:
                raise TableAlreadyExists(match.group(1))
        elif 'duplicate column' in str_e:
            match = _DUPLICATE_COLUMN_PATTERN.search(str_e)
            if match:
                raise DuplicateColumn(match.group(1))
        else:
            raise e

    def execute(self, sql: str, commit: Optional[bool] = None) -> None:
        if self.verbose >= 2:
            logger.info(f'SQL: {sql}')
        try:
            self.cursor.execute(sql)
        except sqlite3.OperationalError as e:
            self._handle_error(e)

        self._try_commit(commit)

    def executemany(self, sql: str, list_values: List[Tuple], commit: Optional[bool] = None) -> None:
        '''SQL简单封装
        '''
        if self.verbose >= 2:
            logger.info(f'executemany SQL: {sql}: len={len(list_values)}')

        if not list_values:
            return

        try:
            self.cursor.executemany(sql, list_values)
        except sqlite3.OperationalError as e:
            self._handle_error(e)
        except sqlite3.ProgrammingError as e:
            raise e

        self._try_commit(commit)

    def fetchall(self) -> List[Tuple]:
        # 读取数据可能需要以前的游标，而不是重建
        return self.cursor.fetchall()

    def fetchone(self) -> Optional[Tuple]:
        return self.cursor.fetchone()

    def _update(self, kv: Dict[str, Any], where: Optional[str], commit: Optional[bool] = None) -> None:
        # Use f-strings for better performance
        kv_string = ','.join([f'{k}="{v}"' for k, v in kv.items()])
        sql = f'UPDATE {self._table_name} SET {kv_string}'
        if where:
            sql += f' WHERE {where}'
        self.execute(sql, commit=commit)

    def select_one(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
                   option: Optional[str] = None) -> Optional[Dict]:
        rv = self.select(fields, where, option=option, limit=1, to_dict=True)
        return rv[0] if rv else None

    def select_all(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
                   option: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        return self.select(fields, where, option=option, limit=limit, to_dict=True)

    def query_all(self, sql: str) -> List[Dict]:
        return self.query(sql, to_dict=True)

    def query(self, sql: str, to_dict: bool = False) -> Union[List[Tuple], List[Dict]]:
        #print(self.database.db_file)
        #print(sql)
        self.execute(sql)
        data = self.fetchall()
        if to_dict and data:
            data = [dict(item) for item in data]
        return data

    def select(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
               option: Optional[str] = None, limit: Optional[int] = None, to_dict: bool = False) -> Union[List[Tuple], List[Dict]]:
        if fields is None:
            field_string = '*'
        elif isinstance(fields, (tuple, list)):
            field_string = ','.join(fields)
        elif isinstance(fields, str):
            field_string = fields
        else:
            raise Exception('BadFields')

        # Use f-strings for better performance
        sql = f'SELECT {field_string} FROM {self._table_name}'
        if where:
            sql += f' WHERE {where}'
        if option:
            sql += f' {option}'
        if limit:
            sql += f' LIMIT {limit}'

        return self.query(sql, to_dict)
        #self.execute(sql)
        #data = self.fetchall()
        #if to_dict and data:
        #    data = [dict(item) for item in data]
        #return data

    def insert(self, kv: Dict[str, Any], replace: bool = False, ignore: bool = False, 
               commit: Optional[bool] = None, ensure_table: bool = False, 
               ensure_column: bool = False, **kwargs) -> None:
        if not kv:
            return
        sql, _ = getsqlarg_dict_insert(self._table_name, kv, replace=replace, ignore=ignore)
        self._execute_with_fallback(sql, commit, kv, ensure_table, ensure_column, **kwargs)

    def _insert_one_batch(self, batch_data: List[Dict[str, Any]], 
                          replace: bool = False, ignore: bool = False,
                          commit: bool = True, ensure_table: bool = False, 
                          ensure_column: bool = False, **kwargs) -> None:
        if not batch_data:
            return

        default_values = self.default_values()
        od_fields, list_values = getarg_diclist_insertmany(batch_data,
                                                           default_values)
        fields = list(od_fields.keys())
        sql = getsqlarg_dict_insertmany(self._table_name, fields, replace=replace, ignore=ignore)
        # list_values = [tuple(kv.values()) for kv in batch_data] 
        try:
            self.executemany(sql, list_values, commit=commit)
        except NoSuchTable as e:
            if ensure_table:
                if self.verbose >= 1:
                    logger.info(f'Table {self._table_name} Not Exist, try-to-create by Data')
                # self.create_table_like(batch_data[0], **kwargs)
                # Note 这里忽略了not_null等参数, 用了默认数值
                # 用户能传的只有 columns(包含了not_null等信息), pk
                columns = suggest_columns(od_fields)
                self.create_table(columns, **kwargs)
                self.executemany(sql, list_values, commit=commit)
            else:
                raise e
        except NoSuchColumn as e:
            if ensure_column:
                self.ensure_columns(od_fields)
                # self.ensure_columns_like(batch_data[0])
                self.executemany(sql, list_values, commit=commit)
            else:
                raise e

    def insert_all(self, list_kv: List[Dict[str, Any]], replace: bool = False, 
                   ignore: bool = False, batch_size: int = 5000, commit: bool = True, 
                   ensure_table: bool = False, ensure_column: bool = False, **kwargs) -> None:
        # 要求keys一致: 但并不检验
        if not list_kv:
            return
        # fields = list(list_kv[0].keys())
        # sql = getsqlarg_dict_insertmany(self._table_name, fields, replace=replace, ignore=ignore)
        n = len(list_kv)
        n_batch = math.ceil(n / batch_size)

        commit_size = 5000
        written_size = 0 
        for i in range(n_batch):
            start_idx = i * batch_size
            end_idx = min(start_idx + batch_size, n)
            batch_data = list_kv[start_idx:end_idx]
            
            if self.verbose >= 2:
                logger.info(f'Write batch_size: {i+1}/{n_batch} ..')

            written_size += len(batch_data)
            my_commit = False
            if commit and written_size >= commit_size:
                my_commit = True
                written_size = 0

            self._insert_one_batch(
                batch_data, 
                replace=replace, ignore=ignore,
                commit=my_commit, 
                ensure_table=ensure_table, 
                ensure_column=ensure_column, **kwargs
            )

        self._try_commit(commit)


    def __list_insertmany(self, list_values: List[Tuple], replace: bool = False, ignore: bool = False,
                        batch_size: int = 50000, commit: bool = True) -> None:
        '''要求字段完全匹配

        使用不方便，废弃
        '''
        assert 0
        if not list_values:
            return
        action = getsubsql_insert(replace=replace, ignore=ignore)
        n_fields = len(self._fields)
        sql = f"{action} {self._table_name} VALUES ({','.join(['?'] * n_fields)})"
        self.executemany(sql, list_values, batch_size=batch_size, commit=commit)

    def delete(self, where: str) -> None:
        sql = f'DELETE FROM {self._table_name} WHERE {where}'
        self.execute(sql, commit=True)

    def delete_all(self) -> None:
        # commit 字段无效
        sql = f'DELETE FROM {self._table_name}'
        self.execute(sql, commit=True)

    def get_all_index(self, full_info: bool = False) -> Union[List[str], List[Dict]]:
        # 获取当前已经被创建的index
        sql = f"SELECT * FROM sqlite_master WHERE type == 'index' AND tbl_name == \"{self._table_name}\""
        self.execute(sql)
        data = self.fetchall()
        if full_info:
            return [dict(item) for item in data]
        else:
            return [item['name'] for item in data]
        # return [dict(item) for item in data]

    def drop_all_index(self) -> None:
        # https://stackoverflow.com/questions/2121583/how-to-drop-all-indexes-of-a-sqlite-table
        list_index = self.get_all_index()
        for index in list_index:
            logger.info(f'drop index {index} ...')
            self.drop_index(index)

    def drop_index(self, index_name: str) -> None:
        sql = f'DROP INDEX {index_name}'
        self.execute(sql, commit=True)

    def drop_column_index(self, column: str) -> int:
        # index [{'type': 'index', 'name': 'data_name_index', 'tbl_name': 'data', 'rootpage': 5, 'sql': 'CREATE INDEX data_name_index on data (name asc)'}]
        items = self.get_all_index(full_info=True)
        to_del = set()
        for item in items:
            match = _INDEX_COLUMN_PATTERN.search(item['sql'])
            if match:
                _column = match.group(1).split(' ')[0]
                if _column == column:
                    to_del.add(item['name'])

        for index_name in to_del:
            if self.verbose >= 1:
                logger.info(f'Droping index {index_name}')
            self.drop_index(index_name)

        return len(to_del)

    def drop_table(self) -> None:
        sql = f'DROP TABLE IF EXISTS {self._table_name}'
        self.execute(sql, commit=True)

    def commit(self, rollback_if_err: bool = True) -> None:
        try:
            self.conn.commit()
        except Exception as e:
            logger.error(f'Error {str(e)}')
            if rollback_if_err:
                self.conn.rollback()

    def _try_commit(self, commit: Optional[bool]) -> None:
        if commit is None:
            if self.auto_commit:
                self.commit()
        elif commit:
            self.commit()

    def close(self) -> None:
        if self.cursor:
            self.cursor.close()
            self.cursor = None

    @property
    def cursor(self) -> sqlite3.Cursor:
        # 如果conn 已经关闭，可以自动连接
        return self.database.cursor

    @property
    def conn(self) -> sqlite3.Connection:
        return self.database.conn

    @property
    def database(self) -> "my_database.Database":
        return self._database

    @property
    def table_name(self) -> str:
        return self._table_name

    def _table_info(self) -> List[Dict]:
        # [{'cid': 0, 'name': 'art_code', 'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 1}, 
        if self._table_info_cache is None:
            table = self.table_name
            sql = f"PRAGMA table_info({quoted_sql_text(table)})"
            rows = self.conn.execute(sql)
            self._table_info_cache = [dict(item) for item in rows.fetchall()]
        return self._table_info_cache

    def default_values(self) -> Dict[str, Any]:
        '''
        return default or None
        '''
        if self._default_values_cache is None:
            infos = self._table_info()
            self._default_values_cache = {item['name']: item['dflt_value'] for item in infos}
        return self._default_values_cache
        # [{'cid': 0, 'name': 'art_code', 'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 1}, 

    def pk(self) -> List[str]:
        if self._pk_cache is None:
            infos = self._table_info()
            infos = sorted([info for info in infos if info['pk'] > 0], key=lambda x: x['pk'])
            self._pk_cache = [item['name'] for item in infos]
        return self._pk_cache

    def column_info(self) -> Dict[str, Any]:
        _infos = self._table_info()
        infos = sorted([i for i in _infos], key=lambda i: i['cid'])
        info_dict = {}
        info_dict['column_type'] = OrderedDict([(i['name'], i['type']) for i in infos])
        # info_dict['not_null'] = OrderedDict([(i['name'], bool(i['notnull'])) for i in infos])
        info_dict['not_null'] = [item['name'] for item in infos if item['notnull'] != 0]
        info_dict['default'] = {item['name']: item['dflt_value'] for item in infos if item['dflt_value'] is not None}
        return info_dict
        # print(table_info)
        # table_info['column_type'] = OrderedDict([tuple(item['name'], item['type'] for item in infos]
        # https://stackoverflow.com/questions/54412977/sqlite3-python-get-list-of-primary-and-foreign-keys
        # rows = self.conn.execute("PRAGMA foreign_key_list({})".format(quoted_sql_text(table)))
```

### version.py

文件路径: `version.py`

```python
__version__ = '1.1.0'
```

