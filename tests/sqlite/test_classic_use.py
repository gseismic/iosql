import shutil
import config
import pytest
import random
import time
import arrow
import logging
from iosql import Database
import sqlite3
from mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')
logging.getLogger("iosql").setLevel(logging.DEBUG)


db = None
def cleanup():
    logging.info('Test: Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)


def test_select():
    # 数据库被删除，需要重新连接，因为保持的变量显示在连接中
    cleanup()
    db = Database('sqlite://database/finance.sqlite3')

    data = []
    d = arrow.get('2020-01-01')
    min_id, max_id = 0, 60*12*2*3
    for i in range(min_id, max_id):
        date = d.shift(minutes=i)
        dic = {'id': i, 'date': date.timestamp(), 'iii': i*i*i}
        print(i, dic)
        data.append(dic)

    db['demo'].insert_all(data, pk=['id','date'], 
                           replace=True, 
                           partition='TStoYYYYMMDD(date)')
    print(db['demo'].table_parts())
    # assert len(db['demo'].table_parts()) == 3

    values = db['demo'].select(
        'id,date', 
        where={'field': 'id', 'op': '<', 'value': 3}
    )
    print(values, len(values))
    assert len(values) == 3

    values = db['demo'].select(
        'id,date', 
        where={'field': 'date', 'op': '<', 'value': arrow.get('2020-01-02').timestamp()}
    )
    print([dict(item) for item in values], len(values))

    values = db['demo'].select(
        'id,date', 
        where={'field': 'date', 'op': 'between_and', 
               'value':
               [arrow.get('2020-01-02 00').timestamp(),
                arrow.get('2020-01-02 01').timestamp()]}
    )
    print(values)
    print([dict(item) for item in values], len(values))
    print(arrow.get(dict(values[0])['date']))
    print(arrow.get(dict(values[-1])['date']))


if __name__ == '__main__':
    if 1:
        test_select()
