import re
import math
import json
import arrow
import logging
import sqlite3
from .exception import (
    NoSuchColumn, 
    NoSuchTable,
    TableAlreadyExists,
    DuplicateColumn
)
from collections import OrderedDict
# from .database import Database
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
# from ._helpers import (easy_create_table, easy_create_index)

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

class Table(object):
    """
    设计原则： 只有commit抛出的异常才catch
    定位: SQL 完备的工具，而非好用的工具
    """
    def __init__(self, database, table_name, db_options={}):
        '''
            数据安全最重要，auto_commit默认打开
            不要用uri, 需要手动关闭吗？ 这里不推荐使用
        '''
        assert isinstance(database, my_database.Database)
        self._database = database
        self._table_name = table_name
        self.auto_commit = self.database.auto_commit
        self.verbose = self.database.verbose
        # self._fields = [item['name'] for item in self._table_fields]
        # 使用SqliteDatabase 因为可以自动维护conn
        # 同时考虑: 因为fetchall需要，所以设计一个SqliteTable只有一个
        # 固定的cursor
        # self.cursor = self._database.conn.cursor()
        # self._cursor = self.get_cursor()

    def pragma(self, key, value=None):
        # https://www.programcreek.com/python/?CodeExample=set+sqlite+pragma
        value = value or ''
        if value:
            self.cursor.execute(f'PRAGMA {key}={value};')
        else:
            self.cursor.execute(f'PRAGMA {key};')

    def table_exists(self):
        sql = getsql_query_table(self._table_name, name_only=True)
        self.execute(sql)
        # rv {'type': 'table', 'name': 'not_exist2', 'tbl_name': 'not_exist2', 'rootpage': 11, 'sql': 'CREATE TABLE not_exist2(\n\tid integer,\n\tname5 text,\n\tname6 text)'}
        rv = self.fetchone()
        if rv:
            return True
        return False

    def create_table_like(self, dic, columns=None, pk=None, exist_ok=True, 
                          save_meta=True, table_comment=None,
                          exist_no_warn=False, **kwargs):
        if columns is None:
            columns = suggest_data_columns(dic)
            if self.verbose >= 1:
                logger.info(f'Data suggest columns: {columns}')
        return self.create_table(columns, pk=pk, 
                                 exist_ok=exist_ok,
                                 save_meta=save_meta,
                                 table_comment=table_comment,
                                 exist_no_warn=exist_no_warn, **kwargs)

    def create_table(self, columns, pk=None, exist_ok=True, 
                     save_meta=True, table_comment=None,
                     exist_no_warn=False,
                     **kwargs):
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

    def add_column(self, column_name, definition):
        # logger.info(f'Table {self._table_name}: Added Column `{column_name}: {type_}`')
        sql = getsql_add_column(self._table_name, column_name, definition)
        self.execute(sql, commit=True)

    def ensure_column(self, column_name, definition):
        try:
            self.add_column(column_name, definition)
            return True
        except DuplicateColumn:
            pass
        return False

    def ensure_columns(self, columns):
        for column_name, type_ in columns.items():
            is_added = self.ensure_column(column_name, definition=type_)
            if is_added and self.verbose >= 1:
                logger.info(f'{self._table_name}: Added column {column_name} {type_}')

    def ensure_columns_like(self, dic):
        for column_name, v in dic.items():
            try:
                type_ = suggest_value_type(v)
                self.add_column(column_name, type_)
                if self.verbose >= 1:
                    logger.info(f'{self._table_name}: Added column {column_name} {type_}')
            except DuplicateColumn:
                pass

    def create_index(self, column, order='asc', exist_ok=False):
        sql = getsql_create_index(self._table_name, column, order=order,
                                  if_not_exists=exist_ok)
        if self.verbose >= 1:
            logger.info(f'Create Index on `{column}` {order}')
        self.execute(sql, commit=True)

    def _handle_error(self, e):
        str_e = str(e)
        if str_e.find('no column named') != -1:
            column = re.findall(r'no column named (\w+)', str_e)[0]
            raise NoSuchColumn(column)
        elif str_e.find('no such table') != -1:
            _table = re.findall(r'no such table: (\w+)', str_e)[0]
            raise NoSuchTable(_table)
        elif str_e.find('already exists') != -1:
            _table = re.findall(r'table (\w+) already exists', str_e)[0]
            raise TableAlreadyExists(_table)
        elif str_e.find('duplicate column') != -1:
            column = re.findall(r'duplicate column name: (\w+)', str_e)[0]
            raise DuplicateColumn(column)
        else:
            raise e
        return None

    def execute(self, sql, commit=None):
        if self.verbose >= 2:
            logger.info(f'SQL: {sql}')
        try:
            self.cursor.execute(sql)
        except sqlite3.OperationalError as e:
            self._handle_error(e)

        self._try_commit(commit)

    def executemany(self, sql, list_values, commit=None):
        '''SQL简单封装
        # 废弃，插入一般失败，不好处理
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

    def fetchall(self):
        # 读取数据可能需要以前的游标，而不是重建
        data = self.cursor.fetchall()
        return data

    def fetchone(self):
        data = self.cursor.fetchone()
        return data

    def _update(self, kv, where, commit=None):
        sql = 'update %s' % self._table_name 
        # fixed:  and --> ','
        kv_string = ','.join(['%s="%s"' % (str(k), str(v)) \
                                  for k, v in kv.items()])
        sql += ' set ' + kv_string
        if where:
            sql += ' where ' + where
        self.execute(sql, commit=commit)

    def select_one(self, fields, where=None, option=None):
        rv = self.select(fields, where, option=option, limit=1, to_dict=True)
        return rv[0] if rv else None

    def select_all(self, fields, where=None, option=None, limit=None):
        return self.select(fields, where, option=option, limit=limit, to_dict=True)

    def query_all(self, sql):
        return self.query(self, sql, to_dict=True)

    def query(self, sql, to_dict=False):
        self.execute(sql)
        data = self.fetchall()
        if to_dict and data:
            data = [dict(item) for item in data]
        return data

    def select(self, fields, where=None, option=None, limit=None, to_dict=False):
        if fields is None:
            field_string = '*'
        elif isinstance(fields, (tuple, list)):
            field_string = ','.join(fields)
        elif isinstance(fields, str):
            field_string = fields
        else:
            raise Exception('BadFields')

        sql = 'select %s' % field_string
        sql += ' from %s' % self._table_name
        if where:
            sql += ' where %s' % where
        if option:
            sql += ' %s' % option
        if limit:
            sql += f' limit {limit}'

        return self.query(sql, to_dict)
        #self.execute(sql)
        #data = self.fetchall()
        #if to_dict and data:
        #    data = [dict(item) for item in data]
        #return data

    def insert(self, kv, replace=False, ignore=False, commit=None, 
                    ensure_table=False, ensure_column=False, **kwargs):
        if not kv: return
        sql, _ = getsqlarg_dict_insert(self._table_name, kv, replace=replace, ignore=ignore)
        try:
            self.execute(sql, commit=commit)
        except NoSuchTable as e:
            if ensure_table:
                if self.verbose >= 1:
                    logger.info(f'Table {self._table_name} Not Exist, try-to-create by Data')
                self.create_table_like(kv, **kwargs)
                # 创建了table，就不会报错 NoSuchColumn
                self.execute(sql, commit=commit)
            else:
                raise e
        except NoSuchColumn as e:
            if ensure_column:
                self.ensure_columns_like(kv)
                self.execute(sql, commit=commit)
            else:
                raise e

    def _insert_one_batch(self, batch_data, 
                          replace=False, ignore=False,
                          commit=True,
                          ensure_table=False, ensure_column=False, **kwargs):
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

    def insert_all(self, list_kv, replace=False, 
                   ignore=False, batch_size=5000, commit=True, 
                   ensure_table=False, ensure_column=False, **kwargs):
        # 要求keys一致: 但并不检验
        if not list_kv: return
        # fields = list(list_kv[0].keys())
        # sql = getsqlarg_dict_insertmany(self._table_name, fields, replace=replace, ignore=ignore)
        n = len(list_kv)
        n_batch = math.ceil(n/float(batch_size))

        commit_size = 5000
        written_size = 0 
        for i in range(n_batch):
            batch_data = list_kv[i*batch_size: min((i+1)*batch_size, n)]
            if self.verbose >= 2:
                logger.info(f'Write batch_size: {i+1}/{n_batch} ..')

            written_size += batch_size
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


    def __list_insertmany(self, list_values, replace=False, ignore=False,
                        batch_size=50000, commit=True):
        '''要求字段完全匹配

        使用不方便，废弃
        '''
        assert 0
        if not list_values: return
        action = getsubsql_insert(replace=replace, ignore=ignore)
        n_fields = len(self._fields)
        sql = '%s %s values (%s)' % (action, self._table_name, 
                                     ','.join(['?'] * n_fields))
        self.executemany(sql, list_values, batch_size=batch_size, commit=commit)

    def delete(self, where):
        sql = 'delete ' 
        sql += ' from %s' % self._table_name
        sql += ' where %s' % where
        self.execute(sql, commit=True)

    def delete_all(self):
        # commit 字段无效
        sql = 'delete from %s' % self._table_name
        self.execute(sql, commit=True)

    def get_all_index(self, full_info=False):
        # 获取当前已经被创建的index
        sql = ("SELECT * FROM sqlite_master " 
               "WHERE type == 'index' AND "
               "tbl_name == '%s'" % self._table_name)
        self.execute(sql)
        data = self.fetchall()
        if full_info:
            return [dict(item) for item in data]
        else:
            return [item['name'] for item in data]
        # return [dict(item) for item in data]

    def drop_all_index(self):
        # https://stackoverflow.com/questions/2121583/how-to-drop-all-indexes-of-a-sqlite-table
        list_index = self.get_all_index()
        for index in list_index:
            logger.info('drop index %s ...' % index)
            self.drop_index(index)

    def drop_index(self, index_name):
        sql = 'drop index %s' % index_name
        self.execute(sql, commit=True)

    def drop_column_index(self, column):
        # index [{'type': 'index', 'name': 'data_name_index', 'tbl_name': 'data', 'rootpage': 5, 'sql': 'CREATE INDEX data_name_index on data (name asc)'}]
        items = self.get_all_index(full_info=True)
        to_del = set()
        for item in items:
            _column = re.findall(r'\((.*?)\)', item['sql'])[0].split(' ')[0]
            if _column == column:
                to_del.add(item['name'])

        for index_name in to_del:
            if self.verbose >= 1:
                logger.info(f'Droping index {index_name}')
            self.drop_index(index_name)

        return len(to_del)

    def drop_table(self):
        sql = 'drop table if exists %s' % self._table_name
        self.execute(sql, commit=True)

    def commit(self, rollback_if_err=True):
        try:
            self.conn.commit()
        except Exception as e:
            logger.error('Error %s' % str(e))
            self.conn.rollback()

    def _try_commit(self, commit):
        if commit is None:
            if self.auto_commit:
                self.commit()
        elif commit:
            self.commit()

    def close(self):
        if self.cursor:
            self.cursor.close()
            self.cursor = None

    @property
    def cursor(self):
        # 如果conn 已经关闭，可以自动连接
        return self.database.cursor

    @property
    def conn(self):
        return self.database.conn

    @property
    def database(self):
        return self._database

    @property
    def table_name(self):
        return self._table_name

    def _table_info(self):
        # [{'cid': 0, 'name': 'art_code', 'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 1}, 
        table = self.table_name
        sql = "PRAGMA table_info({})".format(quoted_sql_text(table))
        rows = self.conn.execute(sql)
        return [dict(item) for item in rows.fetchall()]

    def default_values(self):
        '''
        return default or None
        '''
        table = self.table_name
        sql = "PRAGMA table_info({})".format(quoted_sql_text(table))
        rows = self.conn.execute(sql)
        return {item['name']: item['dflt_value'] for item in rows}
        # [{'cid': 0, 'name': 'art_code', 'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 1}, 

    def pk(self):
        infos = self._table_info()
        infos = sorted([info for info in infos if info['pk'] > 0], key=lambda x:x['pk'])
        return [item['name'] for item in infos]

    def column_info(self):
        _infos = self._table_info()
        infos = sorted([i for i in _infos], key=lambda i:i['cid'])
        info_dict = {}
        info_dict['column_type'] = OrderedDict([(i['name'], i['type']) for i in infos])
        # info_dict['not_null'] = OrderedDict([(i['name'], bool(i['notnull'])) for i in infos])
        info_dict['not_null'] = [item['name'] for item in infos if item['notnull'] != 0]
        info_dict['default'] = OrderedDict([(i['name'], i['dflt_value']) for i in infos])
        info_dict['default'] = {item['name']: item['dflt_value'] for item in infos if item['dflt_value'] is not None}
        return info_dict
        # print(table_info)
        # table_info['column_type'] = OrderedDict([tuple(item['name'], item['type'] for item in infos]
        # https://stackoverflow.com/questions/54412977/sqlite3-python-get-list-of-primary-and-foreign-keys
        # rows = self.conn.execute("PRAGMA foreign_key_list({})".format(quoted_sql_text(table)))
