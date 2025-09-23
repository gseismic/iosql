import shutil
import logging

import sys
from iosql.utils.mysqlite import Database
from iosql.utils.mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')

db = None
def cleanup():
    logging.info('Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)
    global db
    db = Database('database/mysqlite.sqlite3', auto_commit=True, verbose=3)


def run():
    cleanup()
    db = Database('database/mysqlite.sqlite3', auto_commit=True, verbose=3)
    print('empty default', db['demo'].default_values())
    db['demo'].create_table([
        {'name': 'id', 'type': 'text'},
        {'name': 'name', 'type': 'text', 'default': 'default_name'},
        {'name': 'value', 'type': 'float', 'default': -1}
    ], pk=('id'))

    # 如果本来是float，解析'a'失败，返回的是字符串
    db['demo'].insert({'id': 0, 'value':'333'}, replace=False)
    db['demo'].insert({'id': 1, 'name': None, 'value':'a'}, replace=False)# name IS default value 
    db['demo'].insert({'id': 2, 'name': '', 'value':'a'}, replace=False) # name NOT default value 

    db['demo'].insert_all([
        {'id': i+10, 'name': str(i), 'value':i*i} for i in range(3)], 
        ensure_table=True,
        ensure_column=True
    )

    db['demo'].insert_all([
        {'id': i+100, 'name': str(i), 'value':None} for i in range(3)]
    )

    data = db['demo'].select_all('*')
    print(f'{data=}')

if __name__ == '__main__':
    run()
