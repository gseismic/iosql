import shutil
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

logging.basicConfig(
    level=logging.DEBUG, 
    format='%(asctime)s: %(threadName)s: %(levelname)s: %(message)s')

db = None
def cleanup():
    logging.info('Test: Clear database ...')
    shutil.rmtree('./database', ignore_errors=True)


def test_connection():
    cleanup()

    # First insert: 5 rows with id, name, and value
    data = [
        {'id': 1, 'name': 'Alice', 'value': 100},
        {'id': 2, 'name': 'Bob', 'value': 200},
        {'id': 3, 'name': 'Carol', 'value': 300},
    ]
    with Database('sqlite://database/finance.iosql', verbose=0) as db:
        t = db['lrb']
        t.insert_all(data, pk=['id'])
        print(t.table_parts())

    # Second: update existing rows (id 1-3) with new value
    # Each row has id and value - will be merged with existing name
    update_data = [
        {'id': 1, 'value': 150},
        {'id': 2, 'value': 250},
    ]
    with Database('sqlite://database/finance.iosql', verbose=0) as db:
        t = db['lrb']
        t.upsert_all(update_data, pk=['id'])

    # Third: add NEW row (id=4) with same column structure
    new_data = [{'id': 4, 'name': 'Dave', 'value': 400}]
    with Database('sqlite://database/finance.iosql', verbose=0) as db:
        t = db['lrb']
        t.upsert_all(new_data, pk=['id'])

    # Verify: should have 4 rows (id 1-4)
    with Database('sqlite://database/finance.iosql', verbose=0) as db:
        t = db['lrb']
        result = t.select_all('*', where=None)
        assert len(result) == 4, f"Expected 4 rows, got {len(result)}"
        # Verify updated values
        result_dict = {r['id']: r for r in result}
        assert result_dict[1]['value'] == 150  # Updated
        assert result_dict[1]['name'] == 'Alice'  # Preserved
        assert result_dict[2]['value'] == 250  # Updated
        assert result_dict[2]['name'] == 'Bob'  # Preserved
        assert result_dict[3]['value'] == 300  # Unchanged
        assert result_dict[4]['name'] == 'Dave'  # New row

if __name__ == '__main__':
    if 1:
        test_connection()
