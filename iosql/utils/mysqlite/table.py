import re
import math
import json
import sqlite3
from collections import OrderedDict
from typing import Dict, List, Optional, Union, Any, Tuple, TYPE_CHECKING
from contextlib import contextmanager

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
    
    @contextmanager
    def _get_cursor(self):
        """获取游标的上下文管理器
        
        Returns:
            tuple: (cursor, connection) 游标和连接对象
        """
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            try:
                yield cursor, conn
            finally:
                cursor.close()
    
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

    def pragma(self, key: str, value: Optional[str] = None) -> None:
        # https://www.programcreek.com/python/?CodeExample=set+sqlite+pragma
        value = value or ''
        with self._get_cursor() as (cursor, conn):
            if value:
                cursor.execute(f'PRAGMA {key}={value};')
            else:
                cursor.execute(f'PRAGMA {key};')

    def table_exists(self) -> bool:
        sql = getsql_query_table(self._table_name, name_only=True)
        # 使用新的fetchone方法，它会自动处理execute和fetch
        rv = self.fetchone(sql)
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
        sql = getsql_add_column(self._table_name, column_name, definition)
        self.execute(sql, commit=True)
        self._invalidate_cache()

    def ensure_column(self, column_name: str, column_type: str, 
                      default: Optional[Any] = None) -> bool:
        definition = column_type
        if default is not None:
            if isinstance(default, str):
                definition += f" DEFAULT '{default}'"
            else:
                definition += f" DEFAULT {default}"
        try:
            self.add_column(column_name, definition)
            return True
        except DuplicateColumn:
            pass
        return False

    def ensure_columns(self, columns: Dict[str, str]) -> None:
        for column_name, type_ in columns.items():
            is_added = self.ensure_column(column_name, column_type=type_)
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

    def _handle_error_on_connection(self, e: Exception, conn: sqlite3.Connection) -> None:
        """处理特定连接上的异常，包含回滚逻辑"""
        conn.rollback()
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

    def _execute_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, 
                              sql: str, commit: Optional[bool] = None) -> None:
        """在指定连接上执行SQL语句"""
        if self.verbose >= 2:
            logger.info(f'SQL: {sql}')
        
        try:
            cursor.execute(sql)
            self._try_commit_on_connection(conn, commit)
        except sqlite3.OperationalError as e:
            self._handle_error_on_connection(e, conn)

    def _executemany_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection,
                                  sql: str, list_values: List[Tuple], 
                                  commit: Optional[bool] = None) -> None:
        """在指定连接上执行批量SQL语句"""
        if self.verbose >= 2:
            logger.info(f'executemany SQL: {sql}: len={len(list_values)}')

        if not list_values:
            return

        try:
            cursor.executemany(sql, list_values)
            self._try_commit_on_connection(conn, commit)
        except (sqlite3.OperationalError, sqlite3.ProgrammingError) as e:
            self._handle_error_on_connection(e, conn)

    def execute(self, sql: str, commit: Optional[bool] = None) -> None:
        if self.verbose >= 2:
            logger.info(f'SQL: {sql}')
        
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql)
                self._try_commit_on_connection(conn, commit)
            except sqlite3.OperationalError as e:
                self._handle_error_on_connection(e, conn)

    def executemany(self, sql: str, list_values: List[Tuple], commit: Optional[bool] = None) -> None:
        '''SQL简单封装
        '''
        if self.verbose >= 2:
            logger.info(f'executemany SQL: {sql}: len={len(list_values)}')

        if not list_values:
            return

        with self._get_cursor() as (cursor, conn):
            try:
                cursor.executemany(sql, list_values)
                self._try_commit_on_connection(conn, commit)
            except (sqlite3.OperationalError, sqlite3.ProgrammingError) as e:
                self._handle_error_on_connection(e, conn)

    def fetchall(self, sql: str) -> List[Tuple]:
        """执行查询并返回所有结果"""
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql)
                return cursor.fetchall()
            except sqlite3.OperationalError as e:
                self._handle_error_on_connection(e, conn)

    def fetchone(self, sql: str) -> Optional[Tuple]:
        """执行查询并返回单个结果"""
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql)
                return cursor.fetchone()
            except sqlite3.OperationalError as e:
                self._handle_error_on_connection(e, conn)
    
    def _try_commit_on_connection(self, conn: sqlite3.Connection, commit: Optional[bool]) -> None:
        """在指定连接上尝试提交
        
        这个方法替代了原来的_try_commit方法，直接在给定的连接上操作，
        避免了对self.conn的依赖，更适合连接池模式。
        
        Args:
            conn: 数据库连接对象
            commit: 是否提交事务，None表示根据auto_commit决定
        """
        if commit is None:
            if self.auto_commit:
                conn.commit()
        elif commit:
            conn.commit()

    def _update(self, kv: Dict[str, Any], where: Optional[str], commit: Optional[bool] = None) -> None:
        # Use f-strings for better performance
        kv_string = ','.join([f'{k}="{v}"' for k, v in kv.items()])
        sql = f'UPDATE {self._table_name} SET {kv_string}'
        if where:
            sql += f' WHERE {where}'
        self.execute(sql, commit=commit)

    def select_one(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
                   order_by: Optional[str] = None, option: Optional[str] = None) -> Optional[Dict]:
        rv = self.select(fields, where, order_by=order_by, option=option, limit=1, to_dict=True)
        return rv[0] if rv else None

    def select_all(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
                   order_by: Optional[str] = None, option: Optional[str] = None, 
                   limit: Optional[int] = None) -> List[Dict]:
        return self.select(fields, where, order_by=order_by, option=option, limit=limit, to_dict=True)

    def query_all(self, sql: str) -> List[Dict]:
        return self.query(sql, to_dict=True)

    def query(self, sql: str, to_dict: bool = False) -> Union[List[Tuple], List[Dict]]:
        #print(self.database.db_file)
        #print(sql)
        data = self.fetchall(sql)
        if to_dict and data:
            data = [dict(item) for item in data]
        return data

    def select(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
               order_by: Optional[str] = None, option: Optional[str] = None, 
               limit: Optional[int] = None, to_dict: bool = False) -> Union[List[Tuple], List[Dict]]:
        if fields is None:
            field_string = '*'
        elif isinstance(fields, (tuple, list)):
            field_string = ','.join(fields)
        elif isinstance(fields, str):
            field_string = fields
        else:
            raise Exception('BadFields')

        sql = f'SELECT {field_string} FROM {self._table_name}'
        if where:
            sql += f' WHERE {where}'
        if order_by:
            sql += f' ORDER BY {order_by}'
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

    def _insert_one_batch_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection,
                                       batch_data: List[Dict[str, Any]], 
                                       replace: bool = False, ignore: bool = False,
                                       ensure_table: bool = False, 
                                       ensure_column: bool = False, **kwargs) -> None:
        """在指定连接上执行单批次插入"""
        if not batch_data:
            return

        default_values = self.default_values()
        od_fields, list_values = getarg_diclist_insertmany(batch_data, default_values)
        fields = list(od_fields.keys())
        sql = getsqlarg_dict_insertmany(self._table_name, fields, replace=replace, ignore=ignore)
        
        try:
            self._executemany_on_connection(cursor, conn, sql, list_values)
        except NoSuchTable as e:
            if ensure_table:
                # Note 这里忽略了not_null等参数, 用了默认数值
                # 用户能传的只有 columns(包含了not_null等信息), pk
                columns = suggest_columns(od_fields)
                self.create_table(columns, **kwargs)
                self._executemany_on_connection(cursor, conn, sql, list_values)
            else:
                raise e
        except NoSuchColumn as e:
            if ensure_column:
                self.ensure_columns(od_fields)
                # self.ensure_columns_like(batch_data[0])
                self._executemany_on_connection(cursor, conn, sql, list_values)
            else:
                 raise e


    def _create_table_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection,
                                   columns: List[Dict], pk: Optional[Union[str, List[str]]] = None, 
                                   exist_ok: bool = True, save_meta: bool = True, 
                                   table_comment: Optional[str] = None, exist_no_warn: bool = False,
                                   **kwargs) -> None:
        """在指定连接上创建表"""
        table_created = False
        sql, meta = getsql_create_table(self._table_name, columns, pk, if_not_exists=False)
        try:
            cursor.execute(sql)
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

        # save meta - 在同一个连接上创建元数据表和插入元数据
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
        cursor.execute(_sql)

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
        cursor.execute(__sql)
        logger.debug(f'Table {self._table_name} meta info Created.')

    def _ensure_columns_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, 
                                     od_fields: Dict[str, Any]) -> None:
        """在指定连接上确保列存在"""
        for column_name, value in od_fields.items():
            try:
                type_ = suggest_value_type(value)
                sql = getsql_add_column(self._table_name, column_name, type_)
                cursor.execute(sql)
                self._invalidate_cache()
                if self.verbose >= 1:
                    logger.info(f'{self._table_name}: Added column {column_name} {type_}')
            except DuplicateColumn:
                pass

    def insert_all(self, list_kv: List[Dict[str, Any]], replace: bool = False, 
                   ignore: bool = False, batch_size: int = 5000, commit: bool = True, 
                   ensure_table: bool = False, ensure_column: bool = False, **kwargs) -> None:
        """批量插入数据，使用单个连接进行整个批量操作以提高性能"""
        # 要求keys一致: 但并不检验
        if not list_kv:
            return
        
        n = len(list_kv)
        n_batch = math.ceil(n / batch_size)
        
        # 使用单个连接进行整个批量操作
        with self._get_cursor() as (cursor, conn):
            try:
                # 开始事务
                conn.execute('BEGIN IMMEDIATE;')
                
                for i in range(n_batch):
                    start_idx = i * batch_size
                    end_idx = min(start_idx + batch_size, n)
                    batch_data = list_kv[start_idx:end_idx]
                    
                    if self.verbose >= 2:
                        logger.info(f'Write batch_size: {i+1}/{n_batch} ..')
                    
                    # 在同一个连接上执行批量操作
                    self._insert_one_batch_on_connection(
                        cursor, conn, batch_data,
                        replace=replace, ignore=ignore,
                        ensure_table=ensure_table,
                        ensure_column=ensure_column, **kwargs
                    )
                
                # 提交整个事务
                if commit:
                    conn.commit()
                    
            except Exception as e:
                conn.rollback()
                raise e


    def _list_insertmany(self, list_values: List[Tuple], replace: bool = False, ignore: bool = False,
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

    def update(self, kv: Dict[str, Any], where: str) -> None:
        kv_string = ','.join([f'{k}=?' for k in kv.keys()])
        values = list(kv.values())
        sql = f'UPDATE {self._table_name} SET {kv_string} WHERE {where}'
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql, values)
                self._try_commit_on_connection(conn, True)
            except sqlite3.OperationalError as e:
                self._handle_error_on_connection(e, conn)

    def count(self, where: Optional[str] = None) -> int:
        sql = f'SELECT COUNT(*) FROM {self._table_name}'
        if where:
            sql += f' WHERE {where}'
        result = self.fetchone(sql)
        return result[0] if result else 0

    def delete_all(self) -> None:
        # commit 字段无效
        sql = f'DELETE FROM {self._table_name}'
        self.execute(sql, commit=True)

    def get_all_index(self, full_info: bool = False) -> Union[List[str], List[Dict]]:
        sql = f"SELECT * FROM sqlite_master WHERE type == 'index' AND tbl_name == \"{self._table_name}\""
        data = self.fetchall(sql)
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
        # 在连接池模式下，commit应该在具体的连接上下文中进行
        raise NotImplementedError("在连接池模式下，请在具体的连接上下文中调用conn.commit()")

    def _try_commit(self, commit: Optional[bool], conn: sqlite3.Connection = None) -> None:
        if commit is None:
            if self.auto_commit and conn:
                try:
                    conn.commit()
                except Exception as e:
                    logger.error(f'Error {str(e)}')
                    conn.rollback()
        elif commit and conn:
            try:
                conn.commit()
            except Exception as e:
                logger.error(f'Error {str(e)}')
                conn.rollback()

    def close(self) -> None:
        # 在连接池模式下，不需要手动关闭cursor
        # 连接池会自动管理连接的生命周期
        pass

    @property
    def cursor(self) -> sqlite3.Cursor:
        # 连接池模式下，每次需要时创建新的cursor
        # 注意：这个cursor需要在使用后手动关闭
        raise NotImplementedError("在连接池模式下，请使用 database.get_connection() 上下文管理器获取连接和cursor")

    @property
    def conn(self) -> sqlite3.Connection:
        # 连接池模式下，不再提供直接的连接访问
        raise NotImplementedError("在连接池模式下，请使用 database.get_connection() 上下文管理器获取连接")

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
            with self._get_cursor() as (cursor, conn):
                cursor.execute(sql)
                self._table_info_cache = [dict(item) for item in cursor.fetchall()]
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
        # 在连接池模式下，需要使用 with self._get_cursor() as (cursor, conn): 来获取连接
        # rows = cursor.execute("PRAGMA foreign_key_list({})".format(quoted_sql_text(table)))
