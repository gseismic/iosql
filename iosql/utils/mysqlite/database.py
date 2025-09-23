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
                 timeout: float = 5.0, **kwargs):
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
