from .table import Table
from .api import get_engine
from .utils.schema import parse_db_schema


class DatabaseBase(object):

    def __init__(self, uri):
        self._uri = uri
        schema, _ = parse_db_schema(uri)
        self._engine = get_engine(uri)
        self._tables = {}

    def __getitem__(self, table_name):
        if table_name not in self._tables:
            self._tables[table_name] = Table(self._engine, table_name=table_name)
        return self._tables[table_name]

    def select(self, fields, table, where, limit=None, **kwargs):
        '''
        note:
            fields: [lrv zcfzb.fzl], from_='lrb'
        '''
        pass

    def delete(self, table, where):
        # cases = parse_where(where)
        # self._engine.delete(where=cases)
        pass

    def create_index(self, table_name, field, order='ASC', exist_ok=True):
        self.create_index(table_name, field, order='ASC', exist_ok=True)

    def drop_table(self):
        pass

    def drop_index(self):
        pass

    def set(self, key, value, ttl=None):
        self._engine.store(key, value)

    def get(self, key):
        return self._engine.load(key)

    def clear(self, key):
        return self._engine.clear(key)

    def set_option(self, key, value):
        self._engine.set_option(key, value)

    def alter_role(self, role_name, rights):
        pass

    def delete_role(self, role_name):
        pass

    def select_role(self):
        pass

    def grant(self, user, role, on):
        pass

    def close(self):
        self._engine.close()

    def export_table(self, src_tblname, dst_database, dst_tblname=None):
        pass

    def export_all_tables(self, src_database):
        pass

    def import_table(self, src_tblname, src_database, dst_tblname=None):
        pass

    def import_all_tables(self, src_database):
        pass

    def backup(self, uri, tblnames=None, compress=True, compress_algo='xz'):
        pass

    def restore(self, uri, tblnames=None, replace=False):
        '''
        如果完全放弃最新的表，可以把当前表删除
        replace=False: insert or ignore into
        replace=True: insert or replace into

        # bkp_only='keep', cur_only='keep', common='replace'
        usql.py backup uri bkp_uri --tblnames tbl1 tbl2 tbl3
        usql.py restore bkp_uri uri --tblnames tbl1 tbl2 tbl3
        '''
        pass

    @property
    def tables(self):
        pass

    def table_names(self):
        db.table_names()
