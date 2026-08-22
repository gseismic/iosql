from __future__ import annotations

import os
import re
import math
import warnings
from typing import Any, Dict, List, Optional
import arrow
import psycopg2
from psycopg2 import sql

from ...table import BaseTable, resolve_select_fields
from ...partition import Partition, DurationPartition, CalendarPartition
from ...core.query import Where
from ...exception import BadArgument, InvalidUriSchema
from ...utils.schema import parse_db_schema
from ...logger import default_logger as logger
from ...spec import TableContract, TableMigrationResult, WriteResult
from ..sqlite.query_util import (
    _quote_sql_identifier,
    build_or_cases_subquery,
    build_select_fields_sql,
)


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
        self._contract = None
        self._primary_key = []
        self._declared_column_types = {}
        self._table_created = False
        
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

    def rename(self, new_name, exist_ok=False, overwrite=False):
        """Rename the PostgreSQL table and matching native partition children."""
        new_name = self._validate_relation_name(new_name)
        old_name = self._table_name
        if new_name == old_name:
            if exist_ok:
                return self
            raise BadArgument('rename target is the current table')
        if overwrite and exist_ok:
            raise BadArgument('rename cannot use overwrite and exist_ok together')

        conn = self.connect()
        cursor = conn.cursor()
        if not self._relation_exists(old_name):
            raise BadArgument(f'table does not exist: {old_name}')
        if self._relation_exists(new_name):
            if not overwrite:
                raise BadArgument(f'table already exists: {new_name}')
            cursor.execute(
                sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(new_name))
            )

        child_renames = self._native_partition_child_renames(old_name, new_name)
        for _, target_child in child_renames:
            if self._relation_exists(target_child):
                if not overwrite:
                    raise BadArgument(f'table already exists: {target_child}')
                cursor.execute(
                    sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(target_child))
                )

        cursor.execute(
            sql.SQL('ALTER TABLE {} RENAME TO {}').format(
                sql.Identifier(old_name),
                sql.Identifier(new_name),
            )
        )
        for old_child, new_child in child_renames:
            cursor.execute(
                sql.SQL('ALTER TABLE {} RENAME TO {}').format(
                    sql.Identifier(old_child),
                    sql.Identifier(new_child),
                )
            )

        self._table_name = new_name
        self._table_created = True
        return self

    def _validate_relation_name(self, name):
        value = str(name or '').strip()
        if not value:
            raise BadArgument('table name must not be empty')
        if '\x00' in value or '.' in value:
            raise BadArgument(f'invalid table name: {name}')
        return value

    def _relation_exists(self, name):
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            select 1
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s
              and c.relname = %s
              and c.relkind in ('r', 'p')
            limit 1
            """,
            ('public', name),
        )
        return cursor.fetchone() is not None

    def _native_partition_child_renames(self, old_name, new_name):
        if self.partition_mode != self.PARTITION_MODE_NATIVE:
            return []
        old_prefix = f'{old_name}_p_'
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            select child.relname
            from pg_inherits i
            join pg_class parent on parent.oid = i.inhparent
            join pg_class child on child.oid = i.inhrelid
            join pg_namespace n on n.oid = child.relnamespace
            where n.nspname = %s
              and parent.relname = %s
            order by child.relname
            """,
            ('public', old_name),
        )
        renames = []
        for (child_name,) in cursor.fetchall():
            if child_name.startswith(old_prefix):
                renames.append((child_name, f'{new_name}{child_name[len(old_name):]}'))
        return renames
    
    def pragma(self, key, value=None):
        """PostgreSQL does not use PRAGMA, but we keep the interface for compatibility."""
        pass

    def use_contract(self, contract):
        contract = TableContract.ensure(contract)
        self._contract = contract
        self._partition = contract.to_partition()
        self._primary_key = list(contract.primary_key)
        self._declared_column_types = dict(contract.columns or {})
        self.create_table(
            contract.columns,
            pk=contract.primary_key,
            partition=self._partition,
        )
        self._table_created = True
        return self

    def use_spec(self, spec):
        return self.use_contract(spec)

    def spec(self):
        return self._contract
    
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
        
        self._declared_column_types.update(dict(column_type or {}))
        self._primary_key = list(pk or [])
        columns_def = []
        for col_name, col_type in column_type.items():
            col_def = (
                sql.Identifier(col_name)
                + sql.SQL(' ')
                + sql.SQL(self._pg_column_type(col_type))
            )
            columns_def.append(col_def)
        if pk:
            columns_def.append(
                sql.SQL('PRIMARY KEY ({})').format(
                    sql.SQL(', ').join(sql.Identifier(col) for col in pk)
                )
            )
        
        if partition is not None:
            if self.partition_mode == self.PARTITION_MODE_NATIVE:
                self._create_native_partitioned_table(columns_def, pk, partition)
            else:
                self._create_file_partitioned_table(columns_def, pk, partition)
        else:
            self._create_simple_table(columns_def, pk)
        self._table_created = True

    def _pg_column_type(self, col_type):
        value = str(col_type or 'text').strip().lower()
        mapping = {
            'int': 'BIGINT',
            'integer': 'BIGINT',
            'bigint': 'BIGINT',
            'float': 'DOUBLE PRECISION',
            'real': 'DOUBLE PRECISION',
            'double': 'DOUBLE PRECISION',
            'double precision': 'DOUBLE PRECISION',
            'text': 'TEXT',
            'str': 'TEXT',
            'string': 'TEXT',
            'bool': 'BOOLEAN',
            'boolean': 'BOOLEAN',
            'date': 'DATE',
            'timestamp': 'TIMESTAMP',
            'json': 'JSONB',
        }
        return mapping.get(value, value.upper())
    
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

    def write(self, rows, mode=None, commit=True, **kwargs):
        if isinstance(rows, dict):
            rows = [rows]
        elif not isinstance(rows, (list, tuple, set)):
            raise BadArgument('write: `list/tuple/set/dict` required for rows')
        rows = list(rows)
        if not rows:
            return WriteResult()

        spec = self.spec()
        if not spec:
            raise BadArgument(
                f"表 `{self._table_name}` 没有持久化契约，无法 write()。"
                "修复: db.table(...) 传入建表参数（pk/columns/partition...）重新声明，"
                "或 db.table(..., spec=...) 绑定已有契约"
            )
        if mode is None:
            mode = spec.conflict
        mode = str(mode or 'append').lower()
        if mode not in ['append', 'upsert', 'replace', 'ignore']:
            raise BadArgument(f'Unsupported write mode `{mode}`')

        return self.insert_all(
            rows,
            pk=spec.primary_key,
            partition=spec.to_partition(),
            replace=mode in ['upsert', 'replace'],
            ignore=mode == 'ignore',
            commit=commit,
            column_type=dict(spec.columns or {}),
            **kwargs,
        )

    def do_insert_all(self, data, pk, partition=None, replace=False,
                      ignore=False, commit=True, **kwargs):
        return self.insert_all(
            data,
            pk=pk,
            partition=partition,
            replace=replace,
            ignore=ignore,
            commit=commit,
            **kwargs,
        )

    def do_create_table(self, createtable_meta, alter=False):
        columns = {
            key: item.get('type', 'TEXT')
            for key, item in (createtable_meta.get('column_info') or {}).items()
        }
        return self.create_table(columns, pk=createtable_meta.get('pk') or [])
    
    def insert_all(self, data, pk=None, extra_data=None, partition=None,
                   replace=False, ignore=False, commit=True, **kwargs):
        """Insert multiple rows with partitioning."""
        if replace and ignore:
            raise BadArgument(
                'insert_all: replace 与 ignore 互斥，只能传一个；'
                '高层写入请使用 write(rows, mode=...)'
            )
        if not data:
            return WriteResult()
        if isinstance(data, dict):
            data = [data]
        data = list(data)
        
        if partition is None:
            partition = self._partition
        if pk is None:
            pk = self._primary_key
        
        # Add extra data if provided
        if extra_data:
            data = [{**item, **extra_data} for item in data]

        if not self._table_created:
            column_types = kwargs.get('column_type') or self.suggest_column_types(
                data,
                column_type={},
            )
            self.create_table(column_types, pk=pk, partition=partition)

        partitioned_data = None
        if partition is None:
            # No partitioning - insert all into single table
            self._insert_into_table(
                data,
                self._table_name,
                pk=pk,
                replace=replace,
                ignore=ignore,
            )
        else:
            # Partition data and insert into appropriate partitions
            partitioned_data: Dict[Any, List] = {}
            for row in data:
                key = partition.get_key(row)
                if key not in partitioned_data:
                    partitioned_data[key] = []
                partitioned_data[key].append(row)
            
            for part_key, part_rows in partitioned_data.items():
                self._insert_partition(
                    part_key,
                    part_rows,
                    partition,
                    pk=pk,
                    replace=replace,
                    ignore=ignore,
                )
        return WriteResult(
            rows_seen=len(data),
            rows_written=None if ignore else len(data),
            partitions_touched=(
                list(partitioned_data)
                if partitioned_data is not None
                else [None]
            ),
        )
    
    def _insert_into_table(self, data: List[Dict], table_name: str, pk=None,
                           replace=False, ignore=False):
        """Insert data into a specific table."""
        if not data:
            return
        
        conn = self.connect()
        cursor = conn.cursor()
        
        columns = list(data[0].keys())
        col_identifiers = [sql.Identifier(col) for col in columns]
        
        if replace:
            if not pk:
                raise BadArgument('PostgreSQL replace/upsert requires primary key')
            # Use ON CONFLICT DO UPDATE
            for row in data:
                placeholders = [sql.Placeholder(col) for col in columns]
                insert_sql = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(table_name),
                    sql.SQL(', ').join(col_identifiers),
                    sql.SQL(', ').join(placeholders)
                )
                update_parts = [
                    sql.Identifier(col) + sql.SQL(' = EXCLUDED.') + sql.Identifier(col)
                    for col in columns
                    if col not in pk
                ]
                if update_parts:
                    insert_sql += sql.SQL(' ON CONFLICT ({}) DO UPDATE SET {}').format(
                        sql.SQL(', ').join(sql.Identifier(col) for col in pk),
                        sql.SQL(', ').join(update_parts),
                    )
                else:
                    insert_sql += sql.SQL(' ON CONFLICT ({}) DO NOTHING').format(
                        sql.SQL(', ').join(sql.Identifier(col) for col in pk)
                    )
                
                cursor.execute(insert_sql, row)
        elif ignore:
            # Use ON CONFLICT DO NOTHING
            for row in data:
                placeholders = [sql.Placeholder(col) for col in columns]
                insert_sql = sql.SQL(
                    'INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING'
                ).format(
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
                         pk=None,
                         replace=False, ignore=False):
        """Insert data into a specific partition."""
        if self.partition_mode == self.PARTITION_MODE_NATIVE:
            self._insert_into_native_partition(
                part_key,
                data,
                partition,
                pk=pk,
                replace=replace,
                ignore=ignore,
            )
        else:
            self._insert_into_file_partition(
                part_key,
                data,
                partition,
                replace=replace,
                ignore=ignore,
            )
    
    def _insert_into_native_partition(self, part_key, data: List[Dict], partition: Partition,
                                    pk=None,
                                    replace=False, ignore=False):
        """Insert into a native PostgreSQL partition.
        
        Creates the partition if it doesn't exist.
        """
        conn = self.connect()
        cursor = conn.cursor()
        
        # Create partition if not exists
        partition_name = self._partition_table_name(part_key)
        start_bound, end_bound = self._partition_bounds(
            partition,
            part_key,
            rows=data,
        )
        
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
            cursor.execute(create_part_sql, (start_bound, end_bound))
            if self.verbose >= 2:
                logger.info(f'Created partition {partition_name}')
        except psycopg2.errors.DuplicateTable:
            if self.verbose >= 2:
                logger.info(f'Partition {partition_name} already exists')
        
        # Insert through the parent so PostgreSQL routes and enforces parent constraints.
        self._insert_into_table(
            data,
            self._table_name,
            pk=pk,
            replace=replace,
            ignore=ignore,
        )

    def _partition_bounds(self, partition: Partition, part_key, rows=None):
        if isinstance(partition, CalendarPartition):
            return self._calendar_partition_bounds(partition, part_key, rows)
        return partition.get_range(part_key)

    def _calendar_partition_bounds(
        self,
        partition: CalendarPartition,
        part_key,
        rows=None,
    ):
        start = arrow.get(str(part_key), partition.fmt, tzinfo=partition.tz)
        end = start.shift(**partition._shift_kwargs())
        column_type = self._partition_column_type(partition)
        normalized = str(column_type or 'text').strip().lower()

        if normalized in ['date']:
            return start.format('YYYY-MM-DD'), end.format('YYYY-MM-DD')

        if normalized in [
            'timestamp',
            'datetime',
            'timestamp without time zone',
            'timestamp with time zone',
            'timestamptz',
        ]:
            return (
                start.format('YYYY-MM-DD HH:mm:ss'),
                end.format('YYYY-MM-DD HH:mm:ss'),
            )

        return self._calendar_text_bounds(partition, start, end, rows)

    def _partition_column_type(self, partition: Partition):
        column = partition.TS_COL
        if self._contract and self._contract.columns:
            if column in self._contract.columns:
                return self._contract.columns[column]
        return self._declared_column_types.get(column, 'text')

    def _calendar_text_bounds(self, partition, start, end, rows=None):
        sample = self._calendar_sample_value(partition, rows)
        if sample is not None and re.fullmatch(r'\d+', str(sample)):
            return start.format(partition.fmt), end.format(partition.fmt)

        separator = 'T' if sample is not None and 'T' in str(sample) else ' '
        unit = getattr(partition, '_every_unit', 'day')
        if unit == 'year':
            fmt = 'YYYY'
        elif unit == 'month':
            fmt = 'YYYY-MM'
        elif unit in ['week', 'day']:
            fmt = 'YYYY-MM-DD'
        elif unit == 'hour':
            fmt = f'YYYY-MM-DD{separator}HH:00:00'
        else:
            fmt = f'YYYY-MM-DD{separator}HH:mm:00'
        return start.format(fmt), end.format(fmt)

    def _calendar_sample_value(self, partition, rows):
        for row in rows or []:
            if partition.TS_COL in row:
                value = row[partition.TS_COL]
                if value is not None:
                    return value
        return None

    def _partition_table_name(self, part_key):
        suffix = re.sub(r'[^0-9A-Za-z_]+', '_', str(part_key))
        suffix = suffix.strip('_') or 'all'
        if suffix[0].isdigit():
            suffix = f'p_{suffix}'
        return f'{self._table_name}_p_{suffix}'
    
    def _insert_into_file_partition(self, part_key, data: List[Dict], partition: Partition,
                                   replace=False, ignore=False):
        """Insert into a file-per-partition (stores as local SQLite file).
        
        This is useful for syncing data between PostgreSQL and SQLite.
        """
        # For file-per-partition, we delegate to SQLite
        from ...utils.mysqlite import Database as MySQLiteDB
        
        # Create a SQLite file for this partition
        partition_file = self._get_partition_file_path(part_key)
        os.makedirs(os.path.dirname(partition_file), exist_ok=True)
        
        db = MySQLiteDB(partition_file, auto_commit=True)
        try:
            # Ensure table exists
            if len(data) > 0:
                columns = list(data[0].keys())
                col_defs = [{'name': col, 'type': 'TEXT'} for col in columns]  # Simplified - could infer types
                
                db[self._table_name].create_table(col_defs)
                db[self._table_name].insert_all(data, replace=replace, ignore=ignore)
        finally:
            db.close()
    
    def _get_partition_file_path(self, part_key) -> str:
        """Get the file path for a file-per-partition."""
        # For PostgreSQL file-per-partition, we store in a subdirectory
        base_dir = os.path.expanduser('~/.iosql/postgresql')
        db_name = self._uri_options.get('db', 'default')
        return os.path.join(base_dir, db_name, self._table_name, f'{part_key}.sqlite3')
    
    def select_all(self, fields='*', where=None, limit=None, **kwargs):
        warnings.warn(
            "select_all() 已废弃：等价于 select(fields, where, to_dict=True)，"
            "请改用 select(to_dict=True)",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.select(fields, where, limit=limit, to_dict=True, **kwargs)

    def select(self, fields='*', where=None, limit=None, order_by=None,
               to_dict=True, columns=None, **kwargs):
        """Query data with where conditions."""
        fields = resolve_select_fields(fields, columns, 'select')
        query, params = self._select_query(
            fields,
            where,
            order_by=order_by,
            limit=limit,
        )
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(query, params)
        columns_ = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        if not to_dict:
            return rows
        return [dict(zip(columns_, row)) for row in rows]

    def iter_select(self, fields='*', where=None, limit=None, order_by=None,
                    to_dict=True, batch_size=10_000, columns=None, **kwargs):
        fields = resolve_select_fields(fields, columns, 'iter_select')
        for batch in self.iter_select_batches(
            fields,
            where,
            limit=limit,
            order_by=order_by,
            to_dict=to_dict,
            batch_size=batch_size,
            **kwargs,
        ):
            for row in batch:
                yield row

    def iter_select_batches(self, fields='*', where=None, limit=None, order_by=None,
                            to_dict=True, batch_size=10_000, columns=None, **kwargs):
        fields = resolve_select_fields(fields, columns, 'iter_select_batches')
        batch_size = self._normalize_batch_size(batch_size)
        limit = self._normalize_limit(limit)
        if limit == 0:
            return

        emitted = 0
        offset = 0
        while True:
            current_limit = batch_size
            if limit is not None:
                remaining = limit - emitted
                if remaining <= 0:
                    break
                current_limit = min(batch_size, remaining)

            query, params = self._select_query(
                fields,
                where,
                order_by=order_by,
                limit=current_limit,
                offset=offset,
            )
            conn = self.connect()
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            if not rows:
                break
            if to_dict:
                columns = [desc[0] for desc in cursor.description]
                rows = [dict(zip(columns, row)) for row in rows]
            yield rows
            emitted += len(rows)
            if len(rows) < current_limit:
                break
            offset += len(rows)

    def count_rows(self):
        query, params = self._select_query("count(*)", None)
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(query, params)
        if hasattr(cursor, "fetchone"):
            row = cursor.fetchone()
        else:
            rows = cursor.fetchall()
            row = rows[0] if rows else None
        return int(row[0]) if row else 0

    def iter_rows(self, batch_size=10_000):
        yield from self.iter_select_batches(
            '*',
            where=None,
            batch_size=batch_size,
            to_dict=True,
        )

    def migrate_to(self, target_uri, target_table=None, mode='append',
                   batch_size=10_000, verify=None, dry_run=False):
        spec = self.spec()
        if not spec:
            raise BadArgument('migrate_to requires a TableContract')
        table_name = target_table or self._table_name
        if dry_run:
            return self._migration_dry_run_result(
                target_uri=target_uri,
                target_name=table_name,
                batch_size=batch_size,
                verify=verify,
            )

        from ...database import Database

        target_db = Database(target_uri)
        try:
            target = target_db.table(table_name, spec=spec)
            return self._migrate_to_target_table(
                target,
                target_uri=target_uri,
                target_name=table_name,
                mode=mode,
                batch_size=batch_size,
                verify=verify,
            )
        finally:
            target_db.close()

    def _migrate_to_target_table(self, target_table, target_uri=None,
                                 target_name=None, mode='append',
                                 batch_size=10_000, verify=None):
        batch_size = self._normalize_batch_size(batch_size)
        verify = self._normalize_migration_verify(verify)

        source_rows = self.count_rows() if verify == 'row-count' else None
        rows_copied = 0
        batches = 0
        for batch in self.iter_rows(batch_size=batch_size):
            if not batch:
                continue
            target_table.write(batch, mode=mode)
            rows_copied += len(batch)
            batches += 1

        result = TableMigrationResult(
            table=self._table_name,
            target_table=target_name or getattr(
                target_table,
                'table_name',
                self._table_name,
            ),
            source_uri=self._uri,
            target_uri=target_uri,
            rows_copied=rows_copied,
            batches=batches,
            source_rows=source_rows,
            verify=verify,
        )
        if verify == 'row-count':
            target_rows = target_table.count_rows()
            result.target_rows = target_rows
            result.verified = (
                source_rows == rows_copied
                and target_rows == source_rows
            )
            if not result.verified:
                raise BadArgument(
                    'row-count verification failed: '
                    f'table={self._table_name}, source_rows={source_rows}, '
                    f'rows_copied={rows_copied}, target_rows={target_rows}'
                )
        return result

    def _migration_dry_run_result(self, target_uri=None, target_name=None,
                                  batch_size=10_000, verify=None):
        batch_size = self._normalize_batch_size(batch_size)
        verify = self._normalize_migration_verify(verify)
        source_rows = self.count_rows()
        return TableMigrationResult(
            table=self._table_name,
            target_table=target_name or self._table_name,
            source_uri=self._uri,
            target_uri=target_uri,
            rows_copied=0,
            batches=math.ceil(source_rows / batch_size) if source_rows else 0,
            source_rows=source_rows,
            target_rows=None,
            verify=verify,
            verified=None,
            dry_run=True,
        )

    def _normalize_migration_verify(self, verify):
        if verify in [None, False, '']:
            return None
        value = str(verify).strip().lower()
        if value in ['none', 'false', '0']:
            return None
        if value != 'row-count':
            raise BadArgument('verify only supports row-count')
        return value

    def _select_query(self, fields, where=None, order_by=None,
                      limit=None, offset=None):
        column_types = self._column_types()
        field_sql = build_select_fields_sql(fields)
        query = f'SELECT {field_sql} FROM {_quote_sql_identifier(self._table_name)}'
        params = []

        where_sql, where_params = self._where_sql(where, column_types)
        if where_sql:
            query += f' WHERE {where_sql}'
            params.extend(where_params)

        order_sql = self._order_by_sql(order_by, column_types)
        if order_sql:
            query += f' ORDER BY {order_sql}'

        normalized_limit = self._normalize_limit(limit)
        if normalized_limit is not None:
            query += ' LIMIT %s'
            params.append(normalized_limit)
        if offset is not None:
            offset = int(offset)
            if offset < 0:
                raise BadArgument('offset must be non-negative')
            query += ' OFFSET %s'
            params.append(offset)
        return query, params

    def _where_sql(self, where, column_types):
        where_obj = Where(where)
        if not where_obj.or_and_cases:
            return '', []
        query = build_or_cases_subquery(where_obj.or_and_cases, column_types)
        return query.sql.replace('?', '%s'), list(query.params)

    def _column_types(self):
        if self._contract and self._contract.columns:
            return dict(self._contract.columns)

        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            select column_name, data_type
            from information_schema.columns
            where table_schema = %s
              and table_name = %s
            order by ordinal_position
            """,
            ('public', self._table_name),
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _normalize_limit(self, limit):
        if limit is None:
            return None
        limit = int(limit)
        if limit < 0:
            raise BadArgument('limit must be non-negative')
        return limit

    def _normalize_batch_size(self, batch_size):
        if isinstance(batch_size, bool):
            raise BadArgument('batch_size must be a positive integer')
        batch_size = int(batch_size)
        if batch_size <= 0:
            raise BadArgument('batch_size must be a positive integer')
        return batch_size

    def _parse_order_by(self, order_by):
        if not order_by:
            return []

        items = order_by
        if isinstance(order_by, str):
            items = [item.strip() for item in order_by.split(',') if item.strip()]
        elif not isinstance(order_by, (list, tuple)):
            raise BadArgument('order_by must be a string/list/tuple')

        specs = []
        for item in items:
            if isinstance(item, str):
                parts = item.strip().split()
                if len(parts) == 1:
                    field, direction = parts[0], 'asc'
                elif len(parts) == 2:
                    field, direction = parts
                else:
                    raise BadArgument(f'Bad order_by item `{item}`')
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                field, direction = item
            else:
                raise BadArgument(f'Bad order_by item `{item}`')

            direction = str(direction).lower()
            if direction not in ['asc', 'desc']:
                raise BadArgument(f'Bad order direction `{direction}`')
            field = str(field).strip().strip('"').strip("'")
            if not field:
                raise BadArgument('order_by field is empty')
            specs.append((field, direction))
        return specs

    def _order_by_sql(self, order_by, column_types):
        specs = self._parse_order_by(order_by)
        parts = []
        for field, direction in specs:
            if column_types and field not in column_types:
                raise BadArgument(f'Unknown order_by field `{field}`')
            parts.append(
                f'{_quote_sql_identifier(field)} {direction.upper()}'
            )
        return ', '.join(parts)
    
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
        """Delete rows matching conditions.

        where 与 select 同源：dict/list/Case/文本四种形式统一经 Where
        参数化编译，不做原始 SQL 拼接；None 等价 delete_all。
        """
        conn = self.connect()
        cursor = conn.cursor()

        query = sql.SQL('DELETE FROM {}').format(sql.Identifier(self._table_name))
        params = []
        if where:
            where_sql, params = self._where_sql(where, self._column_types())
            if where_sql:
                query += sql.SQL(' WHERE ') + sql.SQL(where_sql)

        cursor.execute(query, params)
    
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

    def drop_index(self, column):
        raise NotImplementedError("drop_index not implemented for postgresql engine")
    
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
