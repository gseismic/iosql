import logging
import sys
from iosql.utils.mysqlite import Database

logging.basicConfig(level=logging.INFO, format='%(asctime)s: %(message)s')


def run():
    db = Database('mysqlite.sqlite3', auto_commit=True, verbose=2)

    db['demo'].insert({'id': 0, 'value':'333'}, replace=False, ensure_table=True)
    db['demo'].insert({'id': 1, 'name': '1', 'value':'a'}, replace=False, ensure_column=True)

    db['demo'].insert_all([
        {'id': i+100, 'name': str(i), 'value':None} for i in range(3)]
    )

    data = db['demo'].select_all('*')
    print(f'{data=}')


if __name__ == '__main__':
    run()

