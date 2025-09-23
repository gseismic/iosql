import time
import sqlite3
import logging
import copy
import threading
import contextlib
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
import sqlite3
from contextlib import contextmanager
from .connection_pool import DatabaseConnectionPool
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
                 pragma_dict: Optional[Dict[str, Any]] = None, 
                 wal: bool = True, max_connections: int = 10, 
                 timeout: float = 5.0, **kwargs):
        """
        初始化数据库实例
        
        Args:
            db_file: 数据库文件路径
            auto_commit: 自动提交事务
            verbose: 详细级别 (0=静默, 1=信息, 2=调试)
            pragma_dict: 自定义PRAGMA设置
            wal: 启用WAL模式
            max_connections: 最大连接数
            timeout: 连接超时时间（秒）
            **kwargs: 其他连接参数
        """
        self.db_file = Path(db_file)
        self.verbose = verbose
        self.auto_commit = auto_commit
        self._kwargs = kwargs
        self._kwargs.setdefault('timeout', timeout)
        self._tables = {}
        
        # 添加连接池
        self._pool = DatabaseConnectionPool(
            str(self.db_file), 
            max_connections=max_connections,
            timeout=timeout,
            **kwargs
        )
        
        # 简化锁的使用
        self._lock = threading.Lock()  # 只用于关键配置操作
        self._pragma_dict = pragma_dict or copy.deepcopy(_DEFAULT_PRAGMA)

        # 如果请求则启用WAL模式
        if wal:
            self.enable_wal()

    def enable_wal(self, enable: bool = True) -> None:
        """启用或禁用WAL模式"""
        if enable:
            self._pragma_dict['journal_mode'] = 'wal'
        else:
            self._pragma_dict['journal_mode'] = 'delete'

    def _pragma_one(self, key: str, value: Optional[str] = None, ensure: bool = False) -> None:
        """设置单个PRAGMA值"""
        value = value or ''
        current_value = self._pragma_dict.get(key.lower(), '')
        if str(value).lower() != current_value.lower():
            self._pragma_dict[key.lower()] = str(value)

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
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        """上下文管理器退出，进行适当的清理"""
        # 连接池会自动管理连接，无需手动清理
        pass

    def __getitem__(self, table_name: str) -> 'table.Table':
        """通过名称获取表，如果不存在则创建"""
        if table_name not in self._tables:
            if self.verbose >= 2:
                logger.debug(f'创建表 {self.db_file}: {table_name}')
            _table = table.Table(self, table_name=table_name)
            self._tables[table_name] = _table
        return self._tables[table_name]

    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器"""
        # 确保目录存在
        db_dirname = self.db_file.parent
        if not db_dirname.exists():
            if self.verbose >= 1:
                logger.debug(f'创建数据库目录: {db_dirname} ...')
            db_dirname.mkdir(parents=True, exist_ok=True)
            
        with self._pool.get_connection() as conn:
            # 确保连接配置正确
            self._apply_pragma_to_connection(conn)
            yield conn
    
    def _apply_pragma_to_connection(self, conn: sqlite3.Connection) -> None:
        """为单个连接应用PRAGMA设置"""
        cursor = conn.cursor()
        for key, value in self._pragma_dict.items():
            cursor.execute(f'PRAGMA {key}={value};')
        cursor.close()
        


    def execute(self, sql: str) -> None:
        """执行SQL语句，包含适当的错误处理"""
        if self.verbose >= 2:
            logger.debug(f'{self.db_file}: 执行: {sql}')

        with self.get_connection() as conn:
            try:
                conn.execute(sql)
                if self.auto_commit:
                    conn.commit()
            except sqlite3.Error as e:
                logger.error(f"SQL execution failed: {sql}")
                conn.rollback()
                raise DatabaseError(f"SQL execution failed: {e}") from e

    def commit(self) -> None:
        """提交当前事务 - 注意：在连接池模式下，每个操作都是自动提交的"""
        if self.verbose >= 2:
            logger.debug(f'连接池模式下事务自动提交 {self.db_file}')

    def rollback(self) -> None:
        """回滚当前事务 - 注意：在连接池模式下，每个操作都是自动提交的"""
        if self.verbose >= 2:
            logger.debug(f'连接池模式下无法回滚已提交的事务 {self.db_file}')

    def close(self) -> None:
        """关闭数据库连接池"""
        if self.verbose >= 1:
            logger.debug(f'连接池会自动管理连接 {self.db_file}')

    def is_closed(self) -> bool:
        """检查连接是否已关闭 - 连接池模式下总是可用"""
        return False

    def get_conn(self) -> sqlite3.Connection:
        """获取数据库连接 - 不推荐在连接池模式下直接使用"""
        raise DatabaseError("在连接池模式下请使用 get_connection() 上下文管理器")

    def get_cursor(self) -> sqlite3.Cursor:
        """获取数据库游标 - 不推荐在连接池模式下直接使用"""
        raise DatabaseError("在连接池模式下请使用 get_connection() 上下文管理器")

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接 - 不推荐在连接池模式下直接使用"""
        raise DatabaseError("在连接池模式下请使用 get_connection() 上下文管理器")

    @property
    def cursor(self) -> sqlite3.Cursor:
        """获取数据库游标 - 不推荐在连接池模式下直接使用"""
        raise DatabaseError("在连接池模式下请使用 get_connection() 上下文管理器")

    def __table_names(self) -> List[str]:
        """获取所有表名（已弃用，请使用get_all_tables）"""
        return self.get_all_tables()

    def get_all_tables(self) -> List[str]:
        """获取数据库中所有表名"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
                table_names = [row[0] for row in cursor.fetchall()]
                cursor.close()
                return table_names
        except sqlite3.Error as e:
            raise DatabaseError(f"Failed to get table names: {e}") from e

    def get_table_indices(self, table_name: str, exclude_autoindex: bool = True) -> List[str]:
        """获取指定表的所有索引"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                sql = ("SELECT name FROM sqlite_master " 
                       "WHERE type = 'index' AND tbl_name = ?")
                cursor.execute(sql, (table_name,))
                indices = [row[0] for row in cursor.fetchall()]
                cursor.close()
                
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
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT name FROM sqlite_master WHERE type="index"')
                indices = [row[0] for row in cursor.fetchall()]
                cursor.close()
                
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
