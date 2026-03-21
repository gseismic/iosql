import os
from typing import Any, Dict, List, Optional
import psycopg2
from psycopg2 import sql

from ..table import BaseTable
from ..partition import Partition, DurationPartition, CalendarPartition
from ..exception import InvalidUriSchema
from ..utils.schema import parse_db_schema
from ..logger import default_logger as logger


class Table(BaseTable):
    """PostgreSQL table with partition support.
    
    Supports two partition modes:
    - 'file': File-per-partition (same as SQLite, using foreign tables)
    - 'native': Native PostgreSQL PARTITION BY RANGE
    
    URI format:
        postgresql://user:pass@host:port/dbname
    """
    
    PARTITION_MODE_FILE = 'file'
    PARTITION_MODE_NATIVE = 'native'
    
    def __init__(self, uri, table_name, verbose=0, 
                 partition_mode='native', partition: Partition = None):
        """
        Args:
            uri: PostgreSQL connection URI
            table_name: Name of the table
            verbose: Verbosity level
            partition_mode: 'native' for native PostgreSQL partitioning, 'file' for file-per-partition
            partition: Partition object for data partitioning
        """
        self._uri = uri
        self._table_name = table_name
        self._uri_schema, self._uri_options = parse_db_schema(uri)
        self.verbose = verbose
        self.partition_mode = partition_mode
        self._partition = partition
        self._conn = None
        
        if self.verbose >= 1:
            logger.info(f'PostgreSQL Table {table_name} initialized with partition_mode={partition_mode}')
    
    def connect(self):
        """Establish database connection."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._uri)
            self._conn.autocommit = True
        return self._conn
    
    def close(self):
        """Close database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
    
    def pragma(self, key, value=None):
        """PostgreSQL does not use PRAGMA, but we keep the interface for compatibility."""
        pass
    
    def create_table(self, column_type, pk, partition=None, **kwargs):
        """Create partitioned table.
        
        Args:
            column_type: Dict of column_name -> column_type
            pk: Primary key columns
            partition: Partition object
            **kwargs: Additional arguments
        """
        if partition is None:
            partition = self._partition
        
        columns_def = []
        for col_name, col_type in column_type.items():
            col_def = sql.Identifier(col_name) + sql.SQL(' ') + sql.SQL(col_type)
            if pk and col_name in pk:
                col_def += sql.SQL(' PRIMARY KEY')
            columns_def.append(col_def)
        
        if partition is not None:
            if self.partition_mode == self.PARTITION_MODE_NATIVE:
                self._create_native_partitioned_table(columns_def, pk, partition)
            else:
                self._create_file_partitioned_table(columns_def, pk, partition)
        else:
            self._create_simple_table(columns_def, pk)
    
    def _create_simple_table(self, columns_def: List, pk):
        """Create a simple non-partitioned table."""
        conn = self.connect()
        cursor = conn.cursor()
        
        create_sql = sql.SQL('CREATE TABLE IF NOT EXISTS {} ({})').format(
            sql.Identifier(self._table_name),
            sql.SQL(', ').join(columns_def)
        )
        
        cursor.execute(create_sql)
        if self.verbose >= 1:
            logger.info(f'Created table {self._table_name}')
    
    def _create_native_partitioned_table(self, columns_def: List, pk, partition: Partition):
        """Create native PostgreSQL partitioned table."""
        conn = self.connect()
        cursor = conn.cursor()
        
        partition_col = sql.Identifier(partition.TS_COL)
        
        create_sql = sql.SQL('CREATE TABLE IF NOT EXISTS {} ({}) PARTITION BY RANGE ({})').format(
            sql.Identifier(self._table_name),
            sql.SQL(', ').join(columns_def),
            partition_col
        )
        
        cursor.execute(create_sql)
        if self.verbose >= 1:
            logger.info(f'Created partitioned table {self._table_name} by {partition_col}')
    
    def _create_file_partitioned_table(self, columns_def: List, pk, partition: Partition):
        """Create file-per-partition table (uses foreign tables or regular tables per partition)."""
        # For now, just create the main table - partitions will be created on-demand
        self._create_simple_table(columns_def, pk)
    
    def insert(self, dic, pk=None, extra_data=None, partition=None,
               replace=False, ignore=False, commit=True, **kwargs):
        """Insert a single row."""
        if not dic:
            return
        return self.insert_all([dic], pk=pk, extra_data=extra_data, partition=partition,
                              replace=replace, ignore=ignore, commit=commit, **kwargs)
    
    def insert_all(self, data, pk=None, extra_data=None, partition=None,
                   replace=False, ignore=False, commit=True, **kwargs):
        """Insert multiple rows with partitioning."""
        if not data:
            return
        
        if partition is None:
            partition = self._partition
        
        # Add extra data if provided
        if extra_data:
            data = [{**item, **extra_data} for item in data]
        
        if partition is None:
            # No partitioning - insert all into single table
            self._insert_into_table(data, self._table_name, replace=replace, ignore=ignore)
        else:
            # Partition data and insert into appropriate partitions
            partitioned_data: Dict[Any, List] = {}
            for row in data:
                key = partition.get_key(row)
                if key not in partitioned_data:
                    partitioned_data[key] = []
                partitioned_data[key].append(row)
            
            for part_key, part_rows in partitioned_data.items():
                self._insert_partition(part_key, part_rows, partition, replace=replace, ignore=ignore)
    
    def _insert_into_table(self, data: List[Dict], table_name: str, replace=False, ignore=False):
        """Insert data into a specific table."""
        if not data:
            return
        
        conn = self.connect()
        cursor = conn.cursor()
        
        columns = list(data[0].keys())
        col_identifiers = [sql.Identifier(col) for col in columns]
        
        if replace:
            # Use ON CONFLICT DO UPDATE
            for row in data:
                placeholders = [sql.Placeholder(col) for col in columns]
                insert_sql = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(table_name),
                    sql.SQL(', ').join(col_identifiers),
                    sql.SQL(', ').join(placeholders)
                )
                insert_sql += sql.SQL(' ON CONFLICT DO UPDATE SET ')
                update_parts = [sql.Identifier(col) + sql.SQL(' = EXCLUDED.') + sql.Identifier(col) 
                              for col in columns if col != columns[0]]
                insert_sql += sql.SQL(', ').join(update_parts)
                
                cursor.execute(insert_sql, row)
        elif ignore:
            # Use ON CONFLICT DO NOTHING
            for row in data:
                placeholders = [sql.Placeholder(col) for col in columns]
                insert_sql = sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
                    sql.Identifier(table_name),
                    sql.SQL(', ').join(col_identifiers),
                    sql.SQL(', ').join(placeholders)
                )
                cursor.execute(insert_sql, row)
        else:
            # Regular INSERT
            for row in data:
                placeholders = [sql.Placeholder(col) for col in columns]
                insert_sql = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(table_name),
                    sql.SQL(', ').join(col_identifiers),
                    sql.SQL(', ').join(placeholders)
                )
                cursor.execute(insert_sql, row)
    
    def _insert_partition(self, part_key, data: List[Dict], partition: Partition, 
                         replace=False, ignore=False):
        """Insert data into a specific partition."""
        if self.partition_mode == self.PARTITION_MODE_NATIVE:
            self._insert_into_native_partition(part_key, data, partition, replace=replace, ignore=ignore)
        else:
            self._insert_into_file_partition(part_key, data, partition, replace=replace, ignore=ignore)
    
    def _insert_into_native_partition(self, part_key, data: List[Dict], partition: Partition,
                                    replace=False, ignore=False):
        """Insert into a native PostgreSQL partition.
        
        Creates the partition if it doesn't exist.
        """
        conn = self.connect()
        cursor = conn.cursor()
        
        # Create partition if not exists
        partition_name = f'{self._table_name}_p_{part_key}'
        start_ts, end_ts = partition.get_range(part_key)
        
        create_part_sql = sql.SQL(
            'CREATE TABLE IF NOT EXISTS {} PARTITION OF {} '
            'FOR VALUES FROM ({}) TO ({})'
        ).format(
            sql.Identifier(partition_name),
            sql.Identifier(self._table_name),
            sql.Placeholder(),
            sql.Placeholder()
        )
        
        try:
            cursor.execute(create_part_sql, (start_ts, end_ts))
            if self.verbose >= 2:
                logger.info(f'Created partition {partition_name}')
        except psycopg2.errors.DuplicateTable:
            if self.verbose >= 2:
                logger.info(f'Partition {partition_name} already exists')
        
        # Insert data
        self._insert_into_table(data, partition_name, replace=replace, ignore=ignore)
    
    def _insert_into_file_partition(self, part_key, data: List[Dict], partition: Partition,
                                   replace=False, ignore=False):
        """Insert into a file-per-partition (stores as local SQLite file).
        
        This is useful for syncing data between PostgreSQL and SQLite.
        """
        # For file-per-partition, we delegate to SQLite
        from ..utils.mysqlite import Database as MySQLiteDB
        
        # Create a SQLite file for this partition
        partition_file = self._get_partition_file_path(part_key)
        os.makedirs(os.path.dirname(partition_file), exist_ok=True)
        
        db = MySQLiteDB(partition_file, auto_commit=True)
        
        # Ensure table exists
        if len(data) > 0:
            columns = list(data[0].keys())
            col_defs = [{'name': col, 'type': 'TEXT'} for col in columns]  # Simplified - could infer types
            
            db[self._table_name].create_table(col_defs)
            db[self._table_name].insert_all(data, replace=replace, ignore=ignore)
    
    def _get_partition_file_path(self, part_key) -> str:
        """Get the file path for a file-per-partition."""
        # For PostgreSQL file-per-partition, we store in a subdirectory
        base_dir = os.path.expanduser('~/.iosql/postgresql')
        db_name = self._uri_options.get('db', 'default')
        return os.path.join(base_dir, db_name, self._table_name, f'{part_key}.sqlite3')
    
    def select_all(self, fields, where, limit=None, **kwargs):
        """Query all data matching the conditions."""
        return self.select(fields, where, limit=limit, **kwargs)
    
    def select(self, fields, where, limit=None, **kwargs):
        """Query data with where conditions."""
        conn = self.connect()
        cursor = conn.cursor()
        
        # Build SELECT statement
        if fields == '*':
            field_str = '*'
        elif isinstance(fields, list):
            field_str = ', '.join(fields)
        else:
            field_str = fields
        
        query = f'SELECT {field_str} FROM {self._table_name}'
        
        if where:
            query += f' WHERE {where}'
        
        if limit:
            query += f' LIMIT {limit}'
        
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        return results
    
    def upsert_all(self, kv_list, pk, where=None, partition=None, commit=True, **kwargs):
        """Upsert (insert or update) multiple rows."""
        if isinstance(kv_list, dict):
            kv_list = [kv_list]
        
        # Select existing data
        existing = self.select_all('*', where=where) if where else []
        
        # Build map by pk
        existing_map = {tuple([item[k] for k in pk]): item for item in existing}
        
        for dic in kv_list:
            key = tuple([dic[k] for k in pk])
            if key in existing_map:
                existing_map[key] = {**existing_map[key], **dic}
            else:
                existing_map[key] = dic
        
        # Insert updated data
        self.insert_all(list(existing_map.values()), pk=pk, replace=True, 
                      partition=partition, **kwargs)
    
    def delete(self, where):
        """Delete rows matching conditions."""
        conn = self.connect()
        cursor = conn.cursor()
        
        query = f'DELETE FROM {self._table_name}'
        if where:
            query += f' WHERE {where}'
        
        cursor.execute(query)
    
    def delete_all(self):
        """Delete all rows."""
        self.delete(where=None)
    
    def create_index(self, column, order='asc', exist_ok=True):
        """Create an index on a column."""
        conn = self.connect()
        cursor = conn.cursor()
        
        order_str = 'DESC' if order.lower() == 'desc' else 'ASC'
        index_name = f'{self._table_name}_{column}_idx'
        
        query = f'CREATE INDEX IF NOT EXISTS {index_name} ON {self._table_name} ({column} {order_str})'
        
        cursor.execute(query)
        if self.verbose >= 1:
            logger.info(f'Created index {index_name}')
    
    def suggest_column_types(self, data, column_type, n_suggest_check=None):
        """Suggest column types based on data."""
        # Simplified implementation
        suggested = {}
        for key, value in data[0].items():
            if column_type and key in column_type:
                suggested[key] = column_type[key]
            elif isinstance(value, int):
                suggested[key] = 'BIGINT'
            elif isinstance(value, float):
                suggested[key] = 'DOUBLE PRECISION'
            elif isinstance(value, str):
                suggested[key] = 'TEXT'
            else:
                suggested[key] = 'TEXT'
        return suggested
    
    @property
    def uri(self):
        return self._uri
    
    @property
    def uri_schema(self):
        return self._uri_schema
    
    @property
    def uri_options(self):
        return self._uri_options
    
    @property
    def table_name(self):
        return self._table_name
    
    @property
    def partition(self) -> Partition | None:
        """Get the partition object for this table."""
        return self._partition
    
    def table_parts(self) -> List:
        """Get list of partition keys for this table."""
        if self.partition_mode == self.PARTITION_MODE_NATIVE:
            return self._get_native_partitions()
        else:
            return self._get_file_partitions()
    
    def _get_native_partitions(self) -> List[str]:
        """Get list of native PostgreSQL partitions."""
        conn = self.connect()
        cursor = conn.cursor()
        
        query = """
            SELECT inhrelid::regclass::text 
            FROM pg_inherits 
            WHERE inhparent = %s::regclass
        """
        cursor.execute(query, (self._table_name,))
        return [row[0] for row in cursor.fetchall()]
    
    def _get_file_partitions(self) -> List[str]:
        """Get list of file-per-partition files."""
        partition_dir = os.path.dirname(self._get_partition_file_path(''))
        if not os.path.exists(partition_dir):
            return []
        return [f.replace('.sqlite3', '') for f in os.listdir(partition_dir) if f.endswith('.sqlite3')]
