import os
import sqlite3
import threading
import pytest
from iosql.utils.mysqlite import Database
from iosql.utils.mysqlite.database import DatabaseConnectionError, DatabaseError
from iosql.utils.mysqlite.exception import NoSuchTable, NoSuchColumn, DuplicateColumn


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(TEST_DIR, 'test_mysqlite.db')


@pytest.fixture(autouse=True)
def cleanup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


class TestBasic:
    def test_insert_with_ensure_table(self):
        with Database(TEST_DB, auto_commit=True, verbose=0) as db:
            db['data'].insert({'id': 3, 'test': 5}, pk=['id'], ensure_table=True)

    def test_insert_with_ensure_table_and_column(self):
        with Database(TEST_DB, auto_commit=True, verbose=0) as db:
            db['data'].insert({'id': 5, 'test': 5, 'name': 'lsl'}, pk=['id'],
                              ensure_table=True, ensure_column=True)

    def test_database_can_be_created_in_worker_thread(self):
        errors = []

        def worker():
            try:
                Database(TEST_DB, auto_commit=True, verbose=0).close()
            except Exception as e:
                errors.append(e)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert errors == []

    def test_database_close_blocks_later_use(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['data'].insert({'id': 1}, ensure_table=True)
        db.close()

        assert db.is_closed() is True
        with pytest.raises(DatabaseConnectionError):
            db['data'].insert({'id': 2})

    def test_database_exposes_pool_status(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0, max_connections=2)
        db['data'].insert({'id': 1}, ensure_table=True)

        status = db.get_connection_pool_status()

        assert status['max_connections'] == 2
        assert status['created_connections'] <= 2
        assert db.is_connection_pool_healthy() is True

    def test_transaction_commit_and_rollback(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        with db.transaction():
            db['items'].insert({'id': 1, 'name': 'commit'}, pk='id', ensure_table=True)

        assert db['items'].count() == 1

        with pytest.raises(RuntimeError):
            with db.transaction():
                db['items'].insert({'id': 2, 'name': 'rollback'})
                raise RuntimeError('rollback')

        assert db['items'].select_all('*', order_by='id') == [
            {'id': 1, 'name': 'commit'}
        ]

    def test_commit_requires_transaction_context(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        with pytest.raises(DatabaseError):
            db.commit()

    def test_transaction_creates_parent_directory(self, tmp_path):
        db_file = tmp_path / 'nested' / 'tx.sqlite3'
        db = Database(str(db_file), auto_commit=True, verbose=0)

        with db.transaction():
            db['items'].insert({'id': 1}, pk='id', ensure_table=True)

        assert db_file.exists()
        assert db['items'].count() == 1


class TestDefault:
    def test_default_values(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['demo2'].create_table([
            {'name': 'id', 'type': 'text'},
            {'name': 'name', 'type': 'text', 'default': '2'},
            {'name': 'value', 'type': 'float', 'default': '666'}
        ], pk=['id'])

        defaults = db['demo2'].default_values()
        assert defaults['id'] is None
        assert defaults['name'] == '2'
        assert defaults['value'] == '666'

    def test_default_value_override(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['demo2'].create_table([
            {'name': 'id', 'type': 'text'},
            {'name': 'name', 'type': 'text', 'default': '2'},
            {'name': 'value', 'type': 'float', 'default': '666'}
        ], pk=['id'])

        db['demo2'].insert({'id': 3, 'value': '333'}, replace=False)
        db['demo2'].insert({'id': 5, 'name': 7}, replace=False)

        data = db['demo2'].select_all('*')
        d_data = {item['id']: dict(item) for item in data}

        assert d_data['3']['value'] == 333
        assert d_data['3']['name'] == '2'
        assert d_data['5']['value'] == 666


class TestTable:
    def test_insert_without_table_raises(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        with pytest.raises(NoSuchTable):
            db['demo2'].insert({'value': '333'}, replace=True)

    def test_create_table_and_insert(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['demo2'].create_table([
            {'name': 'id', 'type': 'text'},
            {'name': 'name', 'type': 'text'},
            {'name': 'sss', 'type': 'text', 'default': '2'},
            {'name': 'value', 'type': 'float', 'default': 666}
        ], pk=['id'])

        with pytest.raises(NoSuchColumn):
            db['demo2'].insert({'a': 1}, replace=True)

    def test_duplicate_column_raises(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['demo5'].create_table([
            {'name': 'id', 'type': 'text'},
            {'name': 'name', 'type': 'float'}
        ], pk='id', exist_no_warn=True)

        db['demo5'].insert({'id': 1, 'name': 'lsl'}, ignore=True)
        db['demo5'].add_column('name3', 'text')

        with pytest.raises(DuplicateColumn):
            db['demo5'].add_column('name3', 'text')

    def test_ensure_table_and_column(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['not_exist'].insert({'id': 1, 'name': 'lsl'}, ensure_table=True)
        db['not_exist'].insert({'id': 1, 'name3': 'lsl'}, ensure_column=True)
        db['not_exist'].insert({'id': 1, 'name5': 'lsl', 'name6': 'lsl'}, ensure_column=True)

        db['not_exist2'].insert(
            {'id': 1, 'name5': 'lsl', 'name6': 'lsl'},
            ensure_column=True, ensure_table=True
        )

    def test_table_exists(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['demo'].create_table([{'name': 'id', 'type': 'text'}], pk=['id'])
        assert db['demo'].table_exists() is True
        assert db['not_exist'].table_exists() is False

    def test_reserved_identifiers_and_quoted_values(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['select'].insert(
            {'from': 1, 'value': 'a"b'},
            pk='from',
            ensure_table=True,
        )

        assert db['select'].select_all(['from', 'value']) == [
            {'from': 1, 'value': 'a"b'}
        ]


class TestInsert:
    def test_insert_all(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        data1 = [{'id': i, 'name': str(i*i), 'value': float(i)} for i in range(30)]
        data2 = [{'name': str(i*i*i), 'id': i+100} for i in range(30)]
        data = data1 + data2

        db['data'].create_table([
            {'name': 'id', 'type': 'integer', 'default': '2'},
            {'name': 'name', 'type': 'text', 'default': '2'},
            {'name': 'value', 'type': 'float', 'default': 666.0}
        ], pk=['id', 'name'])

        db['data'].insert_all(data)
        out_data = db['data'].select('*', to_dict=True)
        assert len(out_data) == len(data)

    def test_insert_all_without_table_raises(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        data = [{'id': i, 'name': str(i*i)} for i in range(10)]
        with pytest.raises(NoSuchTable):
            db['data2'].insert_all(data)

    def test_insert_all_with_ensure_table(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        data = [{'id': i, 'name': str(i*i)} for i in range(10)]
        db['data3'].insert_all(data, ensure_table=True)
        assert db['data3'].table_exists()

    def test_insert_all_does_not_mutate_input_rows(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        data = [{'id': 1}, {'id': 2, 'name': 'Bob'}]
        original = [row.copy() for row in data]

        db['data4'].insert_all(data, ensure_table=True)

        assert data == original

    def test_insert_all_type_promotion_uses_float_for_mixed_numbers(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['data5'].insert_all([
            {'id': 1, 'value': 1},
            {'id': 2, 'value': 1.5},
        ], ensure_table=True)

        assert db['data5'].column_info()['column_type']['value'] == 'float'

    def test_insert_all_batches_are_one_atomic_transaction(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['atomic'].create_table([
            {'name': 'id', 'type': 'integer'},
            {'name': 'value', 'type': 'text'},
        ], pk=['id'])
        db['atomic'].insert({'id': 1, 'value': 'before'})

        with pytest.raises(sqlite3.IntegrityError):
            db['atomic'].insert_all(
                [
                    {'id': 2, 'value': 'must rollback'},
                    {'id': 1, 'value': 'duplicate'},
                ],
                batch_size=1,
            )

        assert db['atomic'].select_all('*', order_by='id') == [
            {'id': 1, 'value': 'before'},
        ]


class TestDelete:
    def test_delete_where(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['data'].insert_all([
            {'id': i, 'name': str(i*i), 'x': 3, 'y': 9}
            for i in range(30)], ensure_table=True, ensure_column=True
        )

        db['data'].delete('id > 3')
        remaining = db['data'].select('*', to_dict=True)
        assert all(item['id'] <= 3 for item in remaining)

    def test_delete_all(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['data'].insert_all([
            {'id': i, 'name': str(i*i)}
            for i in range(10)], ensure_table=True
        )

        db['data'].delete_all()
        remaining = db['data'].select('*', to_dict=True)
        assert len(remaining) == 0


class TestIndex:
    def test_create_and_drop_index(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['data'].insert_all([
            {'id': i, 'name': str(i*i), 'x': 3, 'y': 9}
            for i in range(30)], ensure_table=True, ensure_column=True
        )

        db['data'].create_index('name')
        assert len(db['data'].get_all_index()) == 1

        db['data'].drop_column_index('name')
        assert len(db['data'].get_all_index()) == 0

    def test_drop_table(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['data'].insert_all([
            {'id': i, 'name': str(i*i)}
            for i in range(10)], ensure_table=True
        )

        db['data'].drop_table()
        assert db['data'].table_exists() is False

        with pytest.raises(NoSuchTable):
            db['data'].select('*', to_dict=True)


class TestSchemaless:
    def test_insert_without_creating_table(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['users'].insert({'id': 1, 'name': 'Alice', 'age': 30}, ensure_table=True)
        assert db['users'].table_exists()

        result = db['users'].select_all('*')
        assert len(result) == 1
        assert result[0]['name'] == 'Alice'

    def test_insert_all_without_creating_table(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        data = [{'id': i, 'name': f'user_{i}'} for i in range(5)]
        db['users'].insert_all(data, ensure_table=True)

        assert db['users'].table_exists()
        assert db['users'].count() == 5

    def test_insert_without_creating_table_raises(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        with pytest.raises(NoSuchTable):
            db['users'].insert({'id': 1, 'name': 'Alice'})

    def test_dynamic_columns_with_ensure_column(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['events'].insert({'id': 1, 'type': 'click'}, ensure_table=True)
        db['events'].insert({'id': 2, 'type': 'scroll', 'duration': 1.5}, ensure_column=True)

        columns = list(db['events'].column_info()['column_type'].keys())
        assert 'duration' in columns

        result = db['events'].select_all('*')
        assert len(result) == 2
        assert result[1]['duration'] == 1.5

    def test_insert_with_replace(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['items'].insert({'id': 1, 'name': 'first'}, pk='id', ensure_table=True)
        db['items'].insert({'id': 1, 'name': 'updated'}, replace=True)

        result = db['items'].select_all('*')
        assert len(result) == 1
        assert result[0]['name'] == 'updated'

    def test_insert_with_ignore(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['items'].insert({'id': 1, 'name': 'first'}, pk='id', ensure_table=True)
        db['items'].insert({'id': 1, 'name': 'ignored'}, ignore=True)

        result = db['items'].select_all('*')
        assert len(result) == 1
        assert result[0]['name'] == 'first'

    def test_composite_primary_key(self):
        db = Database(TEST_DB, auto_commit=True, verbose=0)
        db['order_items'].create_table([
            {'name': 'order_id', 'type': 'INTEGER'},
            {'name': 'product_id', 'type': 'INTEGER'},
            {'name': 'quantity', 'type': 'INTEGER'},
        ], pk=['order_id', 'product_id'])

        assert db['order_items'].pk() == ['order_id', 'product_id']

        db['order_items'].insert({'order_id': 1, 'product_id': 100, 'quantity': 2})
        db['order_items'].insert({'order_id': 1, 'product_id': 101, 'quantity': 1})
        db['order_items'].insert({'order_id': 2, 'product_id': 100, 'quantity': 5})
        assert db['order_items'].count() == 3

        db['order_items'].insert({'order_id': 1, 'product_id': 100, 'quantity': 99}, replace=True)
        assert db['order_items'].count() == 3
        result = db['order_items'].select_one('*', where="order_id=1 AND product_id=100")
        assert result['quantity'] == 99
