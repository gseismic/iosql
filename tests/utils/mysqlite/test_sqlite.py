import os
import sqlite3
import shutil
import logging
#import aexit


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')
dbfile = 'demo.sqlite3'
def cleanup():
    logging.info('Clear database ...')
    if os.path.exists(dbfile):
        os.remove(dbfile)

cleanup()

db = sqlite3.connect(dbfile)
db.row_factory = sqlite3.Row

cu = db.cursor()
if 0:
    cu.execute('''
    create table if not exists test2 
               (id integer primary key, name text)
    ''')
if 1:
    # 结论: primary key 需要加NOT NULL 属性，否则id为None时，可以增加任意数据
    # id 为int类型时，会自增
    cu.execute('''
    create table if not exists test2 
               (id text NOT NULL, name text,
               xxx text default 10,
               	PRIMARY KEY (id)
              )
    ''')

if 0:
    cu.execute('insert into test2 (name) values ("lsl")')
    cu.execute('insert into test2 (name) values ("lsl")')
    cu.execute('insert into test2 (name) values ("lsl5")')
    cu.execute('insert into test2 (id, name) values (5, "lsl5")')
    # cu.execute('insert into test2 (id, name) values (5, "lsl5")')
    db.commit()
    cu.execute('select * from test2')
    data = [dict(item) for item in cu.fetchall()]
    print(data)

if 0:
    cu.execute('insert into test2 values (2, "lsl")')
    cu.execute('insert into test2 values (2, "lsl3")')
    cu.execute('insert into test2 values (3, "lsl3")')
    cu.execute('insert into test2 values (5, "lsl5")')
    cu.execute('insert into test2 values (6, "lsl6")')
    db.commit()
    cu.execute('select * from test2')
    data = [dict(item) for item in cu.fetchall()]
    print(data)
if 0:
    # 不会返回2个，只会返回最后一次的结果
    cu.execute('select * from test2 where id=2')
    cu.execute('select * from test2 where id=3')
    print(cu.fetchall())
if 0:
    # cu1 [(2, 'lsl')]
    # cu2 []
    cu1 = db.cursor()
    cu2 = db.cursor()
    r = cu1.execute('select * from test2 where id=2')
    print('rrr', r) # rrr <sqlite3.Cursor object at 0x103fa8ea0>
    cu2.execute('select * from test2 where id=3')
    print('cu1', cu1.fetchall())
    print('cu2', cu1.fetchall())

# cu.close()
cu.close()
