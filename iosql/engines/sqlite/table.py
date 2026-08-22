from __future__ import annotations

import copy
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import warnings

# from itertools import chain
from collections import OrderedDict
from contextlib import contextmanager

import arrow

from ...exception import (
    BadArgument,
    InvalidUriSchema,
    MissingPrimaryKey,
    MissingTimeColumn,
    SchemaConflict,
    TableSpecConflict,
    UnsafeOperation,
)
from ...logger import default_logger as logger
from ...partition import CalendarPartition, DurationPartition, Partition
from ...core.query import Case, CaseExpression, Where, get_case_range
from ...spec import (
    CleanupResult,
    CompactResult,
    DEFAULT_LOGICAL_PART,
    LayoutReport,
    PartitionStats,
    PhysicalLayout,
    QueryPlan,
    QuerySpec,
    RepartitionResult,
    TableContract,
    TableMigrationResult,
    SegmentValidationResult,
    SegmentInfo,
    TableSpec,
    WriteResult,
)
from ...table import BaseTable, resolve_select_fields
from ...utils.column import sqlite_type_of_value, suggest_sqlite_column_types
from ...utils.createtable import build_createtable_meta
from ...utils.mysqlite import Database as MyDatabase
from ...utils.mysqlite.const import _DEFAULT_PRAGMA as _MYSQLITE_DEFAULT_PRAGMA
from ...utils.schema import parse_db_schema

# from mysqlite import Database as MyDatabase
from ...utils.mysqlite.exception import DuplicateColumn, NoSuchColumn, NoSuchTable
from .const import (
    _DB_META_FILE,
    _DB_META_TABLE_NAME,
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
    build_delete_query,
    build_select_cases_query,
    build_select_or_cases_query,
)


# 用户可声明的 SQLite 合法类型名 → 库规范类型。表外的自定义声明
# （timestamp/json 等）不做强校验，回落宽松行为，避免误拒既有用法
_SQLITE_TYPE_ALIASES = {
    'int': 'integer',
    'integer': 'integer',
    'bigint': 'integer',
    'smallint': 'integer',
    'tinyint': 'integer',
    'mediumint': 'integer',
    'boolean': 'integer',
    'bool': 'integer',
    'float': 'float',
    'real': 'float',
    'double': 'float',
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


def _canon_sqlite_type(type_):
    """声明类型归一到库规范类型（integer/float/text/blob）；无法识别返回 None。"""
    value = str(type_).strip().lower().split('(', 1)[0].strip()
    return _SQLITE_TYPE_ALIASES.get(value)


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
        return iter(self.to_list())

    def spec(self):
        return self._query_spec


class _BufferedWriter:
    def __init__(self, table, mode=None, max_rows=10_000, **write_kwargs):
        self._table = table
        self._mode = mode
        self._max_rows = int(max_rows)
        self._write_kwargs = write_kwargs
        self._buffer = []
        self._results = []
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
        result = self._table.write(rows, mode=self._mode, **self._write_kwargs)
        self._results.append(result)
        return result

    def close(self):
        return self.flush()

    def results(self):
        return list(self._results)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if exc_type is None:
            self.flush()


class Table(BaseTable):
    """
    定位：有状态的Table
    分区模式，因为不知道在哪个分区，插入时才创建.sqlite3文件
    """

    def __init__(self, *args, **kwargs):
        pragma_dict = kwargs.pop("pragma_dict", None)
        max_open_partitions = kwargs.pop("max_open_partitions", 5)
        max_connections_per_file = kwargs.pop("max_connections_per_file", 1)
        max_process_connections = kwargs.pop("max_process_connections", 32)
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
        self._pragma_dict = copy.deepcopy(_MYSQLITE_DEFAULT_PRAGMA)
        self._pragma_dict.update(pragma_dict or {})
        self._meta_db = None
        self._table_meta_db = None
        self._db_parts = OrderedDict()
        self._db_parts_status = OrderedDict()
        self._db_parts_leases = {}
        self._db_parts_condition = threading.Condition(threading.RLock())
        self._db_parts_closing = False
        self._compact_lock = threading.RLock()
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

    def connect(self):
        """连接
        connect called by super().__init__
        """
        path = self.uri_options.get("path")
        db = self.uri_options.get("db")
        # path 可以为''
        if path is None or not db:
            raise InvalidUriSchema()

        self.db_path = os.path.join(path, db)
        self.db_meta_path = os.path.join(self.db_path, _DB_META_FILE)

        self.table_path = os.path.join(self.db_path, self._table_name)
        self.table_meta_path = os.path.join(self.table_path, _TABLE_META_FILE)
        self.table_part_path = os.path.join(self.table_path, _TABLE_PARTS_SUBDIR)
        logger.debug("Database Connected")

        self._table_meta_db = self.get_table_meta_db()
        self._meta_db = self.get_meta_db()

    def close(self):
        with self._db_parts_condition:
            while self._db_parts_closing:
                self._db_parts_condition.wait(timeout=0.1)
            self._db_parts_closing = True
            while any(self._db_parts_leases.values()):
                self._db_parts_condition.wait(timeout=0.1)
            part_databases = list(self._db_parts.values())
            self._db_parts.clear()
            self._db_parts_status.clear()
            self._db_parts_leases.clear()
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
                self._db_parts_condition.notify_all()

    def rename(self, new_name, exist_ok=False, overwrite=False):
        """重命名 SQLite 逻辑表目录并刷新表内 manifest。"""
        new_name = self._validate_rename_table_name(new_name)
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
        if os.path.exists(target_path):
            if not overwrite:
                raise BadArgument(f'table already exists: {new_name}')
            if not os.path.isdir(target_path):
                raise UnsafeOperation(
                    f'table rename target exists and is not a directory: {target_path}'
                )

        self.close()
        if os.path.exists(target_path):
            shutil.rmtree(target_path)
        os.replace(source_path, target_path)

        self._table_name = new_name
        self.connect()
        self._rename_table_manifest(old_name, new_name)
        self._partition = self._load_partition()
        return self

    def _validate_rename_table_name(self, name):
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
        for table_name in [
            _TABLE_META_TABLE_NAME,
            _TABLE_PART_TABLE_NAME,
            _TABLE_PART_COLUMN_RANGE_TABLE_NAME,
            _TABLE_SEGMENT_TABLE_NAME,
        ]:
            try:
                self.table_meta_db[table_name].execute_params(
                    f'update {table_name} set table_name = ? where table_name = ?',
                    [new_name, old_name],
                    commit=True,
                )
            except NoSuchTable:
                continue

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
        """Set the partition object for this table and persist to metadata.

        Args:
            value: Partition object (DurationPartition, CalendarPartition) or None

        Raises:
            Exception: If partition type changes after table has data
        """
        self._partition = value

        meta = self.get_table_meta()
        if meta is None:
            return

        meta["partition"] = value.to_meta() if value else None
        self._overwrite_table_meta(meta)

    def use_contract(self, contract):
        contract = TableContract.ensure(contract)
        current_contract = self.spec()
        if current_contract:
            self._ensure_spec_compatible(current_contract, contract)
        self._declared_spec = contract
        self._partition = contract.to_partition()
        return self

    def use_spec(self, spec):
        return self.use_contract(spec)

    def spec(self):
        stored_spec = TableContract.from_table_meta(self.get_table_meta())
        if stored_spec:
            return stored_spec
        return self._declared_spec

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

        contract = self.spec()
        if contract:
            self._ensure_contract_type_compatible(contract, data)
            pk = pk if pk is not None else contract.primary_key
            partition = partition if partition is not None else contract.to_partition()
            column_type = dict(contract.columns or {})
            column_type.update(kwargs.pop('column_type', {}) or {})
            kwargs['column_type'] = column_type
            kwargs.setdefault('alter', contract.schema_policy == 'evolve')
            kwargs['table_contract'] = contract.to_dict()
            if not replace and not ignore:
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

        before_parts = set(self.table_parts())
        before_meta = self.get_table_meta()
        rows = self._add_extra_data(data, extra_data)

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
            **kwargs,
        )

    def write_buffer(self, mode=None, max_rows=10_000, **write_kwargs):
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
                       columns=None):
        fields = resolve_select_fields(fields, columns, 'explain_select')
        spec = QuerySpec(
            table=self._table_name,
            columns=fields,
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

        with self._compact_lock:
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
        for ref in self._physical_part_refs(include_obsolete=include_obsolete):
            result.checked_segments += 1
            self._validate_segment_ref(ref, result)
            if deep:
                self._validate_segment_ref_deep(ref, result)
        return result

    def cleanup_obsolete(self, partition=None, dry_run=True, validate=True):
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

        result.manifest_rows_deleted = self._delete_segment_manifest_rows(deleted_refs)
        return result

    def iter_rows(self, batch_size=10_000):
        if isinstance(batch_size, bool):
            raise BadArgument('batch_size must be a positive integer')
        batch_size = int(batch_size)
        if batch_size <= 0:
            raise BadArgument('batch_size must be a positive integer')

        for ref in self._physical_part_refs():
            part_dbfile = self._get_part_dbfile(ref.part_key)
            offset = 0
            while True:
                with self._part_db(part_dbfile, auto_commit=False) as db:
                    try:
                        rows = db[_PART_DATA_TABLE_NAME].query(
                            (
                                f'select * from {_PART_DATA_TABLE_NAME} '
                                'limit ? offset ?'
                            ),
                            params=[batch_size, offset],
                            to_dict=True,
                        )
                    except NoSuchTable:
                        rows = []
                if not rows:
                    break
                batch = [dict(row) for row in rows]
                yield batch
                if len(batch) < batch_size:
                    break
                offset += len(batch)

    def count_rows(self):
        total = 0
        for ref in self._physical_part_refs():
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=False) as db:
                try:
                    total += db[_PART_DATA_TABLE_NAME].count()
                except NoSuchTable:
                    continue
        return total

    def migrate_to(self, target_uri, target_table=None, mode='append',
                   batch_size=10_000, verify=None, dry_run=False):
        spec = self.spec()
        if not spec:
            raise BadArgument('migrate_to requires a TableContract')
        verify = self._normalize_migration_verify(verify)
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
        batch_size = self._normalize_migration_batch_size(batch_size)
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
        batch_size = self._normalize_migration_batch_size(batch_size)
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

    def _normalize_migration_batch_size(self, batch_size):
        if isinstance(batch_size, bool):
            raise BadArgument('batch_size must be a positive integer')
        batch_size = int(batch_size)
        if batch_size <= 0:
            raise BadArgument('batch_size must be a positive integer')
        return batch_size

    def _normalize_migration_verify(self, verify):
        if verify in [None, False, '']:
            return None
        value = str(verify).strip().lower()
        if value in ['none', 'false', '0']:
            return None
        if value != 'row-count':
            raise BadArgument('verify only supports row-count')
        return value

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

        rows = []
        for ref in refs:
            rows.extend(
                self._rows_from_part_dbfile(self._get_part_dbfile(ref.part_key))
            )

        rows = self._compact_sort_rows(rows, spec)
        chunks = self._compact_row_chunks(
            rows,
            spec.primary_key,
            target_rows,
        )

        result = CompactResult(
            table=self._table_name,
            partitions=[logical_part],
            rows_read=len(rows),
            old_segments=len(refs),
        )
        new_infos = []
        try:
            new_infos = self._write_compact_chunks(
                logical_part,
                chunks,
                spec,
            )
            self._mark_segments_obsolete(refs)
        except Exception:
            self._mark_segments_obsolete(new_infos)
            raise

        result.rows_written = sum(info.row_count for info in new_infos)
        result.new_segments = len(new_infos)
        result.obsolete_segments = len(refs)
        return result

    def _compact_sort_rows(self, rows, spec):
        if not rows:
            return []
        fields = []
        for field in [
            spec.order_key,
            spec.time.name if spec.time else None,
            spec.partition.column if spec.partition else None,
        ]:
            if field and field not in fields:
                fields.append(field)
        for field in spec.primary_key:
            if field not in fields:
                fields.append(field)
        if not fields:
            return list(rows)

        def sort_key(row):
            return tuple(self._row_sort_value(row, field) for field in fields)

        try:
            return sorted(rows, key=sort_key)
        except TypeError:
            return sorted(
                rows,
                key=lambda row: tuple(str(row.get(field)) for field in fields),
            )

    def _compact_row_chunks(self, rows, primary_key, target_rows):
        chunks = []
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
                chunks.append(current)
                current = []
                current_keys = set()
            current.append(row)
            if pk_value is not None:
                current_keys.add(pk_value)
        if current:
            chunks.append(current)
        return chunks

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
                raise UnsafeOperation(f'compact target segment already exists: {path}')
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
        if not ref.part_key:
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
            with sqlite3.connect(path) as conn:
                rows = conn.execute('PRAGMA integrity_check;').fetchall()
        except sqlite3.Error as exc:
            return [str(exc)]
        return [row[0] for row in rows]

    def _data_table_exists(self, path):
        try:
            with sqlite3.connect(path) as conn:
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
            with sqlite3.connect(path) as conn:
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
        table_spec = self.spec()
        if not table_spec:
            raise BadArgument('latest requires a TableSpec')
        symbol_column = table_spec.symbol_column
        group_fields = self._normalize_latest_by(by, symbol_column)
        rows = _TableQuery(self, query_spec).to_list()
        if not rows:
            return []

        self._ensure_latest_fields(rows[0], group_fields)

        requested_symbols = self._normalize_symbols(query_spec.symbols)
        latest_by_key = OrderedDict()
        for row in rows:
            if requested_symbols is not None:
                symbol = row.get(symbol_column)
                if symbol not in requested_symbols:
                    continue
            key = tuple(row[field] for field in group_fields)
            if key in latest_by_key:
                continue
            latest_by_key[key] = row

        if requested_symbols is None:
            latest_rows = list(latest_by_key.values())
        else:
            latest_rows = self._order_latest_rows_by_symbols(
                latest_by_key,
                requested_symbols,
                symbol_column,
                group_fields,
            )
        return self._project_columns(latest_rows, columns)

    def _normalize_latest_by(self, by, symbol_column):
        if by is None:
            return [symbol_column]
        if isinstance(by, str):
            fields = [item.strip() for item in by.split(',') if item.strip()]
        elif isinstance(by, (list, tuple)):
            fields = list(by)
        else:
            raise BadArgument('latest by must be a string/list/tuple')
        if not fields:
            raise BadArgument('latest by must not be empty')
        return fields

    def _resolve_latest_by_arg(self, by, group_by):
        if group_by is not None:
            warnings.warn(
                "latest(group_by=) 已废弃：group_by 是 by 的别名，请改用 by=",
                DeprecationWarning,
                stacklevel=3,
            )
            if by is not None and by != group_by:
                raise BadArgument('latest by and group_by conflict')
            return group_by
        return by

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

    def _normalize_symbols(self, symbols):
        if symbols is None:
            return None
        if isinstance(symbols, str) or not isinstance(symbols, (list, tuple, set)):
            return [symbols]
        return list(symbols)

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

    def _compile_query_where(self, query_spec):
        generated = []
        table_spec = self.spec()

        if query_spec.symbols is not None:
            symbol_column = table_spec.symbol_column if table_spec else 'symbol'
            symbols = query_spec.symbols
            if isinstance(symbols, str) or not isinstance(symbols, (list, tuple, set)):
                symbols = [symbols]
            symbols = list(symbols)
            if len(symbols) == 1:
                generated.append({
                    'field': symbol_column,
                    'op': '=',
                    'value': symbols[0],
                })
            elif symbols:
                generated.append({
                    'field': symbol_column,
                    'op': 'in',
                    'value': symbols,
                })

        if query_spec.date is not None:
            time_column = self._query_time_column(table_spec)
            start, end = time_column.normalize_date_range(query_spec.date)
            if time_column.unit == 'date':
                generated.append({
                    'field': time_column.name,
                    'op': '=',
                    'value': start,
                })
            else:
                generated.extend([
                    {'field': time_column.name, 'op': '>=', 'value': start},
                    {'field': time_column.name, 'op': '<=', 'value': end},
                ])

        if query_spec.session is not None:
            session_column = self._query_session_column(table_spec)
            sessions = query_spec.session
            if isinstance(sessions, str) or not isinstance(sessions, (list, tuple, set)):
                sessions = [sessions]
            sessions = list(sessions)
            if len(sessions) == 1:
                generated.append({
                    'field': session_column,
                    'op': '=',
                    'value': sessions[0],
                })
            elif sessions:
                generated.append({
                    'field': session_column,
                    'op': 'in',
                    'value': sessions,
                })

        if query_spec.start is not None or query_spec.end is not None:
            time_column = self._query_time_column(table_spec)
            if query_spec.start is not None:
                generated.append({
                    'field': time_column.name,
                    'op': '>=',
                    'value': time_column.normalize_value(query_spec.start),
                })
            if query_spec.end is not None:
                generated.append({
                    'field': time_column.name,
                    'op': '<=',
                    'value': time_column.normalize_value(query_spec.end),
                })

        return self._combine_query_where(query_spec.where, generated)

    def _query_time_column(self, table_spec):
        if not table_spec or not table_spec.time:
            raise MissingTimeColumn(
                f"表 `{self._table_name}` 没有 time 契约，query(start/end/date) "
                "与 latest()/asof() 需要它。"
                "修复: 建表时传 time=\"ts\"，或使用 duration/calendar 分区"
                "（可自动反推 time）"
            )
        return table_spec.time

    def _query_session_column(self, table_spec):
        if not table_spec or not table_spec.session_column:
            raise BadArgument(
                'query session requires TableSpec.session_column'
            )
        return table_spec.session_column

    def _combine_query_where(self, where, generated):
        if not generated:
            return where
        if where is None:
            return generated
        if isinstance(where, str):
            parsed = Where(where).or_and_cases or []
            if not parsed:
                return generated
            return [list(cases) + generated for cases in parsed]
        if isinstance(where, dict):
            return [where] + generated
        if isinstance(where, (list, tuple)):
            if any(isinstance(item, (list, tuple)) for item in where):
                combined = []
                for item in where:
                    if isinstance(item, (list, tuple)):
                        combined.append(list(item) + generated)
                    else:
                        combined.append([item] + generated)
                return combined
            return list(where) + generated
        if isinstance(where, CaseExpression):
            raise BadArgument(
                'query() cannot combine CaseExpression with symbols/start/end; '
                'use select() for complex expressions'
            )
        return [where] + generated

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

        w = Where(compiled_where)
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
                candidate_refs = self._physical_part_refs(candidate_parts)
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
        return os.path.join(self.table_part_path, f"{part_key}.sqlite3")

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
        if state not in ['active', 'sealed', 'obsolete']:
            state = 'sealed' if self._bool_value(row.get('sealed')) else 'active'
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

    def _data_file_column_stats(self, part_key):
        meta = self.get_table_meta()
        columns = list((meta or {}).get('column_info') or {})
        if not columns:
            return {}

        part_dbfile = self._get_part_dbfile(part_key)
        if not os.path.isfile(part_dbfile):
            return {}

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
            state='sealed' if sealed else 'active',
            created_at=segment.created_at,
        )
        self._upsert_segment_info(info)
        return info

    def _pk_cases(self, row, pk):
        return [Case(field, '=', row[field]) for field in pk]

    def _column_types_for_pk_lookup(self, table_spec):
        meta = self.get_table_meta()
        if meta:
            return {
                key: item["type"]
                for key, item in meta["column_info"].items()
            }
        return dict(table_spec.columns or {})

    def _find_existing_pk_segment(self, logical_part, row, pk, table_spec):
        if not pk:
            return None

        column_types = self._column_types_for_pk_lookup(table_spec)
        cases = self._pk_cases(row, pk)
        query = build_select_cases_query(
            _PART_DATA_TABLE_NAME,
            '1',
            cases,
            column_types,
        )
        for ref in self._physical_part_refs([logical_part]):
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=False) as db:
                try:
                    rows = db[_PART_DATA_TABLE_NAME].query(
                        query.sql,
                        params=query.params,
                    )
                except NoSuchTable:
                    continue
            if rows:
                return ref
        return None

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

    def _legacy_segment_info(self, logical_part):
        path = self._get_part_dbfile(logical_part)
        if not os.path.isfile(path):
            return None
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

    def _physical_part_refs(self, logical_parts=None, include_obsolete=False):
        parts = self.table_parts() if logical_parts is None else list(logical_parts)
        refs = []
        for part in parts:
            legacy = self._legacy_segment_info(part)
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
        excluded = set()
        for info in refs:
            stats = (info.stats or {}).get(case.field)
            if stats and self._stats_excludes_case(stats, case):
                excluded.add(self._segment_lookup_key(info))
        return excluded

    def _stats_excludes_case(self, stats, case):
        stat_min = stats.get('min')
        stat_max = stats.get('max')
        if stat_min is None or stat_max is None:
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
        if case.op in ['eq', '==', '=']:
            values = [case.value['value']]
        elif case.op == 'in':
            values = list(case.value['value'])
        else:
            return set()

        excluded = set()
        for info in refs:
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

    def _get_select_part_refs(self, cases):
        need_parts = self._get_select_parts(cases)
        refs = self._physical_part_refs(need_parts)
        if not cases:
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

    def iter_select(
        self,
        fields='*',
        where=None,
        limit=None,
        order_by=None,
        to_dict=False,
        batch_size=10_000,
        columns=None,
        **kwargs,
    ):
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

    def iter_select_batches(
        self,
        fields='*',
        where=None,
        limit=None,
        order_by=None,
        to_dict=False,
        batch_size=10_000,
        columns=None,
        **kwargs,
    ):
        fields = resolve_select_fields(fields, columns, 'iter_select_batches')
        batch_size = self._normalize_batch_size(batch_size)
        limit = self._normalize_limit(limit)
        if limit == 0:
            return

        if order_by:
            rows = self.select(
                fields,
                where,
                limit=limit,
                order_by=order_by,
                to_dict=to_dict,
                **kwargs,
            )
            for start in range(0, len(rows), batch_size):
                yield rows[start:start + batch_size]
            return

        meta = self.get_table_meta()
        if not meta:
            return
        column_types = {
            key: item["type"] for key, item in meta["column_info"].items()
        }
        w = Where(where)
        fetch_fields, project_fields = self._select_fields_with_order(
            fields,
            None,
            column_types,
        )

        emitted = 0
        pending = []
        for rows in self._iter_select_raw_batches(
            fetch_fields,
            w,
            column_types,
            batch_size,
        ):
            if not rows:
                continue
            if project_fields is not None:
                rows = self._project_fields(rows, project_fields)
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
            if limit is not None and emitted >= limit:
                break
        if pending:
            yield pending

    def select(self, fields='*', where=None, limit=None, order_by=None,
               to_dict=False, columns=None, **kwargs):
        """条件查询，返回行列表（急切求值）。

        fields/where 均可省略：select() 即全表查询；
        columns= 是 fields= 的别名（与 query(columns=) 对齐），两者同传冲突报错。
        """
        fields = resolve_select_fields(fields, columns, 'select')
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
            return []

        w = Where(where)
        limit = self._normalize_limit(limit)
        fetch_fields, project_fields = self._select_fields_with_order(
            fields,
            order_by,
            column_types,
        )

        full_data = []
        if not w.or_and_cases:
            # select all
            data = self._select_and_cases(fetch_fields, None, column_types)
            # full_data.append(data)
            if data:
                full_data.extend(data)
        elif len(w.or_and_cases) > 1:
            data = self._select_or_cases(
                fetch_fields,
                w.or_and_cases,
                column_types,
            )
            if data:
                full_data.extend(data)
        else:
            for cases in w.or_and_cases:
                # 一个基本查询单元
                data = self._select_and_cases(fetch_fields, cases, column_types)
                # full_data.append((cases, data))
                # full_data.append(data)
                if data:
                    full_data.extend(data)
        # full_data = list(chain(*full_data))
        full_data = self._apply_order_by(full_data, order_by)
        if limit is not None:
            full_data = full_data[:limit]
        if project_fields is not None:
            full_data = self._project_fields(full_data, project_fields)
        if to_dict:
            full_data = [dict(item) for item in full_data]
        return full_data

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

    def _select_fields_with_order(self, fields, order_by, column_types):
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

    def _project_fields(self, rows, fields):
        return [
            {field: row[field] for field in fields}
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

    def _select_and_cases(self, fields, cases, column_types):
        """获得And[Case..] 的查询结果
        And[Case..] 为一个基本查询单元
        """
        # table field必须的，涉及到查询是否需要加 "
        # 分成独立文件是为了同步数据方便，也导致数据容易被盗
        need_refs = self._get_select_part_refs(cases)
        data = []
        for ref in need_refs:
            part_table_name = _PART_DATA_TABLE_NAME
            query = build_select_cases_query(
                part_table_name,
                fields,
                cases,
                column_types,
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

    def _iter_select_raw_batches(self, fields, where_obj, column_types, batch_size):
        for ref, query in self._select_queries_for_where(
            fields,
            where_obj,
            column_types,
        ):
            part_dbfile = self._get_part_dbfile(ref.part_key)
            logger.debug(
                f"{part_dbfile}: {ref.part_key}: "
                f"SQL: {query.sql}; params={query.params}"
            )
            yield from self._iter_part_query_batches(
                part_dbfile,
                query,
                batch_size,
            )

    def _select_queries_for_where(self, fields, where_obj, column_types):
        if not where_obj.or_and_cases:
            for ref in self._get_select_part_refs(None):
                yield (
                    ref,
                    build_select_cases_query(
                        _PART_DATA_TABLE_NAME,
                        fields,
                        None,
                        column_types,
                    ),
                )
            return

        if len(where_obj.or_and_cases) > 1:
            refs_by_key = OrderedDict()
            for cases in where_obj.or_and_cases:
                for ref in self._get_select_part_refs(cases):
                    refs_by_key[self._segment_lookup_key(ref)] = ref
            query = build_select_or_cases_query(
                _PART_DATA_TABLE_NAME,
                fields,
                where_obj.or_and_cases,
                column_types,
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
        )
        for ref in self._get_select_part_refs(cases):
            yield ref, query

    def _iter_part_query_batches(self, part_dbfile, query, batch_size):
        offset = 0
        base_sql = query.sql.rstrip().rstrip(';')
        while True:
            sql = f'{base_sql} limit ? offset ?'
            params = list(query.params) + [batch_size, offset]
            with self._part_db(part_dbfile, auto_commit=False) as db:
                try:
                    rows = db[_PART_DATA_TABLE_NAME].query(
                        sql,
                        params=params,
                    )
                except NoSuchTable:
                    rows = []
            if not rows:
                break
            yield list(rows)
            if len(rows) < batch_size:
                break
            offset += len(rows)

    def _select_or_cases(self, fields, or_cases, column_types):
        refs_by_key = OrderedDict()
        for cases in or_cases:
            for ref in self._get_select_part_refs(cases):
                refs_by_key[self._segment_lookup_key(ref)] = ref

        data = []
        for ref in refs_by_key.values():
            query = build_select_or_cases_query(
                _PART_DATA_TABLE_NAME,
                fields,
                or_cases,
                column_types,
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
        for ref in self._physical_part_refs():
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=True) as part_db:
                part_db[_PART_DATA_TABLE_NAME].create_index(
                    column, order, exist_ok=exist_ok
                )

    def drop_index(self, column):
        dropped = 0
        for ref in self._physical_part_refs():
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=True) as part_db:
                dropped += part_db[_PART_DATA_TABLE_NAME].drop_column_index(column)
        return dropped

    def upsert_all(
        self, kv_list, pk, partition=None, commit=True, **kwargs
    ):
        """Update or insert rows based on primary key.

        Upsert semantics:
        - For each item in kv_list with pk that exists in table: UPDATE (merge fields)
        - For each item in kv_list with pk that doesn't exist: INSERT

        Logic:
        1. SELECT all existing rows (no WHERE - pk determines what to update)
        2. Build a dict keyed by pk for fast lookup
        3. For each item in kv_list:
           - If pk exists: MERGE (update fields with new values)
           - If pk is NEW: INSERT the new row
        4. INSERT all rows with REPLACE

        Args:
            kv_list: List of dicts with values to update/insert
            pk: Primary key columns
            partition: Partition object (auto-loaded if not provided)
            commit: Whether to commit transaction
        """
        if isinstance(kv_list, dict):
            kv_list = [kv_list]
        elif not isinstance(kv_list, (list, tuple, set)):
            raise BadArgument("upsert_all: `list/tuple/set/dict` required for `kv_list`")

        if not kv_list:
            return

        # Step 1: SELECT all existing rows (no WHERE - pk is the identity)
        data = self.select("*", where=None, to_dict=True)

        # Step 2: Build dict keyed by pk for O(1) lookup
        # Format: {(pk1, pk2, ...): {full_row_dict}}
        data = {tuple([item[k] for k in pk]): item for item in data}

        # Step 3: Merge kv_list into existing data
        # - If pk exists: update only the fields present in kv_list (partial update)
        # - If pk is new: insert the complete new row
        for dic in kv_list:
            key = tuple([dic[k] for k in pk])
            if key in data:
                # Existing row: merge fields (new values override old)
                # Using {**existing, **new} ensures new values take precedence
                data[key] = {**data[key], **dic}
            else:
                # New row: insert the complete row
                data[key] = dic

        # Step 4: INSERT with REPLACE (SQLite upsert behavior)
        # - Existing rows: UPDATE based on pk
        # - New rows: INSERT
        self.insert_all(
            list(data.values()),
            pk=pk,
            replace=True,
            alter=True,
            partition=partition,
            commit=commit,
            **kwargs,
        )
        # def insert_all(self, data, pk=None, extra_data=None, partition=None,
        #                replace=False, ignore=False, commit=True, **kwargs):
        # raise NotImplementedError()

    def delete(self, where):
        """Delete rows matching the where clause from all relevant partitions.

        Args:
            where: Dict format where clause, e.g. {'field': 'id', 'op': '>', 'value': 10}
        """
        if where is None:
            warnings.warn(
                'delete(None) is deprecated; use truncate(confirm=True)',
                DeprecationWarning,
                stacklevel=2,
            )
            return self._delete_all_count()

        meta = self.get_table_meta()
        if not meta:
            return 0

        column_types = {key: item["type"] for key, item in meta["column_info"].items()}

        w = Where(where)

        total_deleted = 0
        for cases in w.or_and_cases:
            deleted = self._delete_and_cases(cases, column_types)
            total_deleted += deleted

        return total_deleted

    def truncate(self, confirm=False):
        if not confirm:
            raise UnsafeOperation('truncate requires confirm=True')
        partitions = list(self.table_parts())
        self._delete_all_count()
        return WriteResult(partitions_touched=partitions)

    def _delete_all_count(self):
        meta = self.get_table_meta()
        if not meta:
            return 0
        column_types = {key: item["type"] for key, item in meta["column_info"].items()}
        return self._delete_and_cases(None, column_types)

    def _delete_and_cases(self, cases, column_types):
        """Delete rows matching And[Case..] from all relevant partitions."""
        need_refs = self._get_select_part_refs(cases)
        total_deleted = 0
        spec = self.spec()
        layout = spec.physical_layout if spec else PhysicalLayout()

        for ref in need_refs:
            part_dbfile = self._get_part_dbfile(ref.part_key)
            with self._part_db(part_dbfile, auto_commit=True) as db:
                part_table = db[_PART_DATA_TABLE_NAME]

                select_query = build_select_cases_query(
                    _PART_DATA_TABLE_NAME, "COUNT(*)", cases, column_types
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
                    db[_PART_DATA_TABLE_NAME].execute_params(
                        delete_query.sql,
                        delete_query.params,
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

        for row in rows:
            existing = self._find_existing_pk_segment(
                logical_part,
                row,
                pk,
                table_spec,
            )
            if existing is not None:
                if ignore:
                    continue
                self._write_segment_rows(
                    existing,
                    logical_part,
                    part_column,
                    [row],
                    pk,
                    partition,
                    replace,
                    ignore,
                    commit,
                    layout,
                    part_kwargs,
                )
            else:
                self._append_segment_rows(
                    logical_part,
                    part_column,
                    [row],
                    pk,
                    partition,
                    replace,
                    ignore,
                    commit,
                    layout,
                    part_kwargs,
                )

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

    def _table_columns(self):
        pass

    def table_names(self):
        if not os.path.isdir(self.db_path):
            return []
        return sorted([
            item for item in os.listdir(self.db_path)
            if os.path.isdir(os.path.join(self.db_path, item))
            and not item.startswith('__')
        ])

    def _overwrite_table_meta(self, createtable_meta):
        """table 创建信息"""
        # 某个表格的各分区最大最小数值
        # table_meta_db = MyDatabase(self.table_meta_path, pragma_dict=self._pragma_dict)
        # table_meta_db.enable_wal()

        # OrderDict
        createtable_meta["column_info"] = list(createtable_meta["column_info"].items())
        meta = json.dumps(createtable_meta)

        updated_at = arrow.now().format("YYYY-MM-DD HH:mm:ss")
        info = {
            "table_name": self._table_name,
            "meta": meta,
            "updated_at": updated_at,
        }
        self.table_meta_db[_TABLE_META_TABLE_NAME].insert(
            info, pk=["table_name"], replace=True, ensure_table=True
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
            info, pk=["table_name", "part", "column"], ensure_table=True, replace=True
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
        table_name = table_name or self._table_name
        if target_uri == self._uri and table_name == self._table_name:
            raise BadArgument('repartition_to requires a different target uri or table')

        target_spec = TableSpec.ensure(spec) if spec is not None else self.spec()
        if not target_spec:
            raise BadArgument('repartition_to requires a TableSpec')

        from ...database import Database

        target_schema, _ = parse_db_schema(target_uri)
        target_options = {"verbose": self.verbose}
        if target_schema in ["sqlite", "sqlite3"]:
            target_options.update({
                "pragma_dict": self._pragma_dict,
                "max_open_partitions": self._num_opened_max_allowed,
                "max_connections_per_file": self._max_connections_per_file,
                "max_process_connections": self._max_process_connections,
            })
        target_db = Database(target_uri, **target_options)
        try:
            target_table = target_db.table(table_name, spec=target_spec)
            rows_copied = 0
            for part in self.table_parts():
                rows = self._part_rows(part)
                for start in range(0, len(rows), batch_size):
                    batch = rows[start:start + batch_size]
                    if batch:
                        target_table.write(batch, mode=mode)
                        rows_copied += len(batch)

            partitions_created = target_table.table_parts()
        finally:
            target_db.close()
        return RepartitionResult(
            source_uri=self._uri,
            target_uri=target_uri,
            table=table_name,
            rows_copied=rows_copied,
            partitions_created=partitions_created,
        )

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

        with self._part_db(part_dbfile, auto_commit=True) as db:
            db[part_table_name].create_table(
                mysqlite_columns,
                pk=createtable_meta["pk"],
                save_meta=True,
                table_comment=None,
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
        try:
            yield db
        finally:
            with self._db_parts_condition:
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
        data_suggested_types = self.suggest_column_types(
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
