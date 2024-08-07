import os
import time
import sqlite3
import logging
from . import table
import copy
import threading
from .logger import logger
from .const import _DEFAULT_PRAGMA 


# https://docs.python.org/2/library/sqlite3.html#sqlite3.connect
# default timeout 5
# _DEFAULT_TIMEOUT = 5
class Database(object):
    """
    如果文件不存在，自动创建

    定位: SQL 完备的工具，而非好用的工具

    留意应对
    OSError: [Errno 24] Too many open files
    """
    # _lock = threading.Lock()
    # [@2024-04-02 02:37:10] rm lock, 发现wal后一直等待
    def __init__(self, db_file, auto_commit=True, verbose=1,
                 pragma_dict=None, wal=True, **kwargs):
        # auto_commit: 命名一致原则
        # self._lock = threading.Lock()
        self.db_file = db_file
        self.verbose = verbose
        self.auto_commit = auto_commit
        self._kwargs = kwargs
        self._tables = {}
        self._conn = None
        self._cursor = None
        # https://www.sqlite.org/pragma.html#pragma_cache_size
        # 10M 
        self._pragma_dict = pragma_dict or copy.deepcopy(_DEFAULT_PRAGMA)
        # [@2024-04-02 02:33:33] new
        # self.enable_wal()
        # [@2024-04-02 03:00:25] add
        # self.connect() # with 时才connect

    def enable_wal(self, enable=True):
        # PRAGMA journal_mode=WAL
        self._pragma_dict['journal_mode'] = 'wal'
        self._ensure_pragma('journal_mode', 'wal')

    def _pragma_one(self, key, value=None, ensure=False):
        value = value or ''
        if ensure:
            self._ensure_pragma(key, value)
        else:
            if str(value).lower() != self._pragma_dict[key.lower()].lower():
                self._pragma_dict[key.lower()] = str(value)
                self._ensure_pragma(key, value)

    def pragma(self, key_or_dict, value=None, ensure=False):
        if isinstance(key_or_dict, dict):
            assert value is None
            for k, v in key_or_dict.items():
                self._pragma_one(k, v, ensure=ensure)
        else:
            self._pragma_one(key_or_dict, value, ensure=ensure)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        # return isinstance(value, TypeError)
        self.close()

    def __getitem__(self, table_name):
        if table_name not in self._tables:
            logger.debug(f'Create table {self.db_file}: {table_name}')
            _table = table.Table(self, table_name=table_name)
            self._tables[table_name] = _table
        # print(f'Use table {self.db_file}: {table_name}')
        return self._tables[table_name]

    def connect(self):
        '''重置了连接
        '''
        db_dirname = os.path.dirname(os.path.abspath(self.db_file))
        if self.verbose >= 2:
            logger.debug('Database data dir: %s' % db_dirname)
        if not os.path.exists(db_dirname):
            if self.verbose >= 1:
                logger.debug('Making database dir: %s ...' % db_dirname)
            # exist_ok: in case of multithread
            os.makedirs(db_dirname, exist_ok=True)

        if self._conn is not None:
            logger.info(f'Already Connected .. {self.db_file}, ignored.')
            return

        self._conn = sqlite3.connect(self.db_file, **self._kwargs)
        self._conn.row_factory = sqlite3.Row
        self._cursor = self._conn.cursor()
        # default 默认激活
        self._ensure_all_pragma()
        logger.debug(f'Connected .. {self.db_file}')

    def execute(self, sql):
        # raise NotImplementedError()
        # self.conn.cursor().execute(sql)
        if self.verbose >= 2:
            logger.debug(f'{self.db_file}: Execute: {sql}')

        #with self._lock:
        self.conn.execute(sql)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        if self._cursor:
            self._cursor.close()
            self._cursor = None
        if self._conn:
            self._conn.close()
            self._conn = None
        logger.debug(f'Closed.. {self.db_file}')

    def is_closed(self):
        return self._conn is None

    def get_conn(self):
        if not self._conn:
            self.connect()
        return self._conn

    def get_cursor(self):
        if not self._cursor:
            # print(f'Create conn .. {self.db_file}')
            assert self._conn is None
            self.connect()
        return self._cursor

    @property
    def conn(self):
        return self.get_conn()

    @property
    def cursor(self):
        return self.get_cursor()

    def __table_names(self):
        return self.get_all_tables()

    def get_all_tables(self):
        cu = self.conn.cursor()
        sql = 'SELECT Name FROM sqlite_master WHERE type="table"'
        cu.execute(sql)
        table_names = cu.fetchall()
        table_names = [t[0] for t in table_names]
        return table_names

    def get_table_indices(self, table_name, exclude_autoindex=True):
        # autoindex: 无法涉及primary_key，是无法删除
        sql = ("SELECT name FROM sqlite_master " 
               "WHERE type == 'index' AND "
               "tbl_name == '%s'" % table_name)
        cu = self.conn.cursor()
        cu.execute(sql)
        data = cu.fetchall()
        indices = [it[0] for it in data]
        if exclude_autoindex:
            indices = self._exclude_autoindex(indices)
        return indices

    def _exclude_autoindex(self, indices):
        new_indices = [index for index in indices if index.find('autoindex') ==-1]
        return new_indices

    def get_all_indices(self, exclude_autoindex=True):
        cu = self.conn.cursor()
        sql = 'SELECT Name FROM sqlite_master WHERE type="index"'
        cu.execute(sql)
        r = cu.fetchall()
        indices = [t[0] for t in r]
        if exclude_autoindex:
            indices = self._exclude_autoindex(indices)
        return indices

    def drop_table(self, table_name):
        if isinstance(table_name, str):
            table_names = [table_name]
        elif isinstance(table_name, (list, tuple)):
            table_names = table_name
        else:
            raise Exception('table_name required to be str/list/tuple')

        for tbl in table_names:
            sql = 'drop table %s' % tbl
            if self.verbose:
                logger.info(sql)
            self.execute(sql)

    def drop_index(self, index_or_indices):
        if isinstance(index_or_indices, (list, tuple)):
            indices = index_or_indices
        elif isinstance(index_or_indices, str):
            indices = [index_or_indices]
        else:
            raise Exception('index required to be str/list/tuple')

        # exec
        cu = self.conn.cursor()
        for index in indices:
            sql = 'drop index %s' % index
            if self.verbose:
                logger.info(sql)
            cu.execute(sql)

    def drop_all_indices(self, exclude_autoindex=True):
        logger.info('Drop all indices of all tables ... sleep 10 sec')
        time.sleep(10)
        indices = self.get_all_indices(exclude_autoindex)
        self.drop_index(indices)

    def create_index(self, table_name, field, 
                     option='ASC', ignore_if_exists=False):
        if ignore_if_exists:
            base_sql = 'CREATE INDEX IF NOT EXISTS'
        else:
            base_sql = 'CREATE INDEX'

        # 增加table名
        sql = '%s %s_%s_index on %s (%s ' % (base_sql, table_name, field, table_name, field)
        if option:
            sql += option
        sql += ')'

        self.execute(sql)

    def _ensure_pragma(self, key, value=None):
        if value:
            self.execute(f'PRAGMA {key}={value};')
        else:
            self.execute(f'PRAGMA {key};')

    def _ensure_all_pragma(self):
        for key, value in self._pragma_dict.items():
            self._ensure_pragma(key, value)
