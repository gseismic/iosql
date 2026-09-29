from __future__ import annotations

import re
import math
import uuid
import hashlib
import warnings
import itertools
import time
from contextlib import contextmanager
from typing import Any, Dict, List
import arrow
import psycopg2
from psycopg2 import sql

from ...table import BaseTable, resolve_select_fields
from ...partition import Partition, CalendarPartition
from ...core.query import Where
from ...exception import BadArgument, SchemaConflict
from ...utils.schema import parse_db_schema
from ...logger import default_logger as logger
from ...spec import TableContract, TableMigrationResult, WriteResult
from ..sqlite.query_util import (
    _quote_sql_identifier,
    build_or_cases_subquery,
)


_cursor_seq = itertools.count(1)

# PostgreSQL 标识符字节上限；超长静默截断会导致索引/临时表撞名
_IDENTIFIER_MAX_BYTES = 63
# 单条 INSERT 语句最多绑定的参数量，超出则拆页（远低于 65535 上限）
_INSERT_MAX_PARAMS = 60000

# 契约列类型 -> PostgreSQL DDL 类型的封闭映射：
# 不在表内的类型一律拒绝，避免把任意字符串（含多语句注入）拼进 DDL
_PG_TYPE_MAP = {
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
    'datetime': 'TIMESTAMP',
    'timestamp without time zone': 'TIMESTAMP',
    'timestamptz': 'TIMESTAMPTZ',
    'timestamp with time zone': 'TIMESTAMPTZ',
    'json': 'JSONB',
    'jsonb': 'JSONB',
    'blob': 'BYTEA',
    'bytes': 'BYTEA',
    'bytea': 'BYTEA',
}

# 合法裸列名：字符串 fields 只接受标识符列表，杜绝 SQL 片段
_FIELD_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')
# pg_get_expr(relpartbound) 形如 FOR VALUES FROM ('a') TO ('b')
_PARTITION_BOUNDS_RE = re.compile(r'FROM \((.*)\) TO \((.*)\)', re.IGNORECASE)


def _pg_identifier(name):
    """带参数执行时的标识符引用：转义 % 避免 psycopg2 占位符插值误判。"""
    return _quote_sql_identifier(name).replace('%', '%%')


def _rewrite_placeholders(sql_text):
    """把共享编译器的 `?` 占位符改写为 `%s`。

    双引号标识符内部的 `?`/`%` 原样保留（`%` 需转义成 `%%`），
    避免字段名含 `?`/`%` 时被全局替换破坏。
    """
    out = []
    in_quote = False
    for ch in str(sql_text):
        if ch == '"':
            in_quote = not in_quote
            out.append(ch)
        elif in_quote and ch == '%':
            out.append('%%')
        elif not in_quote and ch == '?':
            out.append('%s')
        else:
            out.append(ch)
    return ''.join(out)


def _fit_identifier(name, suffix):
    """把 `name + suffix` 限制在 PostgreSQL 63 字节内（前缀截断 + 后缀保留）。"""
    raw = f'{name}{suffix}'
    if len(raw.encode('utf-8')) <= _IDENTIFIER_MAX_BYTES:
        return raw
    budget = _IDENTIFIER_MAX_BYTES - len(suffix.encode('utf-8'))
    if budget <= 0:
        return suffix[:_IDENTIFIER_MAX_BYTES]
    prefix = name.encode('utf-8')[:budget].decode('utf-8', errors='ignore')
    return f'{prefix}{suffix}'


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
        if partition_mode not in (self.PARTITION_MODE_NATIVE, self.PARTITION_MODE_FILE):
            raise BadArgument(f'invalid partition_mode: {partition_mode}')
        if partition_mode == self.PARTITION_MODE_FILE:
            raise BadArgument(
                'PostgreSQL file partition mode is not supported: '
                'file-per-partition writes to local SQLite files while reads '
                'query only the PostgreSQL table, causing silent data loss. '
                'Use native partitioning (default).'
            )
        self.partition_mode = partition_mode
        self._partition = partition
        self._conn = None
        self._contract = None
        self._primary_key = []
        self._declared_column_types = {}
        self._table_created = False
        # current_schema() 缓存，避免 public 硬编码
        self._schema = None
        # commit=False 写入会保持事务打开，后续写操作并入同一事务
        self._in_explicit_tx = False
        self._tx_previous_autocommit = True
        
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
            if self._in_explicit_tx:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                self._in_explicit_tx = False
            self._conn.close()

    @contextmanager
    def _transaction(self, commit=True):
        """写事务：commit=True 成功提交/失败回滚；commit=False 保持事务打开。

        保持事务打开时，调用方后续可用 commit=True 的写入或 delete 提交；
        连接关闭时未提交事务自动回滚。
        """
        conn = self.connect()
        if self._in_explicit_tx:
            try:
                yield conn
            except BaseException:
                conn.rollback()
                self._in_explicit_tx = False
                self._set_autocommit(conn, self._tx_previous_autocommit)
                raise
            if commit:
                conn.commit()
                self._in_explicit_tx = False
                self._set_autocommit(conn, self._tx_previous_autocommit)
            return

        previous_autocommit = conn.autocommit
        self._set_autocommit(conn, False)
        self._in_explicit_tx = True
        self._tx_previous_autocommit = previous_autocommit
        try:
            yield conn
        except BaseException:
            conn.rollback()
            self._in_explicit_tx = False
            self._set_autocommit(conn, previous_autocommit)
            raise
        if commit:
            conn.commit()
            self._in_explicit_tx = False
            self._set_autocommit(conn, previous_autocommit)

    @staticmethod
    def _set_autocommit(conn, value):
        try:
            conn.autocommit = value
        except Exception:
            # 事务仍应由调用方显式提交/回滚；连接属性设置失败不掩盖主流程
            pass

    def _schema_name(self):
        """解析当前会话 search_path 的首个有效 schema，兜底 public。"""
        if self._schema is None:
            conn = self.connect()
            cursor = conn.cursor()
            cursor.execute('select current_schema()')
            row = cursor.fetchone()
            self._schema = (row[0] if row else None) or 'public'
        return self._schema

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
        with self._transaction(True):
            cursor = conn.cursor()
            if not self._relation_exists(old_name):
                raise BadArgument(f'table does not exist: {old_name}')

            # 先完成全部可行性检查，再执行任何破坏性语句
            child_renames = self._native_partition_child_renames(old_name, new_name)
            target_parent_exists = self._relation_exists(new_name)
            conflicting_children = [
                target_child
                for _, target_child in child_renames
                if self._relation_exists(target_child)
            ]
            if not overwrite:
                if target_parent_exists:
                    raise BadArgument(f'table already exists: {new_name}')
                if conflicting_children:
                    raise BadArgument(
                        f'table already exists: {conflicting_children[0]}'
                    )

            trash = None
            if target_parent_exists:
                # 目标改名到临时名而非直接 DROP；后续任一语句失败可整体回滚
                trash = self._trash_name(new_name)
                cursor.execute(
                    sql.SQL('ALTER TABLE {} RENAME TO {}').format(
                        sql.Identifier(new_name),
                        sql.Identifier(trash),
                    )
                )
            for target_child in conflicting_children:
                cursor.execute(
                    sql.SQL('DROP TABLE {} CASCADE').format(
                        sql.Identifier(target_child)
                    )
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
            if trash is not None:
                cursor.execute(
                    sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(trash))
                )

        self._table_name = new_name
        self._table_created = True
        owner_ref = getattr(self, '_owner_db', None)
        owner = owner_ref() if owner_ref is not None else None
        if owner is not None:
            owner._on_table_renamed(self, old_name, new_name)
        return self

    def _trash_name(self, name):
        suffix = f'__iosql_old_{uuid.uuid4().hex[:8]}'
        return _fit_identifier(name, suffix)

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
            (self._schema_name(), name),
        )
        return cursor.fetchone() is not None

    def _fetch_table_columns(self):
        """读取已有物理表的列名集合（information_schema）。"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            select column_name
            from information_schema.columns
            where table_schema = %s
              and table_name = %s
            order by ordinal_position
            """,
            (self._schema_name(), self._table_name),
        )
        return {row[0] for row in cursor.fetchall()}

    def _is_partitioned_table(self):
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            select 1
            from pg_partitioned_table pt
            join pg_class c on c.oid = pt.partrelid
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s
              and c.relname = %s
            limit 1
            """,
            (self._schema_name(), self._table_name),
        )
        return cursor.fetchone() is not None

    def _verify_existing_table(self, column_type, partition):
        """已有表漂移检查：缺列/分区形态不符时抛 SchemaConflict。"""
        existing = self._fetch_table_columns()
        declared = list((column_type or {}).keys())
        if existing:
            missing = [col for col in declared if col not in existing]
            if missing:
                raise SchemaConflict(
                    f'表 `{self._table_name}` 已存在但缺少契约列 {missing}；'
                    '物理表与契约不一致。修复: 对齐 columns= 声明、'
                    'DROP TABLE 后重建，或改用新的表名。'
                )
        if partition is not None and not self._is_partitioned_table():
            raise SchemaConflict(
                f'表 `{self._table_name}` 已存在但未启用 native 分区，'
                '而契约声明了 partition。修复: DROP TABLE 后重建，'
                '或去掉 partition 声明。'
            )

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
            (self._schema_name(), old_name),
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
        self._validate_contract_metadata(contract)
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

    def _validate_contract_metadata(self, contract):
        """确保元数据映射只引用契约列，且值域对应 PostgreSQL 数值列。"""
        columns = contract.columns or {}
        declared_columns = set(columns)

        for field_name in ("column_descriptions", "column_ranges"):
            metadata_columns = set(getattr(contract, field_name) or {})
            unknown_columns = sorted(metadata_columns - declared_columns)
            if unknown_columns:
                raise BadArgument(
                    f"{field_name} references undeclared columns "
                    f"{unknown_columns}; include them in columns"
                )

        numeric_pg_types = {"BIGINT", "DOUBLE PRECISION"}
        for column in contract.column_ranges or {}:
            declared_type = columns[column]
            pg_type = self._pg_column_type(declared_type)
            if pg_type not in numeric_pg_types:
                raise BadArgument(
                    f"column_ranges[{column!r}] requires a numeric PostgreSQL "
                    f"column type; got {declared_type!r}"
                )

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
        
        column_type = dict(column_type or {})
        self._declared_column_types.update(column_type)
        self._primary_key = list(pk or [])

        # 先做封闭类型映射校验（未知类型一律拒绝，即使表已存在）
        mapped_types = {
            col_name: self._pg_column_type(col_type)
            for col_name, col_type in column_type.items()
        }

        # 已有表：不再依赖 CREATE TABLE IF NOT EXISTS 静默跳过，先做漂移检查
        if self._relation_exists(self._table_name):
            self._verify_existing_table(column_type, partition)
            self._table_created = True
            return

        columns_def = []
        for col_name, mapped_type in mapped_types.items():
            col_def = (
                sql.Identifier(col_name)
                + sql.SQL(' ')
                + sql.SQL(mapped_type)
            )
            columns_def.append(col_def)
        if pk:
            columns_def.append(
                sql.SQL('PRIMARY KEY ({})').format(
                    sql.SQL(', ').join(sql.Identifier(col) for col in pk)
                )
            )
        
        if partition is not None:
            self._create_native_partitioned_table(columns_def, pk, partition)
        else:
            self._create_simple_table(columns_def, pk)
        self._table_created = True

    def _pg_column_type(self, col_type):
        """封闭类型映射：未知类型直接拒绝，绝不透传用户字符串。"""
        value = str(col_type or 'text').strip().lower()
        mapped = _PG_TYPE_MAP.get(value)
        if mapped is None:
            allowed = ', '.join(sorted(set(_PG_TYPE_MAP)))
            raise BadArgument(
                f'不支持的 PostgreSQL 列类型 `{col_type}`；'
                f'合法类型: {allowed}'
            )
        return mapped
    
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
        if isinstance(pk, str):
            pk = [pk]
        
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
        # 整批写入放在一个显式事务中：失败不留半批脏数据
        with self._transaction(commit):
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
    
    def _union_columns(self, data: List[Dict]):
        """按首次出现顺序取整批行的列并集；缺列在参数里补 None。

        避免旧实现按 data[0] 取列造成的静默丢列/KeyError。
        """
        columns = []
        seen = set()
        for row in data:
            if not isinstance(row, dict):
                raise BadArgument(
                    'insert_all: 每行必须是 dict，'
                    f'得到 {type(row).__name__}: {row!r}'
                )
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        if not columns:
            raise BadArgument('insert_all: 数据行不能是空 dict')
        return columns

    def _insert_into_table(self, data: List[Dict], table_name: str, pk=None,
                           replace=False, ignore=False):
        """Insert data into a specific table (multi-row VALUES 分页批量)。"""
        if not data:
            return

        if replace and not pk:
            raise BadArgument('PostgreSQL replace/upsert requires primary key')

        conn = self.connect()
        cursor = conn.cursor()

        columns = self._union_columns(data)
        col_identifiers = ', '.join(_pg_identifier(col) for col in columns)
        param_rows = [
            [row.get(col) for col in columns]
            for row in data
        ]
        page_size = max(1, _INSERT_MAX_PARAMS // max(1, len(columns)))

        for start in range(0, len(param_rows), page_size):
            page = param_rows[start:start + page_size]
            values_sql = ', '.join(
                '(' + ', '.join(['%s'] * len(columns)) + ')'
                for _ in page
            )
            insert_sql = (
                f'INSERT INTO {_pg_identifier(table_name)} '
                f'({col_identifiers}) VALUES {values_sql}'
            )
            if replace:
                update_parts = [
                    f'{_pg_identifier(col)} = EXCLUDED.{_pg_identifier(col)}'
                    for col in columns
                    if col not in pk
                ]
                conflict_cols = ', '.join(_pg_identifier(col) for col in pk)
                if update_parts:
                    insert_sql += (
                        f' ON CONFLICT ({conflict_cols}) DO UPDATE SET '
                        + ', '.join(update_parts)
                    )
                else:
                    insert_sql += f' ON CONFLICT ({conflict_cols}) DO NOTHING'
            elif ignore:
                insert_sql += ' ON CONFLICT DO NOTHING'

            flat_params = [value for row in page for value in row]
            cursor.execute(insert_sql, flat_params)
    
    def _insert_partition(self, part_key, data: List[Dict], partition: Partition,
                         pk=None,
                         replace=False, ignore=False):
        """Insert data into a specific native partition."""
        self._insert_into_native_partition(
            part_key,
            data,
            partition,
            pk=pk,
            replace=replace,
            ignore=ignore,
        )
    
    def _insert_into_native_partition(self, part_key, data: List[Dict], partition: Partition,
                                    pk=None,
                                    replace=False, ignore=False):
        """Insert into a native PostgreSQL partition.

        分区不存在则创建；同名分区已存在时校验边界，边界不一致立即报错，
        不再静默跳过（边界重叠/漂移会被显式暴露）。
        """
        conn = self.connect()
        cursor = conn.cursor()
        
        partition_name = self._partition_table_name(part_key)
        start_bound, end_bound = self._partition_bounds(
            partition,
            part_key,
            rows=data,
        )
        existing_bound = self._existing_partition_bounds(partition_name)
        if existing_bound is not None:
            if not self._partition_bounds_equal(existing_bound, start_bound, end_bound):
                raise BadArgument(
                    f'分区 `{partition_name}` 已存在但边界与期望不一致: '
                    f'{existing_bound!r}，期望 FROM ({start_bound!r}) TO ({end_bound!r})。'
                    '修复: 重建该分区，或修正分区键/契约中的时间列与粒度。'
                )
            if self.verbose >= 2:
                logger.info(f'Partition {partition_name} already exists')
        else:
            create_part_sql = sql.SQL(
                'CREATE TABLE {} PARTITION OF {} '
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
            except (
                psycopg2.errors.DuplicateTable,
                psycopg2.errors.UniqueViolation,
            ) as exc:
                raise BadArgument(
                    f'分区 `{partition_name}` 并发创建冲突，请重试'
                ) from exc
            except psycopg2.errors.InvalidObjectDefinition as exc:
                raise BadArgument(
                    f'分区 `{partition_name}` 与现有分区边界冲突: {exc}；'
                    '修复: 清理重叠分区（含 DEFAULT 分区）或调整分区粒度'
                ) from exc
        
        # Insert through the parent so PostgreSQL routes and enforces parent constraints.
        self._insert_into_table(
            data,
            self._table_name,
            pk=pk,
            replace=replace,
            ignore=ignore,
        )

    def _existing_partition_bounds(self, partition_name):
        """读取同名子分区的 pg_get_expr(relpartbound)；不存在返回 None。"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            select pg_get_expr(c.relpartbound, c.oid)
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s
              and c.relname = %s
              and c.relkind = 'r'
            limit 1
            """,
            (self._schema_name(), partition_name),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def _partition_bounds_equal(self, existing_expr, start_bound, end_bound):
        match = _PARTITION_BOUNDS_RE.search(str(existing_expr or ''))
        if not match:
            return False
        return (
            self._bound_value_equal(match.group(1), start_bound)
            and self._bound_value_equal(match.group(2), end_bound)
        )

    @staticmethod
    def _bound_value_equal(raw_value, expected):
        text = str(raw_value or '').strip()
        text = re.sub(r'::[A-Za-z_][A-Za-z0-9_ ]*$', '', text).strip()
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1].replace("''", "'")
        if str(expected) == text:
            return True
        try:
            return float(text) == float(expected)
        except (TypeError, ValueError):
            pass
        try:
            return arrow.get(text) == arrow.get(expected)
        except Exception:
            return False

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

        if normalized in ['timestamp', 'datetime', 'timestamp without time zone']:
            return (
                start.format('YYYY-MM-DD HH:mm:ss'),
                end.format('YYYY-MM-DD HH:mm:ss'),
            )

        if normalized in ['timestamp with time zone', 'timestamptz']:
            # 显式带 UTC 偏移，边界不依赖会话 TimeZone 解释
            return (
                start.to('UTC').format('YYYY-MM-DD HH:mm:ssZZ'),
                end.to('UTC').format('YYYY-MM-DD HH:mm:ssZZ'),
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
            # 粗粒度前缀作为边界：省略秒/分的文本值（'09'、'09:30'）也落在
            # [lower, upper) 内，不会被补全的 ':00:00' 越界排除
            fmt = f'YYYY-MM-DD{separator}HH'
        else:
            fmt = f'YYYY-MM-DD{separator}HH:mm'
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
        self._reject_unknown_kwargs('select', kwargs)
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
                    to_dict=True, batch_size=10_000, columns=None,
                    timeout_ms=None, **kwargs):
        self._reject_unknown_kwargs('iter_select', kwargs)
        fields = resolve_select_fields(fields, columns, 'iter_select')
        for batch in self.iter_select_batches(
            fields,
            where,
            limit=limit,
            order_by=order_by,
            to_dict=to_dict,
            batch_size=batch_size,
            timeout_ms=timeout_ms,
        ):
            for row in batch:
                yield row

    def iter_select_batches(self, fields='*', where=None, limit=None, order_by=None,
                            to_dict=True, batch_size=10_000, columns=None,
                            timeout_ms=None, **kwargs):
        """批量流式查询。

        timeout_ms 为可选正整数迭代期限；None 表示不设期限。期限从首次
        迭代开始计时，包含调用方处理已产出批次的时间；超时回滚该读取事务
        并抛 TimeoutError。显式事务中不能传 timeout_ms。
        """
        self._reject_unknown_kwargs('iter_select_batches', kwargs)
        fields = resolve_select_fields(fields, columns, 'iter_select_batches')
        batch_size = self._normalize_batch_size(batch_size)
        limit = self._normalize_limit(limit)
        timeout_ms = self._normalize_timeout_ms(timeout_ms)
        deadline = (
            time.monotonic() + timeout_ms / 1000
            if timeout_ms is not None else None
        )
        if limit == 0:
            return
        if timeout_ms is not None and self._in_explicit_tx:
            raise BadArgument(
                'timeout_ms is not supported inside an explicit transaction'
            )

        query, params = self._select_query(
            fields,
            where,
            order_by=order_by,
            limit=limit,
        )
        conn = self.connect()

        def _apply_statement_timeout():
            if deadline is None:
                return
            remaining_ms = math.ceil((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError('PostgreSQL query exceeded timeout_ms budget')
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f'{remaining_ms}ms',),
                )
            finally:
                cursor.close()

        def _iterate():
            # 非 WITH HOLD 游标：服务端边取边发，避免服务端物化全量结果
            cursor_name = f'iosql_iter_select_{next(_cursor_seq)}'
            cursor = conn.cursor(name=cursor_name)
            try:
                cursor.itersize = batch_size
                _apply_statement_timeout()
                cursor.execute(query, params)
                # psycopg2 命名游标在首批 FETCH 前 description 为 None。
                column_names = None
                while True:
                    _apply_statement_timeout()
                    rows = cursor.fetchmany(batch_size)
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            'PostgreSQL query exceeded timeout_ms budget'
                        )
                    if not rows:
                        break
                    if to_dict:
                        if column_names is None:
                            column_names = [desc[0] for desc in cursor.description]
                        rows = [dict(zip(column_names, row)) for row in rows]
                    yield rows
            finally:
                try:
                    cursor.close()
                except Exception:
                    # statement_timeout 会回滚整笔只读迭代事务，事务回滚时
                    # PostgreSQL 已销毁 portal；不要让 CLOSE 的次生错误覆盖超时。
                    if deadline is None or time.monotonic() < deadline:
                        raise

        if self._in_explicit_tx:
            # 复用调用方未提交事务，不擅自结束
            yield from _iterate()
            return

        previous_autocommit = conn.autocommit
        self._set_autocommit(conn, False)
        try:
            yield from _iterate()
        except BaseException as exc:
            conn.rollback()
            if timeout_ms is not None and getattr(exc, 'pgcode', None) == '57014':
                raise TimeoutError(
                    'PostgreSQL query exceeded timeout_ms budget'
                ) from exc
            raise
        else:
            conn.commit()
        finally:
            self._set_autocommit(conn, previous_autocommit)

    def count_rows(self):
        query = f'SELECT count(*) FROM {_pg_identifier(self._table_name)}'
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(query, [])
        if hasattr(cursor, "fetchone"):
            row = cursor.fetchone()
        else:
            rows = cursor.fetchall()
            row = rows[0] if rows else None
        return int(row[0]) if row else 0

    def aggregate_by_bucket(self, x_col, value_cols, n_buckets, where=None,
                            group_col=None):
        """SQL 端等宽分桶聚合（PLAN-070，与 sqlite 引擎同名方法语义一致）。

        PG 为强类型存储：x 列模式由列类型决定（numeric 列恒为 numeric 模式；
        text 列用正则探测纯数字文本行数，非全纯数字时按 mixed/non_numeric
        交由调用方报错）。数值列不可能存文本，value_bad_rows 恒 0。
        返回结构与 sqlite 引擎完全一致，桶网格公式相同。
        """
        x_col = str(x_col)
        value_cols = [str(c) for c in (value_cols or [])]
        n_buckets = int(n_buckets)
        if n_buckets <= 0:
            raise BadArgument('n_buckets 必须为正整数')

        def _empty_result():
            result = {
                'scanned': 0,
                'x_min': None,
                'x_max': None,
                'x_numeric_rows': 0,
                'x_digit_text_rows': 0,
                'value_bad_rows': {c: 0 for c in value_cols},
                'bucket_min': None,
                'bucket_width': None,
                'x_mode': 'empty',
                'sums': None,
                'counts': None,
            }
            if group_col is not None:
                result['groups'] = {}
            return result

        column_types = self._column_types()
        if not column_types:
            return _empty_result()
        required_cols = [x_col] + value_cols
        if group_col is not None:
            group_col = str(group_col)
            required_cols.append(group_col)
        for col in required_cols:
            if col not in column_types:
                raise BadArgument(f'Unknown field `{col}`')

        x_sql = _pg_identifier(x_col)
        # 契约声明类型（text/str/string）与 information_schema 类型
        # （text/character varying）都归为文本列
        x_type = str(column_types.get(x_col, '')).lower()
        x_is_text = any(t in x_type for t in ('text', 'char', 'str'))

        for col in value_cols:
            col_type = str(column_types.get(col, '')).lower()
            if any(t in col_type for t in ('text', 'char', 'str', 'bytea')):
                raise BadArgument(
                    f'数值列 `{col}` 是文本类型，无法聚合'
                )

        where_sql, where_params = self._where_sql(where, column_types)

        # ---- 探测：空值数/范围 + text 列的纯数字行数 ----
        if x_is_text:
            digit_expr = f"SUM(CASE WHEN TRIM({x_sql}::text) ~ '^-?[0-9]+$'"\
                ' THEN 1 ELSE 0 END)'
            numeric_min = (
                f"MIN(CASE WHEN TRIM({x_sql}::text) ~ '^-?[0-9]+$' "
                f'THEN TRIM({x_sql}::text)::numeric END)'
            )
            numeric_max = numeric_min.replace('MIN(', 'MAX(', 1)
        else:
            digit_expr = '0'
            numeric_min = numeric_max = 'NULL'
        stats_sql = (
            f'SELECT COUNT({x_sql}), MIN({x_sql}), MAX({x_sql}),'
            f' {digit_expr}, {numeric_min}, {numeric_max}'
            f' FROM {_pg_identifier(self._table_name)}'
        )
        if where_sql:
            stats_sql += f' WHERE {where_sql}'
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(stats_sql, list(where_params))
        stats = cursor.fetchone()
        scanned = int(stats[0] or 0)
        if scanned == 0:
            return _empty_result()

        if not x_is_text:
            x_mode = 'numeric'
            x_min, x_max = stats[1], stats[2]
            digit_rows = 0
        else:
            digit_rows = int(stats[3] or 0)
            if digit_rows == scanned:
                x_mode = 'text'
                grid_min, grid_max = int(stats[4]), int(stats[5])
                if grid_min < -(2**63) or grid_max > 2**63 - 1:
                    raise BadArgument('数字文本横轴超出 PostgreSQL bigint 范围')
                x_min, x_max = str(grid_min), str(grid_max)
            elif digit_rows == 0:
                x_mode = 'non_numeric'
                x_min = x_max = None
            else:
                x_mode = 'mixed'
                x_min = x_max = None

        result = {
            'scanned': scanned,
            'x_min': x_min,
            'x_max': x_max,
            'x_numeric_rows': 0 if x_is_text else scanned,
            'x_digit_text_rows': digit_rows,
            'value_bad_rows': {c: 0 for c in value_cols},
            'bucket_min': None,
            'bucket_width': None,
            'x_mode': x_mode,
            'sums': None,
            'counts': None,
        }
        if x_mode not in ('numeric', 'text'):
            return result

        # ---- 分桶聚合：桶下标 = floor((x - min) / width)，网格与 sqlite 一致 ----
        if x_mode == 'text':
            grid_min = int(str(x_min).strip())
            grid_max = int(str(x_max).strip())
            bucket_expr = f'FLOOR((CAST({x_sql} AS bigint) - %s) / %s)'
        else:
            grid_min = x_min
            grid_max = x_max
            bucket_expr = f'FLOOR(({x_sql} - %s) / %s)'
        span = grid_max - grid_min
        if x_mode == 'text' and span > 2**63 - 1:
            raise BadArgument('数字文本横轴跨度超出 PostgreSQL bigint 范围')
        width = span // n_buckets + 1 if span >= 0 else 1
        select_items = [bucket_expr]
        if group_col is not None:
            select_items.append(_pg_identifier(group_col))
        for col in value_cols:
            c_sql = _pg_identifier(col)
            select_items.append(f'SUM({c_sql})')
            select_items.append(f'COUNT({c_sql})')
        agg_sql = (
            f'SELECT {", ".join(select_items)}'
            f' FROM {_pg_identifier(self._table_name)}'
        )
        if where_sql:
            agg_sql += f' WHERE {where_sql} AND {x_sql} IS NOT NULL'
        else:
            agg_sql += f' WHERE {x_sql} IS NOT NULL'
        agg_sql += ' GROUP BY 1, 2' if group_col is not None else ' GROUP BY 1'
        cursor.execute(agg_sql, [grid_min, width] + list(where_params))
        sums = {c: [0.0] * n_buckets for c in value_cols}
        counts = {c: [0] * n_buckets for c in value_cols}
        grouped = {}
        for row in cursor.fetchall():
            idx = row[0]
            if idx is None:
                continue
            idx = int(idx)
            if idx < 0 or idx >= n_buckets:
                continue  # 防御：越界桶不参与聚合
            if group_col is None:
                sums_for_group = sums
                counts_for_group = counts
                value_offset = 1
            else:
                group_key = row[1]
                group = grouped.setdefault(group_key, {
                    'sums': {c: [0.0] * n_buckets for c in value_cols},
                    'counts': {c: [0] * n_buckets for c in value_cols},
                })
                sums_for_group = group['sums']
                counts_for_group = group['counts']
                value_offset = 2
            for i, c in enumerate(value_cols):
                s = row[value_offset + i * 2]
                n = row[value_offset + i * 2 + 1]
                if s is not None:
                    sums_for_group[c][idx] += float(s)
                if n is not None:
                    counts_for_group[c][idx] += int(n)
        result['bucket_min'] = grid_min
        result['bucket_width'] = width
        result['sums'] = sums
        result['counts'] = counts
        if group_col is not None:
            result['groups'] = grouped
        return result

    def _reject_unknown_kwargs(self, func_name, kwargs):
        if kwargs:
            names = ', '.join(sorted(kwargs))
            raise BadArgument(f'{func_name}() 收到未知参数: {names}')

    def iter_batches(self, batch_size=10_000):
        yield from self.iter_select_batches(
            '*',
            where=None,
            batch_size=batch_size,
            to_dict=True,
        )

    def iter_rows(self, batch_size=10_000):
        for batch in self.iter_batches(batch_size=batch_size):
            yield from batch

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
        for batch in self.iter_batches(batch_size=batch_size):
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

    def _select_query(self, fields, where=None, order_by=None, limit=None):
        column_types = self._column_types()
        field_sql = self._select_fields_sql(fields)
        query = f'SELECT {field_sql} FROM {_pg_identifier(self._table_name)}'
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
        return query, params

    def _select_fields_sql(self, fields):
        """fields 只接受 `*` 或标识符列表；字符串含 SQL 语法直接拒绝。"""
        if fields is None:
            return '*'
        if isinstance(fields, (tuple, list)):
            names = [str(field).strip() for field in fields]
            if not names or any(not name for name in names):
                raise BadArgument('select field is empty')
            if names == ['*']:
                return '*'
            return ', '.join(_pg_identifier(name) for name in names)
        if isinstance(fields, str):
            text = fields.strip().strip(',')
            if not text:
                raise BadArgument('select fields must not be empty')
            if text == '*':
                return '*'
            names = []
            for part in text.split(','):
                name = part.strip()
                if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
                    # 允许显式双引号列名，与共享 fields 编译器保持一致
                    name = name[1:-1].replace('""', '"').strip()
                if not _FIELD_IDENTIFIER_RE.fullmatch(name or ''):
                    raise BadArgument(
                        f'非法 select fields `{fields}`：'
                        '只允许列名或逗号分隔列名列表'
                    )
                names.append(name)
            return ', '.join(_pg_identifier(name) for name in names)
        raise BadArgument('BadFieldType')

    def _where_sql(self, where, column_types):
        where_obj = Where(where)
        if not where_obj.or_and_cases:
            return '', []
        query = build_or_cases_subquery(where_obj.or_and_cases, column_types)
        return _rewrite_placeholders(query.sql), list(query.params)

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
            (self._schema_name(), self._table_name),
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

    @staticmethod
    def _normalize_timeout_ms(timeout_ms):
        if timeout_ms is None:
            return None
        if isinstance(timeout_ms, bool):
            raise BadArgument('timeout_ms must be a positive integer')
        try:
            normalized = int(timeout_ms)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BadArgument('timeout_ms must be a positive integer') from exc
        if normalized <= 0 or normalized != timeout_ms:
            raise BadArgument('timeout_ms must be a positive integer')
        return normalized

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
            # 与 SQLite 对齐：ASC NULLS FIRST / DESC NULLS LAST
            nulls = 'FIRST' if direction == 'asc' else 'LAST'
            parts.append(
                f'{_pg_identifier(field)} {direction.upper()} NULLS {nulls}'
            )
        return ', '.join(parts)
    
    def upsert_all(self, kv_list, pk, partition=None, commit=True, *,
                   where=None, **kwargs):
        """Upsert (insert or update) multiple rows.

        `where` 为历史遗留关键字参数，已废弃并忽略：upsert 以 pk 为身份
        标识，与 SQLite 一致地在全表范围合并既有行。
        """
        if where is not None:
            warnings.warn(
                'upsert_all(where=...) 已废弃并被忽略：upsert 以 pk 为身份标识，'
                '不再按 where 过滤既有行',
                DeprecationWarning,
                stacklevel=2,
            )
        if isinstance(kv_list, dict):
            kv_list = [kv_list]
        elif not isinstance(kv_list, (list, tuple, set)):
            raise BadArgument(
                'upsert_all: `list/tuple/set/dict` required for `kv_list`'
            )
        if not kv_list:
            return
        
        # Select existing data (全表按 pk 合并，不做 where 过滤)
        existing = self.select('*', where=None, to_dict=True)
        
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
                      partition=partition, commit=commit, **kwargs)
    
    def delete(self, where):
        """Delete rows matching conditions.

        where 与 select 同源：dict/list/Case/文本四种形式统一经 Where
        参数化编译，不做原始 SQL 拼接。
        where=None 为显式全表删除（DeprecationWarning），请改用 delete_all()；
        []/{}/""/0/False 等 falsy 非 None 值抛 BadArgument，绝不退化为全表删除。
        返回删除行数（cursor.rowcount），与 SQLite 对齐。
        """
        if where is None:
            warnings.warn(
                'delete(None) 已废弃：等价于全表删除，请显式调用 delete_all()',
                DeprecationWarning,
                stacklevel=2,
            )
            return self.delete_all()

        where_sql, params = self._where_sql(where, self._column_types())
        if not where_sql:
            raise BadArgument(
                'delete: where 不能为空（[]/{}/""）；'
                '删除全表请显式调用 delete_all()'
            )

        conn = self.connect()
        cursor = conn.cursor()
        query = sql.SQL('DELETE FROM {} WHERE {}').format(
            sql.Identifier(self._table_name),
            sql.SQL(where_sql),
        )
        cursor.execute(query, params)
        return cursor.rowcount
    
    def delete_all(self):
        """Delete all rows (explicit full-table delete)."""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            sql.SQL('DELETE FROM {}').format(sql.Identifier(self._table_name))
        )
        return cursor.rowcount
    
    def create_index(self, column, order='asc', exist_ok=True):
        """Create an index on a column."""
        conn = self.connect()
        cursor = conn.cursor()

        order_value = str(order or 'asc').strip().lower()
        if order_value not in ('asc', 'desc'):
            raise BadArgument(f'invalid index order: {order}')
        order_str = 'DESC' if order_value == 'desc' else 'ASC'
        index_name = self._index_name(column)
        create_tpl = (
            'CREATE INDEX IF NOT EXISTS {} ON {} ({} {})'
            if exist_ok else 'CREATE INDEX {} ON {} ({} {})'
        )
        create_sql = sql.SQL(create_tpl).format(
            sql.Identifier(index_name),
            sql.Identifier(self._table_name),
            sql.Identifier(column),
            sql.SQL(order_str),
        )

        cursor.execute(create_sql)
        if self.verbose >= 1:
            logger.info(f'Created index {index_name}')

    def _index_name(self, column):
        raw = f'{self._table_name}_{column}_idx'
        if len(raw.encode('utf-8')) <= _IDENTIFIER_MAX_BYTES:
            return raw
        # PG 标识符 63 字节静默截断会撞名，超长时截断并追加哈希后缀
        suffix = f'_{hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]}_idx'
        return _fit_identifier(f'{self._table_name}_{column}', suffix)

    def drop_index(self, column):
        raise NotImplementedError("drop_index not implemented for postgresql engine")
    
    def suggest_column_types(self, data, column_type, n_suggest_check=None):
        """Suggest column types based on data (按整批行列并集推断)。"""
        suggested = {}
        for row in data:
            for key, value in row.items():
                if key in suggested:
                    continue
                if column_type and key in column_type:
                    suggested[key] = column_type[key]
                elif isinstance(value, bool):
                    suggested[key] = 'BOOLEAN'
                elif isinstance(value, int):
                    suggested[key] = 'BIGINT'
                elif isinstance(value, float):
                    suggested[key] = 'DOUBLE PRECISION'
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
        """Get sorted child partition relation names.

        表不存在或无分区时返回空列表（与 SQLite 空表行为一致），不抛异常。
        """
        return self._get_native_partitions()
    
    def _get_native_partitions(self) -> List[str]:
        """Get list of native PostgreSQL partitions (按名字排序)。"""
        conn = self.connect()
        cursor = conn.cursor()
        # to_regclass 解析带引号的关系名：不存在返回 NULL 而非报错
        relation = (
            f'{_quote_sql_identifier(self._schema_name())}.'
            f'{_quote_sql_identifier(self._table_name)}'
        )
        cursor.execute(
            """
            select child.relname
            from pg_inherits i
            join pg_class parent on parent.oid = i.inhparent
            join pg_class child on child.oid = i.inhrelid
            where parent.oid = to_regclass(%s)
            order by child.relname
            """,
            (relation,),
        )
        return sorted(row[0] for row in cursor.fetchall())
