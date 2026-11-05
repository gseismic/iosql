from __future__ import annotations

import re
import math
import uuid
import hashlib
import warnings
import itertools
import time
import sys
import json
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from typing import Any, Dict, List
import arrow
import psycopg2
from dateutil.tz import resolve_imaginary
from psycopg2 import sql

from ...table import BaseTable, resolve_select_fields
from ...partition import Partition, CalendarPartition
from ...core.query import Where
from ...migration import OMITTED
from ...exception import BadArgument, SchemaConflict, TableSpecConflict
from ...utils.schema import parse_db_schema
from ...logger import default_logger as logger
from ...spec import TableContract, WriteResult
from .query import PostgreSQLQueryMixin
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
_PARTITION_MARKER_PREFIX = '__iosql_part_v1_'
_CALENDAR_TRANSITION_PREFIX = 'iosql_tzday_'


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


class Table(PostgreSQLQueryMixin, BaseTable):
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
        self._iosql_transaction_lease = None
        
        if self.verbose >= 1:
            logger.info(f'PostgreSQL Table {table_name} initialized with partition_mode={partition_mode}')
    
    def connect(self):
        """Establish database connection."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._uri)
            self._conn.autocommit = True
        return self._conn

    def _preflight_public_call(self, name, args, kwargs):
        """纯参数错误先于协调连接，保留离线契约校验能力。"""
        if name != 'create_table':
            return
        columns = args[0] if args else kwargs.get('column_type')
        pk = args[1] if len(args) > 1 else kwargs.get('pk')
        partition = args[2] if len(args) > 2 else kwargs.get('partition')
        partition = partition if partition is not None else self._partition
        columns = dict(columns or {})
        for type_ in columns.values():
            self._pg_column_type(type_)
        if any(column not in columns for column in pk or []):
            raise BadArgument('主键列未声明')
        if partition is not None:
            if partition.TS_COL not in columns:
                raise BadArgument(f'分区列未声明: {partition.TS_COL}')
            if pk and partition.TS_COL not in pk:
                raise BadArgument('PostgreSQL 分区主键必须包含分区列；请将分区列加入 pk')

    def _restore_contract_after_migration(self, previous):
        """已绑定对象重连只读校验，不能因一次读取重新创建被删除的表。"""
        from ...exception import UnsafeOperation
        if not self._relation_exists(self._table_name):
            raise UnsafeOperation('表已被移除或重命名，请重新打开对应表')
        contract = previous
        with self.connect().cursor() as cursor:
            cursor.execute("SELECT to_regclass('__iosql_control.table_contracts')")
            if cursor.fetchone()[0] is not None:
                cursor.execute('SELECT contract FROM __iosql_control.table_contracts '
                               'WHERE schema_name=%s AND table_name=%s AND relation_oid=to_regclass(%s)',
                               (self._schema_name(), self._table_name, self._qualified_relation()))
                row = cursor.fetchone()
                if row:
                    import json
                    stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    contract = TableContract.from_dict(stored)
        self._contract = contract
        self._partition = contract.to_partition()
        self._primary_key = list(contract.primary_key)
        self._declared_column_types = dict(contract.columns)
        self._verify_existing_table(contract.columns, self._partition)
        self._table_created = True
    
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
        self._release_transaction_lease()

    def _release_transaction_lease(self):
        held = getattr(self, '_iosql_transaction_lease', None)
        self._iosql_transaction_lease = None
        if held is not None:
            held.__exit__(None, None, None)

    @contextmanager
    def _transaction(self, commit=True):
        """写事务：commit=True 成功提交/失败回滚；commit=False 保持事务打开。

        保持事务打开时，调用方后续可用 commit=True 的写入或 delete 提交；
        连接关闭时未提交事务自动回滚。
        """
        conn = self.connect()
        if getattr(self, '_iosql_migration_owned', False):
            if commit:
                raise BadArgument('迁移正式连接只能由发布器统一提交')
            yield conn
            return
        if self._in_explicit_tx:
            try:
                yield conn
            except BaseException:
                conn.rollback()
                self._in_explicit_tx = False
                self._set_autocommit(conn, self._tx_previous_autocommit)
                self._release_transaction_lease()
                raise
            if commit:
                conn.commit()
                self._in_explicit_tx = False
                self._set_autocommit(conn, self._tx_previous_autocommit)
                self._release_transaction_lease()
            return

        previous_autocommit = conn.autocommit
        self._set_autocommit(conn, False)
        self._in_explicit_tx = True
        self._tx_previous_autocommit = previous_autocommit
        if not commit:
            from ...coordination import lease
            pending = lease(self._uri)
            pending.__enter__()
            self._iosql_transaction_lease = pending
        try:
            yield conn
        except BaseException:
            conn.rollback()
            self._in_explicit_tx = False
            self._set_autocommit(conn, previous_autocommit)
            self._release_transaction_lease()
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
        with self.connect().cursor() as cursor:
            cursor.execute(
                'SELECT column_name, udt_name FROM information_schema.columns '
                'WHERE table_schema=%s AND table_name=%s',
                (self._schema_name(), self._table_name),
            )
            physical = dict(cursor.fetchall())
            for name, declared in (column_type or {}).items():
                if physical[name] != {'BIGINT': 'int8', 'DOUBLE PRECISION': 'float8',
                                     'TEXT': 'text', 'BOOLEAN': 'bool', 'DATE': 'date',
                                     'TIMESTAMP': 'timestamp', 'TIMESTAMPTZ': 'timestamptz',
                                     'JSONB': 'jsonb', 'BYTEA': 'bytea'}[self._pg_column_type(declared)]:
                    raise SchemaConflict(f'表 `{self._table_name}` 列 `{name}` 类型与契约不一致')
            cursor.execute(
                '''SELECT array_agg(a.attname ORDER BY k.ordinality)
                   FROM pg_index i
                   CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ordinality)
                   JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                   WHERE i.indrelid=to_regclass(%s) AND i.indisprimary
                     AND k.ordinality<=i.indnkeyatts''',
                (self._qualified_relation(),),
            )
            row = cursor.fetchone()
            physical_pk = list(row[0] or []) if row else []
            if physical_pk != self._primary_key:
                raise SchemaConflict(f'表 `{self._table_name}` 主键与契约不一致: {physical_pk}')
            cursor.execute(
                '''SELECT p.partstrat, p.partnatts, a.attname
                   FROM pg_partitioned_table p
                   LEFT JOIN pg_attribute a ON a.attrelid=p.partrelid
                        AND a.attnum=p.partattrs[0]
                   WHERE p.partrelid=to_regclass(%s)''',
                (self._qualified_relation(),),
            )
            row = cursor.fetchone()
            if partition is None:
                if row:
                    raise SchemaConflict('物理表已分区，但契约未声明 partition')
            elif not row or tuple(row) != ('r', 1, partition.TS_COL):
                raise SchemaConflict('物理分区方式或分区键与契约不一致，要求单列 RANGE')

    def _qualified_relation(self):
        """供 to_regclass 使用的完整关系名，避免 schema 与特殊标识符歧义。"""
        return f'{_quote_sql_identifier(self._schema_name())}.{_quote_sql_identifier(self._table_name)}'

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
        previous = (self._contract, self._partition, self._primary_key,
                    self._declared_column_types, self._table_created)
        try:
            self._contract = contract
            self._partition = contract.to_partition()
            self._primary_key = list(contract.primary_key)
            self._declared_column_types = dict(contract.columns or {})
            self.create_table(contract.columns, pk=contract.primary_key, partition=self._partition)
            if self._partition is not None:
                children = self._inspect_native_layout(self._partition)
                old = previous[1]
                if (old is not None and self._partition_contract_meta(old) != self._partition_contract_meta(self._partition)
                        and (children or not contract.force_partition)):
                    raise TableSpecConflict('已有分区与请求的 partition 不一致；请使用 migrate_to/repartition_to')
                self._check_partition_marker(self._partition, children)
                self._check_control_partition(self._partition, children)
        except BaseException:
            (self._contract, self._partition, self._primary_key,
             self._declared_column_types, self._table_created) = previous
            raise
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
        missing = [name for name in self._primary_key if name not in column_type]
        if missing:
            raise BadArgument(f'主键列未声明: {missing}')
        if partition is not None:
            if partition.TS_COL not in column_type:
                raise BadArgument(f'分区列未声明: {partition.TS_COL}')
            if self._primary_key and partition.TS_COL not in self._primary_key:
                raise BadArgument('PostgreSQL 分区主键必须包含分区列；请将分区列加入 pk')

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
        if self._contract is not None:
            expected = self._contract.to_partition()
            if self._partition_contract_meta(expected) != self._partition_contract_meta(partition):
                raise TableSpecConflict('写入 partition 与已绑定的分区契约不一致；请使用 migrate_to/repartition_to')
        if pk is None:
            pk = self._primary_key
        if isinstance(pk, str):
            pk = [pk]
        
        # Add extra data if provided
        if extra_data:
            data = [{**item, **extra_data} for item in data]

        if self._contract and self._contract.time:
            name = self._contract.time.name
            kind = self._pg_column_type(self._contract.columns[name])
            if kind in ('DATE', 'TIMESTAMP', 'TIMESTAMPTZ'):
                clock = self._query_time_column(self._contract)
                data = [dict(row, **{name: clock.normalize_value(row[name])})
                        if isinstance(row, dict) and name in row and row[name] is not None else row
                        for row in data]

        self._union_columns(data)
        for row in data:
            missing = [name for name in (pk or []) if name not in row or row[name] is None]
            if missing:
                raise BadArgument(f'写入缺少非空主键字段: {missing}')

        if not self._table_created:
            column_types = kwargs.get('column_type') or self.suggest_column_types(
                data,
                column_type={},
            )
            self.create_table(column_types, pk=pk, partition=partition)

        partitioned_data = None
        # 整批写入放在一个显式事务中：失败不留半批脏数据
        with self._transaction(commit):
            if partition is not None:
                self._lock_partition_layout()
                # 持有父表原生锁，使绕过 iosql 的并发 DDL 不能穿过布局检查。
                with self.connect().cursor() as cursor:
                    cursor.execute(sql.SQL('LOCK TABLE ONLY {} IN ROW EXCLUSIVE MODE').format(
                        sql.Identifier(self._schema_name(), self._table_name)))
                children = self._inspect_native_layout(partition)
                self._check_partition_marker(partition, children)
                self._check_control_partition(partition, children, update=True)
                self._ensure_partition_marker(partition, children)
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
                    key = self._native_partition_key(partition, row)
                    if key not in partitioned_data:
                        partitioned_data[key] = []
                    partitioned_data[key].append(row)
                
                for part_key, part_rows in partitioned_data.items():
                    self._check_legacy_transition_overlap(partition, part_key, part_rows, children)
                    self._repair_legacy_calendar_boundaries(partition, part_key, part_rows, children)
                    self._insert_partition(
                        part_key,
                        part_rows,
                        partition,
                        pk=pk,
                        replace=replace,
                        ignore=ignore,
                        children=children,
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

    def _native_partition_key(self, partition, row, *, merge_transition_day=True):
        """墙上时间按本地边界路由，时钟变化日使用不重叠的整日分区。"""
        if isinstance(partition, CalendarPartition):
            parsed = self._calendar_native_local(partition, row[partition.TS_COL])
            if merge_transition_day and self._calendar_transition_enabled(partition):
                start, end = self._calendar_day_bounds(parsed)
                if self._calendar_day_has_transition(start, end):
                    return _CALENDAR_TRANSITION_PREFIX + start.format('YYYYMMDD')
            return partition.get_key({partition.TS_COL: parsed})
        return partition.get_key(row)

    def _calendar_native_local(self, partition, value):
        kind = self._pg_column_type(self._partition_column_type(partition))
        if kind in ('DATE', 'TIMESTAMP', 'TEXT') and getattr(value, 'tzinfo', None) is None and not (
            isinstance(value, str) and re.search(r'(Z|[+-]\d{2}:?\d{2})$', value)
        ):
            if isinstance(value, str) and value.isdigit():
                formats = {4: 'YYYY', 6: 'YYYYMM', 8: 'YYYYMMDD',
                           10: 'YYYYMMDDHH', 12: 'YYYYMMDDHHmm', 14: 'YYYYMMDDHHmmss'}
                parsed = arrow.get(value, formats.get(len(value), partition.fmt))
            else:
                parsed = arrow.get(value)
            return parsed.replace(tzinfo=partition.tz)
        return partition._datetime(value)

    @staticmethod
    def _calendar_transition_enabled(partition):
        unit = partition._every_unit
        minutes = partition._every_count * (60 if unit == 'hour' else 1)
        return not partition._utc_timezone and unit in ('hour', 'minute') and minutes <= 24 * 60

    @staticmethod
    def _calendar_day_bounds(dt):
        start = dt.floor('day')
        end = start.shift(days=1)
        # 某些时区在午夜前拨，00:00 不存在；使用当天第一个真实时刻。
        return (arrow.get(resolve_imaginary(start.datetime)),
                arrow.get(resolve_imaginary(end.datetime)))

    @staticmethod
    def _calendar_day_has_transition(start, end):
        return (end.to('UTC') - start.to('UTC')).total_seconds() != 24 * 60 * 60

    def _check_legacy_transition_overlap(self, partition, part_key, rows, children):
        legacy = [child for child in children if child.get('legacy_tzday')]
        if not legacy:
            return
        new_start, new_end = self._partition_bounds(partition, part_key, rows)
        column_type = self._pg_column_type(self._partition_column_type(partition))
        compare = sql.SQL('SELECT %s::{} < %s::{} AND %s::{} < %s::{}').format(
            *(sql.SQL(column_type),) * 4)
        with self.connect().cursor() as cursor:
            for child in legacy:
                old_start, old_end = child['bounds']
                cursor.execute(compare, (new_start, old_end, old_start, new_end))
                if cursor.fetchone()[0]:
                    raise TableSpecConflict(
                        '旧版夏令时切换日包含重叠的细粒度分区；请使用 migrate_to 迁移'
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
                         replace=False, ignore=False, children=None):
        """Insert data into a specific native partition."""
        self._insert_into_native_partition(
            part_key,
            data,
            partition,
            pk=pk,
            replace=replace,
            ignore=ignore,
            children=children,
        )
    
    def _insert_into_native_partition(self, part_key, data: List[Dict], partition: Partition,
                                    pk=None,
                                    replace=False, ignore=False, children=None):
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
        # 旧版或外部创建的子表可能使用不同名称；边界一致时交给父表路由。
        if any(child['bounds'] == (start_bound, end_bound) and child['legacy_bounds'] is None
               for child in children or []):
            self._insert_into_table(data, self._table_name, pk=pk, replace=replace, ignore=ignore)
            return
        existing_bound = self._existing_partition_bounds(partition_name)
        if (existing_bound is not None and
                not self._partition_bounds_equal(existing_bound, start_bound, end_bound, partition=partition) and
                any(child['schema'] == self._schema_name() and child['name'] == partition_name
                    for child in children or [])):
            # 旧版负数分区可能占用正数的名称，使用确定性后缀避开冲突。
            suffix = '_h' + hashlib.sha256(str(part_key).encode()).hexdigest()[:12]
            partition_name = _fit_identifier(partition_name, suffix)
            existing_bound = self._existing_partition_bounds(partition_name)
        if existing_bound is not None:
            if not self._partition_bounds_equal(existing_bound, start_bound, end_bound, partition=partition):
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

    def _lock_partition_layout(self):
        """同一父表的布局检查与写入在事务中串行化，锁随事务释放。"""
        identity = f'{len(self._schema_name())}:{self._schema_name()}:{self._table_name}'
        key = int.from_bytes(hashlib.sha256(f'iosql-partition:{identity}'.encode()).digest()[:8],
                             'big', signed=True)
        with self.connect().cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', (key,))

    @staticmethod
    def _partition_contract_meta(partition):
        if partition is None:
            return None
        meta = partition.to_meta()
        if meta.get('date_key') == 'local':
            # 原生 DATE/TEXT 按业务日期路由，目录文件路由版本与 PG 布局无关。
            meta.pop('date_key')
            if meta.get('time_unit') == 'date':
                meta.pop('time_unit')
        return meta

    @staticmethod
    def _partition_marker(partition):
        payload = json.dumps(Table._partition_contract_meta(partition), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        return _PARTITION_MARKER_PREFIX + hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _partition_markers(self):
        with self.connect().cursor() as cursor:
            cursor.execute('''SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
                              WHERE conrelid=to_regclass(%s) AND contype='c'
                                AND left(conname, %s)=%s''',
                           (self._qualified_relation(), len(_PARTITION_MARKER_PREFIX),
                            _PARTITION_MARKER_PREFIX))
            rows = cursor.fetchall()
        for name, definition in rows:
            if (not re.fullmatch(re.escape(_PARTITION_MARKER_PREFIX) + r'[0-9a-f]{32}', name)
                    or re.sub(r'\s+', '', definition).lower() != 'check(true)'):
                raise TableSpecConflict(f'约束 `{name}` 占用了 iosql 分区契约名称，请检查后再操作')
        return [name for name, _ in rows]

    def _check_partition_marker(self, partition, children):
        markers = self._partition_markers()
        expected = self._partition_marker(partition)
        if (markers and markers != [expected] and
                (children or self._contract is None or not self._contract.force_partition)):
            raise TableSpecConflict('已有表的分区契约与请求的 partition 不一致；请使用 migrate_to/repartition_to')
        return markers

    def _control_partition_contract(self):
        """读取旧迁移已保存的契约；没有控制表或当前关系未登记时返回 None。"""
        with self.connect().cursor() as cursor:
            cursor.execute("SELECT to_regclass('__iosql_control.table_contracts')")
            row = cursor.fetchone()
            if not row or row[0] is None:
                return None
            cursor.execute('''SELECT contract FROM __iosql_control.table_contracts
                              WHERE schema_name=%s AND table_name=%s
                                AND relation_oid=to_regclass(%s)''',
                           (self._schema_name(), self._table_name, self._qualified_relation()))
            row = cursor.fetchone()
        if not row:
            return None
        payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return TableContract.from_dict(payload)

    def _check_control_partition(self, partition, children, update=False):
        stored = self._control_partition_contract()
        if stored is None:
            return
        previous = stored.to_partition()
        if self._partition_contract_meta(previous) == self._partition_contract_meta(partition):
            return
        if children or self._contract is None or not self._contract.force_partition:
            raise TableSpecConflict('历史迁移保存的分区契约与请求不一致；请使用 migrate_to/repartition_to')
        from ...database import Database
        Database._ensure_contract_core_compatible(stored, self._contract)
        if stored.time != self._contract.time:
            raise TableSpecConflict('空表变更分区不能同时修改历史时间契约；请重新声明原时间语义或迁移')
        if update:
            updated = stored.to_dict()
            updated['partition'] = self._contract.partition.to_dict() if self._contract.partition else None
            updated['force_partition'] = True
            with self.connect().cursor() as cursor:
                cursor.execute('''UPDATE __iosql_control.table_contracts SET contract=%s
                                  WHERE schema_name=%s AND table_name=%s
                                    AND relation_oid=to_regclass(%s)''',
                               (json.dumps(updated), self._schema_name(),
                                self._table_name, self._qualified_relation()))

    def _ensure_partition_marker(self, partition, children):
        markers = self._check_partition_marker(partition, children)
        expected = self._partition_marker(partition)
        if markers == [expected]:
            return
        # 无子分区时尚无数据布局，可以替换空表上遗留的旧摘要。
        parent = sql.Identifier(self._schema_name(), self._table_name)
        with self.connect().cursor() as cursor:
            for marker in markers:
                cursor.execute(sql.SQL('ALTER TABLE {} DROP CONSTRAINT {}').format(
                    parent, sql.Identifier(marker)))
            try:
                cursor.execute(sql.SQL('ALTER TABLE {} ADD CONSTRAINT {} CHECK (TRUE)').format(
                    parent, sql.Identifier(expected)))
            except psycopg2.errors.InsufficientPrivilege as exc:
                raise TableSpecConflict(
                    '首次写入需要表所有者权限以固定分区契约，请联系表所有者授权或迁移到新表'
                ) from exc

    @staticmethod
    def _bound_text(raw_value):
        text = str(raw_value or '').strip()
        text = re.sub(r'::[A-Za-z_][A-Za-z0-9_ ]*$', '', text).strip()
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1].replace("''", "'")
        return text

    def _inspect_native_layout(self, partition):
        """验证全部子分区；识别旧版可能跨年的多日 calendar 边界。"""
        with self.connect().cursor() as cursor:
            cursor.execute('''SELECT n.nspname, c.relname, pg_get_expr(c.relpartbound, c.oid)
                              FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
                              JOIN pg_namespace n ON n.oid=c.relnamespace
                              WHERE i.inhparent=to_regclass(%s) ORDER BY n.nspname,c.relname''',
                           (self._qualified_relation(),))
            rows = cursor.fetchall()
        children = []
        kind = self._pg_column_type(self._partition_column_type(partition))
        for schema, name, expression in rows:
            match = _PARTITION_BOUNDS_RE.search(str(expression or ''))
            if not match:
                raise TableSpecConflict(f'分区 `{schema}.{name}` 的边界无法验证；请迁移到新表')
            text = self._bound_text(match.group(1))
            try:
                value = int(text) if kind == 'BIGINT' else float(text) if kind == 'DOUBLE PRECISION' else text
                sample = {partition.TS_COL: value}
                key = self._native_partition_key(partition, sample)
                bounds = self._partition_bounds(partition, key, [sample])
                legacy_bounds = (self._calendar_partition_bounds(partition, key, [sample], legacy=True)
                                 if isinstance(partition, CalendarPartition) else None)
            except (BadArgument, TypeError, ValueError, OverflowError, KeyError) as exc:
                raise TableSpecConflict(f'无法验证分区 `{schema}.{name}` 的边界；请迁移到新表') from exc
            legacy = False
            legacy_tzday = None
            if not self._partition_bounds_equal(expression, *bounds, partition=partition):
                if isinstance(key, str) and key.startswith(_CALENDAR_TRANSITION_PREFIX):
                    old_key = self._native_partition_key(partition, sample, merge_transition_day=False)
                    old_bounds = self._calendar_partition_bounds(partition, old_key, [sample], legacy=True)
                    if self._partition_bounds_equal(expression, *old_bounds, partition=partition):
                        bounds = old_bounds
                        legacy_tzday = key
                legacy = (legacy_bounds is not None and bounds != legacy_bounds and
                          self._partition_bounds_equal(expression, *legacy_bounds, partition=partition))
                if not legacy and legacy_tzday is None:
                    raise TableSpecConflict(
                        f'已有分区 `{schema}.{name}` 的边界与请求的 partition 不一致；'
                        '请使用 migrate_to/repartition_to'
                    )
            children.append({'schema': schema, 'name': name, 'bounds': bounds,
                             'legacy_bounds': legacy_bounds if legacy else None,
                             'legacy_tzday': legacy_tzday})
        return children

    def _repair_legacy_calendar_boundaries(self, partition, part_key, rows, children):
        legacy = [child for child in children if child['legacy_bounds'] is not None]
        if not legacy:
            return
        new_start, new_end = self._partition_bounds(partition, part_key, rows)
        column_type = self._pg_column_type(self._partition_column_type(partition))
        for child in legacy:
            start, clipped_end = child['bounds']
            old_end = child['legacy_bounds'][1]
            with self.connect().cursor() as cursor:
                compare = sql.SQL('SELECT %s::{} < %s::{} AND %s::{} < %s::{}').format(
                    *(sql.SQL(column_type),) * 4)
                cursor.execute(compare, (new_start, old_end, clipped_end, new_end))
                overlaps = cursor.fetchone()[0]
                if not overlaps and child['bounds'] != (new_start, new_end):
                    continue
                parent = sql.Identifier(self._schema_name(), self._table_name)
                child_name = sql.Identifier(child['schema'], child['name'])
                cursor.execute(sql.SQL('LOCK TABLE ONLY {} IN ACCESS EXCLUSIVE MODE').format(parent))
                # 外部 DDL 不遵循 advisory 锁；获得原生锁后重新检查边界。
                current = {(item['schema'], item['name']): item
                           for item in self._inspect_native_layout(partition)}
                latest = current.get((child['schema'], child['name']))
                if latest is None or latest['legacy_bounds'] != child['legacy_bounds']:
                    raise TableSpecConflict('历史分区布局在检查时发生变化，请重试或迁移到新表')
                cursor.execute(sql.SQL('SELECT EXISTS (SELECT 1 FROM ONLY {} WHERE {} >= %s)').format(
                    child_name, sql.Identifier(partition.TS_COL)), (clipped_end,))
                if cursor.fetchone()[0]:
                    raise TableSpecConflict('历史跨年分区包含越界数据，无法自动调整；请使用 migrate_to 迁移')
                try:
                    cursor.execute(sql.SQL('ALTER TABLE {} DETACH PARTITION {}').format(parent, child_name))
                    cursor.execute(sql.SQL('ALTER TABLE {} ATTACH PARTITION {} FOR VALUES FROM (%s) TO (%s)').format(
                        parent, child_name), (start, clipped_end))
                except psycopg2.Error as exc:
                    raise TableSpecConflict(
                        '无法安全调整历史跨年分区；事务将回滚，请检查表权限或使用 migrate_to 迁移'
                    ) from exc
                child['legacy_bounds'] = None

    def _partition_bounds_equal(self, existing_expr, start_bound, end_bound, *, partition=None):
        match = _PARTITION_BOUNDS_RE.search(str(existing_expr or ''))
        if not match:
            return False
        partition = partition or self._partition
        if partition is not None and self._pg_column_type(self._partition_column_type(partition)) == 'TEXT':
            # TEXT 的原生范围按字符串排序，不能把不同文本当成同一个时间或数字。
            return (self._bound_text(match.group(1)) == str(start_bound) and
                    self._bound_text(match.group(2)) == str(end_bound))
        return (
            self._bound_value_equal(match.group(1), start_bound)
            and self._bound_value_equal(match.group(2), end_bound)
        )

    @staticmethod
    def _bound_value_equal(raw_value, expected):
        text = Table._bound_text(raw_value)
        if str(expected) == text:
            return True
        try:
            return Decimal(text) == Decimal(str(expected))
        except (InvalidOperation, TypeError, ValueError):
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
        legacy=False,
    ):
        key = str(part_key)
        if key.startswith(_CALENDAR_TRANSITION_PREFIX):
            day = arrow.get(key[len(_CALENDAR_TRANSITION_PREFIX):], 'YYYYMMDD', tzinfo=partition.tz)
            start, end = self._calendar_day_bounds(day)
        else:
            start = arrow.get(key, partition.fmt, tzinfo=partition.tz)
            end = start.shift(**partition._shift_kwargs()) if legacy else partition._bucket_end(start)
            if not legacy and rows and self._calendar_transition_enabled(partition):
                sample = self._calendar_sample_value(partition, rows)
                if sample is not None:
                    local = self._calendar_native_local(partition, sample)
                    day_start, day_end = self._calendar_day_bounds(local)
                    if start < day_start:
                        previous_start, previous_end = self._calendar_day_bounds(start)
                        if self._calendar_day_has_transition(previous_start, previous_end):
                            start = day_start
                    if end > day_end:
                        following_start, following_end = self._calendar_day_bounds(day_end)
                        if self._calendar_day_has_transition(following_start, following_end):
                            end = day_end
        column_type = self._partition_column_type(partition)
        normalized = str(column_type or 'text').strip().lower()

        if self._pg_column_type(normalized) in ('BIGINT', 'DOUBLE PRECISION'):
            clock = self._contract.time if self._contract else None
            unit = (clock.unit if clock and clock.name == partition.TS_COL
                    else getattr(partition, 'time_unit', None))
            if unit not in ('ms', 's'):
                raise BadArgument('数值列的 calendar 分区需要明确的 ms/s 时间单位')
            scale = 1000 if unit == 'ms' else 1
            return (int(start.to('UTC').timestamp() * scale),
                    int(end.to('UTC').timestamp() * scale))

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
        if isinstance(part_key, int) and not isinstance(part_key, bool) and part_key < 0:
            suffix = f'n_{abs(part_key)}'
        else:
            suffix = re.sub(r'[^0-9A-Za-z_]+', '_', str(part_key)).strip('_') or 'all'
            if suffix[0].isdigit():
                suffix = f'p_{suffix}'
        name = f'{self._table_name}_p_{suffix}'
        if len(name.encode('utf-8')) <= _IDENTIFIER_MAX_BYTES:
            return name
        # PostgreSQL 会静默截断标识符；使用完整名称的摘要避免不同分区撞名。
        digest = '_h' + hashlib.sha256(name.encode()).hexdigest()[:16]
        return _fit_identifier(name, digest)
    
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
        return self._execute_query(query, params, to_dict)

    def _execute_query(self, query, params, to_dict=True):
        """执行已编译 SQL 并及时释放普通游标；急切读取仅用于 to_list/select。"""
        with self.connect().cursor() as cursor:
            cursor.execute(query, params)
            names = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(names, row)) for row in rows] if to_dict else rows

    def iter_select(self, fields='*', where=None, limit=None, order_by=None,
                    to_dict=True, batch_size=10_000, columns=None,
                    timeout_ms=None, **kwargs):
        self._reject_unknown_kwargs('iter_select', kwargs)
        fields = resolve_select_fields(fields, columns, 'iter_select')
        batches = self.iter_select_batches(
            fields, where, limit=limit, order_by=order_by, to_dict=to_dict,
            batch_size=batch_size, timeout_ms=timeout_ms,
        )
        try:
            for batch in batches:
                yield from batch
        finally:
            batches.close()

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
        query, params = self._select_query(fields, where, order_by, limit)
        yield from self._iter_query_batches(
            query, params, to_dict=to_dict, batch_size=batch_size, timeout_ms=timeout_ms
        )

    def _iter_query_batches(self, query, params, to_dict=True,
                            batch_size=10_000, timeout_ms=None):
        """任意内部 SELECT 共用命名游标；关闭迭代器时释放 portal 与自有事务。"""
        batch_size = self._normalize_batch_size(batch_size)
        timeout_ms = self._normalize_timeout_ms(timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000 if timeout_ms is not None else None
        if timeout_ms is not None and self._in_explicit_tx:
            raise BadArgument('timeout_ms is not supported inside an explicit transaction')
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
                active_error = sys.exc_info()[0] is not None
                try:
                    cursor.close()
                except Exception:
                    # 失败事务中的 CLOSE 可能再次报错，保留原异常以正确回滚/转换。
                    if not active_error:
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

    def migrate_to(self, target_uri, tables=OMITTED, mode='append',
                   batch_size=10_000, verify=None, dry_run=False, *,
                   specs=None, temp_dir=None, lock_timeout=30.0, target_table=OMITTED):
        """保留源表并复制到目标；tables 与 Database 相同，specs 支持目标重新分区。"""
        from ...migration import migrate_table
        return migrate_table(self, target_uri, tables, mode, batch_size, verify, dry_run,
                             specs=specs, temp_dir=temp_dir, lock_timeout=lock_timeout,
                             target_table=target_table)

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
            if field in self._primary_key:
                # PG 主键禁止 NULL，可用原生索引正/反向顺序而不改变任何结果。
                nulls = 'LAST' if direction == 'asc' else 'FIRST'
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
        
        pk = [pk] if isinstance(pk, str) else list(pk or [])
        if not pk:
            raise BadArgument('upsert_all requires primary key')
        data = list(kv_list)
        self._union_columns(data)
        merged = {}
        for row in data:
            missing = [column for column in pk if column not in row or row[column] is None]
            if missing:
                raise BadArgument(f'upsert_all 缺少非空主键字段: {missing}')
            key = tuple(row[column] for column in pk)
            merged[key] = {**merged.get(key, {}), **row}
        # 仅在输入规模内合并重复主键；不同字段集合分开 SQL，缺字段保留旧值。
        groups = {}
        for row in merged.values():
            groups.setdefault(tuple(sorted(row)), []).append(row)
        with self._transaction(commit):
            for rows in groups.values():
                self.insert_all(rows, pk=pk, replace=True, partition=partition,
                                commit=False, **kwargs)

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
    
    def _index_columns(self, column, columns):
        """统一单列/复合列声明；列顺序就是索引定义。"""
        if column is not None and columns is not None:
            raise BadArgument('column 和 columns 不能同时指定')
        names = [column] if column is not None else columns
        if not isinstance(names, (list, tuple)) or not names:
            raise BadArgument('请提供 column 或非空 columns 列表')
        if any(not isinstance(name, str) or not name for name in names):
            raise BadArgument('索引列名必须是非空字符串')
        if len(set(names)) != len(names):
            raise BadArgument('索引列不能重复')
        types = self._column_types()
        for name in names:
            if name not in types:
                raise BadArgument(f'Unknown field `{name}`')
        return list(names)

    def _index_definitions(self):
        """系统目录提供索引的真实归属、键顺序、约束依赖及 iosql 标记。"""
        with self.connect().cursor() as cursor:
            cursor.execute(
                """SELECT c.relname,
                          ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(num, pos)
                                LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
                                WHERE k.pos<=i.indnkeyatts ORDER BY k.pos),
                          ARRAY(SELECT (opt & 1)=1 FROM unnest(i.indoption) WITH ORDINALITY o(opt, pos)
                                ORDER BY pos),
                          i.indisprimary,
                          EXISTS(SELECT 1 FROM pg_constraint co WHERE co.conindid=i.indexrelid),
                          i.indisvalid, am.amname, i.indexprs IS NULL AND i.indpred IS NULL,
                          obj_description(i.indexrelid, 'pg_class'), i.indnatts=i.indnkeyatts
                   FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                   JOIN pg_am am ON am.oid=c.relam
                   WHERE i.indrelid=to_regclass(%s)""",
                (self._qualified_relation(),),
            )
            return cursor.fetchall()

    @staticmethod
    def _index_marker(columns):
        """标记由 iosql 管理的索引；不凭名称猜测用户对象的归属。"""
        import json
        return 'iosql:index:v1:' + json.dumps(columns, ensure_ascii=False)

    def create_index(self, column=None, order='asc', exist_ok=True, *, columns=None):
        """显式创建父表 B-tree；等价主键可复用，未来子分区自动继承。"""
        order_value = str(order or 'asc').strip().lower()
        if order_value not in ('asc', 'desc'):
            raise BadArgument(f'invalid index order: {order}')
        names = self._index_columns(column, columns)
        index_name = self._index_name(names)
        descending = [order_value == 'desc'] * len(names)
        with self._transaction(not self._in_explicit_tx):
            definitions = self._index_definitions()
            for definition in definitions:
                name, keys, directions, primary, constraint, valid, method, plain, marker, no_include = definition
                equivalent = (list(keys) == names and list(directions) == descending
                              and valid and method == 'btree' and plain and no_include)
                if name == index_name and not equivalent:
                    raise SchemaConflict(f'同名索引 `{name}` 定义与请求不一致')
                if equivalent and (exist_ok or primary):
                    return
            # 同 schema 的同名对象不属于当前表时，CREATE INDEX 会失败，绝不静默忽略。
            with self.connect().cursor() as cursor:
                cursor.execute(sql.SQL('CREATE INDEX {} ON {} USING btree ({})').format(
                    sql.Identifier(index_name), sql.Identifier(self._table_name),
                    sql.SQL(', ').join(sql.Identifier(name) + sql.SQL(' ' + order_value.upper())
                                      for name in names),
                ))
                cursor.execute(sql.SQL('COMMENT ON INDEX {} IS %s').format(
                    sql.Identifier(index_name)), (self._index_marker(names),))

    def _index_name(self, column):
        names = [column] if isinstance(column, str) else list(column)
        stem = f'{self._table_name}_{"_".join(names)}'
        if len(names) == 1:
            suffix = '_idx'
        else:
            import json
            digest = hashlib.sha1(json.dumps(names, ensure_ascii=False).encode()).hexdigest()[:10]
            suffix = f'_{digest}_idx'
        raw = stem + suffix
        if len(raw.encode('utf-8')) <= _IDENTIFIER_MAX_BYTES:
            return raw
        suffix = f'_{hashlib.sha1(raw.encode()).hexdigest()[:10]}_idx'
        return _fit_identifier(stem, suffix)

    def drop_index(self, column=None, *, columns=None):
        """删除当前表的受管二级索引；约束索引与用户索引均受保护。"""
        names = self._index_columns(column, columns)
        marker = self._index_marker(names)
        with self._transaction(not self._in_explicit_tx):
            definitions = self._index_definitions()
            for definition in definitions:
                name, keys, directions, primary, constraint, valid, method, plain, comment, no_include = definition
                if comment != marker:
                    if name == self._index_name(names):
                        raise SchemaConflict(f'索引 `{name}` 无 iosql 归属标记，拒绝删除')
                    continue
                if list(keys) != names or primary or constraint or method != 'btree' or not plain or not no_include:
                    raise SchemaConflict(f'索引 `{name}` 定义或约束已变化，拒绝删除')
                with self.connect().cursor() as cursor:
                    cursor.execute(sql.SQL('DROP INDEX {}').format(
                        sql.Identifier(self._schema_name(), name)))

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
