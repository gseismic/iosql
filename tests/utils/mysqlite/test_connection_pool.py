import os
import threading
import time
import pytest
import sqlite3
from iosql.utils.mysqlite.connection_pool import DatabaseConnectionPool


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_DB = os.path.join(TEST_DIR, 'test_pool.sqlite3')


@pytest.fixture(autouse=True)
def cleanup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test_table (value) VALUES ('test_value')")
    conn.commit()
    conn.close()
    yield
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.fixture
def pool():
    return DatabaseConnectionPool(TEST_DB, max_connections=3)


class TestConnectionPool:
    def test_basic_connection(self, pool):
        with pool.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM test_table WHERE id = 1")
            result = cursor.fetchone()
            assert result[0] == "test_value"

    def test_thread_connection_binding(self, pool):
        connections = []

        def worker():
            with pool.get_connection() as conn:
                connections.append(id(conn))
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(connections)) == 3

    def test_concurrent_access(self, pool):
        results = []
        errors = []

        def worker():
            try:
                with pool.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT value FROM test_table WHERE id = 1")
                    results.append(cursor.fetchone()[0])
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 20
        assert all(r == "test_value" for r in results)
        assert len(errors) == 0

    def test_force_close_all_connections(self, pool):
        with pool.get_connection() as conn:
            conn.execute("SELECT 1")

        pool.force_close_all_connections()

        with pytest.raises(RuntimeError):
            with pool.get_connection():
                pass

    def test_max_connections_bounds_total_open_connections(self):
        pool = DatabaseConnectionPool(TEST_DB, max_connections=2)
        barrier = threading.Barrier(8)
        connection_ids = []
        errors = []

        def worker():
            try:
                barrier.wait()
                with pool.get_connection() as conn:
                    connection_ids.append(id(conn))
                    cursor = conn.execute("SELECT value FROM test_table WHERE id = 1")
                    assert cursor.fetchone()[0] == "test_value"
                    time.sleep(0.02)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(connection_ids) == 8
        assert len(set(connection_ids)) <= 2
        assert pool.get_pool_status()["created_connections"] <= 2

    def test_waiting_checkout_exits_when_pool_is_closed(self):
        pool = DatabaseConnectionPool(TEST_DB, max_connections=1)
        errors = []

        with pool.get_connection():
            thread = threading.Thread(
                target=lambda: self._try_pool_checkout(pool, errors)
            )
            thread.start()
            time.sleep(0.05)
            pool.force_close_all_connections()
            thread.join(timeout=1)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)

    @staticmethod
    def _try_pool_checkout(pool, errors):
        try:
            with pool.get_connection():
                pass
        except Exception as e:
            errors.append(e)
