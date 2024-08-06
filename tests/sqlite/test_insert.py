import shutil
import config
import pytest
import random
import time
import arrow
import logging
import sqlite3
from iosql.utils.mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn
from iosql.query.where import Where
from iosql.query.case import Case
from iosql import Database

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')

db = None
def cleanup():
    logging.info('Test: Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)


def test_connection():
    cleanup()

    data = []
    for i in range(0, 5):
        dic = {'id': i, 'i0': i}
        print(dic)
        data.append(dic)

    with Database('sqlite://database/finance.sqlite3', verbose=1) as db:
        t = db['lrb']
        t.insert_all(data, pk=['id'])
        print(t.table_parts())

    for i in range(0, 5):
        dic = {'id': i, 'ii0': i*i}
        print(dic)
        data.append(dic)

    with Database('sqlite://database/finance.sqlite3', verbose=1) as db:
        t = db['lrb']
        ensure_column = True
        # print(f'-----------{ensure_column=}')
        t.upsert_all(data, pk=['id'])
        # print(t.table_parts())

    for i in range(0, 6):
        dic = {'id': i, 'iii0': i*i*i}
        print(dic)
        data.append(dic)

    with Database('sqlite://database/finance.sqlite3', verbose=1) as db:
        t = db['lrb']
        ensure_column = True
        # print(f'-----------{ensure_column=}')
        t.upsert_all(data, pk=['id'])
        # print(t.table_parts())

if __name__ == '__main__':
    if 1:
        test_connection()
