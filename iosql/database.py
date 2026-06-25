import os

from .utils.schema import parse_db_schema
from .exception import BadArgument, EngineNotExist
from .engines import sqlite
from .spec import MigrationResult, TableContract, TableSpec


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
    ):
        table = self[table_name]
        shortcut_values = [
            pk,
            primary_key,
            columns,
            partition,
            segment,
            order_key,
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
            table.use_contract(TableContract(
                primary_key=normalized_pk if normalized_pk is not None else normalized_primary_key,
                columns=columns or {},
                partition=partition,
                segment=segment,
                order_key=order_key,
                conflict=conflict,
                schema_policy=schema_policy,
            ))
        return table

    def timeseries(self, table_name, time, primary_key, columns=None,
                   partition_every='1day', timezone='UTC',
                   schema_policy='evolve', symbol_column='symbol',
                   session_column=None,
                   target_rows_per_file=400_000,
                   min_rows_per_file=300_000,
                   max_rows_per_file=500_000,
                   time_unit='ms', partition_kind='duration'):
        spec = TableSpec.timeseries(
            time=time,
            primary_key=primary_key,
            columns=columns,
            partition_every=partition_every,
            timezone=timezone,
            time_unit=time_unit,
            partition_kind=partition_kind,
            schema_policy=schema_policy,
            symbol_column=symbol_column,
            session_column=session_column,
            target_rows_per_file=target_rows_per_file,
            min_rows_per_file=min_rows_per_file,
            max_rows_per_file=max_rows_per_file,
        )
        return self.table(table_name, spec=spec)

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
