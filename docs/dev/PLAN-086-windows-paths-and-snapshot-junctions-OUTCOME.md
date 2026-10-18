# PLAN-086 结果：Windows 路径大小写与快照重解析点防护

- 日期：2026-10-18（Asia/Shanghai）。
- 计划：[PLAN-086](PLAN-086-windows-paths-and-snapshot-junctions.md)。
- 状态：修复、自审和 Linux 回归完成；Windows 原机验证未执行；未提交或推送。

## 修复结果

1. `coordination.resource()` 保留 `realpath()` 的实际路径，仅在生成库锁身份时调用 `normcase()`；`physical_path()` 采用同样的分离方式。大小写敏感目录中的 `MyDB` 及其控制目录、已发布代际不会因身份规范化被改写为 `mydb`。
2. `migration.copy_snapshot()` 通过 `_snapshot_stat()` 检查根目录和全部子项。该函数使用 `lstat()` 拒绝符号链接及 `st_file_attributes` 中的重解析标志，再根据读取到的 `st_mode` 分类；junction 不会进入递归，被过滤的元数据名称和 SQLite 侧车文件也先经过安全检查。
3. 新增 `tests/test_windows_paths_and_snapshots.py` 共 23 项参数化用例：本机 19 项通过、4 项 Windows 原机用例跳过。覆盖两后端真实读写、大小写兄弟目录不被接管、已有/不存在的路径、代际控制记录定位、根/嵌套目录及文件重解析点、备份与迁移失败后正式视图完整性。

## 复现与验证

实施前首次运行新增测试：16 失败、1 通过、4 跳过。失败包括真实写入被重定向、代际路径无法定位、目录递归进入模拟 junction，以及库外属性未在过滤之前检查；Parquet 的一项备份用例在此前严格身份校验中已经拒绝该目录。

修复后运行锁、快照、PLAN-084、迁移、Parquet、分区、SQLite 契约和 mysqlite 相关回归：**330 通过、6 跳过，15.74 秒，无失败**。6 个跳过全部为 Windows 专属场景：本轮新增 4 项，PLAN-085 的持锁删除/替换防护 2 项。

```bash
uv run --frozen --no-sync --offline --with pytest --with psycopg2-binary \
  --with duckdb --with fastapi --with httpx --with uvicorn --with 'pip==25.2' \
  --with wheel --with setuptools python -m pytest -q --tb=short \
  tests/test_windows_paths_and_snapshots.py tests/test_file_lock.py \
  tests/test_windows_lock_file.py tests/test_plan084_migration_review.py \
  tests/test_safe_migration.py tests/test_migration.py tests/test_parquet_backend.py \
  tests/test_partition.py tests/test_partition_string.py \
  tests/engines/sqlite/test_timeseries_contract.py \
  tests/engines/sqlite/test_partitioned_table_contract.py \
  tests/utils/mysqlite/test_mysqlite.py
```

新增测试及锁模块/测试的 Ruff 默认规则检查通过；修改的迁移、协调器和新增测试通过 Ruff 的 E9/F63/F7/F82 致命语法与名称检查；`git diff --check` 通过。协调器、迁移文件中此前的全量样式规则问题不计为本次检查通过。

## Windows 原机用例与边界

- 两后端原机用例通过 `fsutil.exe file setCaseSensitiveInfo <目录> enable` 启用真正的大小写敏感目录，再创建独立的 `MyDB` 和 `mydb`，验证写入只落在 URI 指定的库。环境或权限不支持时明确跳过。
- junction 原机用例使用 `mklink /J` 分别创建根下一层和嵌套目录联接，链接到库外含有文件的目录；断言其不是 symlink、确实带重解析属性，并验证快照拒绝递归且库外数据保持完整。用例最终仅移除 junction 自身。
- Linux 的确定性用例模拟 Windows 小写身份和 `lstat()` 重解析属性，补足普通 POSIX 符号链接测试不能覆盖的分支；这些结果不等同于 Windows 原机通过。
- 快照检查针对遍历入口已有的重解析点；未参与协调协议的外部进程在检查后的恶意替换不属于本次保证。

依据：Python 3.9 已支持[不跟随 junction 的 lstat](https://docs.python.org/3.9/library/os.html#os.lstat) 及 [Windows 文件属性](https://docs.python.org/3.9/library/os.html#os.stat_result.st_file_attributes)；NTFS 的目录级大小写敏感配置参见 [Microsoft 文档](https://learn.microsoft.com/en-us/windows/wsl/case-sensitivity)。
