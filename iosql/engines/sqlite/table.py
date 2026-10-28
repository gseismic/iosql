from __future__ import annotations

import copy
import heapq
import json
import os
import pathlib
import pickle
import shutil
import sqlite3
import tempfile
import threading
import time
import warnings
from dataclasses import replace

# from itertools import chain
from collections import OrderedDict
from contextlib import closing, contextmanager
from functools import lru_cache

import arrow

from ...migration import OMITTED
from ...exception import (
    BadArgument,
    InvalidUriSchema,
    MissingPrimaryKey,
    SchemaConflict,
    TableSpecConflict,
    UnsafeOperation,
)
from ...logger import default_logger as logger
from ...partition import Partition
from ...core.query import get_case_range
from ...core.query.timeseries import TimeseriesQueryMixin
from ...spec import (
    CleanupResult,
    CompactResult,
    DEFAULT_LOGICAL_PART,
    LayoutReport,
    PartitionStats,
    PhysicalLayout,
    QueryPlan,
    QuerySpec,
    TableContract,
    SegmentValidationResult,
    SegmentInfo,
    WriteResult,
    _is_numeric_column_type,
    _normalize_column_descriptions,
    _normalize_column_ranges,
)
from ...table import BaseTable, resolve_select_fields
from ...coordination import guard_query
from ...utils.column import sqlite_type_of_value, suggest_sqlite_column_types
from ...utils.createtable import build_createtable_meta
from ...utils.mysqlite import Database as MyDatabase
from ...utils.mysqlite.const import _DEFAULT_PRAGMA as _MYSQLITE_DEFAULT_PRAGMA
from ...utils.mysqlite.connection_pool import (
    release_process_connections,
    reserve_process_connections,
)
from ...utils.storage_identity import iosql_table_names, is_iosql_table

# from mysqlite import Database as MyDatabase
from ...utils.mysqlite.exception import DuplicateColumn, NoSuchColumn, NoSuchTable
from .const import (
    _DB_META_FILE,
    _DEFAULT_BATCH_SIZE,
    _DEFAULT_N_SUGGEST_CHECK,
    _PART_DATA_TABLE_NAME,
    _TABLE_META_FILE,
    _TABLE_META_TABLE_NAME,
    _TABLE_PART_COLUMN_RANGE_TABLE_NAME,
    _TABLE_PART_TABLE_NAME,
    _TABLE_SEGMENT_TABLE_NAME,
    _TABLE_PARTS_SUBDIR,
)
from .query_util import (
    build_bucket_agg_query,
    build_bucket_stats_query,
    build_delete_query,
    build_select_cases_query,
    build_select_count_query,
    build_select_or_cases_query,
    build_select_primary_keys_query,
)


# 用户可声明的 SQLite 合法类型名 → 库规范类型。表外的自定义声明
# （timestamp/json 等）不做强校验，回落宽松行为，避免误拒既有用法
_SQLITE_TYPE_ALIASES = {
    'int': 'integer',
    'int2': 'integer',
    'int4': 'integer',
    'int8': 'integer',
    'integer': 'integer',
    'bigint': 'integer',
    'smallint': 'integer',
    'tinyint': 'integer',
    'mediumint': 'integer',
    'boolean': 'integer',
    'bool': 'integer',
    'float': 'float',
    'float4': 'float',
    'float8': 'float',
    'real': 'float',
    'double': 'float',
    'double precision': 'float',
    'numeric': 'float',
    'decimal': 'float',
    'text': 'text',
    'varchar': 'text',
    'char': 'text',
    'string': 'text',
    'blob': 'blob',
}


# 类型强校验只扫批次头部样本：类型冲突几乎必然在头部显形，且同路径依赖的
# suggest_column_types 本身就按 n_suggest_check 采样推断，全量扫描只增加热路径成本
_TYPE_CHECK_SAMPLE_ROWS = 1000
_IOSQL_METADATA_TABLE_NAME = "_iosql_metadata"
_METADATA_UNSET = object()


def _canon_sqlite_type(type_):
    """声明类型归一到库规范类型（integer/float/text/blob）；无法识别返回 None。"""
    value = str(type_).strip().lower().split('(', 1)[0].strip()
    return _SQLITE_TYPE_ALIASES.get(value)


@lru_cache(maxsize=512)
def _sqlite_parse_column_declaration(declaration, column):
    """由 SQLite 区分类型与列约束，并读取该列的内置排序规则。"""
    quoted = '"' + column.replace('"', '""') + '"'
    try:
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE keys (' + quoted + ' ' + declaration + ')')
            columns = conn.execute('PRAGMA table_info(keys)').fetchall()
            if len(columns) != 1 or columns[0][1] != column:
                raise BadArgument('SQLite 列声明包含额外的列')
            type_name = columns[0][2]
            conn.execute('CREATE INDEX key_index ON keys(' + quoted + ')')
            collation = conn.execute('PRAGMA index_xinfo(key_index)').fetchone()[4].upper()
    except (sqlite3.Error, TypeError, IndexError) as exc:
        raise BadArgument(f'无法确认 SQLite 列声明: {declaration}') from exc
    if collation not in ('BINARY', 'NOCASE', 'RTRIM'):
        raise BadArgument(f'不支持 SQLite 列排序规则: {collation}')
    return type_name, collation


def _sqlite_column_declaration(type_, column=None):
    # mysqlite 建表时未给出类型会使用 TEXT，而非 SQLite 的无类型列。
    declaration = str(type_ or 'TEXT')
    try:
        return _sqlite_parse_column_declaration(declaration, '__iosql_value')
    except BadArgument:
        if column is None:
            raise
        return _sqlite_parse_column_declaration(declaration, str(column))


def _sqlite_affinity(type_, column=None):
    """按 SQLite 实际声明类型求亲和性，约束中的文本不参与判断。"""
    value = _sqlite_column_declaration(type_, column)[0].upper()
    if 'INT' in value:
        return 'INTEGER'
    if any(token in value for token in ('CHAR', 'CLOB', 'TEXT')):
        return 'TEXT'
    if not value or 'BLOB' in value:
        return 'BLOB'
    if any(token in value for token in ('REAL', 'FLOA', 'DOUB')):
        return 'REAL'
    return 'NUMERIC'


def _sqlite_declared_collation(type_, column=None):
    return _sqlite_column_declaration(type_, column)[1]


_SQLITE_NOCASE_TRANSLATION = str.maketrans(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz',
)


def _sqlite_sort_rank(value):
    """SQLite 值序的类别秩：NULL < 数值 < 文本 < blob < 其他（按 str 兜底）。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):  # bool 是 int 子类，同数值秩
        return 1
    if isinstance(value, str):
        return 2
    if isinstance(value, bytes):
        return 3
    if isinstance(value, (bytearray, memoryview)):
        return 3
    return 4


class _SqliteSortKey:
    """对齐 SQLite 比较语义的单列排序键。

    IEEE nulls-first/last、跨类型秩序、DESC 逐列内联反转；
    供 heapq.merge 的 key 使用（只要求 __lt__，附 __eq__ 加速元组比较）。
    """
    __slots__ = ('rank', 'value', 'desc')

    def __init__(self, value, reverse):
        self.rank = _sqlite_sort_rank(value)
        self.value = value
        self.desc = reverse

    def _value_lt(self, other_value):
        try:
            return self.value < other_value
        except TypeError:
            return str(self.value) < str(other_value)

    def __lt__(self, other):
        if self.rank != other.rank:
            # DESC 反转整个比较（含秩）：SQLite desc 下 NULL 最大
            less = self.rank < other.rank
            return (not less) if self.desc else less
        if self.desc:
            return other._value_lt(self.value)
        return self._value_lt(other.value)

    def __eq__(self, other):
        # 由 __lt__（严格全序）派生的相等判定，与元组比较的等价判定一致
        return not (self < other) and not (other < self)


@guard_query
class _TableQuery:
    def __init__(self, table, query_spec):
        self._table = table
        self._query_spec = query_spec

    def explain(self):
        return self._table._build_query_plan(self._query_spec)

    def to_list(self):
        where = self._table._compile_query_where(self._query_spec)
        return self._table.select(
            self._query_spec.columns,
            where=where,
            order_by=self._query_spec.order_by,
            limit=self._query_spec.limit,
            to_dict=True,
        )

    def iter_rows(self, batch_size=10_000):
        where = self._table._compile_query_where(self._query_spec)
        return self._table.iter_select(
            self._query_spec.columns,
            where=where,
            order_by=self._query_spec.order_by,
            limit=self._query_spec.limit,
            batch_size=batch_size,
            to_dict=True,
        )

    def iter_batches(self, batch_size=10_000):
        where = self._table._compile_query_where(self._query_spec)
        return self._table.iter_select_batches(
            self._query_spec.columns,
            where=where,
            order_by=self._query_spec.order_by,
            limit=self._query_spec.limit,
            batch_size=batch_size,
            to_dict=True,
        )

    def spec(self):
        return self._query_spec


@guard_query
class _LatestQuery:
    def __init__(self, table, query_spec, columns, by):
        self._table = table
        self._query_spec = query_spec
        self._columns = columns
        self._by = by

    def explain(self):
        return self._table._build_query_plan(self._query_spec)

    def to_list(self):
        return self._table._latest_rows(
            self._query_spec,
            self._columns,
            self._by,
        )

    def one(self):
        rows = self.to_list()
        if len(rows) > 1:
            raise BadArgument('latest().one() requires at most one row')
        return rows[0] if rows else None

    def iter_rows(self):
        return self._table._iter_latest_rows(self._query_spec, self._columns, self._by)

    def spec(self):
        return self._query_spec


@guard_query
class _BufferedWriter:
    def __init__(self, table, mode=None, max_rows=10_000, **write_kwargs):
        self._table = table
        self._mode = mode
        self._max_rows = int(max_rows)
        self._write_kwargs = write_kwargs
        self._buffer = []
        self._results = []
        self._context = None
        if self._max_rows <= 0:
            raise BadArgument('max_rows must be positive')

    def append(self, rows):
        if isinstance(rows, dict):
            rows = [rows]
        self._buffer.extend(list(rows))
        if len(self._buffer) >= self._max_rows:
            self.flush()
        return self

    def flush(self):
        if not self._buffer:
            return WriteResult()
        rows = self._buffer
        self._buffer = []
        try:
            result = self._table.write(rows, mode=self._mode, **self._write_kwargs)
        except BaseException:
            self._buffer = rows + self._buffer
            raise
        self._results.append(result)
        return result

    def close(self):
        return self.flush()

    def results(self):
        return list(self._results)

    def __enter__(self):
        if self._context is not None:
            raise UnsafeOperation('buffered_writer 上下文不能重复进入')
        from ...coordination import operation
        context = operation(self._table)
        context.__enter__()
        self._context = context
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        try:
            if exc_type is None:
                self.flush()
                return
            if self._buffer:
                warnings.warn(
                    f"buffered_writer 因异常未 flush {len(self._buffer)} 行缓冲数据",
                    RuntimeWarning,
                    stacklevel=2,
                )
        finally:
            context, self._context = self._context, None
            if context is not None:
                context.__exit__(exc_type, exc_value, exc_tb)


class Table(TimeseriesQueryMixin, BaseTable):
    """
    定位：有状态的Table
    分区模式，因为不知道在哪个分区，插入时才创建.sqlite3文件
    """

    _storage_engine = 'sqlite'
    _data_suffix = '.sqlite3'

    def __init__(self, *args, **kwargs):
        pragma_dict = kwargs.pop("pragma_dict", None)
        max_open_partitions = kwargs.pop("max_open_partitions", 5)
        max_connections_per_file = kwargs.pop("max_connections_per_file", 1)
        max_process_connections = kwargs.pop("max_process_connections", 32)
        max_ordered_read_connections = kwargs.pop(
            "max_ordered_read_connections", 128
        )
        self._num_opened_max_allowed = self._positive_int_option(
            "max_open_partitions",
            max_open_partitions,
        )
        self._max_connections_per_file = self._positive_int_option(
            "max_connections_per_file",
            max_connections_per_file,
        )
        self._max_process_connections = self._positive_int_option(
            "max_process_connections",
            max_process_connections,
        )
        self._max_ordered_read_connections = self._positive_int_option(
            "max_ordered_read_connections",
            max_ordered_read_connections,
        )
        self._pragma_dict = copy.deepcopy(_MYSQLITE_DEFAULT_PRAGMA)
        self._pragma_dict.update(pragma_dict or {})
        self._meta_db = None
        self._table_meta_db = None
        self._db_parts = OrderedDict()
        self._db_parts_status = OrderedDict()
        self._db_parts_leases = {}
        # 记录当前线程持有的租约数量：close() 在同一线程内持有租约时直接报错，
        # 避免等待自己释放而永久自旋
        self._db_parts_lease_threads = {}
        self._db_parts_condition = threading.Condition(threading.RLock())
        self._db_parts_closing = False
        self._db_parts_closed = False
        self._compact_lock = threading.RLock()
        self._mutation_lock_state = threading.local()
        # schema 演进锁：只串行化 NoSuchTable/NoSuchColumn 冷路径，
        # 防止并发加列时基于过时 meta 快照互相覆盖；稳态插入不经过这些分支
        self._schema_lock = threading.RLock()
        self._declared_spec = None
        # New partition system
        self._partition = None
        super().__init__(*args, **kwargs)
        # Load partition from metadata after initialization
        self._partition = self._load_partition()
        self.logger = logger

    @contextmanager
    def _mutation_lock(self):
        """同表跨实例/跨进程串行化数据文件与 manifest 的复合写入。"""
        with self._compact_lock:
            depth = getattr(self._mutation_lock_state, 'depth', 0)
            if depth:
                self._mutation_lock_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._mutation_lock_state.depth -= 1
                return

            lock_path = os.path.join(self.table_path, '.iosql.write.lock')
            if os.path.islink(lock_path):
                raise UnsafeOperation(f'表锁文件不能是符号链接: {lock_path}')
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0),
                0o600,
            )
            try:
                try:
                    import fcntl
                except ImportError:
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'\0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    def unlock():
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    def unlock():
                        fcntl.flock(fd, fcntl.LOCK_UN)
                self._mutation_lock_state.depth = 1
                try:
                    yield
                finally:
                    self._mutation_lock_state.depth = 0
                    unlock()
            finally:
                os.close(fd)

    @staticmethod
    def _positive_int_option(name, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BadArgument(f"{name} must be a positive integer")
        return value

    @property
    def max_open_partitions(self):
        return self._num_opened_max_allowed

    @property
    def max_connections_per_file(self):
        return self._max_connections_per_file

    @property
    def max_process_connections(self):
        return self._max_process_connections

    @property
    def max_ordered_read_connections(self):
        return self._max_ordered_read_connections

    def connect(self):
        """连接
        connect called by super().__init__
        """
        path = self.uri_options.get("path")
        db = self.uri_options.get("db")
        # path 可以为''
        if path is None or not db:
            raise InvalidUriSchema()

        self._table_name = self._validate_table_name(self._table_name)

        from ...coordination import physical_path
        self.db_path = str(physical_path(os.path.join(path, db)))
        self.db_meta_path = os.path.join(self.db_path, _DB_META_FILE)

        self.table_path = os.path.join(self.db_path, self._table_name)
        self.table_meta_path = os.path.join(self.table_path, _TABLE_META_FILE)
        self.table_part_path = os.path.join(self.table_path, _TABLE_PARTS_SUBDIR)
        logger.debug("Database Connected")

        self._table_meta_db = self.get_table_meta_db()
        self._meta_db = self.get_meta_db()
        with self._db_parts_condition:
            self._db_parts_closed = False

    def close(self):
        with self._db_parts_condition:
            if self._db_parts_lease_threads.get(threading.get_ident(), 0):
                raise UnsafeOperation(
                    'close() 不能在持有分区连接租约的线程内调用；'
                    '请先退出 _part_db/查询上下文再关闭表'
                )
            while self._db_parts_closing:
                self._db_parts_condition.wait(timeout=0.1)
            self._db_parts_closing = True
            while any(self._db_parts_leases.values()):
                self._db_parts_condition.wait(timeout=0.1)
            part_databases = list(self._db_parts.values())
            self._db_parts.clear()
            self._db_parts_status.clear()
            self._db_parts_leases.clear()
            self._db_parts_lease_threads.clear()
        try:
            if self._table_meta_db and not self._table_meta_db.is_closed():
                self._table_meta_db.close()
            if self._meta_db and not self._meta_db.is_closed():
                self._meta_db.close()
            for db in part_databases:
                if db and not db.is_closed():
                    db.close()
        finally:
            with self._db_parts_condition:
                self._db_parts_closing = False
                self._db_parts_closed = True
                self._db_parts_condition.notify_all()

    def rename(self, new_name, exist_ok=False, overwrite=False):
        """重命名 SQLite 逻辑表；覆盖目标先暂存，失败时恢复两张表。"""
        new_name = self._validate_table_name(new_name)
        old_name = self._table_name
        if new_name == old_name:
            if exist_ok:
                return self
            raise BadArgument('rename target is the current table')
        if overwrite and exist_ok:
            raise BadArgument('rename cannot use overwrite and exist_ok together')

        source_path = self.table_path
        target_path = os.path.join(self.db_path, new_name)
        if not self._is_safe_table_path(target_path):
            raise UnsafeOperation(f'unsafe table rename target: {target_path}')
        if not os.path.isdir(source_path):
            raise BadArgument(f'table does not exist: {old_name}')
        if not is_iosql_table(source_path, backend=self._storage_engine):
            raise UnsafeOperation(f'源目录不是有效 iosql 表: {source_path}')
        if os.path.exists(target_path):
            if not overwrite:
                raise BadArgument(f'table already exists: {new_name}')
            if not os.path.isdir(target_path):
                raise UnsafeOperation(
                    f'table rename target exists and is not a directory: {target_path}'
                )
            if not is_iosql_table(target_path, backend=self._storage_engine):
                raise UnsafeOperation(f'目标目录不是有效 iosql 表: {target_path}')

        owner_ref = getattr(self, '_owner_db', None)
        owner = owner_ref() if owner_ref is not None else None
        cached_target = None
        if owner is not None:
            with owner._tables_lock:
                cached_target = owner._tables.get(new_name)
            if cached_target is self:
                cached_target = None
        backup_path = None
        if os.path.exists(target_path):
            from ...database import Database
            backup_path = Database._rename_backup_path(target_path)
        moved = False
        try:
            self.close()
            if cached_target is not None:
                cached_target.close()
            if backup_path is not None:
                os.replace(target_path, backup_path)
            os.replace(source_path, target_path)
            moved = True
            self._table_name = new_name
            self.connect()
            self._rename_table_manifest(old_name, new_name)
            self._partition = self._load_partition()
            from ...coordination import invalidate_namespace
            invalidate_namespace(self._uri)
        except Exception as exc:
            if moved:
                try:
                    self._rename_table_manifest(new_name, old_name)
                except Exception:
                    pass
                try:
                    self.close()
                except Exception:
                    pass
                os.replace(target_path, source_path)
            self._table_name = old_name
            if backup_path is not None and os.path.exists(backup_path):
                os.replace(backup_path, target_path)
            self.connect()
            if cached_target is not None:
                cached_target.connect()
            if isinstance(exc, OSError):
                raise BadArgument(
                    f'重命名表失败: {exc}；修复: 检查目录权限/跨设备限制后重试'
                ) from exc
            raise
        if owner is not None:
            owner._on_table_renamed(self, old_name, new_name)
        if backup_path is not None:
            try:
                shutil.rmtree(backup_path)
            except OSError as exc:
                warnings.warn(f'旧目标表残留在 {backup_path}: {exc}', RuntimeWarning)
        return self

    @staticmethod
    def _validate_table_name(name):
        value = str(name or '').strip()
        if not value:
            raise BadArgument('table name must not be empty')
        if value in ['.', '..']:
            raise BadArgument(f'invalid table name: {name}')
        if value.startswith('__'):
            raise BadArgument('table name must not start with __')
        if os.sep in value or (os.altsep and os.altsep in value):
            raise BadArgument(f'invalid table name: {name}')
        return value

    def _is_safe_table_path(self, path):
        base = os.path.realpath(self.db_path)
        real = os.path.realpath(path)
        return os.path.dirname(real) == base

    def _rename_table_manifest(self, old_name, new_name):
        # 同一 meta.sqlite3 内一次事务更新，避免中途异常留下半张表的新名字。
        with self.table_meta_db.transaction() as conn:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table_name in [
                _TABLE_META_TABLE_NAME,
                _TABLE_PART_TABLE_NAME,
                _TABLE_PART_COLUMN_RANGE_TABLE_NAME,
                _TABLE_SEGMENT_TABLE_NAME,
            ]:
                if table_name in existing:
                    conn.execute(
                        f'UPDATE "{table_name}" SET table_name = ? WHERE table_name = ?',
                        (new_name, old_name),
                    )

    def _load_partition(self) -> Partition | None:
        """Load partition from table metadata.

        Returns:
            Partition object if partition info exists, None otherwise
        """
        meta = self.get_table_meta()
        if meta and "partition" in meta and meta["partition"]:
            partition_meta = meta["partition"]
            if isinstance(partition_meta, dict) and "type" in partition_meta:
                return Partition.from_meta(partition_meta)
        return None

    @property
    def partition(self) -> Partition | None:
        """Get the partition object for this table."""
        return self._partition

    @partition.setter
    def partition(self, value: Partition | None):
        with self._mutation_lock():
            self._set_partition_locked(value)

    def _set_partition_locked(self, value: Partition | None):
        """Set the partition object for this table and persist to metadata.

        Args:
            value: Partition object (DurationPartition, CalendarPartition) or None

        Raises:
            TableSpecConflict: 已有数据且分区配置与持久化配置不兼容时
        """
        meta = self.get_table_meta()
        current_meta = None
        if meta is not None and meta.get("partition"):
            current_partition = Partition.from_meta(meta["partition"])
            current_meta = current_partition.to_meta() if current_partition else None
        new_meta = value.to_meta() if value else None
        if (
            meta is not None
            and current_meta != new_meta
            and self.table_parts()
        ):
            raise TableSpecConflict(
                f'PartitionCanNotChange: current={current_meta} new={new_meta}；'
                '修复: 用 repartition_to() 迁移到新表，或确认表为空后再设置分区'
            )

        self._partition = value
        if meta is None:
            return

        meta["partition"] = new_meta
        self._overwrite_table_meta(meta)

    def use_contract(self, contract):
        with self._mutation_lock():
            return self._use_contract_locked(contract)

    def _use_contract_locked(self, contract):
        contract = TableContract.ensure(contract)
        current_contract = self.spec()
        if current_contract:
            self._ensure_spec_compatible(current_contract, contract)
        self._persist_contract_metadata(
            contract,
            persist=self.get_table_meta() is not None,
        )
        self._declared_spec = contract
        self._partition = contract.to_partition()
        return self

    def use_spec(self, spec):
        return self.use_contract(spec)

    def spec(self):
        table_meta = self.get_table_meta()
        stored_spec = TableContract.from_table_meta(table_meta)
        if stored_spec is not None and table_meta:
            schema_columns = {}
            for column, info in (table_meta.get("column_info") or {}).items():
                if isinstance(info, dict) and info.get("type") is not None:
                    schema_columns[column] = info["type"]
            schema_columns.update(stored_spec.columns or {})
            if list(schema_columns.items()) != list(
                (stored_spec.columns or {}).items()
            ):
                stored_spec = replace(stored_spec, columns=schema_columns)
        if stored_spec is None:
            contract = self._declared_spec
        elif self._declared_spec is None:
            contract = stored_spec
        else:
            # 已有表：声明侧只覆盖随实例生效的行为字段（pk/分区/段/列已校验兼容），
            # 动态演进出的列等 schema 信息以存量为准，保证 spec() 始终是最新快照
            contract = self._merge_contract_bindings(
                stored_spec,
                self._declared_spec,
            )
        if contract is None:
            return None

        extended = self._read_extended_metadata()
        column_descriptions = {}
        if table_meta:
            for column, info in (table_meta.get("column_info") or {}).items():
                comment = info.get("comment") if isinstance(info, dict) else None
                if isinstance(comment, str):
                    column_descriptions[column] = comment
        else:
            column_descriptions.update(contract.column_descriptions or {})
        column_descriptions.update(extended["column_descriptions"])
        return replace(
            contract,
            description=(
                extended["description"]
                if extended["description"] is not None or table_meta
                else contract.description
            ),
            column_descriptions=column_descriptions,
            column_ranges=(
                extended["column_ranges"]
                if table_meta
                else {
                    **(contract.column_ranges or {}),
                    **extended["column_ranges"],
                }
            ),
        )

    def _read_extended_metadata(self):
        """读取 sidecar；旧库没有扩展表时视为没有新元信息。"""
        table_metadata = {"description": None}
        column_descriptions = {}
        raw_ranges = {}
        try:
            with self.table_meta_db.get_connection() as conn:
                rows = conn.execute(
                    f"SELECT scope, name, metadata_json "
                    f"FROM {_IOSQL_METADATA_TABLE_NAME}"
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return {
                    "description": None,
                    "column_descriptions": {},
                    "column_ranges": {},
                }
            raise

        for scope, name, metadata_json in rows:
            try:
                payload = json.loads(metadata_json)
            except (TypeError, ValueError) as exc:
                raise BadArgument(
                    f"invalid {_IOSQL_METADATA_TABLE_NAME} JSON for {scope}:{name}"
                ) from exc
            if not isinstance(payload, dict):
                raise BadArgument(
                    f"invalid {_IOSQL_METADATA_TABLE_NAME} payload for {scope}:{name}"
                )
            if scope == "table":
                description = payload.get("description")
                if description is not None and not isinstance(description, str):
                    raise BadArgument("stored table description must be a string")
                table_metadata["description"] = description
            elif scope == "column":
                description = payload.get("description")
                if description is not None:
                    if not isinstance(description, str):
                        raise BadArgument(
                            f"stored description for column {name!r} must be a string"
                        )
                    column_descriptions[name] = description
                if "range" in payload:
                    raw_ranges[name] = payload["range"]

        return {
            "description": table_metadata["description"],
            "column_descriptions": _normalize_column_descriptions(
                column_descriptions
            ),
            "column_ranges": _normalize_column_ranges(raw_ranges),
        }

    def _metadata_schema(self, contract=None, table_meta=None):
        if table_meta is None:
            table_meta = self.get_table_meta()
        columns = {}
        if table_meta:
            for column, info in (table_meta.get("column_info") or {}).items():
                if isinstance(info, dict) and info.get("type") is not None:
                    columns[column] = info["type"]
            stored_contract = TableContract.from_table_meta(table_meta)
            if stored_contract is not None:
                columns.update(stored_contract.columns or {})
        if contract is not None:
            columns.update({
                column: type_
                for column, type_ in (contract.columns or {}).items()
                if type_ is not None
            })
        return table_meta, columns

    def _validate_metadata_state(self, metadata, columns, touched_columns=()):
        names = set(columns)
        referenced = (
            set(metadata["column_descriptions"])
            | set(metadata["column_ranges"])
            | set(touched_columns)
        )
        unknown = sorted(referenced - names)
        if unknown:
            available = ", ".join(sorted(names)) or "(none)"
            raise BadArgument(
                f"metadata references unknown column(s) {unknown!r}; "
                f"available columns: {available}"
            )
        for column in metadata["column_ranges"]:
            type_ = columns.get(column)
            normalized = _canon_sqlite_type(type_) if type_ is not None else None
            if (
                not _is_numeric_column_type(type_)
                or normalized not in ("integer", "float")
            ):
                raise BadArgument(
                    f"column_ranges[{column!r}] requires a numeric column type; "
                    f"stored type is {type_!r}"
                )

    def _write_extended_metadata(self, metadata):
        """在单个事务中替换已知 scope 的元数据行。"""
        table_rows = []
        if metadata["description"] is not None:
            table_rows.append((
                "table",
                "",
                json.dumps(
                    {"description": metadata["description"]},
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            ))
        column_names = (
            set(metadata["column_descriptions"])
            | set(metadata["column_ranges"])
        )
        for column in sorted(column_names):
            payload = {}
            description = metadata["column_descriptions"].get(column)
            if description is not None:
                payload["description"] = description
            bounds = metadata["column_ranges"].get(column)
            if bounds is not None:
                payload["range"] = TableContract(
                    column_ranges={column: bounds},
                ).to_dict()["column_ranges"][column]
            table_rows.append((
                "column",
                column,
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
            ))

        with self.table_meta_db.transaction(immediate=True) as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_IOSQL_METADATA_TABLE_NAME} ("
                "scope TEXT NOT NULL, "
                "name TEXT NOT NULL, "
                "metadata_json TEXT NOT NULL, "
                "PRIMARY KEY (scope, name))"
            )
            conn.execute(
                f"DELETE FROM {_IOSQL_METADATA_TABLE_NAME} "
                "WHERE scope IN ('table', 'column')"
            )
            if table_rows:
                conn.executemany(
                    f"INSERT INTO {_IOSQL_METADATA_TABLE_NAME} "
                    "(scope, name, metadata_json) VALUES (?, ?, ?)",
                    table_rows,
                )

    def _apply_metadata_patch(
        self,
        *,
        description=_METADATA_UNSET,
        column_descriptions=_METADATA_UNSET,
        column_ranges=_METADATA_UNSET,
        contract=None,
        require_table_meta=False,
        persist=True,
    ):
        # 把读取、合并、校验和写入放在同一个 BEGIN IMMEDIATE 中，避免并发补充
        # 不同字段时互相覆盖；内部写入方法会复用当前事务连接。
        with self.table_meta_db.transaction(immediate=True):
            return self._apply_metadata_patch_in_transaction(
                description=description,
                column_descriptions=column_descriptions,
                column_ranges=column_ranges,
                contract=contract,
                require_table_meta=require_table_meta,
                persist=persist,
            )

    def _apply_metadata_patch_in_transaction(
        self,
        *,
        description=_METADATA_UNSET,
        column_descriptions=_METADATA_UNSET,
        column_ranges=_METADATA_UNSET,
        contract=None,
        require_table_meta=False,
        persist=True,
    ):
        table_meta, columns = self._metadata_schema(contract=contract)
        if require_table_meta and table_meta is None:
            raise BadArgument(
                f"table {self.table_name!r} has no persisted schema metadata; "
                "write/create the table before calling update_metadata()"
            )

        metadata = self._read_extended_metadata()
        touched_columns = set()
        if description is not _METADATA_UNSET:
            if description is not None and not isinstance(description, str):
                raise BadArgument("description must be str or None")
            metadata["description"] = description

        if column_descriptions is not _METADATA_UNSET:
            if column_descriptions is None:
                metadata["column_descriptions"] = {}
            else:
                patch = _normalize_column_descriptions(column_descriptions)
                touched_columns.update(patch)
                current = dict(metadata["column_descriptions"])
                for column, value in patch.items():
                    if value is None:
                        current.pop(column, None)
                    else:
                        current[column] = value
                metadata["column_descriptions"] = current

        if column_ranges is not _METADATA_UNSET:
            if column_ranges is None:
                metadata["column_ranges"] = {}
            else:
                if not isinstance(column_ranges, dict):
                    raise BadArgument(
                        "column_ranges must be a dict of column -> {low, high}/None"
                    )
                invalid_columns = [
                    column for column in column_ranges
                    if not isinstance(column, str) or not column
                ]
                if invalid_columns:
                    raise BadArgument(
                        "column_ranges keys must be non-empty column names"
                    )
                clear_columns = {
                    column for column, bounds in column_ranges.items()
                    if bounds is None
                }
                touched_columns.update(column_ranges)
                normalized_patch = _normalize_column_ranges({
                    column: bounds
                    for column, bounds in column_ranges.items()
                    if bounds is not None
                })
                current = dict(metadata["column_ranges"])
                for column in clear_columns:
                    current.pop(column, None)
                current.update(normalized_patch)
                metadata["column_ranges"] = current

        self._validate_metadata_state(metadata, columns, touched_columns)
        if persist:
            self._write_extended_metadata(metadata)
            return self.describe()
        return None

    def _persist_contract_metadata(self, contract, persist=True):
        """将声明中的非空元信息作为增量更新写入 sidecar。"""
        patch = {}
        if contract.description is not None:
            patch["description"] = contract.description
        if contract.column_descriptions:
            patch["column_descriptions"] = contract.column_descriptions
        if contract.column_ranges:
            patch["column_ranges"] = contract.column_ranges
        if patch:
            self._apply_metadata_patch(
                contract=contract,
                persist=persist,
                **patch,
            )

    def update_metadata(
        self,
        *,
        description=_METADATA_UNSET,
        column_descriptions=_METADATA_UNSET,
        column_ranges=_METADATA_UNSET,
    ):
        """增量更新已有 SQLite 表的说明和值域，不改写业务数据。"""
        with self._mutation_lock():
            return self._apply_metadata_patch(
                description=description,
                column_descriptions=column_descriptions,
                column_ranges=column_ranges,
                require_table_meta=True,
            )

    def _merge_contract_bindings(self, stored_spec, declared_spec):
        data = stored_spec.to_dict()
        data['conflict'] = declared_spec.conflict
        data['schema_policy'] = declared_spec.schema_policy
        for field in ('order_key', 'symbol_column', 'session_column'):
            value = getattr(declared_spec, field)
            if value is not None:
                data[field] = value
        if declared_spec.time is not None:
            data['time'] = declared_spec.time.to_dict()
        data['force_partition'] = declared_spec.force_partition
        columns = dict(stored_spec.columns or {})
        columns.update(declared_spec.columns or {})
        data['columns'] = columns
        return TableContract.from_dict(data)

    def _ensure_spec_compatible(self, current_spec, new_spec):
        if list(current_spec.primary_key) != list(new_spec.primary_key):
            raise TableSpecConflict(
                f'Primary key conflict: current={current_spec.primary_key} '
                f'new={new_spec.primary_key}'
            )

        current_partition = current_spec.to_partition()
        new_partition = new_spec.to_partition()
        current_meta = current_partition.to_meta() if current_partition else None
        new_meta = new_partition.to_meta() if new_partition else None
        if current_meta != new_meta:
            raise TableSpecConflict(
                f'Partition conflict: current={current_meta} new={new_meta}'
            )
        current_segment = current_spec.segment.to_dict() if current_spec.segment else None
        new_segment = new_spec.segment.to_dict() if new_spec.segment else None
        if current_segment != new_segment:
            raise TableSpecConflict(
                f'Segment conflict: current={current_segment} new={new_segment}'
            )
        self._ensure_spec_columns_compatible(current_spec, new_spec)

    def _ensure_spec_columns_compatible(self, current_spec, new_spec):
        current_columns = {
            key: str(value).lower()
            for key, value in current_spec.columns.items()
            if value is not None
        }
        new_columns = {
            key: str(value).lower()
            for key, value in new_spec.columns.items()
            if value is not None
        }
        for column, type_ in new_columns.items():
            if column in current_columns and current_columns[column] != type_:
                raise SchemaConflict(
                    f'Column `{column}` type conflict: '
                    f'current={current_columns[column]} new={type_}'
                )

    def _ensure_contract_type_compatible(self, contract, rows):
        """evolve 语义三：契约已固化类型的列遇到不兼容数据时报错。

        放行规则：同型、数值互通(int/float)、数字写入 text 声明列
        （SQLite TEXT affinity 转换，既有宽松用法依赖此行为）。
        声明侧归一不到规范类型时跳过该列，回落宽松行为。
        """
        declared = {
            str(column).lower(): str(type_).lower()
            for column, type_ in (contract.columns or {}).items()
            if type_ is not None
        }
        if not declared:
            return
        inferred = {}
        for row in rows[:_TYPE_CHECK_SAMPLE_ROWS]:
            for column, value in row.items():
                if value is None:
                    continue
                type_ = sqlite_type_of_value(value)
                key = str(column).lower()
                existing = inferred.get(key)
                if existing is None or (existing == 'integer' and type_ == 'float'):
                    inferred[key] = type_
        for column, type_ in inferred.items():
            if column not in declared:
                continue
            declared_type = _canon_sqlite_type(declared[column])
            inferred_type = _canon_sqlite_type(type_)
            if declared_type is None or inferred_type is None:
                continue
            if declared_type == inferred_type:
                continue
            if declared_type in ('integer', 'float') and inferred_type in ('integer', 'float'):
                continue
            if declared_type == 'text' and inferred_type in ('integer', 'float'):
                continue
            raise BadArgument(
                f"列 `{column}` 类型冲突：契约已固化为 {declared[column]}，"
                f"本次数据为 {type_}。"
                "修复: 转换数据类型，或重新声明契约 columns"
            )

    def _sync_contract_columns(self, createtable_meta):
        """evolve 语义二：动态加列后回写契约，保证 spec().columns 恒为实际 schema 的上确界。"""
        contract = createtable_meta.get('table_contract')
        if not isinstance(contract, dict):
            return
        columns = dict(contract.get('columns') or {})
        changed = False
        for column, item in (createtable_meta.get('column_info') or {}).items():
            type_ = item.get('type') if isinstance(item, dict) else None
            if type_ is not None and column not in columns:
                columns[column] = type_
                changed = True
        if changed:
            contract['columns'] = columns

    def insert_all(self, data, pk=None, extra_data=None, partition=None,
                   replace=False, ignore=False, commit=True, **kwargs):
        mode_resolved = kwargs.pop('_mode_resolved', False)
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
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                raise BadArgument(f'row[{index}] 必须是 dict')
        data = self._add_extra_data(data, extra_data)

        contract = self.spec()
        if contract:
            if contract.schema_policy == 'strict':
                allowed = set(contract.columns)
                unknown = {field for row in data for field in row if field not in allowed}
                if unknown:
                    raise SchemaConflict(f'严格 schema 禁止新增列: {sorted(unknown)}')
            self._ensure_contract_type_compatible(contract, data)
            pk = pk if pk is not None else contract.primary_key
            partition = partition if partition is not None else contract.to_partition()
            column_type = dict(contract.columns or {})
            column_type.update(kwargs.pop('column_type', {}) or {})
            kwargs['column_type'] = column_type
            kwargs.setdefault('alter', contract.schema_policy == 'evolve')
            kwargs['table_contract'] = contract.to_dict()
            if not mode_resolved and not replace and not ignore:
                if contract.conflict in ['upsert', 'replace']:
                    self._ensure_segment_conflict_supported(
                        contract,
                        contract.conflict,
                    )
                    replace = True
                elif contract.conflict == 'ignore':
                    ignore = True
            if replace:
                self._ensure_segment_conflict_supported(contract, 'replace')
                self._ensure_partition_conflict_supported(contract)

        if replace:
            runtime_partition = partition if partition is not None else self._partition
            self._reject_cross_partition_replace(runtime_partition, pk)

        before_parts = set(self.table_parts())
        before_meta = self.get_table_meta()
        rows = data
        self._ensure_pk_present(rows, pk)

        partitions_touched = self.do_insert_all(
            rows,
            pk=pk,
            partition=partition,
            replace=replace,
            ignore=ignore,
            commit=commit,
            **kwargs,
        )

        after_parts = set(self.table_parts())
        after_meta = self.get_table_meta()
        return WriteResult(
            rows_seen=len(rows),
            rows_written=None if ignore else len(rows),
            partitions_touched=partitions_touched,
            partitions_created=sorted(after_parts - before_parts),
            schema_changed=self._schema_changed(before_meta, after_meta),
            layout_warnings=(
                self._layout_warnings_for_parts(contract, partitions_touched)
                if contract else []
            ),
        )

    def _parts_for_runtime_partition(self, rows, partition):
        if not rows:
            return []
        if partition is None:
            return [None]
        return sorted({partition.get_key(row) for row in rows})

    def _ensure_pk_present(self, rows, pk):
        """主键列缺失必须显式报错：静默会让 single integer pk 走 rowid 自增。"""
        if not pk:
            return
        pk_fields = [pk] if isinstance(pk, str) else list(pk)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            missing = [field for field in pk_fields if field not in row]
            if missing:
                raise BadArgument(
                    f'row[{index}] 缺少主键字段 {missing}；'
                    '修复: 每一行都补齐主键列的值（主键不允许省略）'
                )

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
        if not spec.primary_key:
            raise MissingPrimaryKey('write requires TableContract.primary_key')

        if mode is None:
            mode = spec.conflict
        mode = str(mode).lower()
        if mode not in ['append', 'upsert', 'replace', 'ignore']:
            raise BadArgument(f'Unsupported write mode `{mode}`')
        self._ensure_segment_conflict_supported(spec, mode)
        if mode in ['upsert', 'replace']:
            self._ensure_partition_conflict_supported(spec)

        partition = spec.to_partition()
        self._partition = partition

        column_type = dict(spec.columns)
        column_type.update(kwargs.pop('column_type', {}) or {})
        alter = kwargs.pop('alter', spec.schema_policy == 'evolve')
        replace = mode in ['upsert', 'replace']
        ignore = mode == 'ignore'

        return self.insert_all(
            rows,
            pk=spec.primary_key,
            partition=partition,
            replace=replace,
            ignore=ignore,
            commit=commit,
            alter=alter,
            column_type=column_type,
            table_contract=spec.to_dict(),
            _mode_resolved=True,
            **kwargs,
        )

    def buffered_writer(self, mode=None, max_rows=10_000, **write_kwargs):
        return _BufferedWriter(self, mode=mode, max_rows=max_rows, **write_kwargs)

    def _ensure_segment_conflict_supported(self, spec, mode):
        if not spec or not spec.segment:
            return
        mode = str(mode).lower()
        if mode in ['upsert', 'replace']:
            raise BadArgument(
                'Segment.rows does not support upsert/replace before '
                'global pk index and tombstone are implemented; use '
                'append/ignore or disable segment'
            )

    def _ensure_partition_conflict_supported(self, spec):
        self._reject_cross_partition_replace(spec.to_partition(), spec.primary_key)

    def _reject_cross_partition_replace(self, partition, pk):
        if partition is None:
            return
        pk_fields = [pk] if isinstance(pk, str) else list(pk or [])
        if partition.TS_COL not in pk_fields:
            raise BadArgument(
                '分区表 upsert/replace 要求分区列属于主键；'
                '当前布局没有跨分区全局主键索引，请将分区列加入主键，'
                '或使用 append/ignore 并自行保证跨分区唯一性'
            )

    def query(self, symbols=None, start=None, end=None, date=None, session=None,
              columns='*', where=None, order_by=None, limit=None):
        spec = QuerySpec(
            table=self._table_name,
            columns=columns,
            symbols=symbols,
            start=start,
            end=end,
            date=date,
            session=session,
            where=where,
            order_by=order_by,
            limit=limit,
        )
        return _TableQuery(self, spec)

    def explain_select(self, fields='*', where=None, order_by=None, limit=None,
                       columns=None, symbols=None, start=None, end=None,
                       date=None, session=None):
        fields = resolve_select_fields(fields, columns, 'explain_select')
        spec = QuerySpec(
            table=self._table_name,
            columns=fields,
            symbols=symbols,
            start=start,
            end=end,
            date=date,
            session=session,
            where=where,
            order_by=order_by,
            limit=limit,
        )
        return self._build_query_plan(spec)

    def latest(
        self,
        symbols=None,
        as_of=None,
        columns='*',
        where=None,
        session=None,
        by=None,
        group_by=None,
    ):
        table_spec = self.spec()
        time_column = self._query_time_column(table_spec)
        by = self._resolve_latest_by_arg(by, group_by)
        spec = QuerySpec(
            table=self._table_name,
            columns='*',
            symbols=symbols,
            end=as_of,
            session=session,
            where=where,
            order_by=f'{time_column.name} desc',
        )
        return _LatestQuery(self, spec, columns, by)

    def asof(
        self,
        symbols=None,
        as_of=None,
        columns='*',
        where=None,
        session=None,
        by=None,
        group_by=None,
    ):
        return self.latest(
            symbols=symbols,
            as_of=as_of,
            columns=columns,
            where=where,
            session=session,
            by=by,
            group_by=group_by,
        )

    def segments(self, include_obsolete=False):
        return [
            ref.to_dict()
            for ref in self._physical_part_refs(
                include_obsolete=include_obsolete,
            )
        ]

    def compact(self, partition=None, target_rows=None):
        spec = self.spec()
        if not spec:
            raise BadArgument('compact requires a TableContract')
        if not spec.segment:
            raise BadArgument('compact requires Segment.rows')

        target_rows = self._compact_target_rows(spec, target_rows)
        partitions = self._compact_partitions(partition)
        result = CompactResult(table=self._table_name, partitions=list(partitions))

        with self._mutation_lock():
            for logical_part in partitions:
                part_result = self._compact_partition(
                    logical_part,
                    spec,
                    target_rows,
                )
                result.rows_read += part_result.rows_read
                result.rows_written += part_result.rows_written
                result.old_segments += part_result.old_segments
                result.new_segments += part_result.new_segments
                result.obsolete_segments += part_result.obsolete_segments
        return result

    def validate(self, deep=False, include_obsolete=False):
        result = SegmentValidationResult(
            table=self._table_name,
            deep=bool(deep),
            include_obsolete=bool(include_obsolete),
        )
        # 一次取全量 refs：校验用可见部分，孤儿扫描必须带 obsolete
        all_refs = self._physical_part_refs(include_obsolete=True)
        self._validate_compact_generations(all_refs, result)
        if include_obsolete:
            refs = all_refs
        else:
            refs = [ref for ref in all_refs if ref.state != 'obsolete']
        for ref in refs:
            result.checked_segments += 1
            self._validate_segment_ref(ref, result)
            if deep:
                self._validate_segment_ref_deep(ref, result)
        # 反向扫描孤儿文件：manifest 不引用但磁盘存在的 segment（旧报告 S-M8）
        for path in self._orphan_segment_files(all_refs):
            result.add_warning(
                'orphan_segment_file',
                'segment file is not referenced by manifest',
                path=path,
            )
        return result

    def cleanup_obsolete(self, partition=None, dry_run=True, validate=True):
        if dry_run:
            return self._cleanup_obsolete_locked(partition, dry_run, validate)
        with self._mutation_lock():
            return self._cleanup_obsolete_locked(partition, dry_run, validate)

    def _cleanup_obsolete_locked(self, partition, dry_run, validate):
        spec = self.spec()
        if not spec:
            raise BadArgument('cleanup_obsolete requires a TableContract')
        if not spec.segment:
            raise BadArgument('cleanup_obsolete requires Segment.rows')

        requested_parts = (
            self._compact_partitions(partition)
            if partition is not None
            else None
        )
        refs = [
            info for info in self._segment_infos(
                requested_parts,
                include_obsolete=True,
            )
            if info.state == 'obsolete'
        ]
        result = CleanupResult(
            table=self._table_name,
            dry_run=bool(dry_run),
            partitions=(
                list(requested_parts)
                if requested_parts is not None
                else sorted({ref.logical_part for ref in refs}, key=str)
            ),
            candidate_segments=len(refs),
        )

        for ref in refs:
            files = self._segment_cleanup_files(ref)
            result.files.extend(files)
            result.bytes_reclaimable += sum(self._file_size(path) for path in files)

        if dry_run or not refs:
            return result

        if validate:
            validation = self.validate(deep=True, include_obsolete=False)
            if not validation.ok:
                first = validation.errors[0]
                raise UnsafeOperation(
                    'cleanup_obsolete validation failed before deletion: '
                    f'{first.code}: {first.message}'
                )

        # 与 compact/write 互斥：文件删除 + manifest 清理不能与重建 segment 交错
        with self._mutation_lock():
            deleted_refs = []
            for ref in refs:
                if not self._is_safe_part_path(ref.path):
                    result.skipped.append({
                        'part_key': ref.part_key,
                        'path': ref.path,
                        'reason': 'unsafe_path',
                    })
                    continue
                if not self._close_part_db_if_idle(ref.path):
                    result.skipped.append({
                        'part_key': ref.part_key,
                        'path': ref.path,
                        'reason': 'active_lease',
                    })
                    continue

                files = self._segment_cleanup_files(ref)
                if not files:
                    deleted_refs.append(ref)
                    continue

                deleted_all = True
                bytes_deleted = 0
                files_deleted = 0
                for path in files:
                    try:
                        size = self._file_size(path)
                        os.remove(path)
                        bytes_deleted += size
                        files_deleted += 1
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        deleted_all = False
                        result.skipped.append({
                            'part_key': ref.part_key,
                            'path': path,
                            'reason': f'delete_failed: {exc}',
                        })
                        break
                result.bytes_deleted += bytes_deleted
                result.files_deleted += files_deleted
                if deleted_all:
                    deleted_refs.append(ref)

            result.manifest_rows_deleted = self._delete_segment_manifest_rows(
                deleted_refs
            )
        return result

    def iter_batches(self, batch_size=10_000):
        """按批流式读取全表全部行（batch 为主形态）。"""
        if isinstance(batch_size, bool):
            raise BadArgument('batch_size must be a positive integer')
        batch_size = int(batch_size)
        if batch_size <= 0:
            raise BadArgument('batch_size must be a positive integer')

        yield from self.iter_select_batches(batch_size=batch_size, to_dict=True)

    def iter_rows(self, batch_size=10_000):
        """逐行流式读取全表（iter_batches 的逐行语法糖，与 README 语义一致）。"""
        for batch in self.iter_batches(batch_size=batch_size):
            for row in batch:
                yield row

    def count_rows(self):
        total = 0
        for ref in self._physical_part_refs(lightweight=True):
            self._ensure_ref_file_present(ref)
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=False) as db:
                try:
                    total += db[_PART_DATA_TABLE_NAME].count()
                except NoSuchTable:
                    continue
        return total

    def migrate_to(self, target_uri, tables=OMITTED, mode='append',
                   batch_size=10_000, verify=None, dry_run=False, *,
                   specs=None, temp_dir=None, lock_timeout=30.0, target_table=OMITTED):
        """保留源表并复制到目标；tables 与 Database 相同，specs 支持目标重新分区。"""
        from ...migration import migrate_table
        return migrate_table(self, target_uri, tables, mode, batch_size, verify, dry_run,
                             specs=specs, temp_dir=temp_dir, lock_timeout=lock_timeout,
                             target_table=target_table)

    def _compact_target_rows(self, spec, target_rows):
        layout = spec.physical_layout
        target = layout.target_rows_per_file if target_rows is None else target_rows
        if isinstance(target, bool):
            raise BadArgument('compact target_rows must be a positive integer')
        target = int(target)
        if target <= 0:
            raise BadArgument('compact target_rows must be a positive integer')
        if target > layout.max_rows_per_file:
            raise BadArgument('compact target_rows must be <= segment max_rows')
        return target

    def _compact_partitions(self, partition):
        if partition is None:
            return list(self.table_parts())
        if isinstance(partition, (list, tuple, set)):
            return list(partition)
        return [partition]

    def _compact_partition(self, logical_part, spec, target_rows):
        refs = self._physical_part_refs([logical_part])
        if not refs:
            return CompactResult(table=self._table_name, partitions=[logical_part])
        if any(ref.segment_id < 0 for ref in refs):
            raise BadArgument('compact does not support legacy non-segment files')

        rows = self._compact_sorted_rows(refs, spec)
        chunks = self._compact_row_chunks(
            rows,
            spec.primary_key,
            target_rows,
        )

        result = CompactResult(
            table=self._table_name,
            partitions=[logical_part],
            old_segments=len(refs),
        )
        new_infos = []
        try:
            # 构建代不进入可见 manifest；中断只留下可诊断的孤儿文件。
            self._mutation_lock_state.defer_segment_manifest = True
            with closing(rows):
                new_infos = self._write_compact_chunks(logical_part, chunks, spec)
        finally:
            self._mutation_lock_state.defer_segment_manifest = False
        self._publish_compact_generation(refs, new_infos)

        result.rows_written = sum(info.row_count for info in new_infos)
        result.rows_read = result.rows_written
        result.new_segments = len(new_infos)
        result.obsolete_segments = len(refs)
        return result

    def _publish_compact_generation(self, old_refs, new_infos):
        """一次元数据提交切换整个可见集合，失败时读者继续使用旧代。"""
        with self.table_meta_db.transaction():
            for info in new_infos:
                self._upsert_segment_info(info)
            self._mark_segments_obsolete(old_refs)
            records = self.table_meta_db['_iosql_compactions']
            if new_infos:
                records.insert({
                    'logical_part': new_infos[0].logical_part,
                    'generation': new_infos[0].generation,
                    'part_keys_json': json.dumps([info.part_key for info in new_infos]),
                    'state': 'committed',
                }, pk=['logical_part', 'generation'], replace=True, ensure_table=True)
            try:
                for logical_part, generation in {(ref.logical_part, ref.generation) for ref in old_refs if ref.generation > 0}:
                    records.execute_params(
                        'UPDATE _iosql_compactions SET state=? WHERE logical_part=? AND generation=?',
                        ['obsolete', logical_part, generation], commit=True,
                    )
            except NoSuchTable:
                pass

    def _validate_compact_generations(self, refs, result):
        """发布记录与可见 manifest 必须一致；重复业务主键不属于损坏。"""
        visible = {}
        for ref in refs:
            if ref.state != 'obsolete':
                visible.setdefault((str(ref.logical_part), ref.generation), set()).add(ref.part_key)
        try:
            records = self.table_meta_db['_iosql_compactions'].query(
                'SELECT * FROM _iosql_compactions', to_dict=True)
        except NoSuchTable:
            return
        for record in records:
            if record['state'] == 'obsolete':
                continue
            try:
                expected = set(json.loads(record['part_keys_json']))
            except (TypeError, ValueError):
                expected = None
            actual = visible.get((str(record['logical_part']), record['generation']), set())
            if record['state'] != 'committed' or expected is None or expected != actual:
                result.add_error('incomplete_generation', 'compact 发布记录与可见 segment 不一致')

    def _compact_sorted_rows(self, refs, spec):
        """磁盘暂存加外部排序；除 segment 元数据外，只保留读取批和目标 chunk。"""
        columns = list(spec.columns)
        quoted = ','.join(self._quote_identifier(column) for column in columns)
        order = list(dict.fromkeys(field for field in [
            spec.order_key, spec.time.name if spec.time else None,
            spec.partition.column if spec.partition else None, *spec.primary_key,
        ] if field))
        query = build_select_cases_query('data', '*', None, spec.columns)
        with tempfile.TemporaryDirectory(prefix='iosql-compact-') as directory:
            with closing(sqlite3.connect(os.path.join(directory, 'sort.sqlite3'))) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA temp_store=FILE')
                conn.execute('PRAGMA cache_size=-4096')
                conn.execute('CREATE TABLE rows (' + ','.join(
                    self._quote_identifier(column) + ' BLOB' for column in columns) + ')')
                insert = f'INSERT INTO rows ({quoted}) VALUES (' + ','.join('?' for _ in columns) + ')'
                for ref in refs:
                    self._ensure_ref_file_present(ref)
                    stream = self._iter_part_query_batches(ref.path, query, 1000)
                    with closing(stream):
                        for batch in stream:
                            conn.executemany(insert, [tuple(row[column] for column in columns) for row in batch])
                conn.commit()
                sql = 'SELECT * FROM rows'
                if order:
                    sql += ' ORDER BY ' + ','.join(self._quote_identifier(field) for field in order)
                with closing(conn.execute(sql)) as cursor:
                    while True:
                        batch = cursor.fetchmany(1000)
                        if not batch:
                            break
                        for row in batch:
                            yield dict(row)

    def _compact_row_chunks(self, rows, primary_key, target_rows):
        current = []
        current_keys = set()
        for row in rows:
            pk_value = None
            if primary_key:
                try:
                    pk_value = tuple(row[field] for field in primary_key)
                except KeyError as e:
                    raise MissingPrimaryKey(
                        f'compact row missing primary key column `{e.args[0]}`'
                    ) from e
            if current and (
                len(current) >= target_rows
                or (pk_value is not None and pk_value in current_keys)
            ):
                yield current
                current = []
                current_keys = set()
            current.append(row)
            if pk_value is not None:
                current_keys.add(pk_value)
        if current:
            yield current

    def _write_compact_chunks(self, logical_part, chunks, spec):
        if not chunks:
            return []

        partition = spec.to_partition()
        part_column = partition.TS_COL if partition else None
        layout = spec.physical_layout
        next_segment_id = self._next_segment_id(logical_part)
        generation = self._next_segment_generation(logical_part)
        part_kwargs = {
            'alter': spec.schema_policy == 'evolve',
            'column_type': dict(spec.columns or {}),
            'table_contract': spec.to_dict(),
        }
        new_infos = []
        for chunk in chunks:
            part_key = self._segment_part_key(logical_part, next_segment_id)
            path = self._get_part_dbfile(part_key)
            if os.path.exists(path):
                if self._manifest_references_part_key(part_key):
                    raise UnsafeOperation(
                        f'compact target segment already exists: {path}'
                    )
                # 目标名被历史孤儿文件占用：先安全清理再复用（旧报告 S-M8 修复路径）
                self._remove_orphan_part_file(path)
            segment = SegmentInfo(
                logical_part=logical_part,
                segment_id=next_segment_id,
                part_key=part_key,
                path=path,
                row_count=0,
                sealed=True,
                generation=generation,
                state='sealed',
            )
            info = self._write_segment_rows(
                segment,
                logical_part,
                part_column,
                chunk,
                spec.primary_key,
                partition,
                False,
                False,
                True,
                layout,
                part_kwargs,
            )
            info.sealed = True
            info.state = 'sealed'
            info.generation = generation
            self._upsert_segment_info(info)
            new_infos.append(info)
            next_segment_id += 1
        return new_infos

    def _next_segment_id(self, logical_part):
        segments = self._segment_infos(
            [logical_part],
            include_obsolete=True,
        )
        if not segments:
            return 0
        return max(info.segment_id for info in segments) + 1

    def _next_segment_generation(self, logical_part):
        segments = self._segment_infos(
            [logical_part],
            include_obsolete=True,
        )
        if not segments:
            return 1
        return max(info.generation for info in segments) + 1

    def _mark_segments_obsolete(self, refs):
        for ref in refs:
            if ref.segment_id < 0:
                continue
            ref.sealed = True
            ref.state = 'obsolete'
            self._upsert_segment_info(ref)

    def _validate_segment_ref(self, ref, result):
        if ref.segment_id >= 0 and ref.state not in ['active', 'sealed', 'obsolete']:
            result.add_error(
                'invalid_segment_state',
                f'invalid segment state: {ref.state}',
                **self._segment_issue_context(ref),
            )
        if ref.part_key is None or ref.part_key == '':
            result.add_error(
                'missing_part_key',
                'segment part_key is empty',
                **self._segment_issue_context(ref),
            )
        if not self._is_safe_part_path(ref.path):
            result.add_error(
                'unsafe_path',
                'segment path is outside table parts directory',
                **self._segment_issue_context(ref),
            )

    def _validate_segment_ref_deep(self, ref, result):
        if not os.path.isfile(ref.path):
            self._add_segment_issue(
                result,
                ref,
                'segment_file_missing',
                'segment file does not exist',
            )
            return

        integrity = self._sqlite_integrity_check(ref.path)
        if integrity != ['ok']:
            self._add_segment_issue(
                result,
                ref,
                'sqlite_integrity_failed',
                'SQLite integrity_check failed',
                expected='ok',
                actual=integrity,
            )

        if not self._data_table_exists(ref.path):
            self._add_segment_issue(
                result,
                ref,
                'data_table_missing',
                f'SQLite table `{_PART_DATA_TABLE_NAME}` does not exist',
            )
            return

        self._validate_segment_row_count(ref, result)
        self._validate_segment_time_bounds(ref, result)
        self._validate_segment_session_bounds(ref, result)
        self._validate_segment_stats(ref, result)

    def _validate_segment_row_count(self, ref, result):
        actual = self._data_file_row_count(ref.part_key)
        if int(ref.row_count or 0) != int(actual or 0):
            self._add_segment_issue(
                result,
                ref,
                'row_count_mismatch',
                'manifest row_count does not match physical COUNT(*)',
                expected=ref.row_count,
                actual=actual,
            )

    def _validate_segment_time_bounds(self, ref, result):
        actual_min, actual_max = self._data_file_time_range(ref.part_key)
        if not self._values_equal(ref.time_min, actual_min):
            self._add_segment_issue(
                result,
                ref,
                'time_min_mismatch',
                'manifest time_min does not match physical MIN(time)',
                expected=ref.time_min,
                actual=actual_min,
            )
        if not self._values_equal(ref.time_max, actual_max):
            self._add_segment_issue(
                result,
                ref,
                'time_max_mismatch',
                'manifest time_max does not match physical MAX(time)',
                expected=ref.time_max,
                actual=actual_max,
            )

    def _validate_segment_session_bounds(self, ref, result):
        actual_min, actual_max = self._data_file_session_range(ref.part_key)
        if not self._values_equal(ref.session_min, actual_min):
            self._add_segment_issue(
                result,
                ref,
                'session_min_mismatch',
                'manifest session_min does not match physical MIN(session)',
                expected=ref.session_min,
                actual=actual_min,
            )
        if not self._values_equal(ref.session_max, actual_max):
            self._add_segment_issue(
                result,
                ref,
                'session_max_mismatch',
                'manifest session_max does not match physical MAX(session)',
                expected=ref.session_max,
                actual=actual_max,
            )

    def _validate_segment_stats(self, ref, result):
        for column, bounds in (ref.stats or {}).items():
            if not isinstance(bounds, dict):
                self._add_segment_issue(
                    result,
                    ref,
                    'stats_invalid',
                    f'manifest stats for `{column}` is not an object',
                    actual=bounds,
                )
                continue
            if bounds.get('complete') is False:
                continue

            actual = self._data_file_column_bounds(ref.part_key, column)
            if actual is None:
                self._add_segment_issue(
                    result,
                    ref,
                    'stats_column_missing',
                    f'cannot read physical stats for `{column}`',
                )
                continue

            for key in ['min', 'max']:
                if not self._values_equal(bounds.get(key), actual.get(key)):
                    self._add_segment_issue(
                        result,
                        ref,
                        f'stats_{key}_mismatch',
                        f'manifest stats `{column}.{key}` does not match physical data',
                        expected=bounds.get(key),
                        actual=actual.get(key),
                    )

    def _add_segment_issue(self, result, ref, code, message, **kwargs):
        method = result.add_warning if ref.state == 'obsolete' else result.add_error
        method(
            code,
            message,
            **self._segment_issue_context(ref, **kwargs),
        )

    def _segment_issue_context(self, ref, **kwargs):
        context = {
            'logical_part': ref.logical_part,
            'segment_id': ref.segment_id,
            'part_key': ref.part_key,
            'path': ref.path,
        }
        context.update(kwargs)
        return context

    def _sqlite_integrity_check(self, path):
        try:
            # 显式关闭连接：contextlib.closing 避免 with sqlite3.connect 只提交不关闭
            # 导致的 FD 泄漏（旧报告 S-M1 / 本轮 S1-4）
            with closing(sqlite3.connect(path)) as conn:
                rows = conn.execute('PRAGMA integrity_check;').fetchall()
        except sqlite3.Error as exc:
            return [str(exc)]
        return [row[0] for row in rows]

    def _data_table_exists(self, path):
        try:
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    (
                        "select name from sqlite_master "
                        "where type = 'table' and name = ?"
                    ),
                    [_PART_DATA_TABLE_NAME],
                ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    def _data_file_column_bounds(self, part_key, column):
        path = self._get_part_dbfile(part_key)
        if not os.path.isfile(path):
            return None
        field = self._quote_identifier(column)
        table_name = self._quote_identifier(_PART_DATA_TABLE_NAME)
        try:
            with closing(sqlite3.connect(path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    (
                        f'select MIN({field}) as stat_min, '
                        f'MAX({field}) as stat_max from {table_name};'
                    )
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return {'min': None, 'max': None}
        return {'min': row['stat_min'], 'max': row['stat_max']}

    def _values_equal(self, left, right):
        return left == right

    def _segment_cleanup_files(self, ref):
        if not self._is_safe_part_path(ref.path):
            return []
        paths = [ref.path, f'{ref.path}-wal', f'{ref.path}-shm']
        return [path for path in paths if os.path.exists(path)]

    def _file_size(self, path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _is_safe_part_path(self, path):
        base = os.path.realpath(self.table_part_path)
        real = os.path.realpath(path)
        return real.startswith(base + os.sep)

    def _ensure_ref_file_present(self, ref):
        """读路径前置校验：manifest 引用但文件缺失时立即报错，不隐式建空文件。"""
        path = ref.path or self._get_part_dbfile(ref.part_key)
        if os.path.isfile(path):
            return
        raise UnsafeOperation(
            f'分区数据文件缺失: {path}（manifest 仍引用 part_key={ref.part_key}）；'
            '修复: 运行 validate(deep=True) 检查完整性，'
            '或用 cleanup_obsolete(dry_run=False) 处理 obsolete 引用后从备份恢复'
        )

    def _manifest_references_part_key(self, part_key):
        key = self._part_token(part_key)
        return any(
            self._segment_lookup_key(info) == key
            for info in self._segment_infos(include_obsolete=True)
        )

    def _orphan_segment_files(self, refs=None):
        """扫描 data 目录中不被 manifest 引用的 segment 文件（含 -wal/-shm）。"""
        if not os.path.isdir(self.table_part_path):
            return []
        if refs is None:
            refs = self._physical_part_refs(include_obsolete=True)
        referenced = {os.path.realpath(ref.path) for ref in refs}
        orphans = []
        for name in sorted(os.listdir(self.table_part_path)):
            if not name.endswith(self._data_suffix):
                continue
            path = os.path.join(self.table_part_path, name)
            if os.path.realpath(path) in referenced:
                continue
            orphans.append(path)
        return orphans

    def _remove_orphan_part_file(self, path):
        """安全清理孤儿 segment 文件；被租约占用时拒绝删除。"""
        if not self._is_safe_part_path(path):
            raise UnsafeOperation(f'unsafe orphan file path: {path}')
        if not self._close_part_db_if_idle(path):
            raise UnsafeOperation(
                f'orphan segment file is in use: {path}；'
                '请稍后重试或先关闭并发查询'
            )
        for candidate in [path, f'{path}-wal', f'{path}-shm']:
            try:
                os.remove(candidate)
            except FileNotFoundError:
                continue

    def _close_part_db_if_idle(self, part_dbfile):
        db_to_close = None
        with self._db_parts_condition:
            if self._db_parts_leases.get(part_dbfile, 0):
                return False
            db_to_close = self._db_parts.pop(part_dbfile, None)
            self._db_parts_status.pop(part_dbfile, None)
            self._db_parts_leases.pop(part_dbfile, None)
        if db_to_close and not db_to_close.is_closed():
            db_to_close.close()
        return True

    def _delete_segment_manifest_rows(self, refs):
        if not refs:
            return 0
        try:
            table = self.table_meta_db[_TABLE_SEGMENT_TABLE_NAME]
            for ref in refs:
                table.execute_params(
                    (
                        f'delete from {_TABLE_SEGMENT_TABLE_NAME} '
                        'where table_name = ? '
                        'and logical_part = ? '
                        'and segment_id = ?'
                    ),
                    [self._table_name, ref.logical_part, ref.segment_id],
                    commit=True,
                )
        except NoSuchTable:
            return 0
        return len(refs)

    def _latest_rows(self, query_spec, columns, by):
        return list(self._iter_latest_rows(query_spec, columns, by))

    def _iter_latest_rows(self, query_spec, columns, by):
        table_spec = self.spec()
        if not table_spec:
            raise BadArgument('latest requires a TableSpec')
        symbol_column = table_spec.symbol_column
        if symbol_column is None and by is None:
            raise BadArgument(
                'latest 需要 symbol 分组：表契约未声明 symbol_column。'
                '修复: db.table(..., symbol_column="symbol") 或 '
                'db.timeseries(..., symbol_column="symbol")，'
                '或显式传 latest(by=[...])'
            )
        group_fields = self._normalize_latest_by(by, symbol_column)
        # 首次写入前没有列类型可校验，契约中的分组列仍然有效。
        if not table_spec.columns and not self.table_parts():
            from .query_util import build_select_fields_sql
            build_select_fields_sql(columns)
            return
        from .query_util import validate_select_fields
        validate_select_fields(columns, table_spec.columns)
        for field in group_fields:
            if field not in table_spec.columns:
                raise BadArgument(f'latest grouping field missing: {field}')
        requested_symbols = self._normalize_symbols(query_spec.symbols)
        latest_by_key = OrderedDict()
        time_column = self._query_time_column(table_spec).name
        order = [(time_column, 'desc')] + [
            (field, 'asc') for field in table_spec.primary_key if field != time_column
        ]
        sort_key = self._row_sort_key(self._parse_order_by(order))
        for row in self._iter_latest_candidates(query_spec, group_fields, order):
            if requested_symbols is not None:
                symbol = row.get(symbol_column)
                if symbol not in requested_symbols:
                    continue
            key = tuple(row[field] for field in group_fields)
            previous = latest_by_key.get(key)
            if previous is None or sort_key(row) < sort_key(previous):
                latest_by_key[key] = row

        latest_by_key = OrderedDict(sorted(latest_by_key.items(), key=lambda item: sort_key(item[1])))

        if requested_symbols is None:
            latest_rows = list(latest_by_key.values())
        else:
            latest_rows = self._order_latest_rows_by_symbols(
                latest_by_key,
                requested_symbols,
                symbol_column,
                group_fields,
            )
        yield from self._project_columns(latest_rows, columns)

    def _iter_latest_candidates(self, query_spec, group_fields, order):
        """每个文件只返回每组首行，窗口排序在执行器内完成并可落盘。"""
        spec = self.spec()
        columns = dict(spec.columns)
        fields_sql = ','.join(self._quote_identifier(field) for field in columns)
        marker = '__iosql_latest_rank'
        while marker in columns:
            marker += '_'
        partition_sql = ('PARTITION BY ' + ','.join(self._quote_identifier(field) for field in group_fields) + ' '
                         if group_fields else '')
        order_sql = self._order_by_sql(order, '*', columns)
        where = self._build_where(self._compile_query_where(query_spec))
        for ref, query in self._select_queries_for_where('*', where, columns):
            self._ensure_ref_file_present(ref)
            sql = ('SELECT ' + fields_sql + ' FROM (SELECT ' + fields_sql +
                   ', ROW_NUMBER() OVER (' + partition_sql + 'ORDER BY ' + order_sql + ') AS ' +
                   self._quote_identifier(marker) + ' FROM (' + query.sql.rstrip(';') + ')) WHERE ' +
                   self._quote_identifier(marker) + '=1')
            stream = self._iter_part_query_batches(ref.path, replace(query, sql=sql), 1000)
            with closing(stream):
                for batch in stream:
                    for row in batch:
                        yield dict(row)



    def _ensure_latest_fields(self, row, fields):
        missing = [field for field in fields if field not in row]
        if missing:
            raise BadArgument(
                f'latest grouping field missing from row: {missing}'
            )

    def _order_latest_rows_by_symbols(
        self,
        latest_by_key,
        requested_symbols,
        symbol_column,
        group_fields,
    ):
        if symbol_column not in group_fields:
            return list(latest_by_key.values())

        symbol_index = group_fields.index(symbol_column)
        rows = []
        for symbol in requested_symbols:
            for key, row in latest_by_key.items():
                if key[symbol_index] == symbol:
                    rows.append(row)
        return rows


    def _project_columns(self, rows, columns):
        fields = self._projection_fields(columns)
        if fields is None:
            return rows
        return [
            {field: row[field] for field in fields}
            for row in rows
        ]

    def _projection_fields(self, columns):
        if columns is None:
            return None
        if isinstance(columns, str):
            text = columns.strip()
            if text == '*':
                return None
            return [item.strip() for item in text.split(',') if item.strip()]
        if isinstance(columns, (list, tuple)):
            if any(str(column).strip() == '*' for column in columns):
                return None
            return list(columns)
        raise BadArgument('columns must be a string/list/tuple')

    def _schema_changed(self, before_meta, after_meta):
        if before_meta is None:
            return after_meta is not None
        before_columns = before_meta.get('column_info') or {}
        after_columns = (after_meta or {}).get('column_info') or {}
        return before_columns != after_columns

    def _layout_warnings_for_parts(self, spec, parts):
        if not spec.segment:
            return []
        warnings_ = []
        max_rows = spec.physical_layout.max_rows_per_file
        for info in self._segment_infos(parts):
            row_count = info.row_count
            if row_count > max_rows:
                warnings_.append(
                    f'segment {info.part_key} has {row_count} rows; '
                    f'max_rows_per_file={max_rows}'
                )
        return warnings_






    def _build_query_plan(self, query_spec):
        compiled_where = self._compile_query_where(query_spec)
        all_parts = list(self.table_parts())
        all_refs = self._physical_part_refs(all_parts)
        if not all_parts:
            return QueryPlan(
                table=self._table_name,
                columns=query_spec.columns,
                where=compiled_where,
                order_by=query_spec.order_by,
                limit=query_spec.limit,
            )

        w = self._build_where(compiled_where)
        scanned = set()
        scanned_ref_keys = set()
        candidate_ref_keys = set()
        candidate_partition_keys = set()
        exclusion_reasons = {}
        if not w.or_and_cases:
            scanned.update(all_parts)
            candidate_partition_keys.update(all_parts)
            candidate_ref_keys.update(
                self._segment_lookup_key(ref) for ref in all_refs
            )
            scanned_ref_keys.update(
                self._segment_lookup_key(ref) for ref in all_refs
            )
        else:
            for cases in w.or_and_cases:
                candidate_parts = self._get_select_parts(cases)
                candidate_partition_keys.update(candidate_parts)
                # 裁剪决定复用普通查询的轻量引用；上方完整引用仅用于诊断展示。
                candidate_refs = self._physical_part_refs(candidate_parts, lightweight=True)
                candidate_ref_keys.update(
                    self._segment_lookup_key(ref) for ref in candidate_refs
                )
                excluded = self._segment_exclusion_reasons(candidate_refs, cases)
                exclusion_reasons.update(excluded)
                part_refs = [
                    ref for ref in candidate_refs
                    if self._segment_lookup_key(ref) not in excluded
                ]
                scanned.update(ref.logical_part for ref in part_refs)
                scanned_ref_keys.update(
                    self._segment_lookup_key(ref) for ref in part_refs
                )

        scanned_partitions = [part for part in all_parts if part in scanned]
        skipped_partitions = [part for part in all_parts if part not in scanned]
        candidate_segments = [
            ref for ref in all_refs
            if self._segment_lookup_key(ref) in candidate_ref_keys
        ]
        scanned_segments = [
            ref for ref in candidate_segments
            if self._segment_lookup_key(ref) in scanned_ref_keys
        ]
        skipped_segments = [
            ref for ref in candidate_segments
            if self._segment_lookup_key(ref) not in scanned_ref_keys
        ]
        skip_reasons = {}
        for part in skipped_partitions:
            if part in candidate_partition_keys:
                skip_reasons[part] = self._partition_segment_skip_reason(
                    part,
                    skipped_segments,
                    exclusion_reasons,
                )
            else:
                skip_reasons[part] = 'pruned_by_partition_range'
        segment_skip_reasons = {
            ref.part_key: exclusion_reasons.get(
                self._segment_lookup_key(ref),
                'pruned_by_segment_stats',
            )
            for ref in skipped_segments
            if ref.logical_part in candidate_partition_keys
        }
        return QueryPlan(
            table=self._table_name,
            columns=query_spec.columns,
            where=compiled_where,
            order_by=query_spec.order_by,
            limit=query_spec.limit,
            candidate_partitions=[
                part for part in all_parts
                if part in candidate_partition_keys
            ],
            scanned_partitions=scanned_partitions,
            skipped_partitions=skipped_partitions,
            skip_reasons=skip_reasons,
            candidate_segments=[
                self._segment_to_plan_item(ref) for ref in candidate_segments
            ],
            scanned_segments=[
                self._segment_to_plan_item(ref) for ref in scanned_segments
            ],
            skipped_segments=[
                self._segment_to_plan_item(ref) for ref in skipped_segments
            ],
            segment_skip_reasons=segment_skip_reasons,
            requires_global_sort=bool(query_spec.order_by and len(scanned_segments) > 1),
            requires_python_limit=bool(query_spec.limit is not None and len(scanned_segments) > 1),
        )

    def _get_part_dbfile(self, part_key):
        """Get the file path for a partition key.

        Args:
            part_key: Partition key (int for DurationPartition, str for CalendarPartition)

        Returns:
            Full path to the partition file
        """
        path = os.path.join(self.table_part_path, f"{part_key}{self._data_suffix}")
        # P0-3 纵深防御：即使分区 key 生成侧被绕过（fmt 注入/损坏 manifest），
        # 也不允许分区文件写出 data 目录
        if not self._is_safe_part_path(path):
            raise UnsafeOperation(
                f'unsafe partition file path: {path}；'
                '分区键包含路径分隔符或越出表数据目录，已拒绝访问'
            )
        return path

    def _quote_identifier(self, value):
        return '"' + str(value).replace('"', '""') + '"'

    def _part_token(self, part):
        return str(part)

    def _part_meta_value(self, part):
        return 'None' if part is None else part

    def _meta_key_value(self, value):
        return 'None' if value is None else value

    def _bool_value(self, value):
        if isinstance(value, str):
            return value.strip().lower() not in ['', '0', 'false', 'none']
        return bool(value)

    def _segment_part_key(self, logical_part, segment_id):
        return f'{logical_part}__{int(segment_id):06d}'

    def _segment_from_row(self, row):
        stats = row.get('stats_json')
        if isinstance(stats, str) and stats:
            try:
                stats = json.loads(stats)
            except json.JSONDecodeError:
                stats = {}
        if not isinstance(stats, dict):
            stats = {}
        state = row.get('state')
        if state is None:
            state = 'sealed' if self._bool_value(row.get('sealed')) else 'active'
        state = str(state or 'active').lower()
        return SegmentInfo(
            logical_part=row['logical_part'],
            segment_id=int(row['segment_id']),
            part_key=row['part_key'],
            path=self._get_part_dbfile(row['part_key']),
            row_count=int(row.get('row_count') or 0),
            time_min=row.get('time_min'),
            time_max=row.get('time_max'),
            session_min=row.get('session_min'),
            session_max=row.get('session_max'),
            stats=stats,
            sealed=self._bool_value(row.get('sealed')),
            generation=int(row.get('generation') or 0),
            state=state,
            created_at=row.get('created_at'),
        )

    def _segment_infos(self, logical_parts=None, include_obsolete=False):
        try:
            rows = self.table_meta_db[_TABLE_SEGMENT_TABLE_NAME].query(
                f'select * from {_TABLE_SEGMENT_TABLE_NAME} where table_name = ?',
                params=[self._table_name],
                to_dict=True,
            )
        except NoSuchTable:
            return []

        if logical_parts is not None:
            wanted = {self._part_token(part) for part in logical_parts}
            rows = [
                row for row in rows
                if self._part_token(row['logical_part']) in wanted
            ]

        infos = [self._segment_from_row(row) for row in rows]
        if not include_obsolete:
            if any(info.state not in ('active', 'sealed', 'obsolete') for info in infos):
                raise UnsafeOperation('manifest 含非法发布状态，请运行 validate() 检查')
            infos = [info for info in infos if info.state != 'obsolete']
        return sorted(
            infos,
            key=lambda item: (self._part_token(item.logical_part), item.segment_id),
        )

    def _segment_lookup_key(self, info):
        return self._part_token(info.part_key)

    def _segment_to_plan_item(self, info):
        return info.to_dict()

    def _segment_info(self, logical_part, segment_id, include_obsolete=False):
        for info in self._segment_infos(
            [logical_part],
            include_obsolete=include_obsolete,
        ):
            if info.segment_id == segment_id:
                return info
        return None

    def _segment_created_at(self, logical_part, segment_id):
        if self._segment_info(logical_part, segment_id) is None:
            return None
        try:
            rows = self.table_meta_db[_TABLE_SEGMENT_TABLE_NAME].query(
                (
                    f'select created_at from {_TABLE_SEGMENT_TABLE_NAME} '
                    'where table_name = ? and logical_part = ? and segment_id = ?'
                ),
                params=[self._table_name, logical_part, segment_id],
                to_dict=True,
            )
        except NoSuchTable:
            return None
        return rows[0].get('created_at') if rows else None

    def _upsert_segment_info(self, info):
        if getattr(self._mutation_lock_state, 'defer_segment_manifest', False):
            return
        now = arrow.now().format("YYYY-MM-DD HH:mm:ss")
        state = self._normalized_segment_state(info)
        row = {
            'table_name': self._table_name,
            'logical_part': info.logical_part,
            'segment_id': info.segment_id,
            'part_key': info.part_key,
            'path': info.path,
            'row_count': info.row_count,
            'time_min': info.time_min,
            'time_max': info.time_max,
            'session_min': info.session_min,
            'session_max': info.session_max,
            'stats_json': json.dumps(info.stats or {}),
            'created_at': info.created_at or now,
            'updated_at': now,
            'sealed': int(bool(info.sealed)),
            'generation': info.generation,
            'state': state,
        }
        self.table_meta_db[_TABLE_SEGMENT_TABLE_NAME].insert(
            row,
            pk=['table_name', 'logical_part', 'segment_id'],
            replace=True,
            ensure_table=True,
            ensure_column=True,
            commit=True,
        )

    def _normalized_segment_state(self, info):
        state = str(info.state or '').lower()
        if state == 'obsolete':
            return 'obsolete'
        return 'sealed' if info.sealed else 'active'

    def _data_file_row_count(self, part_key):
        part_dbfile = self._get_part_dbfile(part_key)
        if not os.path.isfile(part_dbfile):
            return 0
        with self._part_db(part_dbfile, auto_commit=False) as db:
            try:
                rows = db[_PART_DATA_TABLE_NAME].query(
                    f'select COUNT(*) from {_PART_DATA_TABLE_NAME};'
                )
            except NoSuchTable:
                return 0
        if not rows:
            return 0
        return dict(rows[0]).get('COUNT(*)', 0)

    def _data_file_time_range(self, part_key):
        spec = self.spec()
        if not spec or not spec.time:
            return None, None

        part_dbfile = self._get_part_dbfile(part_key)
        if not os.path.isfile(part_dbfile):
            return None, None

        field = self._quote_identifier(spec.time.name)
        with self._part_db(part_dbfile, auto_commit=False) as db:
            try:
                rows = db[_PART_DATA_TABLE_NAME].query(
                    (
                        f'select MIN({field}) as time_min, '
                        f'MAX({field}) as time_max from {_PART_DATA_TABLE_NAME};'
                    ),
                    to_dict=True,
                )
            except NoSuchTable:
                return None, None
        if not rows:
            return None, None
        row = rows[0]
        return row.get('time_min'), row.get('time_max')

    def _data_file_session_range(self, part_key):
        spec = self.spec()
        if not spec or not spec.session_column:
            return None, None

        part_dbfile = self._get_part_dbfile(part_key)
        if not os.path.isfile(part_dbfile):
            return None, None

        field = self._quote_identifier(spec.session_column)
        with self._part_db(part_dbfile, auto_commit=False) as db:
            try:
                rows = db[_PART_DATA_TABLE_NAME].query(
                    (
                        f'select MIN({field}) as session_min, '
                        f'MAX({field}) as session_max from {_PART_DATA_TABLE_NAME};'
                    ),
                    to_dict=True,
                )
            except NoSuchTable:
                return None, None
        if not rows:
            return None, None
        row = rows[0]
        return row.get('session_min'), row.get('session_max')

    _column_stats_cache = {}
    _column_stats_cache_hits = 0
    _column_stats_cache_misses = 0

    def _data_file_stat_key(self, path):
        signature = []
        for candidate in (path, path + '-wal'):
            try:
                info = os.stat(candidate)
                signature.append((info.st_mtime_ns, info.st_size))
            except FileNotFoundError:
                signature.append(None)
        return (path, tuple(signature))

    def _data_file_column_stats(self, part_key):
        """按 segment 数据文件缓存列 MIN/MAX 统计（键含 mtime+size，
        任何写入都会改变键从而自动失效）。有 where 的重复查询不再
        每次全段重扫。PLAN-069 P3。"""
        meta = self.get_table_meta()
        columns = list((meta or {}).get('column_info') or {})
        if not columns:
            return {}

        part_dbfile = self._get_part_dbfile(part_key)
        if not os.path.isfile(part_dbfile):
            return {}
        def _stat_key():
            try:
                return self._data_file_stat_key(part_dbfile)
            except OSError:
                return None

        # 查询本身开/关连接可能触发 WAL checkpoint 改写文件 mtime，
        # 因此存储键在查询后取：首次写入的是查询后状态，后续查询即可命中
        cache_key = _stat_key()
        if cache_key is not None and cache_key in self._column_stats_cache:
            type(self)._column_stats_cache_hits += 1
            return dict(self._column_stats_cache[cache_key])
        type(self)._column_stats_cache_misses += 1

        projections = []
        for column in columns:
            field = self._quote_identifier(column)
            alias_min = self._quote_identifier(f'{column}__min')
            alias_max = self._quote_identifier(f'{column}__max')
            projections.append(f'MIN({field}) as {alias_min}')
            projections.append(f'MAX({field}) as {alias_max}')
        with self._part_db(part_dbfile, auto_commit=False) as db:
            try:
                rows = db[_PART_DATA_TABLE_NAME].query(
                    f"select {', '.join(projections)} from {_PART_DATA_TABLE_NAME};",
                    to_dict=True,
                )
            except NoSuchTable:
                return {}
        if not rows:
            return {}

        row = rows[0]
        stats = {}
        for column in columns:
            stats[column] = {
                'min': row.get(f'{column}__min'),
                'max': row.get(f'{column}__max'),
            }
        cache_key = _stat_key()
        if cache_key is not None:
            self._column_stats_cache[cache_key] = stats
        return stats

    def _refresh_segment_info(self, logical_part, segment_id, part_key, layout):
        row_count = self._data_file_row_count(part_key)
        time_min, time_max = self._data_file_time_range(part_key)
        session_min, session_max = self._data_file_session_range(part_key)
        stats = self._data_file_column_stats(part_key)
        existing = self._segment_info(logical_part, segment_id)
        sealed = (
            bool(existing and existing.sealed)
            or row_count >= layout.max_rows_per_file
        )
        info = SegmentInfo(
            logical_part=logical_part,
            segment_id=segment_id,
            part_key=part_key,
            path=self._get_part_dbfile(part_key),
            row_count=row_count,
            time_min=time_min,
            time_max=time_max,
            session_min=session_min,
            session_max=session_max,
            stats=stats,
            sealed=sealed,
            generation=existing.generation if existing else 0,
            state=(
                existing.state
                if existing and existing.state == 'obsolete'
                else ('sealed' if sealed else 'active')
            ),
            created_at=existing.created_at if existing else None,
        )
        self._upsert_segment_info(info)
        return info

    def _batch_column_stats(self, rows, table_spec):
        physical_types = self._physical_column_types(table_spec)
        configured = table_spec.segment.stats_columns
        if configured is None:
            columns = list(table_spec.columns or {})
            seen = set(columns)
            for row in rows:
                for column in row:
                    if column not in seen:
                        columns.append(column)
                        seen.add(column)
        else:
            required = []
            partition = table_spec.to_partition()
            if partition is not None:
                required.append(partition.TS_COL)
            if table_spec.time:
                required.append(table_spec.time.name)
            if table_spec.order_key:
                required.append(table_spec.order_key)
            if table_spec.session_column:
                required.append(table_spec.session_column)
            columns = list(dict.fromkeys(list(configured) + required))

        stats = {}
        for column in columns:
            values = [
                row[column]
                for row in rows
                if column in row and row[column] is not None
            ]
            if not values:
                stats[column] = {'min': None, 'max': None}
                continue
            if not self._stats_values_match_storage(physical_types.get(column), values, column):
                # 转换后的 SQLite 值或排序无法从输入证明，保守扫描而不猜测亲和性。
                stats[column] = {'min': None, 'max': None, 'complete': False}
                continue
            try:
                stats[column] = {'min': min(values), 'max': max(values)}
            except TypeError:
                # 混合不可比较类型时保守禁用该列裁剪，避免错误跳过 segment。
                stats[column] = {
                    'min': None,
                    'max': None,
                    'complete': False,
                }
        return stats

    def _merge_column_stats(self, current, incoming):
        merged = dict(current or {})
        for column, bounds in incoming.items():
            old_bounds = merged.get(column) or {}
            if (
                old_bounds.get('complete') is False
                or bounds.get('complete') is False
            ):
                merged[column] = {
                    'min': None,
                    'max': None,
                    'complete': False,
                }
                continue
            old_min = old_bounds.get('min')
            old_max = old_bounds.get('max')
            new_min = bounds.get('min')
            new_max = bounds.get('max')
            if new_min is None or new_max is None:
                merged.setdefault(column, {'min': old_min, 'max': old_max})
                continue
            if old_min is None or old_max is None:
                merged[column] = {'min': new_min, 'max': new_max}
                continue
            try:
                merged[column] = {
                    'min': min(old_min, new_min),
                    'max': max(old_max, new_max),
                }
            except TypeError:
                merged[column] = {
                    'min': None,
                    'max': None,
                    'complete': False,
                }
        return merged

    def _increment_segment_info(self, segment, rows, layout, table_spec):
        stats = self._merge_column_stats(
            segment.stats,
            self._batch_column_stats(rows, table_spec),
        )

        time_min = segment.time_min
        time_max = segment.time_max
        if table_spec.time:
            time_stats = stats.get(table_spec.time.name) or {}
            time_min = time_stats.get('min')
            time_max = time_stats.get('max')

        session_min = segment.session_min
        session_max = segment.session_max
        if table_spec.session_column:
            session_stats = stats.get(table_spec.session_column) or {}
            session_min = session_stats.get('min')
            session_max = session_stats.get('max')

        row_count = segment.row_count + len(rows)
        sealed = segment.sealed or row_count >= layout.max_rows_per_file
        info = SegmentInfo(
            logical_part=segment.logical_part,
            segment_id=segment.segment_id,
            part_key=segment.part_key,
            path=segment.path,
            row_count=row_count,
            time_min=time_min,
            time_max=time_max,
            session_min=session_min,
            session_max=session_max,
            stats=stats,
            sealed=sealed,
            generation=segment.generation,
            state=(
                segment.state
                if segment.state == 'obsolete'
                else ('sealed' if sealed else 'active')
            ),
            created_at=segment.created_at,
        )
        self._upsert_segment_info(info)
        return info

    def _write_segment_rows(
        self,
        segment,
        logical_part,
        part_column,
        rows,
        pk,
        partition,
        replace,
        ignore,
        commit,
        layout,
        part_kwargs,
    ):
        self._do_insert_part(
            segment.part_key,
            part_column,
            rows,
            pk,
            partition=partition,
            replace=replace,
            ignore=ignore,
            commit=commit,
            logical_part=logical_part,
            **part_kwargs,
        )
        if not replace and not ignore:
            table_spec = TableContract.ensure(part_kwargs['table_contract'])
            return self._increment_segment_info(
                segment,
                rows,
                layout,
                table_spec,
            )
        return self._refresh_segment_info(
            logical_part,
            segment.segment_id,
            segment.part_key,
            layout,
        )

    def _append_segment_rows(
        self,
        logical_part,
        part_column,
        rows,
        pk,
        partition,
        replace,
        ignore,
        commit,
        layout,
        part_kwargs,
    ):
        pending = list(rows)
        while pending:
            active = self._active_segment_for(logical_part, layout)
            capacity = layout.max_rows_per_file - active.row_count
            if capacity <= 0:
                active.sealed = True
                active.state = 'sealed'
                self._upsert_segment_info(active)
                continue

            batch = pending[:capacity]
            self._write_segment_rows(
                active,
                logical_part,
                part_column,
                batch,
                pk,
                partition,
                replace,
                ignore,
                commit,
                layout,
                part_kwargs,
            )
            pending = pending[len(batch):]

    def _active_segment_for(self, logical_part, layout):
        segments = self._segment_infos([logical_part])
        for info in reversed(segments):
            if (
                info.state == 'active'
                and not info.sealed
                and info.row_count < layout.max_rows_per_file
            ):
                return info

        next_segment_id = 0
        all_segments = self._segment_infos(
            [logical_part],
            include_obsolete=True,
        )
        if all_segments:
            next_segment_id = max(item.segment_id for item in all_segments) + 1
        part_key = self._segment_part_key(logical_part, next_segment_id)
        return SegmentInfo(
            logical_part=logical_part,
            segment_id=next_segment_id,
            part_key=part_key,
            path=self._get_part_dbfile(part_key),
            row_count=0,
        )

    def _legacy_segment_info(self, logical_part, lightweight=False):
        """构建 legacy 分区的 segment 信息。

        普通数据访问使用 lightweight，只保留文件寻址字段；完整统计供显式诊断。
        已登记的 legacy 文件缺失时保留引用，由访问前的完整性检查报错。
        """
        path = self._get_part_dbfile(logical_part)
        exists = os.path.isfile(path)
        if not exists:
            spec = self.spec()
            # segmented 布局只有 manifest 中的段文件，不要求存在同名 legacy 文件。
            if spec and spec.segment:
                return None
        if lightweight or not exists:
            return SegmentInfo(
                logical_part=logical_part,
                segment_id=-1,
                part_key=logical_part,
                path=path,
                sealed=True,
                state='sealed',
            )
        time_min, time_max = self._data_file_time_range(logical_part)
        session_min, session_max = self._data_file_session_range(logical_part)
        stats = self._data_file_column_stats(logical_part)
        return SegmentInfo(
            logical_part=logical_part,
            segment_id=-1,
            part_key=logical_part,
            path=path,
            row_count=self._data_file_row_count(logical_part),
            time_min=time_min,
            time_max=time_max,
            session_min=session_min,
            session_max=session_max,
            stats=stats,
            sealed=True,
            state='sealed',
        )

    def _physical_part_refs(self, logical_parts=None, include_obsolete=False,
                            lightweight=False):
        parts = self.table_parts() if logical_parts is None else list(logical_parts)
        refs = []
        for part in parts:
            legacy = self._legacy_segment_info(part, lightweight=lightweight)
            if legacy is not None:
                refs.append(legacy)
        refs.extend(
            self._segment_infos(parts, include_obsolete=include_obsolete)
        )

        order = {self._part_token(part): index for index, part in enumerate(parts)}
        return sorted(
            refs,
            key=lambda item: (
                order.get(self._part_token(item.logical_part), len(order)),
                item.segment_id,
            ),
        )

    def _excluded_segments_by_case(self, refs, case):
        if not refs or case.value['type'] == 'variable':
            return set()
        excluded = set()
        physical_types = self._physical_column_types(self.spec())
        for info in refs:
            stats = (info.stats or {}).get(case.field)
            if stats and self._stats_excludes_case(stats, case, physical_types):
                excluded.add(self._segment_lookup_key(info))
        return excluded

    def _physical_column_types(self, table_spec):
        """优先使用实际建表记录，契约的列类型可能被首次写入参数覆盖。"""
        columns = dict(table_spec.columns if table_spec else {})
        meta = self.get_table_meta() or {}
        columns.update({
            column: info['type']
            for column, info in (meta.get('column_info') or {}).items()
            if info.get('type') is not None
        })
        return columns

    def _stats_values_match_storage(self, type_, values, column=None):
        """无法证明统计值与存储值一致时禁止裁剪，包括历史 manifest。"""
        if self._storage_engine == 'sqlite':
            try:
                if _sqlite_declared_collation(type_, column) != 'BINARY':
                    return False
                affinity = _sqlite_affinity(type_, column)
            except BadArgument:
                return False
            if affinity == 'TEXT':
                return all(isinstance(value, str) for value in values)
            if affinity in ('INTEGER', 'NUMERIC'):
                return all(isinstance(value, (int, float)) and value == value for value in values)
            if affinity != 'REAL':
                return False
        else:
            declared = _canon_sqlite_type(type_)
            if declared == 'text':
                return all(isinstance(value, str) for value in values)
            if declared == 'integer':
                return all(isinstance(value, int) for value in values)
            if declared != 'float':
                return False
        try:
            return all(
                isinstance(value, (int, float)) and float(value) == value
                for value in values
            )
        except OverflowError:
            return False

    def _stats_excludes_case(self, stats, case, physical_types):
        if stats.get('complete') is False or case.value['type'] == 'variable':
            return False
        stat_min = stats.get('min')
        stat_max = stats.get('max')
        if stat_min is None or stat_max is None:
            return False
        if not self._stats_values_match_storage(
            physical_types.get(case.field), (stat_min, stat_max), case.field,
        ):
            return False

        op = case.op
        value = case.value['value']
        try:
            if op == 'in':
                return all(
                    self._value_outside_range(item, stat_min, stat_max)
                    for item in value
                )

            op, case_min, case_max = self._case_partition_range(case)
            if case_max is not None:
                if case_max < stat_min:
                    return True
                if op in ['lt', '<'] and case_max <= stat_min:
                    return True
            if case_min is not None:
                if case_min > stat_max:
                    return True
                if op in ['gt', '>'] and case_min >= stat_max:
                    return True
        except TypeError:
            return False
        return False

    def _value_outside_range(self, value, stat_min, stat_max):
        return value < stat_min or value > stat_max

    def _excluded_segments_by_session_case(self, refs, case):
        table_spec = self.spec()
        if not table_spec or not table_spec.session_column:
            return set()
        if case.field != table_spec.session_column:
            return set()
        if case.value['type'] == 'variable':
            return set()
        if case.op in ['eq', '==', '=']:
            values = [case.value['value']]
        elif case.op == 'in':
            values = list(case.value['value'])
        else:
            return set()

        excluded = set()
        physical_types = self._physical_column_types(table_spec)
        for info in refs:
            bounds = (info.session_min, info.session_max)
            if (all(value is not None for value in bounds)
                    and not self._stats_values_match_storage(
                        physical_types.get(table_spec.session_column), bounds, table_spec.session_column,
                    )):
                continue
            if self._session_values_outside_segment(values, info):
                excluded.add(self._segment_lookup_key(info))
        return excluded

    def _session_values_outside_segment(self, values, info):
        if info.session_min is None or info.session_max is None:
            return False
        try:
            for value in values:
                if info.session_min <= value <= info.session_max:
                    return False
        except TypeError:
            return False
        return True

    def _segment_exclusion_reasons(self, refs, cases):
        reasons = {}
        for case in cases or []:
            for key in self._excluded_segments_by_case(refs, case):
                reasons.setdefault(key, 'pruned_by_segment_stats')
            for key in self._excluded_segments_by_session_case(refs, case):
                reasons[key] = 'pruned_by_segment_session_range'
        return reasons

    def _excluded_segments_by_cases(self, refs, cases):
        return set(self._segment_exclusion_reasons(refs, cases))

    def _partition_segment_skip_reason(self, part, skipped_segments, reasons):
        part_reasons = [
            reasons.get(self._segment_lookup_key(ref))
            for ref in skipped_segments
            if ref.logical_part == part
        ]
        if 'pruned_by_segment_session_range' in part_reasons:
            return 'pruned_by_segment_session_range'
        return 'pruned_by_segment_stats'

    def _get_select_part_refs(self, cases, skip_segment_stats=False):
        need_parts = self._get_select_parts(cases)
        # 分区边界已用于裁剪；普通查询不再为 legacy 文件临时计算列范围和行数。
        # row segment 的既有 manifest 统计仍可使用，完整 where 始终下推 SQL。
        refs = self._physical_part_refs(need_parts, lightweight=True)
        if not cases or skip_segment_stats:
            return refs

        excluded = self._excluded_segments_by_cases(refs, cases)
        return [
            ref for ref in refs
            if self._segment_lookup_key(ref) not in excluded
        ]

    def select_all(self, fields='*', where=None, limit=None, to_dict=False, **kwargs):
        warnings.warn(
            "select_all() 已废弃：等价于 select(fields, where, to_dict=True)，"
            "请改用 select(to_dict=True)",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.select(fields, where, limit=limit, to_dict=True, **kwargs)

    # select 家族不接受业务过滤参数：这些参数会被静默忽略，用户极易误用。
    # 业务语义统一走 query()/explain_select() 编译。
    _BUSINESS_SELECT_KWARGS = ('symbols', 'start', 'end', 'date', 'session')

    def _reject_unknown_select_kwargs(self, kwargs, func_name):
        provided = [
            name for name in self._BUSINESS_SELECT_KWARGS if name in kwargs
        ]
        if provided:
            raise BadArgument(
                f'{func_name} 不支持业务参数 {provided}（会被静默忽略）；'
                '修复: 改用 query(...) 或 explain_select(...)，'
                '它们支持 symbols/start/end/date/session'
            )
        if kwargs:
            names = ', '.join(sorted(kwargs))
            raise BadArgument(f'{func_name}() 收到未知参数: {names}')

    def iter_select(
        self,
        fields='*',
        where=None,
        limit=None,
        order_by=None,
        to_dict=True,
        batch_size=10_000,
        columns=None,
        timeout_ms=None,
        **kwargs,
    ):
        fields = resolve_select_fields(fields, columns, 'iter_select')
        self._reject_unknown_select_kwargs(kwargs, 'iter_select')
        batches = self.iter_select_batches(
            fields,
            where,
            limit=limit,
            order_by=order_by,
            to_dict=to_dict,
            batch_size=batch_size,
            timeout_ms=timeout_ms,
            **kwargs,
        )
        with closing(batches):
            for batch in batches:
                for row in batch:
                    yield row

    def iter_select_batches(
        self,
        fields='*',
        where=None,
        limit=None,
        order_by=None,
        to_dict=True,
        batch_size=10_000,
        columns=None,
        timeout_ms=None,
        **kwargs,
    ):
        """批量流式查询。

        timeout_ms 为可选正整数迭代期限；None 表示不设期限。期限从首次
        迭代开始计时，包含调用方处理已产出批次的时间；超时中止查询并抛
        TimeoutError。
        """
        fields = resolve_select_fields(fields, columns, 'iter_select_batches')
        self._reject_unknown_select_kwargs(kwargs, 'iter_select_batches')
        batch_size = self._normalize_batch_size(batch_size)
        limit = self._normalize_limit(limit)
        timeout_ms = self._normalize_timeout_ms(timeout_ms)
        deadline = (
            time.monotonic() + timeout_ms / 1000
            if timeout_ms is not None else None
        )
        if limit == 0:
            self._validate_projection_only(fields)
            return

        if order_by:
            yield from self._iter_select_ordered_batches(
                fields,
                where,
                limit,
                order_by,
                to_dict,
                batch_size,
                deadline,
            )
            return

        meta = self.get_table_meta()
        if not meta:
            self._validate_projection_only(fields)
            return
        column_types = {
            key: item["type"] for key, item in meta["column_info"].items()
        }
        w = self._build_where(where)
        fetch_fields, project_fields = self._select_fields_with_order(
            fields,
            None,
            column_types,
        )

        emitted = 0
        pending = []
        raw_batches = self._iter_select_raw_batches(
            fetch_fields,
            w,
            column_types,
            batch_size,
            deadline,
            limit,
        )
        with closing(raw_batches):
            for rows in raw_batches:
                if not rows:
                    continue
                if project_fields is not None:
                    rows = self._project_fields(
                        rows, project_fields, to_dict=to_dict
                    )
                if to_dict:
                    rows = [dict(item) for item in rows]

                for row in rows:
                    if limit is not None and emitted >= limit:
                        break
                    pending.append(row)
                    emitted += 1
                    if len(pending) >= batch_size:
                        yield pending
                        pending = []
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError(
                                'SQLite query exceeded timeout_ms budget'
                            )
                if limit is not None and emitted >= limit:
                    break
        if pending:
            yield pending
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('SQLite query exceeded timeout_ms budget')

    def aggregate_by_bucket(self, x_col, value_cols, n_buckets, where=None,
                            group_col=None):
        """SQL 端等宽分桶聚合（PLAN-070，web chart 大表加速路径）。

        把"逐行拉回 Python 再分桶平均"下推为每分区一条 GROUP BY，
        返回行数 ≤ n_buckets，任意表规模下内存有界、耗时线性于行数。
        where 语义与 select() 一致（dict/list/Case/文本），并保留分区裁剪。

        桶网格与旧 Python 路径逐位一致：width = (x_max - x_min) // n + 1，
        idx = floor((x - x_min) / width)（非负值域上 CAST 截断等价 floor）。
        x 列模式按数据探测分类（SQLite 无约束类型）：
        - numeric：全数值；
        - text：全纯数字文本（YYYYMMDD 等，CAST 后按整数分桶）；
        - mixed：数值与文本混存；non_numeric：含非数字文本；
        - empty：无 x 非空行。
        mixed/non_numeric/empty 时 sums/counts 为 None，由调用方决定报错
        或返回空结果（422 文案归 web 层，保持既有 hint 不变）。
        """
        x_col = str(x_col)
        value_cols = [str(c) for c in (value_cols or [])]
        n_buckets = int(n_buckets)
        if n_buckets <= 0:
            raise BadArgument('n_buckets 必须为正整数')

        def _empty_result():
            return {
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

        meta = self.get_table_meta()
        if not meta:
            return _empty_result()
        column_types = {
            key: item["type"] for key, item in meta["column_info"].items()
        }
        where_obj = self._build_where(where)
        or_and_cases = where_obj.or_and_cases

        # 分区裁剪与 _select_queries_for_where 同构：无条件/单 AND/多 OR 三态
        if not or_and_cases:
            refs = self._get_select_part_refs(None)
        elif len(or_and_cases) > 1:
            refs_by_key = OrderedDict()
            for cases in or_and_cases:
                for ref in self._get_select_part_refs(cases):
                    refs_by_key[self._segment_lookup_key(ref)] = ref
            refs = list(refs_by_key.values())
        else:
            refs = self._get_select_part_refs(or_and_cases[0])

        # ---- 第一遍：探测查询求全局范围与类型分布 ----
        scanned = 0
        numeric_rows = 0
        digit_text_rows = 0
        text_overflow_rows = 0
        num_min = num_max = None   # 纯数值分区的 min/max（类型安全合并）
        txt_min = txt_max = None   # 纯数字文本分区的 min/max
        value_bad = [0] * len(value_cols)
        query = build_bucket_stats_query(
            _PART_DATA_TABLE_NAME, x_col, value_cols,
            or_and_cases, column_types,
        )
        reduced_query = build_bucket_stats_query(
            _PART_DATA_TABLE_NAME, x_col, [], or_and_cases, column_types,
        ) if value_cols else None
        for ref in refs:
            self._ensure_ref_file_present(ref)
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=False) as part_db:
                try:
                    rows = part_db[_PART_DATA_TABLE_NAME].query(
                        query.sql, params=query.params,
                    )
                except NoSuchTable:
                    continue
                except sqlite3.OperationalError:
                    # 旧 segment 缺列（schema-on-write 演进）：退化为仅 x 探测，
                    # y 列视为该 segment 全缺（与旧 select(*) 流式路径等价）；
                    # x 列也缺则整个 segment 跳过
                    if reduced_query is None:
                        continue
                    try:
                        rows = part_db[_PART_DATA_TABLE_NAME].query(
                            reduced_query.sql, params=reduced_query.params,
                        )
                    except (NoSuchTable, sqlite3.OperationalError):
                        continue
            if not rows or rows[0] is None:
                continue
            row = rows[0]
            cnt = int(row[0] or 0)
            scanned += cnt
            numeric_rows += int(row[3] or 0)
            digit_text_rows += int(row[4] or 0)
            text_overflow_rows += int(row[7] or 0)
            for i, bad in enumerate(row[8:]):
                value_bad[i] += int(bad or 0)
            p_min, p_max = row[1], row[2]
            if cnt == 0 or p_min is None or p_max is None:
                continue
            if int(row[3] or 0) == cnt:
                # 纯数值分区：min/max 都是数值，可安全跨分区比较
                if num_min is None or p_min < num_min:
                    num_min = p_min
                if num_max is None or p_max > num_max:
                    num_max = p_max
            elif int(row[4] or 0) == cnt:
                p_min, p_max = row[5], row[6]
                if txt_min is None or p_min < txt_min:
                    txt_min = p_min
                if txt_max is None or p_max > txt_max:
                    txt_max = p_max
            # 混合类型分区的 min/max 存储类不定，跳过合并——
            # 全局计数已能识别 mixed，范围值不会被后续消费

        if scanned == 0:
            return _empty_result()

        # ---- x 模式分类（与旧全表扫描语义逐条对齐）----
        if numeric_rows == scanned:
            x_mode = 'numeric'
            x_min, x_max = num_min, num_max
        elif numeric_rows > 0:
            x_mode = 'mixed'      # 有数值也有文本
            x_min = x_max = None
        elif digit_text_rows == scanned:
            if text_overflow_rows:
                raise BadArgument('数字文本横轴超出 SQLite 64 位整数范围')
            x_mode = 'text'
            x_min, x_max = str(txt_min), str(txt_max)
        else:
            x_mode = 'non_numeric'
            x_min = x_max = None

        result = {
            'scanned': scanned,
            'x_min': x_min,
            'x_max': x_max,
            'x_numeric_rows': numeric_rows,
            'x_digit_text_rows': digit_text_rows,
            'value_bad_rows': dict(zip(value_cols, value_bad)),
            'bucket_min': None,
            'bucket_width': None,
            'x_mode': x_mode,
            'sums': None,
            'counts': None,
        }
        if x_mode not in ('numeric', 'text'):
            return result

        # ---- 第二遍：GROUP BY 分桶聚合 ----
        # 网格公式与旧 Python 路径一致：width = span // n + 1
        if x_mode == 'text':
            grid_min = int(str(x_min).strip())
            grid_max = int(str(x_max).strip())
        else:
            grid_min = x_min
            grid_max = x_max
        span = grid_max - grid_min
        if x_mode == 'text' and span > 2**63 - 1:
            raise BadArgument('数字文本横轴跨度超出 SQLite 64 位整数范围')
        width = span // n_buckets + 1 if span >= 0 else 1
        sums = {c: [0.0] * n_buckets for c in value_cols}
        counts = {c: [0] * n_buckets for c in value_cols}
        agg_query = build_bucket_agg_query(
            _PART_DATA_TABLE_NAME, x_col, value_cols,
            or_and_cases, column_types,
            x_text_mode=(x_mode == 'text'), x_min=grid_min, width=width,
            group_col=group_col,
        )
        grouped = {}
        for ref in refs:
            self._ensure_ref_file_present(ref)
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=False) as part_db:
                try:
                    rows = part_db[_PART_DATA_TABLE_NAME].query(
                        agg_query.sql, params=agg_query.params,
                    )
                except NoSuchTable:
                    continue
                except sqlite3.OperationalError:
                    # 旧 segment 缺 y/x 列：该 segment 不参与聚合（贡献为 0），
                    # 与旧 select(*) + row.get(col)→None 的容错等价
                    continue
            for row in rows or []:
                idx = row[0]
                if idx is None or idx < 0 or idx >= n_buckets:
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

    def select(self, fields='*', where=None, limit=None, order_by=None,
               to_dict=True, columns=None, **kwargs):
        """条件查询，返回行列表（急切求值）。

        fields/where 均可省略：select() 即全表查询；
        columns= 是 fields= 的别名（与 query(columns=) 对齐），两者同传冲突报错。
        默认返回 dict 行（与 PG 引擎一致）；to_dict=False 返回 sqlite3.Row 元组。
        """
        fields = resolve_select_fields(fields, columns, 'select')
        self._reject_unknown_select_kwargs(kwargs, 'select')
        # step1. 找到各part记录到的字段最大最小数值, 排除不需要查询的part
        # step2. 查找各个And([case..])的数据
        #       处理where以后, [And([case..])]之间不应有包含关系，否则可以直接去掉被包含的
        # step3. 数据分组cluster，组内排序，组间排序
        #        cluster=None, 返回: [dict..]
        #        cluster='symbol', 返回: OrderedDict[('symbol1', [dict..]), ..]
        # 一次性全部读出内存? 多个表，涉及到排序问题
        meta = self.get_table_meta()
        if meta:
            column_types = {
                key: item["type"] for key, item in meta["column_info"].items()
            }
        else:
            # No such table
            self._validate_projection_only(fields)
            return []

        w = self._build_where(where)
        limit = self._normalize_limit(limit)
        fetch_fields, project_fields = self._select_fields_with_order(
            fields,
            order_by,
            column_types,
        )
        if limit == 0:
            return []

        if not order_by and limit is not None:
            # 无序查询逐文件扣除剩余额度；OR 在单条 SQL 内求并集，保留物理重复行。
            result = []
            for ref, query in self._select_queries_for_where(
                fetch_fields, w, column_types, limit=limit,
            ):
                remaining = limit - len(result)
                query = replace(query, params=list(query.params[:-1]) + [remaining])
                self._ensure_ref_file_present(ref)
                with self._part_db(self._get_part_dbfile(ref.part_key), auto_commit=False) as db:
                    result.extend(db[_PART_DATA_TABLE_NAME].query(query.sql, params=query.params))
                if len(result) >= limit:
                    break
            return [dict(row) for row in result] if to_dict else result

        # ORDER BY + LIMIT 下推：每 segment SQL 先取 top-N，再在 Python 归并，
        # 避免全表物化（UI /rows 大表分页依赖此路径），结果与不下推一致
        pushdown = bool(order_by) and limit is not None
        order_by_sql = (
            self._order_by_sql(order_by, fetch_fields, column_types)
            if pushdown
            else None
        )
        select_kwargs = (
            {'order_by_sql': order_by_sql, 'limit': limit}
            if pushdown
            else {}
        )

        full_data = []
        if not w.or_and_cases:
            # select all
            data = self._select_and_cases(
                fetch_fields, None, column_types, **select_kwargs
            )
            # full_data.append(data)
            if data:
                full_data.extend(data)
        elif len(w.or_and_cases) > 1:
            data = self._select_or_cases(
                fetch_fields,
                w.or_and_cases,
                column_types,
                **select_kwargs,
            )
            if data:
                full_data.extend(data)
        else:
            for cases in w.or_and_cases:
                # 一个基本查询单元
                data = self._select_and_cases(
                    fetch_fields, cases, column_types, **select_kwargs
                )
                # full_data.append((cases, data))
                # full_data.append(data)
                if data:
                    full_data.extend(data)
        # full_data = list(chain(*full_data))
        full_data = self._apply_order_by(full_data, order_by)
        if limit is not None:
            full_data = full_data[:limit]
        if project_fields is not None:
            full_data = self._project_fields(
                full_data, project_fields, to_dict=to_dict
            )
        elif to_dict:
            full_data = [dict(item) for item in full_data]
        return full_data

    def _normalize_limit(self, limit):
        if limit is None:
            return None
        if isinstance(limit, bool):
            raise BadArgument(
                f'limit 不接受布尔值 {limit}；'
                '修复: 传非负整数，如需无条件限制请传 None'
            )
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

    def _select_fields_with_order(self, fields, order_by, column_types):
        from .query_util import validate_select_fields
        validate_select_fields(fields, column_types)
        project_fields = self._plain_select_fields(fields)
        if project_fields is None:
            return fields, None

        fetch_fields = list(project_fields)
        for field, _ in self._parse_order_by(order_by):
            if field not in fetch_fields and field in column_types:
                fetch_fields.append(field)

        if fetch_fields == project_fields:
            return fields, None
        return fetch_fields, project_fields

    def _validate_projection_only(self, fields):
        """空表和 limit=0 仍然验证已声明的投影，错误不能取决于是否有数据。"""
        from .query_util import validate_select_fields
        spec = self.spec()
        if spec is not None:
            validate_select_fields(fields, spec.columns)

    def _plain_select_fields(self, fields):
        if isinstance(fields, (tuple, list)):
            values = list(fields)
        elif isinstance(fields, str):
            text = fields.strip()
            if text == '*' or '*' in text:
                return None
            values = [item.strip() for item in text.split(',') if item.strip()]
        else:
            return None

        plain_fields = []
        for item in values:
            if not isinstance(item, str):
                return None
            field = item.strip().strip('"').strip("'")
            if (
                not field
                or field == '*'
                or any(char in field for char in ' ()+-*/')
            ):
                return None
            plain_fields.append(field)
        return plain_fields

    def _project_fields(self, rows, fields, to_dict=False):
        # 冻结契约：to_dict=False + 投影必须返回 tuple/Row，而不是 dict
        if to_dict:
            return [
                {field: row[field] for field in fields}
                for row in rows
            ]
        return [
            tuple(row[field] for field in fields)
            for row in rows
        ]

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
            specs.append((field, direction == 'desc'))
        return specs

    def _order_by_sql(self, order_by, fetch_fields, column_types):
        """把 order_by 编译为参数化安全的 SQL 片段（标识符加引号，方向白名单）。"""
        specs = self._parse_order_by(order_by)
        fetch_set = (
            set(fetch_fields)
            if isinstance(fetch_fields, (list, tuple))
            else None
        )
        if fetch_set is not None and any(str(field).strip() == '*' for field in fetch_set):
            fetch_set = None
        parts = []
        for field, reverse in specs:
            if fetch_set is not None and field not in fetch_set:
                raise BadArgument(
                    f'order_by field `{field}` must be included in selected fields'
                )
            if column_types and field not in column_types:
                raise BadArgument(
                    f'order_by field `{field}` 不是表字段；'
                    '修复: 使用契约中声明的列名'
                )
            direction = 'desc' if reverse else 'asc'
            parts.append(f'{self._quote_identifier(field)} {direction}')
        return ', '.join(parts)

    def _apply_order_by(self, rows, order_by):
        specs = self._parse_order_by(order_by)
        if not specs or not rows:
            return rows

        ordered = list(rows)
        for field, reverse in reversed(specs):
            try:
                ordered = sorted(
                    ordered,
                    key=lambda row: self._row_sort_value(row, field),
                    reverse=reverse,
                )
            except TypeError:
                ordered = sorted(
                    ordered,
                    key=lambda row: str(self._row_sort_value(row, field)),
                    reverse=reverse,
                )
        return ordered

    def _row_sort_value(self, row, field):
        try:
            value = row[field]
        except (KeyError, IndexError) as e:
            raise BadArgument(
                f'order_by field `{field}` must be included in selected fields'
            ) from e
        # SQLite 默认把 NULL 视为最小值：ASC 在前，DESC 在后。
        return (value is not None, value)

    def _excluded_parts_by_case(self, infos, case):
        op, case_min, case_max = self._case_partition_range(case)
        if (getattr(self._partition, 'PARTITION_TYPE', None) == 'calendar'
                and getattr(self._partition, 'time_unit', None) == 's'
                and case.field == self._partition.TS_COL):
            # 分区范围沿用毫秒格式；segment 的原始秒值统计不经过此换算。
            case_min = case_min * 1000 if case_min is not None else None
            case_max = case_max * 1000 if case_max is not None else None
        excluded_parts = set()
        for info in infos:
            part = info["part"]
            if info["column"] == case.field:
                column_min = info.get("column_min")
                column_max = info.get("column_max")
                if not self._is_numeric_range(
                    case_min, case_max, column_min, column_max
                ):
                    continue
                if (
                    case_max is not None
                    and column_min is not None
                    and (
                        case_max < column_min
                        or (op in ['lt', '<'] and case_max <= column_min)
                    )
                ):
                    excluded_parts.add(part)
                if (
                    case_min is not None
                    and column_max is not None
                    and case_min >= column_max
                ):
                    excluded_parts.add(part)
        return excluded_parts

    def _case_partition_range(self, case):
        op, case_min, case_max = get_case_range(case)
        table_spec = self.spec()
        if (
            not table_spec
            or not table_spec.time
            or table_spec.time.unit != 'date'
            or case.field != table_spec.time.name
        ):
            return op, case_min, case_max

        value = case.value['value']
        try:
            return self._date_case_partition_range(table_spec.time, op, value)
        except (TypeError, ValueError, arrow.parser.ParserError):
            return op, case_min, case_max

    def _date_case_partition_range(self, time_column, op, value):
        if op in ['between_and', 'range']:
            start_ms, _ = self._date_value_partition_bounds(time_column, value[0])
            _, end_ms = self._date_value_partition_bounds(time_column, value[1])
            return op, start_ms, end_ms - 1
        if op in ['eq', '==', '=']:
            start_ms, end_ms = self._date_value_partition_bounds(time_column, value)
            return op, start_ms, end_ms - 1
        if op in ['gt', '>']:
            _, end_ms = self._date_value_partition_bounds(time_column, value)
            return op, end_ms, None
        if op in ['ge', '>=']:
            start_ms, _ = self._date_value_partition_bounds(time_column, value)
            return op, start_ms, None
        if op in ['lt', '<']:
            start_ms, _ = self._date_value_partition_bounds(time_column, value)
            return op, None, start_ms - 1
        if op in ['le', '<=']:
            _, end_ms = self._date_value_partition_bounds(time_column, value)
            return op, None, end_ms - 1
        return op, None, None

    def _date_value_partition_bounds(self, time_column, value):
        normalized = time_column.normalize_value(value)
        start = arrow.get(
            normalized,
            'YYYY-MM-DD',
            tzinfo=time_column.timezone,
        ).to('UTC')
        end = start.shift(days=1)
        return (
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        )

    def _is_numeric_range(self, case_min, case_max, column_min, column_max):
        values = [
            item for item in [case_min, case_max, column_min, column_max]
            if item is not None
        ]
        return all(isinstance(item, (int, float)) for item in values)

    def _excluded_parts_by_cases(self, infos, cases):
        excluded_parts = set()
        for case in cases:
            excluded = self._excluded_parts_by_case(infos, case)
            excluded_parts = excluded_parts.union(excluded)
        return excluded_parts

    def _candidate_part_tokens_by_partition_cases(self, cases):
        partition = self._partition
        if partition is None:
            spec = self.spec()
            partition = spec.to_partition() if spec else None
        if partition is None or not cases:
            return None

        candidates = None
        for case in cases:
            if case.field != partition.TS_COL:
                continue
            case_tokens = self._candidate_part_tokens_for_case(partition, case)
            if case_tokens is None:
                continue
            candidates = case_tokens if candidates is None else candidates & case_tokens
        return candidates

    def _candidate_part_tokens_for_case(self, partition, case):
        op = case.op
        value = case.value['value']
        try:
            if op in ['eq', '==', '=']:
                return {self._part_token(partition.get_key({partition.TS_COL: value}))}
            if op == 'in':
                return {
                    self._part_token(partition.get_key({partition.TS_COL: item}))
                    for item in value
                }
        except (KeyError, TypeError, ValueError):
            return None
        return None

    def _get_select_parts(self, cases):
        """case And[Case..]"""
        infos = self.get_part_column_ranges()
        candidate_tokens = self._candidate_part_tokens_by_partition_cases(cases)
        if cases:
            excluded_parts = self._excluded_parts_by_cases(infos, cases)
        else:
            excluded_parts = set()
        all_parts = self.table_parts()
        need_parts = sorted(set(all_parts) - set(excluded_parts))
        if candidate_tokens is not None:
            need_parts = [
                part for part in need_parts
                if self._part_token(part) in candidate_tokens
            ]
        return need_parts

    def _select_and_cases(self, fields, cases, column_types,
                          order_by_sql=None, limit=None):
        """获得And[Case..] 的查询结果
        And[Case..] 为一个基本查询单元
        order_by_sql/limit 提供时用于 ORDER BY + LIMIT 下推（每 segment 取 top-N）
        """
        # table field必须的，涉及到查询是否需要加 "
        # 分成独立文件是为了同步数据方便，也导致数据容易被盗
        need_refs = self._get_select_part_refs(cases)
        data = []
        for ref in need_refs:
            self._ensure_ref_file_present(ref)
            part_table_name = _PART_DATA_TABLE_NAME
            query = build_select_cases_query(
                part_table_name,
                fields,
                cases,
                column_types,
                order_by_sql=order_by_sql,
                limit=limit,
            )
            part_dbfile = self._get_part_dbfile(ref.part_key)

            logger.debug(
                f"{part_dbfile}: {ref.part_key}: "
                f"SQL: {query.sql}; params={query.params}"
            )
            part_table_name = _PART_DATA_TABLE_NAME
            with self._part_db(part_dbfile, auto_commit=False) as db:
                part_data = db[part_table_name].query(
                    query.sql,
                    params=query.params,
                )
            data.extend(part_data)

        return data

    def _iter_select_raw_batches(self, fields, where_obj, column_types,
                                 batch_size, deadline=None, limit=None):
        for ref, query in self._select_queries_for_where(
            fields,
            where_obj,
            column_types,
            limit=limit,
            skip_segment_stats=deadline is not None,
        ):
            self._ensure_ref_file_present(ref)
            part_dbfile = self._get_part_dbfile(ref.part_key)
            logger.debug(
                f"{part_dbfile}: {ref.part_key}: "
                f"SQL: {query.sql}; params={query.params}"
            )
            yield from self._iter_part_query_batches(
                part_dbfile,
                query,
                batch_size,
                deadline,
            )

    def _select_queries_for_where(self, fields, where_obj, column_types,
                                  order_by_sql=None, limit=None,
                                  skip_segment_stats=False):
        if not where_obj.or_and_cases:
            for ref in self._get_select_part_refs(
                None, skip_segment_stats=skip_segment_stats,
            ):
                yield (
                    ref,
                    build_select_cases_query(
                        _PART_DATA_TABLE_NAME,
                        fields,
                        None,
                        column_types,
                        order_by_sql=order_by_sql,
                        limit=limit,
                    ),
                )
            return

        if len(where_obj.or_and_cases) > 1:
            refs_by_key = OrderedDict()
            for cases in where_obj.or_and_cases:
                for ref in self._get_select_part_refs(
                    cases, skip_segment_stats=skip_segment_stats,
                ):
                    refs_by_key[self._segment_lookup_key(ref)] = ref
            query = build_select_or_cases_query(
                _PART_DATA_TABLE_NAME,
                fields,
                where_obj.or_and_cases,
                column_types,
                order_by_sql=order_by_sql,
                limit=limit,
            )
            for ref in refs_by_key.values():
                yield ref, query
            return

        cases = where_obj.or_and_cases[0]
        query = build_select_cases_query(
            _PART_DATA_TABLE_NAME,
            fields,
            cases,
            column_types,
            order_by_sql=order_by_sql,
            limit=limit,
        )
        for ref in self._get_select_part_refs(
            cases, skip_segment_stats=skip_segment_stats,
        ):
            yield ref, query

    def _iter_part_query_batches(self, part_dbfile, query, batch_size,
                                 deadline=None):
        stream = self._part_ordered_rows(part_dbfile, query, batch_size, deadline)
        reservation = reserve_process_connections(1, self._max_process_connections)
        try:
            if self._max_process_connections == 1:
                # 仅一个配额时先落盘当前文件的结果，释放连接后再交给消费者。
                # 避免消费者读写或 save_as 写目标时等待自己持有的连接。
                with tempfile.TemporaryFile() as spool:
                    names = None
                    row_cursor = None
                    batch_count = 0
                    try:
                        batch = []
                        for row in stream:
                            if names is None:
                                names = tuple(row.keys())
                            batch.append(tuple(row))
                            if len(batch) == batch_size:
                                pickle.dump(batch, spool, protocol=pickle.HIGHEST_PROTOCOL)
                                batch_count += 1
                                batch = []
                        if batch:
                            pickle.dump(batch, spool, protocol=pickle.HIGHEST_PROTOCOL)
                            batch_count += 1
                        stream.close()
                        if names and self._storage_engine == 'sqlite':
                            # 已关闭游标仍保留 description，可重建原生 sqlite3.Row。
                            with closing(sqlite3.connect(':memory:')) as conn:
                                row_cursor = conn.execute('SELECT ' + ','.join(
                                    'NULL AS ' + self._quote_identifier(name) for name in names
                                ))
                    finally:
                        stream.close()
                        release_process_connections(reservation)
                        reservation = None
                    spool.seek(0)
                    for _ in range(batch_count):
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError('SQLite query exceeded timeout_ms budget')
                        try:
                            batch = pickle.load(spool)
                        except (EOFError, pickle.UnpicklingError) as exc:
                            raise UnsafeOperation('单连接流的临时文件不完整或损坏') from exc
                        if row_cursor is not None:
                            yield [sqlite3.Row(row_cursor, values) for values in batch]
                        else:
                            from ..parquet.table import _Row
                            yield [_Row(names, values) for values in batch]
                return
            batch = []
            for row in stream:
                batch.append(row)
                if len(batch) == batch_size:
                    yield batch
                    batch = []
            if batch:
                yield batch
        finally:
            stream.close()
            release_process_connections(reservation)

    def _open_part_read_conn(self, part_dbfile):
        """为有序流式扫描打开专用只读连接（绕开分区连接池）。

        - mode=ro URI：绝不创建缺失文件；文件缺失/无法打开直接抛错（带
          part_dbfile），不做"静默空分区"——否则导出会 200 但缺数据；
        - query_only 防误写；cache_size 压到 1MB/连接（归并态多连接并发）；
        - temp_store=FILE：ORDER BY 的临时 B 树/排序器可落盘，RAM 有界
          （连接池默认 temp_store=memory，大 segment 排序会占 O(segment) 内存）；
        - 只读扫描不经过 _db_parts 池，不受 max_open_partitions 约束；
        - 允许同一串行读取器在不同工作线程恢复或关闭（Web 流断开时，
          ASGI 清理线程可能不同于推进迭代器的线程）；
          归并期间文件被 compact/repartition 替换时，游标继续读旧快照（一致性读）。
        """
        if not os.path.isfile(part_dbfile):
            raise UnsafeOperation(
                f'ordered stream part file missing: {part_dbfile}'
            )
        try:
            uri = pathlib.Path(os.path.abspath(part_dbfile)).as_uri()
            conn = sqlite3.connect(
                f'{uri}?mode=ro', uri=True, timeout=30.0,
                check_same_thread=False,
            )
        except (sqlite3.Error, ValueError) as exc:
            raise UnsafeOperation(
                f'ordered stream open part failed: {part_dbfile}: {exc}'
            ) from exc
        try:
            conn.row_factory = sqlite3.Row  # 字符串索引/字典化与池路径对齐
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA temp_store=FILE')
            conn.execute('PRAGMA cache_size=-1024')
        except sqlite3.Error as exc:
            conn.close()
            raise UnsafeOperation(
                f'ordered stream init part failed: {part_dbfile}: {exc}'
            ) from exc
        return conn

    def _part_ordered_rows(self, part_dbfile, query, chunk_size,
                           deadline=None):
        """单游标流式读取一个 part 的有序行（无 OFFSET）。

        一条 SQL 一个游标：每 part 只排序一次；游标存活期间 SQLite 保持
        语句级读快照（WAL 下不阻塞写入），part 内不重不漏；
        峰值驻留 = 该流当前 fetchmany 的 chunk 行。

        连接或查询失败都带 part_dbfile 抛出 UnsafeOperation，不静默跳过分片。
        """
        conn = self._open_part_read_conn(part_dbfile)
        try:
            if deadline is not None:
                conn.execute(f'PRAGMA busy_timeout={max(0, int((deadline - time.monotonic()) * 1000))}')
                conn.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline), 1000,
                )
            cursor = conn.execute(query.sql, query.params)
            try:
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            'SQLite query exceeded timeout_ms budget'
                        )
                    rows = cursor.fetchmany(chunk_size)
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            'SQLite query exceeded timeout_ms budget'
                        )
                    if not rows:
                        return
                    yield from rows
            finally:
                cursor.close()
        except sqlite3.DatabaseError as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    'SQLite query exceeded timeout_ms budget'
                ) from exc
            raise UnsafeOperation(
                f'ordered stream query part failed: {part_dbfile}: {exc}'
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _merge_stream_chunk(batch_size, part_count):
        """每 part 流的 fetch 粒度：驻留 ≈ P × chunk，总量钳在 ~10 万行。

        P=归并 part 数（受 max_ordered_read_connections 与进程预算约束）；
        chunk 至少为 1，保证 P × chunk 不因固定下限重新超过预算。cursor 存活
        期间不重排，小 chunk 无额外排序成本。
        """
        if part_count <= 0:
            return batch_size
        return max(1, min(batch_size, 100_000 // part_count))

    def _iter_select_ordered_batches(
        self, fields, where, limit, order_by, to_dict, batch_size,
        deadline=None,
    ):
        """带 order_by 的流式条件查询：跨 part k 路归并，内存有界。

        - 每 part 一条专用只读连接 + 单游标（无 OFFSET），单次排序流式产出；
          limit 下推为 per-part LIMIT（SQLite top-N 排序器，排序内存 O(limit)）；
        - 连接数受 max_ordered_read_connections 上限约束（默认 128，超出快速
          失败）；峰值 Python 驻留 ≈ P × chunk（总量钳在 ~10 万行）；
          每 part 的 SQLite 排序可落盘（temp_store=FILE），RAM 有界；
        - 连接或分片查询失败抛错（不静默空分区）；
        - 快照语义：流建立后本迭代不可见新增 part/行（part 内语句级快照，
          跨 part 非原子）；tie 优先早 part（与 select() 稳定排序一致）。
        fields 投影/to_dict/batch_size 语义不变。
        """
        meta = self.get_table_meta()
        if not meta:
            self._validate_projection_only(fields)
            return
        column_types = {
            key: item["type"] for key, item in meta["column_info"].items()
        }
        w = self._build_where(where)
        fetch_fields, project_fields = self._select_fields_with_order(
            fields,
            order_by,
            column_types,
        )
        order_by_sql = self._order_by_sql(order_by, fetch_fields, column_types)
        specs = self._parse_order_by(order_by)
        sort_key = self._row_sort_key(specs)

        stream_specs = []
        for ref, query in self._select_queries_for_where(
            fetch_fields,
            w,
            column_types,
            order_by_sql=order_by_sql,
            limit=limit,
            skip_segment_stats=deadline is not None,
        ):
            self._ensure_ref_file_present(ref)
            part_dbfile = self._get_part_dbfile(ref.part_key)
            logger.debug(
                f"{part_dbfile}: {ref.part_key}: "
                f"SQL: {query.sql}; params={query.params}"
            )
            stream_specs.append((part_dbfile, query))
        if not stream_specs:
            return

        # 有序归并需同时打开全部 part 的只读连接：超出上限直接快速失败，
        # 不做静默降级（否则既丢数据语义，又可能撞 fd/内存上限）
        max_conns = self._max_ordered_read_connections
        if len(stream_specs) > max_conns:
            raise BadArgument(
                f'ordered stream 需要 {len(stream_specs)} 条只读连接，'
                f'超过 max_ordered_read_connections={max_conns}；'
                '修复: 收窄 where 的时间范围，'
                '或调大 max_ordered_read_connections，'
                '或去掉 order_by 改用分区自然序流式导出'
            )

        chunk_size = self._merge_stream_chunk(batch_size, len(stream_specs))
        # 后端在预留连接前固定扫描元数据，避免占满配额后再次申请元数据库连接。
        part_streams = [
            self._part_ordered_rows(path, query, chunk_size, deadline)
            for path, query in stream_specs
        ]
        try:
            reservation = reserve_process_connections(
                len(stream_specs),
                self._max_process_connections,
            )
        except (BadArgument, ValueError) as exc:
            raise BadArgument(
                f'ordered stream 需要 {len(stream_specs)} 条只读连接，'
                f'超过当前进程可用的 max_process_connections='
                f'{self._max_process_connections}；{exc}'
            ) from exc

        merged_stream = None
        try:
            emitted = 0
            pending = []
            merged_stream = heapq.merge(*part_streams, key=sort_key)
            for row in merged_stream:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        'SQLite query exceeded timeout_ms budget'
                    )
                if limit is not None and emitted >= limit:
                    break
                if project_fields is not None:
                    row = self._project_fields([row], project_fields, to_dict)[0]
                elif to_dict:
                    row = dict(row)
                pending.append(row)
                emitted += 1
                if len(pending) >= batch_size:
                    yield pending
                    pending = []
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            'SQLite query exceeded timeout_ms budget'
                        )
            if pending:
                yield pending
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('SQLite query exceeded timeout_ms budget')
        finally:
            try:
                if merged_stream is not None:
                    merged_stream.close()
                for stream in part_streams:
                    stream.close()
            finally:
                release_process_connections(reservation)

    def _row_sort_key(self, specs):
        """构造对齐 SQLite 排序语义的 heap key 函数。"""
        def key(row):
            return tuple(
                _SqliteSortKey(self._row_sort_value(row, field)[1], reverse)
                for field, reverse in specs
            )
        return key

    def _select_or_cases(self, fields, or_cases, column_types,
                         order_by_sql=None, limit=None):
        refs_by_key = OrderedDict()
        for cases in or_cases:
            for ref in self._get_select_part_refs(cases):
                refs_by_key[self._segment_lookup_key(ref)] = ref

        data = []
        for ref in refs_by_key.values():
            self._ensure_ref_file_present(ref)
            query = build_select_or_cases_query(
                _PART_DATA_TABLE_NAME,
                fields,
                or_cases,
                column_types,
                order_by_sql=order_by_sql,
                limit=limit,
            )
            part_dbfile = self._get_part_dbfile(ref.part_key)
            logger.debug(
                f"{part_dbfile}: {ref.part_key}: "
                f"SQL: {query.sql}; params={query.params}"
            )
            with self._part_db(part_dbfile, auto_commit=False) as db:
                try:
                    part_data = db[_PART_DATA_TABLE_NAME].query(
                        query.sql,
                        params=query.params,
                    )
                except NoSuchTable:
                    continue
            data.extend(part_data)
        return data

    def create_index(self, column, order="asc", exist_ok=True):
        order_value = str(order or 'asc').strip().lower()
        if order_value not in ('asc', 'desc'):
            raise BadArgument(
                f'invalid index order: {order}；合法值: asc/desc'
            )
        with self._mutation_lock():
            spec = self.spec()
            if spec is None or column not in spec.columns:
                raise BadArgument(f'Unknown field `{column}`')
            for ref in self._physical_part_refs(lightweight=True):
                self._ensure_ref_file_present(ref)
                part_dbfile = self._get_part_dbfile(ref.part_key)
                with self._part_db(part_dbfile, auto_commit=True) as part_db:
                    part_db[_PART_DATA_TABLE_NAME].create_index(
                        column, order_value, exist_ok=exist_ok
                    )
            self.table_meta_db['_iosql_indexes'].insert(
                {'column_name': column, 'sort_order': order_value},
                pk=['column_name'], replace=True, ensure_table=True,
            )

    def _logical_indexes(self):
        """逻辑表的持久索引定义，供未来分区、compact 与迁移复用。"""
        try:
            return self.table_meta_db['_iosql_indexes'].query(
                'SELECT column_name, sort_order FROM _iosql_indexes', to_dict=True,
            )
        except NoSuchTable:
            return []

    def drop_index(self, column):
        dropped = 0
        with self._mutation_lock():
            for ref in self._physical_part_refs(lightweight=True):
                self._ensure_ref_file_present(ref)
                part_dbfile = self._get_part_dbfile(ref.part_key)
                with self._part_db(part_dbfile, auto_commit=True) as part_db:
                    dropped += part_db[_PART_DATA_TABLE_NAME].drop_column_index(column)
            try:
                self.table_meta_db['_iosql_indexes'].execute_params(
                    'DELETE FROM _iosql_indexes WHERE column_name=?', [column], commit=True,
                )
            except NoSuchTable:
                pass
        return dropped

    def upsert_all(
        self, kv_list, pk, partition=None, commit=True, **kwargs
    ):
        with self._mutation_lock():
            return self._upsert_all_locked(
                kv_list, pk, partition=partition, commit=commit, **kwargs,
            )

    def _upsert_all_locked(
        self, kv_list, pk, partition=None, commit=True, **kwargs
    ):
        """按受影响主键合并字段；缺字段保留旧值，显式 None 写 NULL。

        每组最多 100 个输入键，Python 内存随输入及命中行数增长；
        不读取或重写无关行。Segment.rows 仍按契约拒绝覆盖写入。
        """
        if isinstance(kv_list, dict):
            kv_list = [kv_list]
        elif not isinstance(kv_list, (list, tuple, set)):
            raise BadArgument("upsert_all: `list/tuple/set/dict` required for `kv_list`")

        if not kv_list:
            return

        self._ensure_pk_present(kv_list, pk)

        pk = [pk] if isinstance(pk, str) else list(pk)
        spec = self.spec()
        self._ensure_segment_conflict_supported(spec, 'replace')
        self._reject_cross_partition_replace(partition or self._partition, pk)
        # 只读取输入主键对应的行，避免一个键的修改物化并重写整张表。
        data = self._rows_for_primary_keys(kv_list, pk, spec, partition or self._partition)

        normalization_spec = self._pk_normalization_spec(kv_list, pk, spec, kwargs)
        # 查询返回的是物理存储值，还需应用主键列的排序规则。
        data = {
            self._stored_pk_key(item, pk, normalization_spec): item
            for item in data
        }
        keys = self._normalize_pk_keys(kv_list, pk, normalization_spec)
        for key, dic in zip(keys, kv_list):
            if key in data:
                data[key] = {**data[key], **dic}
            else:
                data[key] = dic

        self.insert_all(
            list(data.values()),
            pk=pk,
            replace=True,
            alter=True,
            partition=partition,
            commit=commit,
            **kwargs,
        )

    def _rows_for_primary_keys(self, rows, pk, spec, partition):
        """按主键中的分区列直达文件，避免对每个键重复枚举整库分区。"""
        groups = OrderedDict()
        known_parts = {self._part_token(part) for part in self.table_parts()}
        for row in rows:
            logical_part = partition.get_key(row) if partition else None
            groups.setdefault(logical_part, []).append(row)
        data = []
        for logical_part, group in groups.items():
            refs = []
            for ref in self._physical_part_refs([logical_part], lightweight=True):
                # 不存在的未登记 legacy 分区只是候选路径；已登记的段缺失必须报错。
                if (ref.segment_id < 0 and not os.path.exists(ref.path)
                        and self._part_token(logical_part) not in known_parts):
                    continue
                self._ensure_ref_file_present(ref)
                refs.append(ref)
            if not refs:
                continue
            for start in range(0, len(group), 100):
                query = build_select_primary_keys_query(
                    'data', '*', group[start:start + 100], pk, spec.columns,
                )
                for ref in refs:
                    with self._part_db(ref.path, auto_commit=False) as db:
                        data.extend(db['data'].query(query.sql, params=query.params, to_dict=True))
        return data

    def delete(self, where):
        """Delete rows matching the where clause from all relevant partitions.

        Args:
            where: Dict format where clause, e.g. {'field': 'id', 'op': '>', 'value': 10}
        """
        if where is None:
            warnings.warn(
                'delete(None) is deprecated; use delete_all() or '
                'truncate(confirm=True)',
                DeprecationWarning,
                stacklevel=2,
            )
            return self.delete_all()

        meta = self.get_table_meta()
        if not meta:
            return 0

        column_types = {key: item["type"] for key, item in meta["column_info"].items()}

        w = self._build_where(where)

        # 与 compact/write 共用 _compact_lock：防止 compact 读取旧 segment 后
        # delete 已删行被重建的新 segment“复活”（P0-2）
        with self._mutation_lock():
            total_deleted = 0
            for cases in w.or_and_cases:
                deleted = self._delete_and_cases(cases, column_types)
                total_deleted += deleted

        return total_deleted

    def delete_all(self):
        """显式清空全表（不要求 confirm；危险操作请优先用 truncate(confirm=True)）。"""
        return self._delete_all_count()

    def truncate(self, confirm=False):
        if not confirm:
            raise UnsafeOperation('truncate requires confirm=True')
        with self._mutation_lock():
            partitions = list(self.table_parts())
            self._delete_all_count()
        return WriteResult(partitions_touched=partitions)

    def _delete_all_count(self):
        meta = self.get_table_meta()
        if not meta:
            return 0
        column_types = {key: item["type"] for key, item in meta["column_info"].items()}
        with self._mutation_lock():
            return self._delete_and_cases(None, column_types)

    def _delete_and_cases(self, cases, column_types):
        """Delete rows matching And[Case..] from all relevant partitions."""
        need_refs = self._get_select_part_refs(cases)
        total_deleted = 0
        spec = self.spec()
        layout = spec.physical_layout if spec else PhysicalLayout()

        with self._mutation_lock():
            for ref in need_refs:
                self._ensure_ref_file_present(ref)
                part_dbfile = self._get_part_dbfile(ref.part_key)
                with self._part_db(part_dbfile, auto_commit=True) as db:
                    part_table = db[_PART_DATA_TABLE_NAME]

                    select_query = build_select_count_query(
                        _PART_DATA_TABLE_NAME, cases, column_types
                    )
                    count_before = part_table.query(
                        select_query.sql,
                        params=select_query.params,
                    )
                    count = dict(count_before[0])["COUNT(*)"] if count_before else 0

                    if count > 0:
                        delete_query = build_delete_query(
                            _PART_DATA_TABLE_NAME, cases, column_types
                        )
                        logger.debug(
                            f"{part_dbfile}: DELETE: {delete_query.sql}; "
                            f"params={delete_query.params}"
                        )
                        # 显式 commit=True：不能依赖共享 auto_commit（并发读会把它翻成
                        # False，连接归还时 rollback 导致删除丢失，P0-1）
                        db[_PART_DATA_TABLE_NAME].execute_params(
                            delete_query.sql,
                            delete_query.params,
                            commit=True,
                        )
                        total_deleted += count
                if count > 0 and ref.segment_id >= 0:
                    self._refresh_segment_info(
                        ref.logical_part,
                        ref.segment_id,
                        ref.part_key,
                        layout,
                    )

        return total_deleted

    def do_insert_all(
        self,
        data,
        pk,
        partition=None,
        replace=False,
        ignore=False,
        commit=True,
        **kwargs,
    ):
        # Use new partition system
        _t0 = time.time()

        # Determine which partition to use:
        # 1. If partition is explicitly passed, use it
        # 2. If _partition is already loaded from metadata, use it
        # 3. If neither, no partitioning (single table)
        part_obj = partition
        if part_obj is None:
            part_obj = self._partition

        table_spec = kwargs.get("table_contract") or kwargs.get("table_spec")
        segment_spec = TableContract.ensure(table_spec) if table_spec is not None else None
        use_segment = bool(segment_spec and segment_spec.segment)
        if use_segment and replace:
            self._ensure_segment_conflict_supported(segment_spec, 'replace')

        if part_obj is None:
            # No partitioning - put all data in a single partition
            logical_part = DEFAULT_LOGICAL_PART if use_segment else None
            partitioned_data = {logical_part: data}
        else:
            # Partition data by key
            partitioned_data = {}
            for row in data:
                key = part_obj.get_key(row)
                if key not in partitioned_data:
                    partitioned_data[key] = []
                partitioned_data[key].append(row)

        _t1 = time.time()
        logger.debug(f"Partition Time: {_t1 - _t0}")

        # Get ts_col for range calculation (needed for metadata)
        ts_col = part_obj.TS_COL if part_obj else None
        segment_kwargs = dict(kwargs)
        segment_kwargs.pop("table_contract", None)
        segment_kwargs.pop("table_spec", None)

        with self._mutation_lock():
            for part_key, part_data in partitioned_data.items():
                if use_segment:
                    self._do_insert_segmented(
                        part_key,
                        ts_col,
                        part_data,
                        pk,
                        partition=part_obj,
                        replace=replace,
                        ignore=ignore,
                        commit=commit,
                        table_spec=segment_spec,
                        **segment_kwargs,
                    )
                else:
                    self._do_insert_part(
                        part_key,
                        ts_col,
                        part_data,
                        pk,
                        partition=part_obj,
                        replace=replace,
                        ignore=ignore,
                        commit=commit,
                        **kwargs,
                    )
        _t2 = time.time()
        logger.debug(f"Insert Time: {_t2 - _t1}")
        keys = list(partitioned_data)
        return keys if len(keys) <= 1 else sorted(keys)

    def _do_insert_segmented(
        self,
        logical_part,
        part_column,
        part_data,
        pk,
        partition=None,
        replace=False,
        ignore=False,
        commit=True,
        table_spec=None,
        **kwargs,
    ):
        layout = table_spec.physical_layout
        rows = list(part_data)
        part_kwargs = dict(kwargs)
        part_kwargs["table_contract"] = table_spec.to_dict()

        if not pk or not (replace or ignore):
            self._append_segment_rows(
                logical_part,
                part_column,
                rows,
                pk,
                partition,
                replace,
                ignore,
                commit,
                layout,
                part_kwargs,
            )
            return

        # 批内先按持久类型去重，随后每文件、每组键一次查询；新增行统一 append。
        normalization_spec = self._pk_normalization_spec(rows, pk, table_spec, part_kwargs)
        normalized = self._unique_pk_rows(rows, pk, normalization_spec)
        existing_keys = set()
        known_parts = {self._part_token(part) for part in self.table_parts()}
        refs = []
        for ref in self._physical_part_refs([logical_part], lightweight=True):
            if (ref.segment_id < 0 and not os.path.exists(ref.path)
                    and self._part_token(logical_part) not in known_parts):
                continue
            self._ensure_ref_file_present(ref)
            refs.append(ref)
        for start in range(0, len(normalized), 100):
            chunk = normalized[start:start + 100]
            if refs:
                query = build_select_primary_keys_query(
                    'data', pk, [row for _, row in chunk], pk, normalization_spec.columns,
                )
            for ref in refs:
                with self._part_db(ref.path, auto_commit=False) as db:
                    for row in db['data'].query(query.sql, params=query.params):
                        existing_keys.add(self._stored_pk_key(row, pk, normalization_spec))
        pending = [row for key, row in normalized if key not in existing_keys]
        if pending:
            self._append_segment_rows(
                logical_part, part_column, pending, pk, partition,
                False, False, commit, layout, part_kwargs,
            )

    def _pk_normalization_spec(self, rows, pk, table_spec, write_kwargs):
        """使用持久列类型；首次写入时先固定与物理建表一致的推断结果。"""
        columns = dict(table_spec.columns if table_spec else {})
        columns.update(write_kwargs.get('column_type') or {})
        meta = self.get_table_meta()
        if meta and meta.get('column_info'):
            columns.update({
                column: info.get('type')
                for column, info in (meta.get('column_info') or {}).items()
                if info.get('type') is not None
            })
        else:
            columns = dict(self.suggest_column_types(
                rows, column_type=columns,
                n_suggest_check=write_kwargs.get('n_suggest_check', _DEFAULT_N_SUGGEST_CHECK),
            ))
            write_kwargs['column_type'] = columns
        if table_spec is None:
            return TableContract(primary_key=pk, columns=columns)
        return replace(table_spec, columns=columns)

    def _unique_pk_rows(self, rows, pk, table_spec):
        """用 SQLite 亲和性归一主键并保留首行，正确处理 text 列的数值输入。"""
        seen = set()
        unique = []
        for key, row in zip(self._normalize_pk_keys(rows, pk, table_spec), rows):
            if key not in seen:
                seen.add(key)
                unique.append((key, row))
        return unique

    def _stored_pk_key(self, row, pk, table_spec):
        """应用 SQLite 内置列排序规则，供输入键和物理存储键共用。"""
        values = []
        for field in pk:
            value = row[field]
            if self._storage_engine == 'sqlite' and isinstance(value, str):
                collation = _sqlite_declared_collation(table_spec.columns.get(field), field)
                if collation == 'NOCASE':
                    # SQLite NOCASE 只折叠 ASCII，遇到 NUL 后仍比较原始字节长度。
                    value = (
                        value.split('\0', 1)[0].translate(_SQLITE_NOCASE_TRANSLATION),
                        len(value.encode('utf-8')),
                    )
                elif collation == 'RTRIM':
                    value = value.rstrip(' ')
            values.append(value)
        return tuple(values)

    def _normalize_pk_keys(self, rows, pk, table_spec):
        """归一输入主键的持久类型，供批量 ignore 与局部 upsert 共用。"""
        with closing(sqlite3.connect(':memory:')) as conn:
            fields = [f'k{i}' for i in range(len(pk))]
            definitions = [
                f'{field} {_sqlite_affinity(table_spec.columns.get(column), column)}'
                for field, column in zip(fields, pk)
            ]
            conn.execute('CREATE TABLE keys (' + ','.join(definitions) + ', ordinal INTEGER)')
            conn.executemany('INSERT INTO keys VALUES (' +
                             ','.join('?' for _ in range(len(pk) + 1)) + ')',
                             [tuple(row[field] for field in pk) + (i,) for i, row in enumerate(rows)])
            return [self._stored_pk_key(dict(zip(pk, item[:-1])), pk, table_spec)
                    for item in conn.execute('SELECT * FROM keys ORDER BY ordinal')]

    def _do_insert_part(
        self,
        part,
        part_column,
        part_data,
        pk,
        partition=None,
        replace=False,
        ignore=False,
        commit=True,
        logical_part=None,
        **kwargs,
    ):
        # 一个table 一个文件夹
        # 因为database可能要查询table的状态，所以table的meta
        # 信息也放在 /database/meta.sqlite3中
        column_type = kwargs.get("column_type", None)  # 用户指定的类型
        if "columns" in kwargs:
            self.logger.error("`columns` deprecated, please use `column_type` instead")
            raise BadArgument("`columns` deprecated, please use `column_type` instead")

        alter = kwargs.get("alter")
        batch_size = kwargs.get("batch_size", _DEFAULT_BATCH_SIZE)
        n_suggest_check = kwargs.get("n_suggest_check", _DEFAULT_N_SUGGEST_CHECK)
        # 当前记录的大表结构
        current_meta = self.get_table_meta()
        if current_meta and set(current_meta["pk"] or []) != set(pk or []):
            raise TableSpecConflict(
                f"PrimaryKeyCanNotChange: supplied pk={pk}, "
                f"table {current_meta['pk']}"
            )
        # Compare partition meta dicts - only check if table already exists
        prev_partition_meta = current_meta.get("partition") if current_meta else None
        curr_partition_meta = partition.to_meta() if partition else None
        if (
            prev_partition_meta is not None
            and prev_partition_meta != curr_partition_meta
        ):
            table_contract = kwargs.get("table_contract") or {}
            force = table_contract.get("force_partition", False)
            if force:
                logger.warning(
                    f"强制使用新分区: prev={prev_partition_meta} curr={curr_partition_meta}"
                )
                current_meta["partition"] = curr_partition_meta
                self._overwrite_table_meta(current_meta)
            else:
                raise TableSpecConflict(
                    f"PartitionCanNotChange: "
                    f"prev={prev_partition_meta} curr={curr_partition_meta}"
                )

        part_dbfile = self._get_part_dbfile(part)
        try:
            part_table_name = _PART_DATA_TABLE_NAME
            with self._part_db(part_dbfile, auto_commit=True) as part_db:
                part_db[part_table_name].insert_all(
                    part_data,
                    replace=replace,
                    ignore=ignore,
                    batch_size=batch_size,
                    commit=commit,
                    ensure_table=False,
                )
        except NoSuchTable:
            # part的表不存在
            #   第一次创建，主表可能也不存在
            #   主表存在，只是该part的表不存在
            # 所以此种情况下，写入主表数据，且part表
            # alter = True 时，还需要检查column是否一致
            # _schema_lock 只串行化 schema 演进段（建分区/补列/meta 回写）：
            # 并发线程各自基于过时 current_meta 快照建文件会互相丢列并覆盖 meta
            with self._schema_lock:
                # 进锁重读 meta：函数入口的快照可能已被并发演进刷新
                current_meta = self.get_table_meta()
                data_suggested_types = self.suggest_column_types(
                    part_data, column_type=column_type, n_suggest_check=n_suggest_check
                )
                createtable_meta = build_createtable_meta(
                    data_suggested_types, pk=pk, partition=partition, **kwargs
                )
                if current_meta:
                    # evolve 语义一：新分区文件必须包含存量全部列，
                    # 相对存量的新列补齐到旧分区，保证旧分区缺失列读为 NULL
                    #（否则 SQLite 双引号字面量会把缺失列静默变成字符串）
                    fresh_types = {
                        col: type_
                        for col, type_ in data_suggested_types.items()
                        if col not in current_meta["column_info"]
                    }
                    new_column_info = current_meta["column_info"]
                    for col, item in createtable_meta["column_info"].items():
                        if col not in new_column_info:
                            new_column_info[col] = item
                    createtable_meta["column_info"] = new_column_info
                    if fresh_types:
                        self._add_columns_for_all_partitions(fresh_types)
                self._sync_contract_columns(createtable_meta)

                # 确保表格建好后，才写meta，确保不删列(不支持)情况下，meta中有的信息, 表中都有
                self.do_create_table(part_dbfile, part_table_name, createtable_meta, alter)

                # update meta
                meta_part = part if logical_part is None else logical_part
                self._update_meta(meta_part, part_column, partition, createtable_meta)

            # 数据写在不持锁：schema 已定型且 ensure_table=False 不再触发改动，
            # 持锁会让不同分区的初始批量导入线程整个写入期互相串行
            with self._part_db(part_dbfile, auto_commit=True) as part_db:
                part_db[part_table_name]._invalidate_cache()
                part_db[part_table_name].insert_all(
                    part_data,
                    replace=replace,
                    ignore=ignore,
                    batch_size=batch_size,
                    commit=commit,
                    ensure_table=False,
                )
        except NoSuchColumn as e:
            # add column for all partition-table
            # alter table
            # 如果该part_data
            if not alter:
                raise e

            # 与 NoSuchTable 分支共用 _schema_lock：加列期间禁止其他线程
            # 基于旧快照建新分区或再加列
            with self._schema_lock:
                # 进锁重读 meta：函数入口的快照可能已被并发演进刷新
                current_meta = self.get_table_meta()
                data_suggested_types = self.suggest_column_types(
                    part_data, column_type=column_type, n_suggest_check=n_suggest_check
                )
                self._add_columns_for_all_partitions(data_suggested_types)

                createtable_meta = build_createtable_meta(
                    data_suggested_types, pk=pk, partition=partition, **kwargs
                )

                # 数据库中没有的列增加，但已经有的，且insert数据没有的字段，并不会删除
                # 所以要更新 createtable_meta
                if current_meta:
                    new_column_info = current_meta["column_info"]
                    for col, item in createtable_meta["column_info"].items():
                        if col not in new_column_info:
                            new_column_info[col] = item
                    createtable_meta["column_info"] = new_column_info
                self._sync_contract_columns(createtable_meta)

                # update meta
                meta_part = part if logical_part is None else logical_part
                self._update_meta(meta_part, part_column, partition, createtable_meta)

            # 数据写在不持锁：列已补齐且 ensure_table=False 不再触发改动（同 NoSuchTable 分支）
            with self._part_db(part_dbfile, auto_commit=True) as part_db:
                part_db[part_table_name].insert_all(
                    part_data,
                    replace=replace,
                    ignore=ignore,
                    batch_size=batch_size,
                    commit=commit,
                    ensure_table=False,
                )

    def _update_meta(self, part_key, ts_col, partition, createtable_meta):
        # 更新table创建的meta信息，直接覆盖
        # Store partition info in createtable_meta if it exists
        if partition is not None:
            createtable_meta["partition"] = partition.to_meta()
        contract_data = createtable_meta.get("table_contract")
        if isinstance(contract_data, dict):
            self._persist_contract_metadata(
                TableContract.from_dict(contract_data)
            )
        self._overwrite_table_meta(createtable_meta)

        # 增加table_part信息, 设置更新时间，方便今后缓存访问
        _t0 = time.time()
        updated_at = arrow.now().format("YYYY-MM-DD HH:mm:ss")
        self.upsert_part_info(part_key, updated_at)
        _t1 = time.time()
        logger.debug(f"upsert_part_info Time: {_t1 - _t0}")

        # Update partition column ranges using new partition system
        column_min, column_max = None, None
        if partition is not None:
            start_ms, end_ms = partition.get_range(part_key)
            column_min = start_ms
            column_max = end_ms
        self.upsert_part_column_range(part_key, ts_col, column_min, column_max)
        _t2 = time.time()
        logger.debug(f"upsert_part_column_range Time: {_t2 - _t1}")

    def get_table_meta(self):
        try:
            rows = self.table_meta_db[_TABLE_META_TABLE_NAME].query(
                f'select meta from {_TABLE_META_TABLE_NAME} where table_name = ?',
                params=[self._table_name],
                to_dict=True,
            )
            if rows:
                meta = json.loads(rows[0]["meta"])
                meta["column_info"] = OrderedDict(meta["column_info"])
                return meta
            return None
        except NoSuchTable:
            return None

    def table_names(self):
        return iosql_table_names(self.db_path, backend=self._storage_engine)

    def _overwrite_table_meta(self, createtable_meta):
        """table 创建信息"""
        # 某个表格的各分区最大最小数值
        # table_meta_db = MyDatabase(self.table_meta_path, pragma_dict=self._pragma_dict)
        # table_meta_db.enable_wal()

        # 扩展元信息只写 sidecar；legacy JSON 继续保持旧版契约结构。
        legacy_meta = dict(createtable_meta)
        for contract_key in ("table_contract", "table_spec"):
            contract = legacy_meta.get(contract_key)
            if isinstance(contract, dict):
                contract = dict(contract)
                for metadata_key in (
                    "description",
                    "column_descriptions",
                    "column_ranges",
                ):
                    contract.pop(metadata_key, None)
                legacy_meta[contract_key] = contract

        # OrderDict
        createtable_meta["column_info"] = list(createtable_meta["column_info"].items())
        legacy_meta["column_info"] = createtable_meta["column_info"]
        meta = json.dumps(legacy_meta)

        updated_at = arrow.now().format("YYYY-MM-DD HH:mm:ss")
        info = {
            "table_name": self._table_name,
            "meta": meta,
            "updated_at": updated_at,
        }
        self.table_meta_db[_TABLE_META_TABLE_NAME].insert(
            info,
            pk=["table_name"],
            replace=True,
            ensure_table=True,
            commit=True,
        )
        # decoupled
        # self.meta_db[_DB_META_TABLE_NAME].insert(
        #    info, pk=['table_name'], replace=True,
        #    ensure_table=True
        # )

    def upsert_part_info(self, part, updated_at):
        """每个表有哪些分区

        更新每个partition的时间，方便后续缓存访问
        """
        # 某个表格的各分区最大最小数值
        info = {
            "table_name": self._table_name,
            "part": self._part_meta_value(part),
            "updated_at": updated_at,
        }
        # table_meta_db = MyDatabase(self.table_meta_path)
        # table_meta_db.enable_wal()
        self.table_meta_db[_TABLE_PART_TABLE_NAME].insert(
            info,
            pk=["table_name", "part"],
            replace=True,
            ensure_table=True,
            commit=True,
        )

    def table_parts(self):
        try:
            parts = self.table_meta_db[_TABLE_PART_TABLE_NAME].query(
                f'select * from {_TABLE_PART_TABLE_NAME} where table_name = ?',
                params=[self._table_name],
                to_dict=True,
            )
        except NoSuchTable:
            return []
        return sorted([item["part"] for item in parts])

    def upsert_part_column_range(self, part, column, column_min, column_max):
        """某个表格的各分区: 各个字段允许的的最大/最小数值"""
        # table_meta_db = MyDatabase(self.table_meta_path)
        # table_meta_db.enable_wal()
        # 增加part的编号: 不必要
        # 未来可以更新，实际的最大数值/最小数值，方便快速查找
        # column_max, column_min: None 表示没有最大/小
        info = {
            "table_name": self._table_name,
            "part": self._part_meta_value(part),
            "column": self._meta_key_value(column),
            "column_min": column_min,
            "column_max": column_max,
        }
        _tblname = _TABLE_PART_COLUMN_RANGE_TABLE_NAME
        self.table_meta_db[_tblname].insert(
            info,
            pk=["table_name", "part", "column"],
            ensure_table=True,
            replace=True,
            commit=True,
        )

    def get_part_column_ranges(self, table_name=None):
        _tblname = _TABLE_PART_COLUMN_RANGE_TABLE_NAME
        if table_name is None:
            table_name = self._table_name
        try:
            infos = self.table_meta_db[_tblname].query(
                f'select * from {_tblname} where table_name = ?',
                params=[table_name],
                to_dict=True,
            )
        except NoSuchTable:
            return []
        return infos

    def _rows_from_part_dbfile(self, part_dbfile):
        if not os.path.isfile(part_dbfile):
            return []
        with self._part_db(part_dbfile, auto_commit=False) as db:
            try:
                rows = db[_PART_DATA_TABLE_NAME].select('*', where=None, to_dict=True)
            except NoSuchTable:
                return []
        return [dict(row) for row in rows]

    def _part_rows(self, part):
        rows = []
        for ref in self._physical_part_refs([part]):
            self._ensure_ref_file_present(ref)
            rows.extend(self._rows_from_part_dbfile(self._get_part_dbfile(ref.part_key)))
        return rows

    def _partition_row_count(self, part):
        return sum(ref.row_count for ref in self._physical_part_refs([part]))

    def _partition_time_range(self, part, rows=None):
        spec = self.spec()
        if not spec or not spec.time:
            return None, None
        rows = self._part_rows(part) if rows is None else rows
        values = [
            row.get(spec.time.name)
            for row in rows
            if row.get(spec.time.name) is not None
        ]
        if not values:
            return None, None
        return min(values), max(values)

    def layout_report(self):
        spec = self.spec()
        layout = spec.physical_layout if spec else PhysicalLayout()
        stats = []
        refs = self._physical_part_refs()
        for ref in refs:
            row_count = ref.row_count
            stats.append(PartitionStats(
                partition_id=ref.part_key,
                path=self._get_part_dbfile(ref.part_key),
                row_count=row_count,
                time_min=ref.time_min,
                time_max=ref.time_max,
                oversized=row_count > layout.max_rows_per_file,
                undersized=(
                    row_count < layout.min_rows_per_file
                    and row_count > 0
                    and len(refs) > 1
                ),
            ))

        recommendation = 'layout_ok'
        if any(item.oversized for item in stats):
            recommendation = (
                'repartition_to a smaller time interval or a row-aware layout'
            )
        elif len(stats) > 1 and all(item.undersized for item in stats):
            recommendation = 'repartition_to a larger time interval'

        return LayoutReport(
            table=self._table_name,
            target_rows_per_file=layout.target_rows_per_file,
            min_rows_per_file=layout.min_rows_per_file,
            max_rows_per_file=layout.max_rows_per_file,
            partitions=stats,
            recommendation=recommendation,
        )

    def repartition_to(self, target_uri, table_name=None, spec=None,
                       mode='append', batch_size=10_000):
        from ...migration import repartition
        return repartition(self, target_uri, table_name=table_name, spec=spec,
                           mode=mode, batch_size=batch_size)

    def _add_columns_for_all_partitions(self, data_suggested_types):
        """
        params:
            n_suggest_check 使用多少条数据检查类型
        """
        # createtable_meta = build_createtable_meta(data_suggested_types,
        #                                          pk=pk,
        #                                          partition=partition, **kwargs)

        # 每个part都手动增加字段
        for ref in self._physical_part_refs():
            self._ensure_ref_file_present(ref)
            logger.info(f"Altering Table Schema: {ref.part_key} ..")
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=True) as part_db:
                for column, type_ in data_suggested_types.items():
                    try:
                        part_db[_PART_DATA_TABLE_NAME].add_column(column, type_)
                        logger.info(f"Added {column} {type_}")
                    except DuplicateColumn:
                        pass

    def suggest_column_types(self, data, column_type, n_suggest_check=None):
        return suggest_sqlite_column_types(
            data, column_type=column_type, n_check=n_suggest_check
        )

    def do_create_table(self, part_dbfile, part_table_name, createtable_meta, alter):
        """
        ensure创建主表meta信息，创建part, 写入part数据
        {'column_info': OrderedDict([('id',
                                {'comment': None, 'default': None,
                               'dtype': None,
                               'index_created': False,
                               'is_pk': True,
                               'name': 'id',
                               'not_null': False,
                               'should_index': False,
                               'type': 'integer'}),
                               )
        }

        mysqlite
        [{
            'name': 'column1',
            'type': 'text'
            'default': default,
            'unique': False
            'not_null': not_null,
            'dtype': 'datetime'
            'comment': '姓名'
        }]
        """
        mysqlite_columns = []
        for column, item in createtable_meta["column_info"].items():
            dic = {
                "name": column,
                "type": item["type"],
                "default": item.get("default"),
                "unique": item.get("unique"),
                "not_null": item.get("not_null"),
                "dtype": item.get("dtype"),
                "comment": item.get("comment"),
            }
            mysqlite_columns.append(dic)

        indexes = self._logical_indexes()
        with self._part_db(part_dbfile, auto_commit=True) as db:
            db[part_table_name].create_table(
                mysqlite_columns,
                pk=createtable_meta["pk"],
                save_meta=True,
                table_comment=None,
            )
            for definition in indexes:
                db[part_table_name].create_index(
                    definition['column_name'], definition['sort_order'], exist_ok=True,
                )

    @property
    def table_meta_db(self):
        return self.get_table_meta_db(auto_commit=True)

    def get_table_meta_db(self, auto_commit=True):
        # self._table_meta_db = 3
        if not self._table_meta_db or self._table_meta_db.is_closed():
            self._table_meta_db = MyDatabase(
                self.table_meta_path,
                auto_commit=auto_commit,
                verbose=self.verbose,
                pragma_dict=self._pragma_dict,
                max_connections=self._max_connections_per_file,
                max_process_connections=self._max_process_connections,
            )
        else:
            pass
        return self._table_meta_db

    @property
    def meta_db(self):
        return self.get_meta_db(auto_commit=True)

    def get_meta_db(self, auto_commit=True):
        if not self._meta_db or self._meta_db.is_closed():
            self._meta_db = MyDatabase(
                self.db_meta_path,
                auto_commit=auto_commit,
                verbose=self.verbose,
                pragma_dict=self._pragma_dict,
                max_connections=self._max_connections_per_file,
                max_process_connections=self._max_process_connections,
            )
        return self._meta_db

    @contextmanager
    def _part_db(self, part_dbfile, auto_commit=True):
        db = self._acquire_part_db(part_dbfile, auto_commit)
        thread_id = threading.get_ident()
        with self._db_parts_condition:
            self._db_parts_lease_threads[thread_id] = (
                self._db_parts_lease_threads.get(thread_id, 0) + 1
            )
        try:
            yield db
        finally:
            with self._db_parts_condition:
                thread_leases = self._db_parts_lease_threads.get(thread_id, 0)
                if thread_leases <= 1:
                    self._db_parts_lease_threads.pop(thread_id, None)
                else:
                    self._db_parts_lease_threads[thread_id] = thread_leases - 1
                leases = self._db_parts_leases.get(part_dbfile, 0)
                if leases <= 1:
                    self._db_parts_leases[part_dbfile] = 0
                else:
                    self._db_parts_leases[part_dbfile] = leases - 1
                self._db_parts_condition.notify_all()

    def _acquire_part_db(self, part_dbfile, auto_commit):
        while True:
            db_to_close = None
            with self._db_parts_condition:
                while self._db_parts_closing:
                    self._db_parts_condition.wait(timeout=0.1)
                if self._db_parts_closed:
                    raise UnsafeOperation(
                        'table 已关闭，不能再获取分区连接；'
                        '修复: 重新通过 Database.table() 打开表'
                    )
                db = self._db_parts.get(part_dbfile)
                if db is not None and not db.is_closed():
                    db.auto_commit = auto_commit
                    self._db_parts.move_to_end(part_dbfile)
                    self._db_parts_status[part_dbfile] = True
                    self._db_parts_leases[part_dbfile] = (
                        self._db_parts_leases.get(part_dbfile, 0) + 1
                    )
                    return db

                if self._open_part_db_count_locked() < self._num_opened_max_allowed:
                    db = MyDatabase(
                        part_dbfile,
                        pragma_dict=self._pragma_dict,
                        auto_commit=auto_commit,
                        max_connections=self._max_connections_per_file,
                        max_process_connections=self._max_process_connections,
                    )
                    self._db_parts[part_dbfile] = db
                    self._db_parts_status[part_dbfile] = True
                    self._db_parts_leases[part_dbfile] = 1
                    return db

                for key, candidate in list(self._db_parts.items()):
                    if self._db_parts_leases.get(key, 0) != 0:
                        continue
                    if candidate and not candidate.is_closed():
                        db_to_close = candidate
                        del self._db_parts[key]
                        self._db_parts_status.pop(key, None)
                        self._db_parts_leases.pop(key, None)
                        logger.debug(f"Clean up, closing {key} ..")
                        break

                if db_to_close is None:
                    self._db_parts_condition.wait(timeout=0.1)
                    continue

            db_to_close.close()

    def _open_part_db_count(self, exclude=None):
        exclude = set(exclude or [])
        with self._db_parts_condition:
            return self._open_part_db_count_locked(exclude)

    def _open_part_db_count_locked(self, exclude=None):
        exclude = set(exclude or [])
        return sum(
            1
            for key, db in self._db_parts.items()
            if key not in exclude and db and not db.is_closed()
        )

    def pragma(self, key, value=None):
        self._pragma_dict[key] = value

    def create_table(self, column_type, pk, partition=None, **kwargs):
        """手动创建表结构（非 insert 路径）。

        通过推断列类型并调用 do_create_table 在主分区文件上建表。
        """
        self.suggest_column_types(
            [], column_type=column_type, n_suggest_check=None
        )
        createtable_meta = build_createtable_meta(
            column_type, pk=pk, partition=partition, **kwargs
        )
        part_key = DEFAULT_LOGICAL_PART if self.spec() and self.spec().segment else None
        part_dbfile = self._get_part_dbfile(part_key)
        self.do_create_table(
            part_dbfile, _PART_DATA_TABLE_NAME, createtable_meta,
            alter=kwargs.get('alter', False),
        )
