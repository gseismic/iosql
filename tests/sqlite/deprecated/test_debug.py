import shutil
import pytest
import time
import arrow
import logging
from iosql import Database
import sqlite3
from iosql.utils.mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')
def cleanup():
    logging.info('Test: Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)

def test_simple():
    cleanup()
    with Database('sqlite://database/finance.sqlite3', verbose=3) as db:
        data = []
        for i in range(0, 7):
            d = arrow.now().shift(days=-10)
            date = d.shift(days=i).format('YYYY-MM-DD HH:MM')
            dic = {'id': i, 'date': date, 'iii10': i*i*i}
            print(dic)
            data.append(dic)

        print('inserting ...')
        t = time.time()
        partition = 'toYYYYMMDD(date)'
        db['lrb'].insert_all(data, pk=['id','date'], 
                             replace=True, partition=partition)

        data = []
        for i in range(100, 127):
            d = arrow.now().shift(days=-10)
            date = d.shift(days=i).format('YYYY-MM-DD HH:MM')
            data.append({'id': i, 'date': date, 'iii22': i*i*i,
                         'xxx': i+10, 'yyy': 'YYY'
                        })

        db['lrb'].insert_all(data, pk=['id','date'], 
                             replace=True, alter=True, ensure_column=True,
                             partition=partition)

def test_create_table():
    cleanup()
    db = Database('sqlite://database/finance.sqlite3', verbose=1)
    data = []
    for i in range(0, 100):
        d = arrow.now().shift(days=-10)
        date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date, 'iii10': i*i*i})

    print('inserting ...')
    t = time.time()
    db['lrb'].insert_all(data, pk=['id','date'], 
                         replace=True, partition='toYYYYMMDD(date)')

    data = []
    for i in range(80, 200):
        d = arrow.now().shift(days=-10)
        date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date, 'iii22': i*i*i,
                     'xxx': i+10, 'yyy': 'YYY'
                    })

    db['lrb'].insert_all(data, pk=['id','date'], 
                         replace=True, alter=True, ensure_column=True,
                         partition='toYYYYMMDD(date)')


if __name__ == '__main__':
    if 0:
        test_simple()
    if 1:
        test_create_table()
