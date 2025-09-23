# 连接池清理功能说明

## 概述

为了防止文件句柄泄露（`OSError: [Errno 24] Too many open files`），我们为连接池添加了强制关闭连接的功能。这些功能确保在异常情况下也能正确释放数据库连接资源。

## 新增功能

### 1. 强制关闭连接方法

#### `DatabaseConnectionPool.force_close_all_connections()`

强制关闭连接池中的所有连接，用于紧急情况下的资源清理。

**使用场景：**
- 应用异常退出时的清理
- 防止文件句柄泄露
- 内存压力下的强制清理

**注意：** 调用此方法后，连接池将不可用，需要重新创建Database实例。

#### `Database.force_close_all_connections()`

Database类的包装方法，提供相同的强制清理功能。

### 2. 连接池状态监控

#### `DatabaseConnectionPool.get_pool_status()`

获取连接池的详细状态信息。

**返回信息：**
- `max_connections`: 最大连接数
- `created_connections`: 已创建的连接数
- `available_connections`: 当前可用的连接数
- `db_file`: 数据库文件路径

#### `DatabaseConnectionPool.is_pool_healthy()`

检查连接池是否健康，包括：
- 检查是否有连接泄露
- 检查连接使用率是否过高（超过80%会发出警告）

#### `Database.get_connection_pool_status()` 和 `Database.is_connection_pool_healthy()`

Database类的包装方法，提供相同的监控功能。

### 3. 自动清理机制

#### 全局数据库实例注册表

所有Database实例都会自动注册到全局注册表中，程序退出时会自动清理所有连接。

#### 信号处理

注册了SIGINT和SIGTERM信号处理器，确保在程序被中断时也能正确清理连接。

#### 异常处理

在上下文管理器的`__exit__`方法中，如果发生异常会自动强制清理连接池。

## 使用示例

### 基本使用

```python
from iosql.utils.mysqlite.database import Database

# 创建数据库实例
db = Database("example.db")

# 查看连接池状态
status = db.get_connection_pool_status()
print(f"连接池状态: {status}")

# 检查健康状态
is_healthy = db.is_connection_pool_healthy()
print(f"连接池健康: {is_healthy}")

# 正常使用
with db.get_connection() as conn:
    conn.execute("SELECT 1")
```

### 强制清理

```python
# 在需要时强制清理所有连接
db.force_close_all_connections()
```

### 异常处理

```python
try:
    with Database("example.db") as db:
        # 数据库操作
        with db.get_connection() as conn:
            conn.execute("SELECT 1")
        # 如果这里发生异常，连接池会自动清理
        raise ValueError("模拟异常")
except ValueError:
    print("异常已处理，连接已自动清理")
```

### 程序退出时的自动清理

```python
# 创建多个数据库实例
db1 = Database("db1.db")
db2 = Database("db2.db")
db3 = Database("db3.db")

# 程序退出时会自动清理所有连接，无需手动处理
```

## 最佳实践

### 1. 使用上下文管理器

```python
# 推荐：使用with语句
with Database("example.db") as db:
    # 数据库操作
    pass
# 自动清理
```

### 2. 监控连接池状态

```python
db = Database("example.db")

# 定期检查连接池状态
status = db.get_connection_pool_status()
if not db.is_connection_pool_healthy():
    print("连接池可能存在问题")
```

### 3. 异常情况下的处理

```python
try:
    with Database("example.db") as db:
        # 数据库操作
        pass
except Exception as e:
    # 异常情况下连接会自动清理
    print(f"操作失败: {e}")
```

### 4. 长时间运行的应用

```python
# 对于长时间运行的应用，可以定期检查连接池健康状态
import time

db = Database("long_running.db")

while True:
    # 业务逻辑
    with db.get_connection() as conn:
        # 数据库操作
        pass
    
    # 定期检查连接池健康状态
    if not db.is_connection_pool_healthy():
        print("连接池不健康，考虑重启应用")
        break
    
    time.sleep(60)  # 每分钟检查一次
```

## 注意事项

1. **强制清理的影响**: 调用`force_close_all_connections()`后，连接池将不可用，需要重新创建Database实例。

2. **信号处理**: 程序会注册SIGINT和SIGTERM信号处理器，确保在程序被中断时也能正确清理。

3. **线程安全**: 所有清理操作都是线程安全的，可以在多线程环境中使用。

4. **性能影响**: 连接池状态检查的开销很小，可以安全地定期调用。

5. **日志记录**: 所有清理操作都会记录日志，便于调试和监控。

## 故障排除

### 问题：仍然出现"Too many open files"错误

**可能原因：**
1. 没有使用上下文管理器
2. 在异常情况下没有正确清理
3. 创建了太多Database实例

**解决方案：**
1. 确保使用`with`语句
2. 检查异常处理逻辑
3. 考虑减少Database实例数量或增加系统文件句柄限制

### 问题：连接池状态显示异常

**检查方法：**
```python
status = db.get_connection_pool_status()
print(f"状态: {status}")
print(f"健康: {db.is_connection_pool_healthy()}")
```

**常见问题：**
- `created_connections` > `max_connections`: 连接泄露
- `available_connections` = 0: 所有连接都在使用中
- 使用率过高: 可能需要增加`max_connections`

## 版本兼容性

这些功能是向后兼容的，不会影响现有代码的正常运行。现有代码可以继续使用，同时可以选择性地使用新的清理和监控功能。
