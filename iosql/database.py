from .utils.schema import parse_db_schema
from .exception import EngineNotExist
from . import sqlite


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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()

    def table_names(self):
        return self['__dummy_table'].table_names()

    def close(self):
        for _, table in self._tables.items():
            table.close()

    @classmethod
    def get_engine(cls, uri):
        uri_schema, _= parse_db_schema(uri)

        engine = None
        if uri_schema in ['sqlite', 'sqlite3']:
            engine = sqlite.Table

        if not engine:
            raise EngineNotExist(uri)

        return engine
