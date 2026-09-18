import os
import gc
import threading
import time
import pytest
import sqlite3
from iosql.utils.mysqlite.connection_pool import (
    DatabaseConnectionPool,
    get_process_connection_budget_status,
)


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

    def test_close_during_connection_validation_does_not_deadlock(self, tmp_path):
        pool = DatabaseConnectionPool(
            str(tmp_path / 'close-race.sqlite3'),
            max_connections=1,
        )
        with pool.get_connection():
            pass

        entered = threading.Event()
        release = threading.Event()
        errors = []
        original_is_alive = pool._is_connection_alive

        def delayed_is_alive(conn):
            entered.set()
            release.wait(timeout=1)
            return original_is_alive(conn)

        pool._is_connection_alive = delayed_is_alive
        thread = threading.Thread(
            target=lambda: self._try_pool_checkout(pool, errors)
        )
        thread.start()
        assert entered.wait(timeout=1)

        pool.force_close_all_connections()
        release.set()
        thread.join(timeout=1)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)

    def test_process_budget_reclaims_idle_connections_across_pools(self, tmp_path):
        pools = [
            DatabaseConnectionPool(
                str(tmp_path / f'pool-{index}.sqlite3'),
                max_connections=1,
                max_process_connections=3,
            )
            for index in range(10)
        ]
        try:
            for item in pools:
                with item.get_connection() as conn:
                    conn.execute('select 1')

            status = get_process_connection_budget_status()
            created = sum(
                item.get_pool_status()['created_connections']
                for item in pools
            )

            assert status['max_process_connections'] == 3
            assert status['open_process_connections'] <= 3
            assert created <= 3
        finally:
            for item in pools:
                item.force_close_all_connections()

    def test_abandoned_pool_returns_process_budget(self, tmp_path):
        gc.collect()
        baseline = get_process_connection_budget_status()[
            'open_process_connections'
        ]
        pool = DatabaseConnectionPool(
            str(tmp_path / 'abandoned.sqlite3'),
            max_connections=1,
            max_process_connections=3,
        )
        with pool.get_connection() as conn:
            conn.execute('select 1')

        del pool
        gc.collect()

        assert get_process_connection_budget_status()[
            'open_process_connections'
        ] == baseline

    def test_idle_small_pool_does_not_compress_process_budget(self, tmp_path):
        large_pool = DatabaseConnectionPool(
            str(tmp_path / 'large.sqlite3'),
            max_connections=4,
            max_process_connections=8,
        )
        try:
            baseline = get_process_connection_budget_status()['max_process_connections']
            assert baseline >= 8

            small_pool = DatabaseConnectionPool(
                str(tmp_path / 'small.sqlite3'),
                max_connections=1,
                max_process_connections=2,
            )
            try:
                # 保守的小池不能压缩整个进程的预算（旧 min() 语义会压缩）
                status = get_process_connection_budget_status()
                assert status['max_process_connections'] >= 8
                assert status['max_process_connections'] == baseline
            finally:
                small_pool.force_close_all_connections()
        finally:
            large_pool.force_close_all_connections()

    @staticmethod
    def _try_pool_checkout(pool, errors):
        try:
            with pool.get_connection():
                pass
        except Exception as e:
            errors.append(e)
