import pytest
from iosql.utils.mysqlite.sql import (
    getsql_create_table,
    getarg_diclist_insertmany,
    _union_fields_of_dictlist
)


class TestSqlModule:
    def test_create_table_sql(self):
        sql, meta = getsql_create_table('demo', [
            {'name': 'id', 'type': 'text', 'default': '2'},
            {'name': 'name', 'type': 'text', 'default': '2'},
            {'name': 'sss', 'type': 'text', 'default': '2'}
        ], pk=['id', 'name'])

        assert 'CREATE TABLE' in sql
        assert 'demo' in sql
        assert meta['table_name'] == 'demo'
        assert len(meta['columns']) == 3

    def test_union_fields_of_dictlist(self):
        fields = _union_fields_of_dictlist([
            {'1': 1, 'b': 3},
            {'3': 2, 'b': 3},
            {'1': 1, 'c': 3}
        ])
        assert list(fields.keys()) == ['1', 'b', '3', 'c']

        fields = _union_fields_of_dictlist([
            {'3': 2, 'b': 3},
            {'1': 1, 'b': 3},
            {'1': 1, 'c': 3}
        ])
        assert list(fields.keys()) == ['3', 'b', '1', 'c']

    def test_getarg_diclist_insertmany(self):
        data = [
            {'3': 2, 'b': 3},
            {'1': 1, 'b': 3},
            {'1': 1, 'c': 3}
        ]
        fields, list_data = getarg_diclist_insertmany(data, {})
        assert list(fields.keys()) == ['3', 'b', '1', 'c']
        assert list_data[0] == [2, 3, None, None]
        assert list_data[1] == [None, 3, 1, None]
        assert list_data[2] == [None, None, 1, 3]