import os
import shutil
import threading
import warnings
import weakref
from dataclasses import replace
from functools import wraps
from .coordination import guard_database_class

from .utils.schema import parse_db_schema
from .exception import (
    BadArgument,
    EngineNotExist,
    SchemaConflict,
    TableSpecConflict,
    UnsafeOperation,
)
from .engines import sqlite
from .partition import Partition
from .spec import Segment, TableContract, TimeColumn, PartitionSpec, parse_partition_spec
from .utils.storage_identity import (
    _check_engine, _marker_engine, detect_storage_engine, iosql_table_names, is_iosql_database,
    is_iosql_scaffold, is_iosql_table, storage_engine_lock,
)


_RENAME_BACKUP_SUFFIX = '.__rename_tmp__'
_MIGRATION_MODES = ['append', 'upsert', 'replace', 'ignore']
_BINDING_DEFAULTS = {'conflict': 'append', 'schema_policy': 'evolve'}


def _looks_like_iosql_database(path):
    """目录是否是可接管的 iosql 库（空目录仅供重命名目标使用）。"""
    return is_iosql_database(path, allow_empty=True)


def _forbidden_rename_target_reason(path):
    """返回 target 被禁止的原因（根/家目录/符号链接），否则 None。"""
    real = os.path.realpath(path)
    if real == os.path.realpath(os.sep):
        return '根目录'
    try:
        home = os.path.realpath(os.path.expanduser('~'))
    except (OSError, RuntimeError):
        home = None
    if home and real == home:
        return '家目录'
    if os.path.islink(path):
        return '符号链接'
    return None


def _binding_fields():
    return ('order_key', 'symbol_column', 'session_column', 'conflict', 'schema_policy')


def _warn_ignored_partition_every(function):
    """在不改变公开默认值的情况下识别调用方显式提供的关键字参数。"""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        if kwargs.get('partition') is not None and 'partition_every' in kwargs:
            warnings.warn(
                f"Database.timeseries: 显式指定的 partition_every={kwargs['partition_every']!r} "
                "被忽略，分区由 partition 决定；请移除 partition_every，"
                "并在 partition 中设置期望的分区规则。",
                UserWarning,
                stacklevel=3,
            )
        return function(self, *args, **kwargs)
    return wrapped


_DEFAULT_MAX_OPEN_PARTITIONS = 5
_DEFAULT_MAX_CONNECTIONS_PER_FILE = 1
_DEFAULT_MAX_PROCESS_CONNECTIONS = 32
_DEFAULT_MAX_ORDERED_READ_CONNECTIONS = 128


def _normalize_pk(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _normalize_migration_verify(verify):
    if verify in [None, False, ""]:
        return None
    value = str(verify).strip().lower()
    if value in ["none", "false", "0"]:
        return None
    if value != "row-count":
        raise BadArgument("verify only supports row-count")
    return value


def _sqlite_path_from_uri(uri):
    schema, options = parse_db_schema(uri)
    if schema not in ["sqlite", "sqlite3"]:
        raise BadArgument("rename is only supported by the SQLite engine")
    if options.get("memory"):
        raise BadArgument("in-memory SQLite database cannot be renamed")
    return os.path.abspath(
        os.path.join(options.get("path") or "", options.get("db") or "")
    )


def _sqlite_uri_from_path(schema, path):
    from .utils.uri import _typed_local_uri
    return _typed_local_uri(schema, os.path.abspath(path))


@guard_database_class
class Database(object):
    '''
    table是BigTable
    '''

    def __init__(
        self,
        uri,
        verbose=0,
        pragma_dict=None,
        max_open_partitions=_DEFAULT_MAX_OPEN_PARTITIONS,
        max_connections_per_file=_DEFAULT_MAX_CONNECTIONS_PER_FILE,
        max_process_connections=_DEFAULT_MAX_PROCESS_CONNECTIONS,
        max_ordered_read_connections=_DEFAULT_MAX_ORDERED_READ_CONNECTIONS,
        engine=None,
        _recover=True,
        _lock_deadline=None,
    ):
        from .utils.uri import resolve_database_uri

        uri = resolve_database_uri(uri, engine=engine, allow_missing=True, recover=_recover,
                                   lock_deadline=_lock_deadline)
        self._uri = uri
        self._table_cls = Database.get_engine(uri)
        self._tables = {}
        # 保护 _tables 的首次并发访问与缓存增删
        self._tables_lock = threading.RLock()
        self.verbose = verbose
        uri_schema, _ = parse_db_schema(uri)
        self._uri_schema = uri_schema
        sqlite_engine = uri_schema in ["sqlite", "sqlite3"]
        directory_engine = sqlite_engine or uri_schema == 'parquet'
        if pragma_dict is not None and not sqlite_engine:
            raise BadArgument("pragma_dict is only supported by the SQLite engine")
        if not directory_engine and (
            max_open_partitions != _DEFAULT_MAX_OPEN_PARTITIONS
            or max_connections_per_file != _DEFAULT_MAX_CONNECTIONS_PER_FILE
            or max_process_connections != _DEFAULT_MAX_PROCESS_CONNECTIONS
            or max_ordered_read_connections
            != _DEFAULT_MAX_ORDERED_READ_CONNECTIONS
        ):
            raise BadArgument(
                "File limits are only supported by the SQLite and Parquet engines"
            )
        self._max_open_partitions = self._positive_int(
            "max_open_partitions",
            max_open_partitions,
        )
        self._max_connections_per_file = self._positive_int(
            "max_connections_per_file",
            max_connections_per_file,
        )
        self._max_process_connections = self._positive_int(
            "max_process_connections",
            max_process_connections,
        )
        self._max_ordered_read_connections = self._positive_int(
            "max_ordered_read_connections",
            max_ordered_read_connections,
        )
        self._pragma_dict = pragma_dict

    @property
    def uri(self):
        return self._uri

    @property
    def engine(self):
        """实际使用的存储引擎，SQLite 的 sqlite3 别名统一显示为 sqlite。"""
        return 'sqlite' if self._uri_schema == 'sqlite3' else self._uri_schema

    @classmethod
    def create(cls, uri, *, engine, **options):
        """显式初始化新的本地目录库；已有库或非空目录不会被覆盖。"""
        from .coordination import lease, physical_root, resource
        from .utils.storage_identity import ensure_storage_engine
        from .utils.uri import resolve_database_uri

        resolved = resolve_database_uri(uri, engine=engine, allow_missing=True)
        item = resource(resolved)
        if item.path is None or item.backend not in ('sqlite', 'sqlite3', 'parquet'):
            raise BadArgument('Database.create 仅支持本地持久 SQLite 或 Parquet 库')
        if item.backend == 'parquet':
            from .engines.parquet.table import _duckdb
            _duckdb()
        # 先验证所有构造参数，失败时不能留下已声明类型的新库。
        database = cls(resolved, **options)
        with lease(resolved, exclusive=True, timeout=30.0):
            root = physical_root(resolved)
            if root.exists() and (not root.is_dir() or any(root.iterdir())):
                raise UnsafeOperation(f'数据库目标已存在或目录非空: {root}')
            ensure_storage_engine(resolved, claim=True)
        return database

    def _reload_after_migration(self):
        """让外部持有的旧表对象在下一次使用时也能重新绑定。"""
        if self._uri_schema == 'postgresql':
            # PG 契约原来由实例保存，保留对象，交给表入口只读重连校验。
            return
        from .coordination import physical_root
        root = physical_root(self._uri)
        with self._tables_lock:
            previous = list(self._tables.values())
            # 保留仍存在的对象，尤其是契约尚未落盘的空表声明。
            self._tables = {name: table for name, table in self._tables.items()
                            if (root / name).is_dir()}
        for table in previous:
            if getattr(table, '_iosql_generation', None) == self._iosql_generation:
                continue
            table._iosql_invalidating = True
            try:
                table.close()
            finally:
                table._iosql_invalidating = False

    @staticmethod
    def _positive_int(name, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BadArgument(f"{name} must be a positive integer")
        return value

    def __getitem__(self, table_name):
        with self._tables_lock:
            if self._uri_schema in ["sqlite", "sqlite3", "parquet"]:
                table_name = self._table_cls._validate_table_name(table_name)
            table = self._tables.get(table_name)
            if table is not None:
                if self._uri_schema in ["sqlite", "sqlite3", "parquet"]:
                    from .coordination import physical_root, resource
                    if resource(self._uri).path is not None:
                        root = physical_root(self._uri)
                        backend = 'parquet' if self._uri_schema == 'parquet' else 'sqlite'
                        # 标记由原子替换发布，检查缓存对象无需等待其他表的写入。
                        _check_engine(_marker_engine(root), backend, root)
                        table_path = root / table_name
                        if os.path.lexists(table_path) and (
                                table_path.is_symlink() or not table_path.is_dir()):
                            raise UnsafeOperation(f"表路径不是普通目录: {table_path}")
                        if (table_path / 'meta.sqlite3').is_symlink():
                            raise UnsafeOperation(f"表元数据不能是符号链接: {table_path}")
                return table
            table_options = {
                "table_name": table_name,
                "verbose": self.verbose,
            }
            if self._uri_schema in ["sqlite", "sqlite3", "parquet"]:
                _, options = parse_db_schema(self._uri)
                db_path = (
                    _sqlite_path_from_uri(self._uri)
                    if self._uri_schema in ['sqlite', 'sqlite3']
                    else os.path.join(options.get('path') or '', options.get('db') or '')
                )
                from .coordination import physical_path
                db_path = str(physical_path(db_path))
                table_path = os.path.join(db_path, table_name)
                backend = 'parquet' if self._uri_schema == 'parquet' else 'sqlite'
                with storage_engine_lock(self._uri, exclusive=True):
                    _check_engine(detect_storage_engine(db_path, verify_tables=False), backend, db_path)
                    if os.path.lexists(table_path):
                        if os.path.islink(table_path) or not os.path.isdir(table_path):
                            raise UnsafeOperation(f"表路径不是普通目录: {table_path}")
                        if os.listdir(table_path) and not (
                            is_iosql_table(table_path, backend=backend) or is_iosql_scaffold(table_path)
                        ):
                            raise UnsafeOperation(
                                f"目录不是有效 iosql 表，拒绝在其中创建元数据: {table_path}"
                            )
            if self._uri_schema in ["sqlite", "sqlite3", "parquet"]:
                table_options.update({
                    "max_open_partitions": self._max_open_partitions,
                    "max_connections_per_file": self._max_connections_per_file,
                    "max_process_connections": self._max_process_connections,
                    "max_ordered_read_connections": (
                        self._max_ordered_read_connections
                    ),
                })
                if self._pragma_dict is not None:
                    table_options["pragma_dict"] = self._pragma_dict
            table = self._table_cls(self._uri, **table_options)
            table._owner_db = weakref.ref(self)
            self._tables[table_name] = table
            return table

    def table(
        self,
        table_name,
        spec=None,
        pk=None,
        primary_key=None,
        columns=None,
        partition=None,
        segment=None,
        order_key=None,
        conflict='append',
        schema_policy='evolve',
        force_partition=None,
        time=None,
        symbol_column=None,
        session_column=None,
        description=None,
        column_descriptions=None,
        column_ranges=None,
    ):
        """创建或打开逻辑表（主入口）。

        `time` / `symbol_column` / `session_column` 让契约感知能力
        （query()/latest()/asof()）无需 timeseries() 即可使用；
        duration/calendar 分区还会在未显式声明 time 时自动反推。
        `description`、`column_descriptions`、`column_ranges` 描述表和列的
        领域语义；只提供这些参数时会增量更新已有 SQLite 表的元数据。
        """
        table = self[table_name]
        shortcut_values = [
            pk,
            primary_key,
            columns,
            partition,
            segment,
            order_key,
            time,
            symbol_column,
            session_column,
        ]
        metadata_values = {
            "description": description,
            "column_descriptions": column_descriptions,
            "column_ranges": column_ranges,
        }
        if (
            column_descriptions is not None
            and not isinstance(column_descriptions, dict)
        ):
            raise BadArgument(
                "column_descriptions must be a dict of column -> str/None"
            )
        if column_ranges is not None and not isinstance(column_ranges, dict):
            raise BadArgument(
                "column_ranges must be a dict of column -> {low, high}"
            )
        has_metadata_values = (
            description is not None
            or bool(column_descriptions)
            or bool(column_ranges)
        )
        if spec is not None and any(value is not None for value in shortcut_values):
            raise BadArgument('Database.table: spec conflicts with shortcut arguments')

        if spec is not None:
            contract = TableContract.ensure(spec)
            if description is not None:
                contract = replace(contract, description=description)
            if column_descriptions is not None:
                if not isinstance(column_descriptions, dict):
                    raise BadArgument(
                        "column_descriptions must be a dict of column -> str/None"
                    )
                merged_descriptions = dict(contract.column_descriptions or {})
                merged_descriptions.update(column_descriptions)
                contract = replace(
                    contract,
                    column_descriptions=merged_descriptions,
                )
            if column_ranges is not None:
                if not isinstance(column_ranges, dict):
                    raise BadArgument(
                        "column_ranges must be a dict of column -> {low, high}"
                    )
                merged_ranges = dict(contract.column_ranges or {})
                merged_ranges.update(column_ranges)
                contract = replace(contract, column_ranges=merged_ranges)
            overrides = {}
            if conflict != 'append':
                overrides['conflict'] = conflict
            if schema_policy != 'evolve':
                overrides['schema_policy'] = schema_policy
            if overrides:
                contract = replace(contract, **overrides)
            if force_partition is not None and bool(contract.force_partition) != bool(force_partition):
                raise BadArgument('Database.table: force_partition 与 spec 冲突')
            self._bind_contract(table, contract, contract.force_partition)
        elif not any(value is not None for value in shortcut_values):
            if has_metadata_values:
                table.update_metadata(**{
                    key: value
                    for key, value in metadata_values.items()
                    if value is not None
                })
        elif any(value is not None for value in shortcut_values):
            normalized_pk = _normalize_pk(pk)
            normalized_primary_key = _normalize_pk(primary_key)
            if (
                normalized_pk is not None
                and normalized_primary_key is not None
                and normalized_pk != normalized_primary_key
            ):
                raise BadArgument('Database.table: pk and primary_key conflict')
            contract_kwargs = {}
            if time is not None:
                contract_kwargs['time'] = time
            if symbol_column is not None:
                contract_kwargs['symbol_column'] = symbol_column
            if session_column is not None:
                contract_kwargs['session_column'] = session_column
            contract = TableContract(
                primary_key=normalized_pk if normalized_pk is not None else normalized_primary_key,
                columns=columns or {},
                partition=partition,
                segment=segment,
                order_key=order_key,
                conflict=conflict,
                schema_policy=schema_policy,
                force_partition=bool(force_partition),
                description=description,
                column_descriptions=column_descriptions,
                column_ranges=column_ranges,
                **contract_kwargs,
            )
            self._bind_contract(table, contract, bool(force_partition))
        return table

    def _bind_contract(self, table, contract, force_partition):
        """把声明契约绑定到（可能已存在的）表上。

        引擎负责 pk/partition/segment/列类型 的核心兼容性校验；
        这里补齐 order_key/symbol_column/session_column/conflict/schema_policy
        的校验与落盘，避免 `spec=` 绑定被静默忽略。
        """
        if hasattr(table, '_mutation_lock'):
            # 检查、绑定和元数据更新必须与同表写入共用一把锁。
            with table._mutation_lock():
                return self._bind_contract_locked(table, contract, force_partition)
        return self._bind_contract_locked(table, contract, force_partition)

    def _bind_contract_locked(self, table, contract, force_partition):
        if hasattr(table, '_date_key_contract'):
            contract = table._date_key_contract(contract)
        stored = table.spec()
        if stored is not None:
            conflicts = []
            for field_name in _binding_fields():
                stored_value = getattr(stored, field_name)
                declared_value = getattr(contract, field_name)
                if stored_value == declared_value:
                    continue
                if field_name in ('conflict', 'schema_policy'):
                    if stored_value == _BINDING_DEFAULTS[field_name]:
                        # 存量仍是默认值，允许按声明覆盖（下方统一落盘）
                        continue
                elif stored_value is None:
                    # 存量未声明该绑定，允许声明补全（下方统一落盘）
                    continue
                conflicts.append(
                    f"{field_name}(当前={stored_value!r}, 声明={declared_value!r})"
                )
            if conflicts:
                raise TableSpecConflict(
                    "既有表契约与声明 spec 的绑定不一致: "
                    + "; ".join(conflicts)
                    + "。修复: 保持声明与既有契约一致；如需变更 "
                    "symbol/session/order_key/conflict，请新建表后 repartition_to"
                )

        if force_partition and stored is not None and hasattr(table, '_mutation_lock'):
            # force 只允许修改尚无物理分区的表；已有数据必须显式迁移。
            self._ensure_contract_core_compatible(stored, contract)
            previous_partition = stored.to_partition()
            requested_partition = contract.to_partition()
            previous_meta = previous_partition.to_meta() if previous_partition else None
            requested_meta = requested_partition.to_meta() if requested_partition else None
            if previous_meta != requested_meta and table.table_parts():
                raise TableSpecConflict(
                    f'Partition conflict: current={previous_meta} new={requested_meta}；'
                    '已有物理分区，不能原地修改；请使用 repartition_to() 或 migrate_to()'
                )
            if hasattr(table, '_declared_spec'):
                table._declared_spec = contract
            table._partition = contract.to_partition()
        else:
            table.use_contract(contract)

        if stored is not None:
            self._persist_declared_bindings(table, stored, contract, force_partition)

    @staticmethod
    def _ensure_contract_core_compatible(stored, contract):
        """force_partition 直连路径仍需校验非分区核心字段，避免静默破坏数据。"""
        if list(stored.primary_key) != list(contract.primary_key):
            raise TableSpecConflict(
                f'Primary key conflict: current={stored.primary_key} '
                f'new={contract.primary_key}；修复: 保持 pk 一致，或新建目标表'
            )
        stored_segment = stored.segment.to_dict() if stored.segment else None
        new_segment = contract.segment.to_dict() if contract.segment else None
        if stored_segment != new_segment:
            raise TableSpecConflict(
                f'Segment conflict: current={stored_segment} new={new_segment}；'
                "修复: 保持 segment 一致，或新建目标表"
            )
        stored_columns = {
            key: str(value).lower()
            for key, value in stored.columns.items()
            if value is not None
        }
        new_columns = {
            key: str(value).lower()
            for key, value in contract.columns.items()
            if value is not None
        }
        for column, type_ in new_columns.items():
            if column in stored_columns and stored_columns[column] != type_:
                raise SchemaConflict(
                    f'Column `{column}` type conflict: '
                    f'current={stored_columns[column]} new={type_}；'
                    "修复: 转换数据类型，或重新声明契约 columns"
                )

    def _persist_declared_bindings(self, table, stored, contract, force_partition):
        """把声明中存量缺失/默认的绑定字段与 force 分区契约写回 manifest。"""
        if not (
            hasattr(table, 'get_table_meta')
            and hasattr(table, '_overwrite_table_meta')
        ):
            return
        merged = dict(stored.to_dict())
        changed = False
        for field_name in _binding_fields():
            if getattr(stored, field_name) != getattr(contract, field_name):
                merged[field_name] = getattr(contract, field_name)
                changed = True
        if force_partition:
            merged['partition'] = contract.partition.to_dict() if contract.partition else None
            merged['force_partition'] = True
            changed = True
        if not changed:
            return
        meta = table.get_table_meta()
        if not meta:
            return
        meta['table_contract'] = merged
        if force_partition:
            partition = contract.to_partition()
            meta['partition'] = partition.to_meta() if partition else None
        table._overwrite_table_meta(meta)

    @_warn_ignored_partition_every
    def timeseries(self, table_name, time, pk=None, primary_key=None,
                   columns=None, *, partition_every='1day', timezone='UTC',
                   symbol_column='symbol', session_column=None,
                   partition=None, segment=None, order_key=None,
                   conflict='append', schema_policy='evolve',
                   force_partition=False,
                   partition_kind=None, time_unit=None,
                   target_rows_per_file=None, min_rows_per_file=None,
                   max_rows_per_file=None, description=None,
                   column_descriptions=None, column_ranges=None):
        """时序语法糖：`table()` 的薄封装。

        等价于 `db.table(..., partition=Partition.duration(time, every=partition_every),
        order_key=time, time=time, ...)`；calendar 场景传
        `partition=Partition.calendar(...)`。
        同时显式传入非 `None` 的 `partition` 和 `partition_every` 时，使用 `partition`，
        并发出 `UserWarning` 提示 `partition_every` 被忽略。

        `description`、`column_descriptions`、`column_ranges` 与 `table()` 相同，
        会随时序契约传递。

        废弃参数（触发 DeprecationWarning，行为等价迁移）：
        - `partition_kind='calendar'` → 改传 `partition=Partition.calendar(...)`
        - `time_unit='date'` → 改用 calendar 分区
        - `target/min/max_rows_per_file` → 改传 `segment=Segment.rows(...)`
        """
        normalized_pk = _normalize_pk(pk)
        normalized_primary_key = _normalize_pk(primary_key)
        if (
            normalized_pk is not None
            and normalized_primary_key is not None
            and normalized_pk != normalized_primary_key
        ):
            raise BadArgument('Database.timeseries: pk and primary_key conflict')

        use_calendar = False
        if partition_kind is not None:
            warnings.warn(
                "timeseries(partition_kind=...) 已废弃：calendar 场景请传 "
                "partition=Partition.calendar(...)，duration 为默认行为",
                DeprecationWarning,
                stacklevel=4,
            )
            use_calendar = str(partition_kind).strip().lower() == 'calendar'
        if time_unit is not None:
            warnings.warn(
                "timeseries(time_unit=...) 已废弃：date 场景请使用 calendar 分区 "
                "(partition=Partition.calendar(...) 或 db.table(partition=...))",
                DeprecationWarning,
                stacklevel=4,
            )
            if str(time_unit).strip().lower() == 'date':
                use_calendar = True
        if any(value is not None for value in (
            target_rows_per_file,
            min_rows_per_file,
            max_rows_per_file,
        )):
            warnings.warn(
                "timeseries(target/min/max_rows_per_file=...) 已废弃：请改传 "
                "segment=Segment.rows(max_rows=..., target_rows=..., min_rows=...)",
                DeprecationWarning,
                stacklevel=4,
            )
            if segment is None:
                segment = Segment.rows(
                    max_rows=max_rows_per_file or 500_000,
                    target_rows=target_rows_per_file or 400_000,
                    min_rows=min_rows_per_file or 300_000,
                )

        # time 支持 str / TimeColumn / dict；列名提取出来供分区与 order_key 使用，
        # 之前直接把 TimeColumn 传进 order_key 会被 str() 污染成对象 repr
        if isinstance(time, TimeColumn):
            time_name = time.name
        elif isinstance(time, dict):
            time_name = time.get('name')
            time = TimeColumn.from_dict(time)
        else:
            time_name = time

        if partition is None:
            if use_calendar:
                partition = Partition.calendar(
                    time_name,
                    every=partition_every,
                    timezone=timezone,
                )
            else:
                # duration 分区按 UTC 对齐，无时区概念；
                # 经 PartitionSpec 构造以支持 "1s"/"1day" 等字符串间隔
                partition = PartitionSpec(
                    column=time_name, every=partition_every, kind='duration',
                ).to_partition()

        if isinstance(time, str):
            partition_spec = parse_partition_spec(partition)
            if (partition_spec.kind == 'calendar' or
                    partition_spec.kind == 'duration' and partition_spec.time_unit == 'date'):
                # calendar 分区以自身 tz 为准，避免与显式 partition 的时区漂移
                tz = partition_spec.timezone or timezone
                time_column = TimeColumn(
                    time, unit=partition_spec.time_unit or 'date', timezone=tz,
                )
            else:
                time_column = TimeColumn(time, unit='ms', timezone=timezone)
        else:
            time_column = time

        return self.table(
            table_name,
            pk=normalized_pk,
            primary_key=normalized_primary_key,
            columns=columns,
            partition=partition,
            segment=segment,
            order_key=order_key or time_name,
            conflict=conflict,
            schema_policy=schema_policy,
            force_partition=force_partition,
            time=time_column,
            symbol_column=symbol_column,
            session_column=session_column,
            description=description,
            column_descriptions=column_descriptions,
            column_ranges=column_ranges,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()

    def table_names(self):
        if self._uri_schema == "postgresql":
            try:
                import psycopg2
            except ImportError as e:
                raise BadArgument(
                    "PostgreSQL support requires psycopg2"
                ) from e
            with psycopg2.connect(self._uri) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        select c.relname
                        from pg_class c
                        join pg_namespace n on n.oid = c.relnamespace
                        where n.nspname = coalesce(current_schema(), 'public')
                          and c.relkind in ('r', 'p')
                          and not exists (
                              select 1
                              from pg_inherits i
                              where i.inhrelid = c.oid
                          )
                        order by c.relname
                        """
                    )
                    return [row[0] for row in cursor.fetchall()]

        _, options = parse_db_schema(self._uri)
        db_path = os.path.join(options.get('path') or '', options.get('db') or '')
        if not os.path.isdir(db_path):
            return []
        from .coordination import physical_path
        backend = 'parquet' if self._uri_schema == 'parquet' else 'sqlite'
        with storage_engine_lock(self._uri, exclusive=True):
            _check_engine(detect_storage_engine(physical_path(db_path)), backend, db_path)
            return iosql_table_names(db_path, backend=backend)

    def rename(self, new_uri_or_name, exist_ok=False, overwrite=False):
        """重命名当前 SQLite 目录库。

        `new_uri_or_name` 可以是完整的 `sqlite://...` URI，也可以是当前库父目录
        下的新目录名。重命名后当前 `Database` 实例会指向新 URI。
        """
        if self._uri_schema not in ["sqlite", "sqlite3"]:
            raise BadArgument("Database.rename is only supported by SQLite")
        if overwrite and exist_ok:
            raise BadArgument("rename cannot use overwrite and exist_ok together")

        source_path = _sqlite_path_from_uri(self._uri)
        target_path = self._resolve_rename_database_target(new_uri_or_name)
        if source_path == target_path:
            if exist_ok:
                return self
            raise BadArgument("rename target is the current database")
        source_real = os.path.realpath(source_path)
        target_real = os.path.realpath(target_path)
        if target_real.startswith(source_real + os.sep):
            raise UnsafeOperation("rename target must not be inside the source database")
        if source_real.startswith(target_real + os.sep):
            raise UnsafeOperation(
                "rename target must not contain the source database"
            )
        if not os.path.isdir(source_path):
            raise BadArgument(f"database does not exist: {source_path}")
        if not _looks_like_iosql_database(source_path):
            raise UnsafeOperation(
                f"源目录不像 iosql 数据库（未找到 */meta.sqlite3）: {source_path}；"
                "修复: 确认 uri 指向 iosql 库目录"
            )
        reason = _forbidden_rename_target_reason(target_path)
        if reason:
            raise UnsafeOperation(
                f"rename target must not be a {reason}: {target_path}"
            )
        if os.path.exists(target_path):
            if not overwrite:
                raise BadArgument(f"rename target already exists: {target_path}")
            if not os.path.isdir(target_path):
                raise UnsafeOperation(
                    f"rename target exists and is not a directory: {target_path}"
                )
            if not _looks_like_iosql_database(target_path):
                raise UnsafeOperation(
                    f"rename target 不像 iosql 数据库（未找到 */meta.sqlite3）: "
                    f"{target_path}；修复: 改用空目录或确认为 iosql 库"
                )

        with self._tables_lock:
            cached_tables = list(self._tables.values())
        from .coordination import physical_root
        if (str(physical_root(self._uri)) != os.path.realpath(source_path)
                or str(physical_root(_sqlite_uri_from_path(self._uri_schema, target_path)))
                != os.path.realpath(target_path)):
            from .migration import rename_published_database
            return rename_published_database(
                self, _sqlite_uri_from_path(self._uri_schema, target_path))
        backup_path = None
        if os.path.exists(target_path):
            backup_path = self._rename_backup_path(target_path)
        try:
            for table in cached_tables:
                table.close()
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
            if backup_path is not None:
                # 先把目标改名到临时目录（可回滚），再整体挪入源库，
                # 避免 rmtree 后失败导致目标数据不可恢复
                os.replace(target_path, backup_path)
            try:
                os.replace(source_path, target_path)
            except OSError:
                if backup_path is not None and os.path.exists(backup_path):
                    os.replace(backup_path, target_path)
                raise
        except OSError as e:
            if (
                backup_path is not None
                and os.path.exists(backup_path)
                and not os.path.exists(target_path)
            ):
                try:
                    os.replace(backup_path, target_path)
                except OSError:
                    pass
            for table in cached_tables:
                try:
                    table._rebind_uri(self._uri)
                except Exception:
                    pass
            raise BadArgument(
                f"重命名数据库失败: {e}；修复: 检查目标路径权限、"
                "是否跨文件系统（EXDEV），或改用 shutil.move 后重新打开"
            ) from e

        if backup_path is not None and os.path.exists(backup_path):
            try:
                shutil.rmtree(backup_path)
            except OSError as e:
                warnings.warn(
                    f"旧目标目录未能删除，残留在 {backup_path}: {e}",
                    RuntimeWarning,
                    stacklevel=2,
                )

        self._uri = _sqlite_uri_from_path(self._uri_schema, target_path)
        self._table_cls = Database.get_engine(self._uri)
        for table in cached_tables:
            table._rebind_uri(self._uri)
            if hasattr(table, "_load_partition"):
                table._partition = table._load_partition()
        return self

    @staticmethod
    def _rename_backup_path(target_path):
        backup_path = target_path + _RENAME_BACKUP_SUFFIX
        index = 0
        while os.path.exists(backup_path):
            index += 1
            backup_path = f"{target_path}{_RENAME_BACKUP_SUFFIX}{index}"
        return backup_path

    def _resolve_rename_database_target(self, new_uri_or_name):
        value = str(new_uri_or_name or "").strip()
        if not value:
            raise BadArgument("rename target must not be empty")
        if "://" in value:
            from .utils.uri import resolve_database_uri
            target_uri = resolve_database_uri(value, engine='sqlite', allow_missing=True,
                                               inspect_existing=False)
            return _sqlite_path_from_uri(target_uri)

        source_path = _sqlite_path_from_uri(self._uri)
        if os.path.isabs(value) or os.sep in value or (os.altsep and os.altsep in value):
            return os.path.abspath(value)
        return os.path.abspath(os.path.join(os.path.dirname(source_path), value))

    def rename_table(self, old_name, new_name, exist_ok=False, overwrite=False):
        """委托引擎重命名；表对象回调同步 Database 缓存。"""
        old_name = str(old_name or "").strip()
        new_name = str(new_name or "").strip()
        if not old_name or not new_name:
            raise BadArgument("table names must not be empty")
        if old_name == new_name:
            if exist_ok:
                return self.table(old_name)
            raise BadArgument("rename target is the current table")
        if overwrite and exist_ok:
            raise BadArgument("rename_table cannot use overwrite and exist_ok together")
        if old_name not in self.table_names():
            raise BadArgument(f"table does not exist: {old_name}")

        return self[old_name].rename(
            new_name, exist_ok=exist_ok, overwrite=overwrite,
        )

    def _on_table_renamed(self, table, old_name, new_name):
        """同步由表对象直接发起的重命名。"""
        with self._tables_lock:
            replaced = self._tables.get(new_name)
        if replaced is not None and replaced is not table:
            try:
                replaced.close()
            except Exception as exc:
                warnings.warn(
                    f"被覆盖表对象关闭失败: {exc}", RuntimeWarning, stacklevel=2,
                )
        with self._tables_lock:
            if self._tables.get(old_name) is table:
                self._tables.pop(old_name, None)
            self._tables.pop(new_name, None)
            self._tables[new_name] = table

    def close(self):
        # 先快照并清空缓存再关闭：避免持锁等待引擎连接租约释放
        with self._tables_lock:
            tables = list(self._tables.values())
            self._tables.clear()
        for table in tables:
            table.close()

    def get_process_connection_budget_status(self):
        if self._uri_schema not in ["sqlite", "sqlite3"]:
            raise BadArgument(
                "SQLite connection budget is only supported by the SQLite engine"
            )
        from .utils.mysqlite.connection_pool import (
            get_process_connection_budget_status,
        )
        return get_process_connection_budget_status()

    def migrate_to(self, target_uri, tables=None, mode='append',
                   batch_size=10_000, verify=None, dry_run=False, *,
                   specs=None, temp_dir=None, lock_timeout=30.0, target_engine=None):
        """保留源库，隔离转换后统一发布；specs 按源表名覆盖目标布局。"""
        from .migration import migrate
        return migrate(self, target_uri, tables, mode, batch_size, verify, dry_run,
                       specs=specs, temp_dir=temp_dir, lock_timeout=lock_timeout,
                       target_engine=target_engine)

    def save_as(self, target_uri, tables=None, mode='append',
                batch_size=10_000, verify=None, dry_run=False, *,
                specs=None, temp_dir=None, lock_timeout=30.0, target_engine=None):
        """将库另存为独立副本，保留源数据、契约和当前对象的 URI。

        参数、发布与锁语义同 migrate_to，返回 MigrationResult。
        目标已有数据时按 mode 合并，不自动清空；完整复制推荐新目标与
        verify='row-count'（要求选中目标表不存在或为空）。
        """
        return self.migrate_to(
            target_uri, tables=tables, mode=mode, batch_size=batch_size,
            verify=verify, dry_run=dry_run, specs=specs, temp_dir=temp_dir,
            lock_timeout=lock_timeout, target_engine=target_engine,
        )

    @staticmethod
    def _normalize_migration_mode(mode):
        value = str(mode or 'append').strip().lower()
        if value not in _MIGRATION_MODES:
            raise BadArgument(
                f"Unsupported migration mode `{mode}`；"
                f"可选: {', '.join(_MIGRATION_MODES)}"
            )
        return value

    @staticmethod
    def _validate_migration_target(target_uri):
        if not target_uri:
            raise BadArgument("migrate_to requires a target uri")
        try:
            schema, _ = parse_db_schema(target_uri)
        except ValueError as e:
            raise BadArgument(
                f"Bad migration target uri `{target_uri}`: {e}"
            ) from e
        if schema not in ['sqlite', 'sqlite3', 'postgresql', 'parquet']:
            raise BadArgument(
                f"Unsupported migration target schema `{schema}`；"
                "可选: sqlite/sqlite3/postgresql/parquet"
            )

    def _reject_migration_self_target(self, target_uri):
        """拒绝把目录库迁移到自身或自身目录内（会静默翻倍/递归）。"""
        if self._uri_schema not in ['sqlite', 'sqlite3', 'parquet']:
            return
        target_schema, _ = parse_db_schema(target_uri)
        if target_schema not in ['sqlite', 'sqlite3', 'parquet']:
            return
        def local_path(uri):
            _, options = parse_db_schema(uri)
            return os.path.realpath(os.path.join(
                options.get('path') or '', options.get('db') or ''
            ))
        source_path = local_path(self._uri)
        target_path = local_path(target_uri)
        if target_path == source_path:
            raise BadArgument(
                "migrate_to target 不能与源库相同；修复: 指定一个新的目标路径"
            )
        if target_path.startswith(source_path + os.sep):
            raise BadArgument(
                "migrate_to target 不能位于源库目录内；修复: 指定源库之外的目标路径"
            )

    def to_postgresql(self, target_uri, tables=None, mode='append',
                      batch_size=10_000, verify=None, dry_run=False, *,
                      specs=None, temp_dir=None, lock_timeout=30.0):
        target_schema, _ = parse_db_schema(target_uri)
        if target_schema != 'postgresql':
            raise BadArgument('to_postgresql requires a postgresql:// target uri')
        return self.migrate_to(
            target_uri,
            tables=tables,
            mode=mode,
            batch_size=batch_size,
            verify=verify,
            dry_run=dry_run, specs=specs, temp_dir=temp_dir, lock_timeout=lock_timeout,
        )

    def _migration_table_mapping(self, tables):
        names = self.table_names()
        # 当前实例中显式声明但尚未首写的空表，也属于本次迁移范围。
        with self._tables_lock:
            for name, table in self._tables.items():
                if name not in names and getattr(table, '_declared_spec', None) is not None:
                    names.append(name)
        if tables is None:
            if not names:
                raise BadArgument('migrate_to found no source tables')
            mapping = {name: name for name in names}
        else:
            if isinstance(tables, str):
                mapping = {tables: tables}
            elif isinstance(tables, dict):
                mapping = dict(tables)
            elif isinstance(tables, (list, tuple, set)):
                mapping = {name: name for name in list(tables)}
            else:
                raise BadArgument(
                    "tables must be a list/dict/str, got "
                    f"`{type(tables).__name__}`"
                )
            if not mapping:
                raise BadArgument("migrate_to requires at least one table")
        known = set(names)
        missing = [name for name in mapping if name not in known]
        # 提前校验源表存在，dry_run 也不会为不存在的表创建目录/meta
        if missing:
            raise BadArgument(
                "migrate_to source table(s) not found: "
                + ", ".join(str(name) for name in missing)
                + "；修复: 先检查 db.table_names()（当前: "
                + (", ".join(sorted(str(name) for name in known)) or "∅")
                + "）"
            )
        return mapping

    def _new_migration_target(self, target_uri):
        return Database(target_uri)

    @classmethod
    def get_engine(cls, uri):
        uri_schema, _= parse_db_schema(uri)

        engine = None
        if uri_schema in ['sqlite', 'sqlite3']:
            engine = sqlite.Table
        elif uri_schema == 'postgresql':
            from .engines import postgresql
            engine = postgresql.Table
        elif uri_schema == 'parquet':
            from .engines import parquet
            engine = parquet.Table

        if not engine:
            raise EngineNotExist(uri)

        return engine
