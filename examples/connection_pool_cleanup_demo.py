#!/usr/bin/env python3
"""
连接池清理功能演示

这个示例展示了如何使用新增的连接池清理功能来防止文件句柄泄露
"""

import os
import sys
import time
import signal
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from iosql.utils.mysqlite.database import Database


def demo_basic_usage():
    """基本使用演示"""
    print("=== 基本使用演示 ===")
    
    # 创建数据库实例
    db = Database("demo_cleanup.db", verbose=2)
    
    # 查看连接池状态
    status = db.get_connection_pool_status()
    print(f"连接池状态: {status}")
    
    # 执行一些操作
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('test1')")
        conn.commit()
    
    # 再次查看状态
    status = db.get_connection_pool_status()
    print(f"操作后连接池状态: {status}")
    
    # 检查连接池健康状态
    is_healthy = db.is_connection_pool_healthy()
    print(f"连接池健康状态: {is_healthy}")
    
    return db


def demo_force_cleanup():
    """强制清理演示"""
    print("\n=== 强制清理演示 ===")
    
    db = Database("demo_force_cleanup.db", verbose=2)
    
    # 执行一些操作
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('test2')")
        conn.commit()
    
    status_before = db.get_connection_pool_status()
    print(f"清理前状态: {status_before}")
    
    # 强制清理所有连接
    db.force_close_all_connections()
    
    status_after = db.get_connection_pool_status()
    print(f"清理后状态: {status_after}")


def demo_exception_handling():
    """异常处理演示"""
    print("\n=== 异常处理演示 ===")
    
    try:
        with Database("demo_exception.db", verbose=2) as db:
            # 模拟异常情况
            with db.get_connection() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, name TEXT)")
                # 故意引发异常
                raise ValueError("模拟异常")
    except ValueError as e:
        print(f"捕获到异常: {e}")
        print("异常情况下，连接池会自动清理")


def demo_signal_handling():
    """信号处理演示"""
    print("\n=== 信号处理演示 ===")
    
    db = Database("demo_signal.db", verbose=2)
    
    # 执行一些操作
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO test (name) VALUES ('test3')")
        conn.commit()
    
    print("数据库实例已创建，程序退出时会自动清理连接")
    print("按 Ctrl+C 可以测试信号处理")
    
    # 模拟程序运行
    try:
        time.sleep(2)
    except KeyboardInterrupt:
        print("收到中断信号，程序将退出")


def demo_multiple_databases():
    """多数据库实例演示"""
    print("\n=== 多数据库实例演示 ===")
    
    databases = []
    for i in range(3):
        db = Database(f"demo_multi_{i}.db", verbose=1)
        databases.append(db)
        
        with db.get_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute(f"INSERT INTO test (name) VALUES ('test_{i}')")
            conn.commit()
    
    print(f"创建了 {len(databases)} 个数据库实例")
    
    # 查看所有数据库的状态
    for i, db in enumerate(databases):
        status = db.get_connection_pool_status()
        print(f"数据库 {i}: {status}")
    
    # 程序退出时会自动清理所有数据库连接


def cleanup_demo_files():
    """清理演示文件"""
    print("\n=== 清理演示文件 ===")
    
    demo_files = [
        "demo_cleanup.db",
        "demo_force_cleanup.db", 
        "demo_exception.db",
        "demo_signal.db",
        "demo_multi_0.db",
        "demo_multi_1.db",
        "demo_multi_2.db"
    ]
    
    for file in demo_files:
        if os.path.exists(file):
            os.remove(file)
            print(f"已删除: {file}")


if __name__ == "__main__":
    try:
        # 运行各种演示
        demo_basic_usage()
        demo_force_cleanup()
        demo_exception_handling()
        demo_multiple_databases()
        
        print("\n=== 演示完成 ===")
        print("所有演示已完成，程序退出时会自动清理所有数据库连接")
        
    except KeyboardInterrupt:
        print("\n程序被中断")
    finally:
        # 清理演示文件
        cleanup_demo_files()
