import config
import logging
from usql import Database


logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(message)s')


def test_basic():
    db = Database('sqlite://demo.sqlite3')
    print('db', db)
    print('db[demo]', db['demo'])


def test_select():
    db = Database('sqlite://demo.sqlite3')
    # 过于复杂不采用
    return
    db['test'].insert_all([
        {'id': i, 'name': f'name_{i}'}
        for i in range(1000)
    ], pk='id', partition={
        'columns': ['id'], 
        'forward_fn': lambda x: x/100,
        'reverse_min_fn': {
            'id': lambda part: part*100,
        },
        'reverse_max_fn': {
            'id': lambda part: (part+1)*100
        }
    })


def test_multi_part():
    from itertools import product
    db = Database('sqlite://demo.sqlite3')
    # 过于复杂不采用
    return
    db['ij'].insert_all([
        {'i': i, 'j': j, 'name': f'name_{i}_{j}'}
        for i,j in product(range(20), range(10))
    ], pk=['i','j'], partition={
        'columns': ['i', 'j'], 
        'forward_fn': lambda i,j: int(i/5)*int(j/5),
        'reverse_min_fn': {
            'id': lambda part: part*100,
        },
        'reverse_max_fn': {
            'id': lambda part: (part+1)*100
        }
    })


if __name__ == '__main__':
    if 1:
        test_basic()
