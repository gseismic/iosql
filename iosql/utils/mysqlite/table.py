import re
import math
import json
import sqlite3
import numbers
from collections import OrderedDict
from typing import Dict, List, Optional, Union, Any, Tuple, TYPE_CHECKING
from contextlib import contextmanager

import arrow

from .exception import (
    BadArgument,
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
    getsqlarg_dict_insert,
    getsqlarg_dict_insertmany,
    getsql_add_column,
    suggest_value_type,
    suggest_data_columns,
    getsql_query_table, quoted_sql_text, sql_literal,
    group_rows_by_keys,
    _union_fields_of_dictlist,
    suggest_columns
)
from .const import META_TABLE_NAME
from .logger import logger

# Pre-compiled regex patterns for better performance
_COLUMN_NAME_PATTERN = re.compile(r'no column named (\w+)')
_TABLE_NAME_PATTERN = re.compile(r'no such table: (\w+)')
_TABLE_EXISTS_PATTERN = re.compile(r'table (\w+) already exists')
_DUPLICATE_COLUMN_PATTERN = re.compile(r'duplicate column name: (\w+)')

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

    def invalidate_cache(self) -> None:
        """清除本对象缓存的表结构（列类型 / 默认值 / 主键）。

        本模块的 DDL 封装（create_table/add_column/drop_table 等）会自动失效缓存；
        如果通过 ``db.execute()`` 或外部连接直接执行了 DDL，
        请在任何 ``column_info()/default_values()/pk()`` 之前手动调用本方法。
        """
        self._invalidate_cache()
    
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
                              ensure_table: bool, ensure_column: bool,
                              params: Optional[List[Any]] = None, **kwargs) -> None:
        """Execute SQL with fallback handling for missing table/column"""
        try:
            self.execute_params(sql, params, commit=commit)
        except NoSuchTable as e:
            if ensure_table:
                if self.verbose >= 1:
                    logger.info(f'Table {self._table_name} Not Exist, try-to-create by Data')
                self.create_table_like(data, **kwargs)
                # 创建了table，就不会报错 NoSuchColumn
                self.execute_params(sql, params, commit=commit)
            else:
                raise e
        except NoSuchColumn as e:
            if ensure_column:
                self.ensure_columns_like(data)
                self.execute_params(sql, params, commit=commit)
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
        """创建表并写入 meta 信息。

        建表与 meta 写入在同一事务中完成：meta 写入失败会整体回滚，
        不会留下「表已建但没有 meta」的半成品。
        """
        with self.database.transaction():
            self._create_table_with_meta(
                columns, pk=pk, exist_ok=exist_ok, save_meta=save_meta,
                table_comment=table_comment, exist_no_warn=exist_no_warn,
                **kwargs
            )

    def _create_table_with_meta(self, columns: List[Dict], pk: Optional[Union[str, List[str]]] = None, 
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

        __sql, __values = getsqlarg_dict_insert(
            meta_table_name, {
                'table_name': self._table_name,
                'columns': json.dumps(columns),
                'pk': ','.join(pk) if isinstance(pk, (list,tuple)) else pk,
                'table_comment': table_comment,
                'author': kwargs.get('author'),
                'author_email': kwargs.get('author_email'),
                'created_at': arrow.now().format('YYYY-MM-DD HH:mm:ss'),
                'license': kwargs.get('license')
            },
            replace=True
        )
        self.execute_params(__sql, __values, commit=True)
        logger.debug(f'Table {self._table_name} meta info Created.')
        self._invalidate_cache()

    def add_column(self, column_name: str, definition: str) -> None:
        sql = getsql_add_column(self._table_name, column_name, definition)
        self.execute(sql, commit=True)
        self._invalidate_cache()

    def ensure_column(self, column_name: str, column_type: str, 
                      default: Optional[Any] = None) -> bool:
        definition = column_type
        if default is not None:
            definition += f" DEFAULT {sql_literal(default)}"
        try:
            self.add_column(column_name, definition)
            return True
        except DuplicateColumn:
            pass
        return False

    def ensure_columns(self, columns: Dict[str, str]) -> None:
        for column_name, type_ in columns.items():
            is_added = self.ensure_column(column_name, column_type=type_ or 'text')
            if is_added and self.verbose >= 1:
                logger.info(f'{self._table_name}: Added column {column_name} {type_}')

    def ensure_columns_like(self, dic: Dict[str, Any]) -> None:
        for column_name, v in dic.items():
            try:
                # None 值推断不出类型，与批量路径保持一致回落到 text
                type_ = suggest_value_type(v) or 'text'
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

    def _handle_error_on_connection(self, e: Exception, conn: sqlite3.Connection) -> None:
        """翻译 sqlite 错误为模块异常类型。

        外层 db.transaction() 存在时连接是共享事务连接，
        此处回滚会清空事务内先前未提交数据，故仅在非事务上下文回滚。
        """
        if not self.database.in_transaction():
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        str_e = str(e)
        if 'no column named' in str_e:
            match = _COLUMN_NAME_PATTERN.search(str_e)
            if match:
                raise NoSuchColumn(match.group(1))
            raise e
        elif str_e.startswith('no such column: '):
            raise NoSuchColumn(str_e.partition(':')[2].strip()) from e
        elif 'no such table' in str_e:
            match = _TABLE_NAME_PATTERN.search(str_e)
            if match:
                raise NoSuchTable(match.group(1))
            raise e
        elif 'already exists' in str_e:
            match = _TABLE_EXISTS_PATTERN.search(str_e)
            if match:
                raise TableAlreadyExists(match.group(1))
            raise e
        elif 'duplicate column' in str_e:
            match = _DUPLICATE_COLUMN_PATTERN.search(str_e)
            if match:
                raise DuplicateColumn(match.group(1))
            raise e
        else:
            raise e

    def _execute_params_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection,
                                      sql: str, params: Optional[List[Any]] = None,
                                      commit: Optional[bool] = None) -> None:
        """在指定连接上执行带参数的SQL语句"""
        if self.verbose >= 2:
            logger.info(f'SQL: {sql}; params={params}')

        try:
            cursor.execute(sql, params or [])
            self._try_commit_on_connection(conn, commit)
        except sqlite3.Error as e:
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
        except sqlite3.Error as e:
            self._handle_error_on_connection(e, conn)

    def execute(self, sql: str, commit: Optional[bool] = None) -> None:
        self._check_write_commit(commit)
        if self.verbose >= 2:
            logger.info(f'SQL: {sql}')
        
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql)
                self._try_commit_on_connection(conn, commit)
            except sqlite3.Error as e:
                self._handle_error_on_connection(e, conn)

    def execute_params(self, sql: str, params: Optional[List[Any]] = None,
                       commit: Optional[bool] = None) -> None:
        self._check_write_commit(commit)
        with self._get_cursor() as (cursor, conn):
            self._execute_params_on_connection(cursor, conn, sql, params, commit)

    def executemany(self, sql: str, list_values: List[Tuple], commit: Optional[bool] = None) -> None:
        '''SQL简单封装
        '''
        if self.verbose >= 2:
            logger.info(f'executemany SQL: {sql}: len={len(list_values)}')

        if not list_values:
            return

        self._check_write_commit(commit)

        with self._get_cursor() as (cursor, conn):
            try:
                cursor.executemany(sql, list_values)
                self._try_commit_on_connection(conn, commit)
            except sqlite3.Error as e:
                self._handle_error_on_connection(e, conn)

    def fetchall(self, sql: str, params: Optional[List[Any]] = None) -> List[Tuple]:
        """执行查询并返回所有结果"""
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql, params or [])
                return cursor.fetchall()
            except sqlite3.Error as e:
                self._handle_error_on_connection(e, conn)

    def fetchone(self, sql: str, params: Optional[List[Any]] = None) -> Optional[Tuple]:
        """执行查询并返回单个结果"""
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql, params or [])
                return cursor.fetchone()
            except sqlite3.Error as e:
                self._handle_error_on_connection(e, conn)
    
    def _check_write_commit(self, commit: Optional[bool]) -> None:
        """拒绝连接池模式下必然丢写的延迟提交用法。

        连接归还时连接池会无条件 rollback，因此池外上下文中：
        - 显式 ``commit=False`` 会丢失写入；
        - ``auto_commit=False`` 时不显式提交（commit=None）的写入会被丢弃。
        显式 transaction() 由外层管理连接和提交，可安全使用 commit=False。
        """
        if self.database.in_transaction():
            return
        if commit is False:
            raise ValueError(
                'commit=False 在连接池模式下会丢失写入：'
                '请传 commit=True，事务提交请使用 database.transaction()'
            )
        if commit is None and not self.auto_commit:
            raise ValueError(
                'auto_commit=False 时写入必须显式传 commit=True '
                '或放在 database.transaction() 上下文中，否则会被连接归还时的 rollback 丢弃'
            )

    def _try_commit_on_connection(self, conn: sqlite3.Connection, commit: Optional[bool]) -> None:
        """在指定连接上尝试提交
        
        这个方法替代了原来的_try_commit方法，直接在给定的连接上操作，
        避免了对self.conn的依赖，更适合连接池模式。
        
        Args:
            conn: 数据库连接对象
            commit: 是否提交事务，None表示根据auto_commit决定
        """
        if self.database.in_transaction():
            return
        if commit is None:
            if self.auto_commit:
                conn.commit()
            else:
                # 防御：低层 execute/execute_params 绕过 _check_write_commit 时也不能静默丢写
                raise ValueError(
                    'auto_commit=False 时写入必须显式传 commit=True '
                    '或放在 database.transaction() 上下文中'
                )
        elif commit:
            conn.commit()

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

    def query(self, sql: str, to_dict: bool = False,
              params: Optional[List[Any]] = None) -> Union[List[Tuple], List[Dict]]:
        #print(self.database.db_file)
        #print(sql)
        data = self.fetchall(sql, params=params)
        if to_dict and data:
            data = [dict(item) for item in data]
        return data

    def select(self, fields: Optional[Union[str, List[str]]], where: Optional[str] = None, 
               order_by: Optional[str] = None, option: Optional[str] = None, 
               limit: Optional[int] = None, to_dict: bool = False) -> Union[List[Tuple], List[Dict]]:
        if fields is None:
            field_string = '*'
        elif isinstance(fields, (tuple, list)):
            field_string = ','.join(quoted_sql_text(field) for field in fields)
        elif isinstance(fields, str):
            field_string = fields
        else:
            raise Exception('BadFields')

        sql = f'SELECT {field_string} FROM {quoted_sql_text(self._table_name)}'
        if where:
            sql += f' WHERE {where}'
        if order_by:
            sql += f' ORDER BY {order_by}'
        if option:
            sql += f' {option}'
        if limit is not None:
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
        self._check_write_commit(commit)
        sql, values = getsqlarg_dict_insert(
            self._table_name, kv, replace=replace, ignore=ignore
        )
        self._execute_with_fallback(
            sql, commit, kv, ensure_table, ensure_column,
            params=values, **kwargs
        )

    def _insert_one_batch_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection,
                                       batch_data: List[Dict[str, Any]], 
                                       replace: bool = False, ignore: bool = False,
                                       ensure_table: bool = False, 
                                       ensure_column: bool = False, **kwargs) -> None:
        """在指定连接上执行单批次插入。

        按行的 key 集合拆分为多条语句：行中缺失的列直接省略，
        让 SQLite 自行求值列默认值（含 CURRENT_TIMESTAMP 等表达式），
        避免把表达式默认值当成字面量写盘。
        """
        if not batch_data:
            return

        # 稳定结构只需按字段分组；类型推断延迟到真实建表/加列的冷路径。
        statements = [
            (
                getsqlarg_dict_insertmany(
                    self._table_name, fields, replace=replace, ignore=ignore
                ),
                values,
            )
            for fields, values in group_rows_by_keys(batch_data)
        ]

        try:
            self._executemany_statements_on_connection(cursor, conn, statements)
        except NoSuchTable as e:
            if ensure_table:
                # Note 这里忽略了not_null等参数, 用了默认数值
                # 用户能传的只有 columns(包含了not_null等信息), pk
                columns = suggest_columns(_union_fields_of_dictlist(batch_data))
                self._create_table_on_connection(cursor, conn, columns, **kwargs)
                self._executemany_statements_on_connection(cursor, conn, statements)
            else:
                raise e
        except NoSuchColumn as e:
            if ensure_column:
                self._ensure_columns_on_connection(cursor, conn, _union_fields_of_dictlist(batch_data))
                self._executemany_statements_on_connection(cursor, conn, statements)
            else:
                 raise e

    def _executemany_statements_on_connection(self, cursor: sqlite3.Cursor,
                                              conn: sqlite3.Connection,
                                              statements: List[Tuple[str, List[List[Any]]]]) -> None:
        """按顺序执行 (sql, 参数列表) 语句组，由外层统一提交。"""
        for sql, values in statements:
            self._executemany_on_connection(cursor, conn, sql, values, commit=False)


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

        __sql, __values = getsqlarg_dict_insert(
            meta_table_name, {
                'table_name': self._table_name,
                'columns': json.dumps(columns),
                'pk': ','.join(pk) if isinstance(pk, (list,tuple)) else pk,
                'table_comment': table_comment,
                'author': kwargs.get('author'),
                'author_email': kwargs.get('author_email'),
                'created_at': arrow.now().format('YYYY-MM-DD HH:mm:ss'),
                'license': kwargs.get('license')
            },
            replace=True
        )
        cursor.execute(__sql, __values)
        logger.debug(f'Table {self._table_name} meta info Created.')
        self._invalidate_cache()

    def _ensure_columns_on_connection(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection, 
                                     od_fields: Dict[str, Any]) -> None:
        """在指定连接上确保列存在"""
        for column_name, value in od_fields.items():
            try:
                type_ = (
                    value
                    if value in ('integer', 'float', 'text', 'blob')
                    else (suggest_value_type(value) or 'text')
                )
                sql = getsql_add_column(self._table_name, column_name, type_)
                cursor.execute(sql)
                self._invalidate_cache()
                if self.verbose >= 1:
                    logger.info(f'{self._table_name}: Added column {column_name} {type_}')
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e):
                    raise

    def insert_all(self, list_kv: List[Dict[str, Any]], replace: bool = False, 
                   ignore: bool = False, batch_size: int = 5000, commit: bool = True, 
                   ensure_table: bool = False, ensure_column: bool = False, **kwargs) -> None:
        """批量插入数据，使用单个连接进行整个批量操作以提高性能"""
        if commit is not True:
            raise ValueError(
                'insert_all(commit=...) 仅支持 True: '
                '连接池模式下批量操作结束时会统一提交，无法支持延迟提交'
            )
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, numbers.Integral)
            or batch_size <= 0
        ):
            raise BadArgument('batch_size must be a positive integer')
        batch_size = int(batch_size)
        # 要求keys一致: 但并不检验
        if not list_kv:
            return
        
        n = len(list_kv)
        n_batch = math.ceil(n / batch_size)
        
        # 使用单个连接进行整个批量操作
        with self._get_cursor() as (cursor, conn):
            own_transaction = not self.database.in_transaction()
            try:
                # 开始事务
                if own_transaction:
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
                
                # 连接池不能把未结束事务放回池中；外层 transaction() 存在时由外层提交。
                if own_transaction:
                    conn.commit()
                    
            except Exception as e:
                if own_transaction:
                    conn.rollback()
                raise e


    def delete(self, where: str) -> None:
        sql = f'DELETE FROM {quoted_sql_text(self._table_name)} WHERE {where}'
        self.execute(sql, commit=True)

    def update(self, kv: Dict[str, Any], where: str) -> None:
        kv_string = ','.join([f'{quoted_sql_text(k)}=?' for k in kv.keys()])
        values = list(kv.values())
        sql = f'UPDATE {quoted_sql_text(self._table_name)} SET {kv_string} WHERE {where}'
        with self._get_cursor() as (cursor, conn):
            try:
                cursor.execute(sql, values)
                self._try_commit_on_connection(conn, True)
            except sqlite3.Error as e:
                self._handle_error_on_connection(e, conn)

    def count(self, where: Optional[str] = None) -> int:
        sql = f'SELECT COUNT(*) FROM {quoted_sql_text(self._table_name)}'
        if where:
            sql += f' WHERE {where}'
        result = self.fetchone(sql)
        return result[0] if result else 0

    def delete_all(self) -> None:
        # commit 字段无效
        sql = f'DELETE FROM {quoted_sql_text(self._table_name)}'
        self.execute(sql, commit=True)

    def get_all_index(self, full_info: bool = False) -> Union[List[str], List[Dict]]:
        sql = (
            "SELECT * FROM sqlite_master WHERE type == 'index' "
            f"AND tbl_name == {sql_literal(self._table_name)}"
        )
        data = self.fetchall(sql)
        if full_info:
            return [dict(item) for item in data]
        else:
            return [item['name'] for item in data]
        # return [dict(item) for item in data]

    def _index_list(self) -> List[Dict]:
        """PRAGMA index_list：含 origin（c=用户创建, u=唯一约束, pk=主键）。"""
        rows = self.fetchall(f'PRAGMA index_list({quoted_sql_text(self._table_name)})')
        return [dict(item) for item in rows]

    def _index_info(self, index_name: str) -> List[Dict]:
        """PRAGMA index_info：索引列列表；表达式索引的列名为 None。"""
        rows = self.fetchall(f'PRAGMA index_info({quoted_sql_text(index_name)})')
        return [dict(item) for item in rows]

    def _is_user_index(self, item: Dict) -> bool:
        if item.get('origin') not in (None, 'c'):
            return False
        return not str(item.get('name', '')).lower().startswith('sqlite_autoindex')

    def drop_all_index(self) -> None:
        # https://stackoverflow.com/questions/2121583/how-to-drop-all-indexes-of-a-sqlite-table
        # 只能删除用户创建的索引：主键/唯一约束的 autoindex 由 SQLite 管理，DROP 会报错
        for item in self._index_list():
            if not self._is_user_index(item):
                continue
            index = item['name']
            logger.info(f'drop index {index} ...')
            self.drop_index(index)

    def drop_index(self, index_name: str) -> None:
        sql = f'DROP INDEX {quoted_sql_text(index_name)}'
        self.execute(sql, commit=True)

    def drop_column_index(self, column: str) -> int:
        to_del = []
        for item in self._index_list():
            if not self._is_user_index(item):
                continue
            # 用 PRAGMA index_info 解析真实索引列，兼容复合索引/表达式索引
            index_columns = [
                row['name'] for row in self._index_info(item['name'])
                if row.get('name') is not None
            ]
            if column in index_columns:
                to_del.append(item['name'])

        for index_name in to_del:
            if self.verbose >= 1:
                logger.info(f'Droping index {index_name}')
            self.drop_index(index_name)

        return len(to_del)

    def drop_table(self) -> None:
        sql = f'DROP TABLE IF EXISTS {quoted_sql_text(self._table_name)}'
        self.execute(sql, commit=True)
        # 表已删除，必须失效缓存，避免 drop 后用旧 schema 重建表
        self.invalidate_cache()

    def commit(self, rollback_if_err: bool = True) -> None:
        # 在连接池模式下，commit应该在具体的连接上下文中进行
        raise NotImplementedError("在连接池模式下，请在具体的连接上下文中调用conn.commit()")

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
    def auto_commit(self) -> bool:
        return self.database.auto_commit

    @property
    def table_name(self) -> str:
        return self._table_name

    def _table_info(self, cursor: Optional[sqlite3.Cursor] = None) -> List[Dict]:
        # [{'cid': 0, 'name': 'art_code', 'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 1}, 
        if self._table_info_cache is None:
            table = self.table_name
            sql = f"PRAGMA table_info({quoted_sql_text(table)})"
            if cursor is None:
                with self._get_cursor() as (owned_cursor, conn):
                    owned_cursor.execute(sql)
                    rows = owned_cursor.fetchall()
            else:
                cursor.execute(sql)
                rows = cursor.fetchall()
            self._table_info_cache = [dict(item) for item in rows]
        return self._table_info_cache

    def default_values(
        self,
        cursor: Optional[sqlite3.Cursor] = None,
    ) -> Dict[str, Any]:
        '''
        return default or None
        '''
        if self._default_values_cache is None:
            infos = self._table_info(cursor=cursor)
            self._default_values_cache = {
                item['name']: self._normalize_default_value(item['dflt_value'])
                for item in infos
            }
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
        info_dict['default'] = {
            item['name']: self._normalize_default_value(item['dflt_value'])
            for item in infos
            if item['dflt_value'] is not None
        }
        return info_dict
        # print(table_info)
        # table_info['column_type'] = OrderedDict([tuple(item['name'], item['type'] for item in infos]
        # https://stackoverflow.com/questions/54412977/sqlite3-python-get-list-of-primary-and-foreign-keys
        # 在连接池模式下，需要使用 with self._get_cursor() as (cursor, conn): 来获取连接
        # rows = cursor.execute("PRAGMA foreign_key_list({})".format(quoted_sql_text(table)))

    def _normalize_default_value(self, value):
        if isinstance(value, str) and len(value) >= 2:
            if value[0] == "'" and value[-1] == "'":
                return value[1:-1].replace("''", "'")
        return value
