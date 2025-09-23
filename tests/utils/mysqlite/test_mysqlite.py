import shutil
import pytest
import sqlite3
import logging
from iosql.utils.mysqlite import Database
from iosql.utils.mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn
from iosql.utils.mysqlite.sql import getsql_create_table


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')

db = None
def cleanup():
    logging.info('Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)
    global db
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)


def test_basic():
    cleanup()
    with Database('database/mysqlite.db', auto_commit=True, verbose=3) as db:
        db['data'].insert({'id': 3, 'test': 5}, pk=['id'], ensure_table=True)
        db['data'].insert({'id': 5, 'test': 5, 'name': 'lsl'}, pk=['id'],
                          ensure_table=True, ensure_column=True)


def test_default():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)
    print('empty default', db['demo2'].default_values())
    db['demo2'].create_table([
        {'name': 'id', 'type': 'text'},
        {'name': 'name', 'type': 'text', 'default': 2},
        {'name': 'value', 'type': 'float', 'default': '666'}
    ], pk=('id'))

    print('default', db['demo2'].default_values())

    # 如果本来是float，解析'a'失败，返回的是字符串
    db['demo2'].insert({'id': 3, 'value':'333'}, replace=False)
    db['demo2'].insert({'id': 5, 'name': 7}, replace=False)
    db['demo2'].insert({'id': 8, 'name': 100}, replace=False)
    db['demo2'].insert({'id': 9, 'value':'a'}, replace=False)
    db['demo2'].insert({'id': 10, 'name': None, 'value':'a'}, replace=False)
    db['demo2'].insert({'id': 11, 'name': 'NULL', 'value':'a'}, replace=False)
    db['demo2'].insert({'id': 12, 'name': '', 'value':'a'}, replace=False)
    data = db['demo2'].select_all('*')
    # print(data)
    # 如果设置成了None, 会存储None, 而非使用默认值
    d_data = {item['id']: dict(item) for item in data}
    assert d_data['3']['value'] == 333
    assert d_data['3']['name'] == '2'
    assert d_data['5']['value'] == 666
    print(d_data['10'])
    print(d_data['11'])
    print(d_data['12'])


def test_table():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)
    with pytest.raises(NoSuchTable): 
        db['demo2'].insert({'value':'333'}, replace=True)

    db['demo2'].create_table([
        {'name': 'id', 'type': 'text'},
        {'name': 'name', 'type': 'text'},
        {'name': 'sss', 'type': 'text', 'default': '2'},
        {'name': 'value', 'type': 'float', 'default': 666}
    #], pk=('name', 'id'))
    ], pk=('id'))

    # print(db['demo2']._table_info())
    with pytest.raises(NoSuchColumn):
        db['demo2'].insert({'a':1}, replace=True)

    with pytest.raises(sqlite3.IntegrityError):
        db['demo2'].insert({'value':'333'}, replace=False)

    db['demo3'].create_table([
        {'name': 'id', 'type': 'text'},
        {'name': 'name', 'type': 'float'},
        {'name': 'value', 'type': 'float'}
    ], pk='id', exist_no_warn=False)

    db['demo5'].create_table([
        {'name': 'id', 'type': 'text'},
        {'name': 'name', 'type': 'float'}
    ], pk='id', exist_no_warn=True)
    db['demo5'].insert({'id': 1, 'name': 'lsl'}, ignore=True)
    db['demo5'].add_column('name3', 'text')
    with pytest.raises(DuplicateColumn):
        db['demo5'].add_column('name3', 'text')

    db['not_exist'].insert({'id': 1, 'name': 'lsl'}, ensure_table=True)
    db['not_exist'].insert({'id': 1, 'name3': 'lsl'}, ensure_column=True)
    db['not_exist'].insert({'id': 1, 'name5': 'lsl', 'name6': 'lsl'}, ensure_column=True)
    db['not_exist2'].insert(
        {'id': 1, 'name5': 'lsl', 'name6': 'lsl'}, 
        ensure_column=True, ensure_table=True
    )

    print([dict(item) for item in db['not_exist2'].select('*', where=None, to_dict=True)])
    print(db['data'].table_exists())
    print(db['not_exist2'].table_exists())
    return


def test_insert():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)
    data1 = [{'id': i, 'name': str(i*i), 'value': float(i)} for i in range(30)]
    data2 = [{'name': str(i*i*i), 'id': i+100} for i in range(30)]
    data = data1 + data2
    db['data'].create_table([
        {'name': 'id', 'type': 'integer', 'default': '2'},
        {'name': 'name', 'type': 'text', 'default': '2'},
        {'name': 'value', 'type': 'float', 'default': 666.0}
    ], pk=('id', 'name'))

    db['data'].insert_all(data)
    out_data = db['data'].select('*', where=None, to_dict=True)
    assert len(out_data) == len(data)
    ref_result = {str(item['id']) + item['name']: item for item in data}
    my_result = {str(item['id']) + item['name']: item for item in out_data}

    print('ref', ref_result.keys()) 
    print('my', my_result.keys())
    print(len(ref_result.keys()), len(my_result.keys()))
    assert len(ref_result.keys()) == len(my_result.keys())

    fields = list(out_data[0].keys())
    for key, my_dic in my_result.items():
        print(my_dic, '|', ref_result[key])
        for k in fields:
            if k == 'value':
                print(key, my_result[key][k], ref_result[key][k])
            assert str(my_result[key][k]) == str(ref_result[key][k])
    # db['data'].insert_all(data)

    with pytest.raises(NoSuchTable):
        db['data2'].insert_all(data)

    db['data3'].insert_all(data, ensure_table=True)
    with pytest.raises(NoSuchColumn):
        db['data3'].insert_all([
            {'id': i, 'name': str(i*i), 'x':3, 'y':9} 
            for i in range(30)]
        )
    db['data3'].insert_all([
        {'id': i, 'name': str(i*i), 'x':3, 'y':9} 
        for i in range(30)], ensure_column=True
    )


def test_delete():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)
    db['data'].insert_all([
        {'id': i, 'name': str(i*i), 'x':3, 'y':9} 
        for i in range(30)], ensure_table=True,
        ensure_column=True
    )
    print(db['data'].select('*', where=None, to_dict=True))
    db['data'].delete('id>3')
    print('new:')
    print(db['data'].select('*', where=None, to_dict=True))

    db['data'].delete_all()
    print(db['data'].select('*', where=None, to_dict=True))


def test_update():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)
    db['data'].insert_all([
        {'id': i, 'name': str(i*i), 'x':3, 'y':9} 
        for i in range(30)], ensure_table=True,
        ensure_column=True
    )
    print(db['data'].select('*', where=None, to_dict=True))

    db['data'].update({'name': '999'}, where='id=3')
    print(db['data'].select('*', where=None, to_dict=True))


def test_index():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)
    db['data'].insert_all([
        {'id': i, 'name': str(i*i), 'x':3, 'y':9} 
        for i in range(30)], ensure_table=True,
        ensure_column=True
    )
    db['data'].create_index('name')
    print('index', db['data'].get_all_index())
    assert len(db['data'].get_all_index()) == 1
    with pytest.raises(Exception, match='no such index'):
        db['data'].drop_index('name')
    db['data'].drop_column_index('name')
    assert len(db['data'].get_all_index()) == 0

    db['data'].drop_table()
    with pytest.raises(NoSuchTable):
        print(db['data'].select('*', where=None, to_dict=True))


def test_select():
    cleanup()
    db = Database('database/mysqlite.db', auto_commit=True, verbose=3)


if __name__ == '__main__':
    if 0:
        test_basic()
    if 0:
        test_default()
    if 0:
        test_table()
    if 1:
        test_insert()
    if 0:
        test_delete()
    if 0:
        test_update()
    if 0:
        test_index()
