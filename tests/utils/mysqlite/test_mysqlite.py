import os
import pytest
from iosql.utils.mysqlite import Database
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