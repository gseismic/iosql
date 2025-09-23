import logging
from iosql.utils.mysqlite.sql import (
    getsql_create_table,
    getarg_diclist_insertmany,
    _union_fields_of_dictlist
)
from iosql.utils.mysqlite import Database


def test_basic():
    sql = getsql_create_table('demo', [
        {'name': 'id', 'type': 'text', 'default': '2'},
        {'name': 'name', 'type': 'text', 'default': '2'},
        {'name': 'sss', 'type': 'text', 'default': '2'}
    ], pk=('id', 'name'))

    print('create table', sql)
    # db['demo'].create_table({
    #db['test'].insert({'a':1}, pk='a', replace=True)
    #db['test'].add_column('e', 'float')

def test_insert():
    fields = _union_fields_of_dictlist([
        {'1':1, 'b': 3},
        {'3':2, 'b': 3},
        {'1':1, 'c': 3}
    ])
    print(fields)
    assert list(fields.keys()) == ['1', 'b', '3', 'c']
    fields = _union_fields_of_dictlist([
        {'3':2, 'b': 3},
        {'1':1, 'b': 3},
        {'1':1, 'c': 3}
    ])
    assert list(fields.keys()) == ['3', 'b', '1', 'c']
    data = [
        {'3':2, 'b': 3},
        {'1':1, 'b': 3},
        {'1':1, 'c': 3}
    ]
    print(data)
    fields, list_data = getarg_diclist_insertmany(data, {})
    print(fields)
    print(list_data)
    assert list(fields.keys()) == ['3', 'b', '1', 'c']
    assert list_data[0] == [2, 3, None, None]
    assert list_data[1] == [None, 3, 1, None]
    assert list_data[2] == [None, None, 1, 3]


if __name__ == '__main__':
    if 0:
        test_basic()
    if 1:
        test_insert()
