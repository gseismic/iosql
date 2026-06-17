import os

from .utils.schema import parse_db_schema
from .exception import EngineNotExist
from . import sqlite
from .spec import TableSpec


class Database(object):
    '''
    table是BigTable
    '''

    def __init__(self, uri, verbose=0):
        self._uri = uri
        self._table_cls = Database.get_engine(uri)
        self._tables = {}
        self.verbose = verbose

    def __getitem__(self, table_name):
        if table_name not in self._tables:
            table = self._table_cls(self._uri, table_name=table_name,
                                    verbose=self.verbose)
            self._tables[table_name] = table
        return self._tables[table_name]

    def table(self, table_name, spec=None):
        table = self[table_name]
        if spec is not None:
            table.use_spec(spec)
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

    @classmethod
    def get_engine(cls, uri):
        uri_schema, _= parse_db_schema(uri)

        engine = None
        if uri_schema in ['sqlite', 'sqlite3']:
            engine = sqlite.Table
        elif uri_schema == 'postgresql':
            from . import postgresql
            engine = postgresql.Table

        if not engine:
            raise EngineNotExist(uri)

        return engine
