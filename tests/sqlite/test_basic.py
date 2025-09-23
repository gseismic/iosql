import shutil
import random
import time
import arrow
import logging
from iosql import Database
import sqlite3
from iosql.utils.mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')
#logging.getLogger("iosql").setLevel(logging.WARNING) 
#logging.getLogger("iosql").setLevel(logging.INFO) 


db = None
def cleanup():
    logging.info('Test: Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)
    # global db
    # db = Database('sqlite://database/finance.sqlite3')


def test_connection():
    cleanup()
    db = Database('sqlite://database/finance.sqlite3', verbose=1)
    data = []
    for i in range(0, 2):
        d = arrow.now().shift(days=-10)
        date = d.shift(days=i).format('YYYY-MM-DD HH:MM')
        dic = {'id': i, 'date': date, 'iii10': i*i*i}
        print(dic)
        data.append(dic)

    #with Database('sqlite://database/finance.sqlite3', verbose=1) as _db:
    #    print(_db['lrb'])
    t = db['lrb']
    t.insert_all(data, pk=['id','date'], partition='toYYYYMMDD(date)')
    print(t.table_parts())


def test_create_table():
    cleanup()
    with Database('sqlite://database/finance.sqlite3', verbose=3) as db:
        print('db', db)
        print('db[demo]', db['demo'])
        data = []
        for i in range(0, 100):
            d = arrow.now().shift(days=-10)
            date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
            data.append({'id': i, 'date': date, 'iii10': i*i*i})

        print('inserting ...')
        t = time.time()
        db['lrb'].insert_all(data, pk=['id','date'], 
                             replace=True, 
                             partition='toYYYYMMDD(date)')

        if 0:
            with pytest.raises(Exception, match='PrimaryKeyCanNotChange'):
                db['lrb'].insert_all(data, pk=['id','date3'], 
                                     replace=True, 
                                     partition='toYYYYMMDD(date)')

        data = []
        for i in range(80, 200):
            d = arrow.now().shift(days=-10)
            date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
            data.append({'id': i, 'date': date, 'iii22': i*i*i,
                         'xxx': i+10, 'yyy': 'YYY'
                        })

        if 0:
            with pytest.raises(Exception, match='PrimaryKeyCanNotChange'):
                db['lrb'].insert_all(data, pk=['id','date3'], 
                                     replace=True, alter=True,
                                     partition='toYYYYMMDD(date)')

        db['lrb'].insert_all(data, pk=['id','date'], 
                             replace=True, alter=True, ensure_column=True,
                             partition='toYYYYMMDD(date)')
        return

        data = []
        for i in range(200, 300):
            d = arrow.now().shift(days=-10)
            date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
            data.append({'id': i, 'date': date, 'iii22': i*i*i,
                         'xxx': i+10, 'yyy': 'YYY'
                        })

        with pytest.raises(Exception, match='PrimaryKeyCanNotChange'):
            db['lrb'].insert_all(data, pk=['id','date', 'yyy'], 
                                 replace=True, 
                                 partition='toYYYYMMDD(date)')

        with pytest.raises(Exception, match='PartitionCanNotChange'):
            db['lrb'].insert_all(data, pk=['id','date'], 
                                 replace=True, alter=True,
                                 partition='toYYYYMM(date)')
        print(time.time() - t)
        data = db['lrb'].select('id, date', where=None)
        print(data[:2], '..', data[-2:], len(data))
        return


def test_insert():
    cleanup()
    db = Database('sqlite://database/finance.sqlite3')

    d = arrow.now().shift(days=-800)
    data = []
    for i in range(0, 100):
        d = arrow.now().shift(days=-10)
        date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date, 'iii10': i*i*i})

    db['lrb'].insert_all(data, pk=['id','date'], 
                         replace=True, alter=False,
                         partition='toYYYYMMDD(date)')

    with pytest.raises(sqlite3.IntegrityError):
        db['lrb'].insert_all(data, pk=['id','date'], 
                             replace=False, alter=False,
                             partition='toYYYYMMDD(date)')

    with pytest.raises(sqlite3.IntegrityError):
        db['lrb'].insert_all(data, pk=['id','date'], 
                             alter=False,
                             partition='toYYYYMMDD(date)')

    with pytest.raises(sqlite3.IntegrityError):
        db['lrb'].insert_all(data, pk=['id','date'], 
                             alter=False, ignore=False,
                             partition='toYYYYMMDD(date)')

    data = []
    for i in range(0, 100):
        d = arrow.now().shift(days=-10)
        date = d.shift(hours=i).format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date, 'yyy': i*i*i})


    with pytest.raises(NoSuchColumn):
        db['lrb'].insert_all(data, pk=['id','date'], 
                             replace=True, alter=False,
                             partition='toYYYYMMDD(date)')

    # print(f"{db['lrb'].get_table_meta()=}")
    # print(f"{db['lrb'].select('*', where=None)=}")
    values = db['lrb'].select(
        'id,date', 
        where={'field': 'id', 'op': '<', 'value': 3}
    )
    print(values, len(values))
    assert len(values) == 3

    values = db['lrb'].select(
        '*', 
        where={'field': 'id', 'op': '<', 'value': 3}
    )
    print(values, len(values))
    assert len(values) == 3

    values = db['lrb'].select( '*', where=None)
    print(values, len(values))
    assert len(values) == 100

    values = db['lrb'].select( '*', where=None)
    print(values, len(values))
    assert len(values) == 100


def test_select():
    cleanup()
    db = Database('sqlite://database/finance.sqlite3')
    # 2 days
    d = arrow.now().span('day')[0]
    data = []
    min_id, max_id = 0, 60*12*2*2
    for i in range(min_id, max_id):
        date = d.shift(minutes=i).format('YYYY-MM-DD HH:MM')
        data.append({'id': i, 'date': date})
    db['demo'].insert_all(data, pk=['id','date'], replace=True, partition='toYYYYMMDD(date)')
    cnt = 50
    while cnt > 0:
        # SQL: select id,date from data  where id  between 2486 and 2880 ;
        i1 = random.randint(min_id, max_id-1)
        i2 = random.randint(min_id, max_id-1)
        # i1, i2 = i2, i1 if i2 < i1
        data = db['demo'].select(['id', 'date'], 
                                       where={'field': 'id', 'op': 'between_and',
                                              'value': [i1, i2]})
        print('i1', i1, 'i2', i2, 'data', len(data), 'i2-i1+1', i2-i1+1)
        if i1 <= i2:
            assert len(data) == i2 - i1 + 1
        else:
            assert len(data) == 0
        # print(data[:3])
        print(f'*** PASS: {cnt}')
        cnt -= 1

    # db.close()


def test_select2():
    # 数据库被删除，需要重新连接，因为保持的变量显示在连接中
    cleanup()
    db = Database('sqlite://database/finance.sqlite3')

    data = []
    d = arrow.get('2020-01-01')
    min_id, max_id = 0, 60*12*2*3
    for i in range(min_id, max_id):
        date = d.shift(minutes=i).format('YYYY-MM-DD HH:mm')
        dic = {'id': i, 'date': date, 'iii': i*i*i}
        # print(i, dic)
        data.append(dic)

    db['demo'].insert_all(data, pk=['id','date'], 
                         replace=True, 
                         partition='toYYYYMMDD(date)')
    print(db['demo'].table_parts())
    assert len(db['demo'].table_parts()) == 3

    wheres = {
        'field':'date', 'op': 'between_and',
        'value': ['2020-01-01 10', '2020-01-01 11']
    }
    data = db['demo'].select(['id', 'date'], where=wheres, to_dict=True)
    print(data)
    import sys
    print(sys.getsizeof(data[0]))
    assert len(data) == 60

    wheres = {
        'field':'date', 'op': 'between_and',
        'value': ['2020-01-01 23:00', '2020-01-02 01:00']
    }
    data = db['demo'].select(['id', 'date'], where=wheres)
    print(data, len(data))
    assert len(data) == 60*2 + 1

    wheres = {
        'field':'date', 'op': 'between_and',
        'value': ['2020-01-01 23', '2020-01-02 01']
    }
    # 位数不一致时，'2020-01-01 23' < '2020-01-01 23:00'
    # 就是说'2020-01-01 23' 基本等价于'2020-01-01 22:59'
    data = db['demo'].select(['id', 'date'], where=wheres)
    print(data, len(data))
    assert len(data) == 60*2

    #data = db['demo'].select(['id', 'date'], where=None)
    #print('end demo', time.time() - t)
    #print(data[0], data[-1])


if __name__ == '__main__':
    if 1:
        test_connection()
    if 0:
        test_create_table()
    if 0:
        test_insert()
    if 0:
        test_select()
    if 0:
        test_select2()
