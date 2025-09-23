META_TABLE_NAME = '_meta'

# _DEFAULT_PRAGMA = {
#     "foreign_keys": "on",
#     "journal_mode": "wal",
#     "cache_size": "-10000" # 10M
# }

# https://poe.com/chat/usbncsnenrdbemi8r6
# 在 const.py 中优化默认PRAGMA设置
_DEFAULT_PRAGMA = {
    "foreign_keys": "on",
    "journal_mode": "wal",
    "synchronous": "normal",  # WAL模式下可以使用normal而不是full
    "cache_size": "-64000",   # 64MB缓存
    "temp_store": "memory",   # 临时表存储在内存中
    "mmap_size": "268435456", # 256MB内存映射
    "wal_autocheckpoint": "1000",  # WAL自动检查点
    "busy_timeout": "30000"   # 30秒忙等待超时
}