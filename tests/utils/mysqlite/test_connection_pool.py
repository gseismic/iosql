
import pytest
import threading
import time
import sqlite3
import queue
from iosql.utils.mysqlite.connection_pool import DatabaseConnectionPool

# 测试用的数据库文件路径
TEST_DB = "test_database.sqlite3"

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    """创建测试数据库和表"""
    conn = sqlite3.connect(TEST_DB)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY, value TEXT)")
    cursor.execute("INSERT INTO test_table (value) VALUES ('test_value')")
    conn.commit()
    conn.close()
    
    yield  # 测试运行
    
    # 测试结束后清理
    import os
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

@pytest.fixture
def connection_pool():
    """创建连接池实例"""
    return DatabaseConnectionPool(TEST_DB, max_connections=3, check_same_thread=False)

def test_single_connection(connection_pool):
    """测试基本连接功能"""
    with connection_pool.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM test_table WHERE id = 1")
        result = cursor.fetchone()
        assert result[0] == "test_value"
    
    # 验证连接已返回池中
    assert connection_pool._pool.qsize() == 1

def test_connection_reuse(connection_pool):
    """测试连接重用"""
    connection_ids = set()
    
    for _ in range(5):
        with connection_pool.get_connection() as conn:
            connection_ids.add(id(conn))
    
    # 验证连接数不超过最大限制
    assert len(connection_ids) <= connection_pool.max_connections
    # 验证连接池中有连接
    assert connection_pool._pool.qsize() > 0

def test_max_connections(connection_pool):
    """测试最大连接数限制"""
    connections = []
    
    # 获取最大连接数 - 使用 context manager 来保持连接
    for _ in range(connection_pool.max_connections):
        conn_context = connection_pool.get_connection()
        conn = conn_context.__enter__()
        connections.append((conn_context, conn))
    
    # 验证池已满
    assert connection_pool._pool.qsize() == 0
    assert connection_pool._created_connections == connection_pool.max_connections
    
    # 尝试获取额外连接（应阻塞）
    extra_conn = None
    extra_context = None
    def get_extra_connection():
        nonlocal extra_conn, extra_context
        extra_context = connection_pool.get_connection()
        extra_conn = extra_context.__enter__()
    
    start_time = time.time()
    thread = threading.Thread(target=get_extra_connection)
    thread.start()
    
    # 等待短暂时间验证阻塞
    time.sleep(0.1)
    assert time.time() - start_time > 0.05
    assert extra_conn is None  # 应该还在阻塞
    
    # 释放一个连接
    conn_context, conn = connections.pop()
    conn_context.__exit__(None, None, None)  # 正确释放连接
    
    # 等待线程完成
    thread.join(timeout=1.0)
    assert thread.is_alive() == False  # 线程应该已完成
    assert extra_conn is not None  # 应该获取到连接
    
    # 清理
    for conn_context, conn in connections:
        conn_context.__exit__(None, None, None)
    if extra_context and extra_conn:
        extra_context.__exit__(None, None, None)

def test_concurrent_access(connection_pool):
    """测试多线程并发访问"""
    results = []
    threads = []
    thread_count = 20
    
    def worker():
        with connection_pool.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM test_table WHERE id = 1")
            results.append(cursor.fetchone()[0])
            # 模拟工作负载
            time.sleep(0.01)
    
    # 创建并启动线程
    for _ in range(thread_count):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    
    # 等待所有线程完成
    for t in threads:
        t.join()
    
    # 验证所有查询成功
    assert len(results) == thread_count
    assert all(r == "test_value" for r in results)
    
    # 验证连接数不超过最大限制
    assert connection_pool._created_connections <= connection_pool.max_connections

def test_connection_validity(connection_pool):
    """测试连接有效性"""
    with connection_pool.get_connection() as conn:
        # 验证连接有效
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1
    
    # 从池中取出连接验证状态
    conn = connection_pool._pool.get()
    try:
        # SQLite3 连接没有 closed 属性，通过执行查询来验证连接有效性
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1
    finally:
        connection_pool._pool.put_nowait(conn)

def test_connection_leak_protection(connection_pool):
    """测试连接泄漏保护"""
    initial_count = connection_pool._created_connections
    
    # 模拟连接未正确返回的情况
    conn_context = connection_pool.get_connection()
    conn = conn_context.__enter__()
    
    # 尝试获取更多连接
    with connection_pool.get_connection() as conn2:
        pass
    
    # 验证连接数增加但不超过最大值
    assert connection_pool._created_connections > initial_count
    assert connection_pool._created_connections <= connection_pool.max_connections
    
    # 清理
    conn_context.__exit__(None, None, None)

def test_pool_full_handling(connection_pool):
    """测试连接池满时的处理"""
    connections = []
    
    # 获取所有可用连接 - 使用 context manager 来保持连接
    for _ in range(connection_pool.max_connections):
        conn_context = connection_pool.get_connection()
        conn = conn_context.__enter__()
        connections.append((conn_context, conn))
    
    # 验证池已满
    assert connection_pool._pool.qsize() == 0
    assert connection_pool._created_connections == connection_pool.max_connections
    
    # 测试池满时的行为：尝试获取额外连接应该阻塞
    extra_conn = None
    extra_context = None
    def get_extra_connection():
        nonlocal extra_conn, extra_context
        extra_context = connection_pool.get_connection()
        extra_conn = extra_context.__enter__()
    
    start_time = time.time()
    thread = threading.Thread(target=get_extra_connection)
    thread.start()
    
    # 等待短暂时间验证阻塞
    time.sleep(0.1)
    assert time.time() - start_time > 0.05
    assert extra_conn is None  # 应该还在阻塞
    
    # 释放一个连接
    conn_context, conn = connections.pop()
    conn_context.__exit__(None, None, None)  # 正确释放连接
    
    # 等待线程完成
    thread.join(timeout=1.0)
    assert thread.is_alive() == False  # 线程应该已完成
    assert extra_conn is not None  # 应该获取到连接
    
    # 清理
    for conn_context, conn in connections:
        conn_context.__exit__(None, None, None)
    if extra_context and extra_conn:
        extra_context.__exit__(None, None, None)
        
if __name__ == '__main__':
    pytest.main()