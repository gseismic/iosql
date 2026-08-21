import os
import shutil
import warnings

from .utils.schema import parse_db_schema
from .exception import BadArgument, EngineNotExist, UnsafeOperation
from .engines import sqlite
from .partition import Partition
from .spec import MigrationResult, Segment, TableContract, TimeColumn, PartitionSpec


def _is_calendar_partition(partition):
    return getattr(partition, 'PARTITION_TYPE', None) == 'calendar'


_DEFAULT_MAX_OPEN_PARTITIONS = 5
_DEFAULT_MAX_CONNECTIONS_PER_FILE = 1
_DEFAULT_MAX_PROCESS_CONNECTIONS = 32


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
    return f"{schema}://{os.path.abspath(path)}"


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
    ):
        self._uri = uri
        self._table_cls = Database.get_engine(uri)
        self._tables = {}
        self.verbose = verbose
        uri_schema, _ = parse_db_schema(uri)
        self._uri_schema = uri_schema
        sqlite_engine = uri_schema in ["sqlite", "sqlite3"]
        if pragma_dict is not None and not sqlite_engine:
            raise BadArgument("pragma_dict is only supported by the SQLite engine")
        if not sqlite_engine and (
            max_open_partitions != _DEFAULT_MAX_OPEN_PARTITIONS
            or max_connections_per_file != _DEFAULT_MAX_CONNECTIONS_PER_FILE
            or max_process_connections != _DEFAULT_MAX_PROCESS_CONNECTIONS
        ):
            raise BadArgument(
                "SQLite file limits are only supported by the SQLite engine"
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
        self._pragma_dict = pragma_dict

    @property
    def uri(self):
        return self._uri

    @staticmethod
    def _positive_int(name, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BadArgument(f"{name} must be a positive integer")
        return value

    def __getitem__(self, table_name):
        if table_name not in self._tables:
            table_options = {
                "table_name": table_name,
                "verbose": self.verbose,
            }
            if self._uri_schema in ["sqlite", "sqlite3"]:
                table_options.update({
                    "max_open_partitions": self._max_open_partitions,
                    "max_connections_per_file": self._max_connections_per_file,
                    "max_process_connections": self._max_process_connections,
                })
                if self._pragma_dict is not None:
                    table_options["pragma_dict"] = self._pragma_dict
            table = self._table_cls(self._uri, **table_options)
            self._tables[table_name] = table
        return self._tables[table_name]

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
        force_partition=False,
        time=None,
        symbol_column=None,
        session_column=None,
    ):
        """创建或打开逻辑表（主入口）。

        `time` / `symbol_column` / `session_column` 让契约感知能力
        （query()/latest()/asof()）无需 timeseries() 即可使用；
        duration/calendar 分区还会在未显式声明 time 时自动反推。
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
        if spec is not None and any(value is not None for value in shortcut_values):
            raise BadArgument('Database.table: spec conflicts with shortcut arguments')

        if spec is not None:
            contract = TableContract.ensure(spec)
            contract.force_partition = force_partition
            table.use_contract(contract)
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
            table.use_contract(TableContract(
                primary_key=normalized_pk if normalized_pk is not None else normalized_primary_key,
                columns=columns or {},
                partition=partition,
                segment=segment,
                order_key=order_key,
                conflict=conflict,
                schema_policy=schema_policy,
                **contract_kwargs,
            ))
        return table

    def timeseries(self, table_name, time, pk=None, primary_key=None,
                   columns=None, *, partition_every='1day', timezone='UTC',
                   symbol_column='symbol', session_column=None,
                   partition=None, segment=None, order_key=None,
                   conflict='append', schema_policy='evolve',
                   force_partition=False,
                   partition_kind=None, time_unit=None,
                   target_rows_per_file=None, min_rows_per_file=None,
                   max_rows_per_file=None):
        """时序语法糖：`table()` 的薄封装。

        等价于 `db.table(..., partition=Partition.duration(time, every=partition_every),
        order_key=time, time=time, ...)`；calendar 场景传
        `partition=Partition.calendar(...)`。

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
                stacklevel=2,
            )
            use_calendar = str(partition_kind).strip().lower() == 'calendar'
        if time_unit is not None:
            warnings.warn(
                "timeseries(time_unit=...) 已废弃：date 场景请使用 calendar 分区 "
                "(partition=Partition.calendar(...) 或 db.table(partition=...))",
                DeprecationWarning,
                stacklevel=2,
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
                stacklevel=2,
            )
            if segment is None:
                segment = Segment.rows(
                    max_rows=max_rows_per_file or 500_000,
                    target_rows=target_rows_per_file or 400_000,
                    min_rows=min_rows_per_file or 300_000,
                )

        if partition is None:
            if use_calendar:
                partition = Partition.calendar(
                    time,
                    every=partition_every,
                    timezone=timezone,
                )
            else:
                # duration 分区按 UTC 对齐，无时区概念；
                # 经 PartitionSpec 构造以支持 "1s"/"1day" 等字符串间隔
                partition = PartitionSpec(
                    column=time, every=partition_every, kind='duration',
                ).to_partition()

        if isinstance(time, str):
            time_column = TimeColumn(
                time,
                unit='date' if _is_calendar_partition(partition) else 'ms',
                timezone=timezone,
            )
        else:
            time_column = time

        return self.table(
            table_name,
            pk=normalized_pk,
            primary_key=normalized_primary_key,
            columns=columns,
            partition=partition,
            segment=segment,
            order_key=order_key or time,
            conflict=conflict,
            schema_policy=schema_policy,
            force_partition=force_partition,
            time=time_column,
            symbol_column=symbol_column,
            session_column=session_column,
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
                        where n.nspname = 'public'
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
        return sorted([
            item for item in os.listdir(db_path)
            if os.path.isdir(os.path.join(db_path, item))
            and not item.startswith('__')
        ])

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
        if os.path.realpath(target_path).startswith(
            os.path.realpath(source_path) + os.sep
        ):
            raise UnsafeOperation("rename target must not be inside the source database")
        if not os.path.isdir(source_path):
            raise BadArgument(f"database does not exist: {source_path}")
        if os.path.exists(target_path):
            if not overwrite:
                raise BadArgument(f"rename target already exists: {target_path}")
            if not os.path.isdir(target_path):
                raise UnsafeOperation(
                    f"rename target exists and is not a directory: {target_path}"
                )
            shutil.rmtree(target_path)

        cached_tables = list(self._tables.values())
        for table in cached_tables:
            table.close()
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        os.replace(source_path, target_path)
        self._uri = _sqlite_uri_from_path(self._uri_schema, target_path)
        self._table_cls = Database.get_engine(self._uri)
        for table in cached_tables:
            table._rebind_uri(self._uri)
            if hasattr(table, "_load_partition"):
                table._partition = table._load_partition()
        return self

    def _resolve_rename_database_target(self, new_uri_or_name):
        value = str(new_uri_or_name or "").strip()
        if not value:
            raise BadArgument("rename target must not be empty")
        if "://" in value:
            schema, _ = parse_db_schema(value)
            if schema not in ["sqlite", "sqlite3"]:
                raise BadArgument("Database.rename target must be sqlite:// or sqlite3://")
            return _sqlite_path_from_uri(value)

        source_path = _sqlite_path_from_uri(self._uri)
        if os.path.isabs(value) or os.sep in value or (os.altsep and os.altsep in value):
            return os.path.abspath(value)
        return os.path.abspath(os.path.join(os.path.dirname(source_path), value))

    def rename_table(self, old_name, new_name, exist_ok=False, overwrite=False):
        """重命名逻辑表并维护当前 Database 的表对象缓存。"""
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

        table = self[old_name]
        cached_target = self._tables.get(new_name)
        if cached_target is not None and cached_target is not table and overwrite:
            cached_target.close()
            self._tables.pop(new_name, None)

        table.rename(new_name, exist_ok=exist_ok, overwrite=overwrite)
        self._tables.pop(old_name, None)
        self._tables[new_name] = table
        return table

    def close(self):
        for _, table in list(self._tables.items()):
            table.close()
        self._tables.clear()

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
                   batch_size=10_000, verify=None, dry_run=False):
        verify = _normalize_migration_verify(verify)
        table_mapping = self._migration_table_mapping(tables)
        if dry_run:
            results = []
            for source_name, target_name in table_mapping.items():
                source_table = self.table(source_name)
                spec = source_table.spec()
                if not spec:
                    raise BadArgument(
                        f'migrate_to requires a TableContract: {source_name}'
                    )
                results.append(
                    source_table._migration_dry_run_result(
                        target_uri=target_uri,
                        target_name=target_name,
                        batch_size=batch_size,
                        verify=verify,
                    )
                )
            return MigrationResult(
                source_uri=self._uri,
                target_uri=target_uri,
                tables=results,
                verify=verify,
                dry_run=True,
            )

        target_db = self._new_migration_target(target_uri)
        results = []
        try:
            for source_name, target_name in table_mapping.items():
                source_table = self.table(source_name)
                spec = source_table.spec()
                if not spec:
                    raise BadArgument(
                        f'migrate_to requires a TableContract: {source_name}'
                    )
                target_table = target_db.table(target_name, spec=spec)
                results.append(
                    source_table._migrate_to_target_table(
                        target_table,
                        target_uri=target_uri,
                        target_name=target_name,
                        mode=mode,
                        batch_size=batch_size,
                        verify=verify,
                    )
                )
        finally:
            target_db.close()
        return MigrationResult(
            source_uri=self._uri,
            target_uri=target_uri,
            tables=results,
            verify=verify,
            dry_run=False,
        )

    def to_postgresql(self, target_uri, tables=None, mode='append',
                      batch_size=10_000, verify=None, dry_run=False):
        target_schema, _ = parse_db_schema(target_uri)
        if target_schema != 'postgresql':
            raise BadArgument('to_postgresql requires a postgresql:// target uri')
        return self.migrate_to(
            target_uri,
            tables=tables,
            mode=mode,
            batch_size=batch_size,
            verify=verify,
            dry_run=dry_run,
        )

    def _migration_table_mapping(self, tables):
        if tables is None:
            names = self.table_names()
            if not names:
                raise BadArgument('migrate_to found no source tables')
            return {name: name for name in names}
        if isinstance(tables, dict):
            return dict(tables)
        if isinstance(tables, str):
            return {tables: tables}
        return {name: name for name in list(tables)}

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

        if not engine:
            raise EngineNotExist(uri)

        return engine
