# PLAN-085 结果：Windows 库锁文件重解析点防护

- 日期：2026-10-18（Asia/Shanghai）。
- 计划：[PLAN-085](PLAN-085-windows-lock-reparse-points.md)。
- 状态：代码修复、自审及本机验证完成；未提交或推送。

## 修复结果

`LocalCoordinator.acquire()` 改用 `file_lock.open_lock_file()`。POSIX 保留原有打开标志；Windows 使用 `CreateFileW` 的 `FILE_FLAG_OPEN_REPARSE_POINT` 和 `OPEN_ALWAYS` 打开最终 `.lock` 文件本身，随后按句柄读取 `FileAttributeTagInfo`。任何重解析点均抛 `UnsafeOperation`，不依赖标签是否为符号链接，也不依赖打开前的路径检查。

打开时只共享读写权限，不共享删除权限，使锁文件在句柄存续期间不能被删除或重命名替换，同时保留共享/独占字节锁的正常协调。检查通过后设置二进制、读写、不可继承的 CRT 描述符；此后由现有登记和释放逻辑管理。属性检查或描述符转换失败（含中断）均关闭原生句柄；打开返回无效句柄时不尝试关闭。

新增 `tests/test_windows_lock_file.py`，包含 18 项参数化用例；本机 16 项通过、2 项因 Windows 专属删除共享语义跳过。补充原有 Win32 绑定断言，并调整两个锁失败测试的打开替身，使其在 Windows 上也能独立验证字节锁失败释放。

## 验证

| 范围 | 结果 |
| --- | --- |
| `tests/test_file_lock.py` 与 `tests/test_windows_lock_file.py` | 28 通过、2 跳过 |
| 锁、PLAN-084、迁移、Parquet、分区、SQLite 契约及 mysqlite 相关回归 | 344 通过、16 跳过、2 项既有失败，耗时 15.71 秒 |
| 新增和调整的锁模块、两个锁测试文件 Ruff 默认规则检查 | 通过 |
| 协调器及上述文件 Ruff 致命语法/名称检查（E9、F63、F7、F82） | 通过 |
| `git diff --check` | 通过 |

扩展回归命令：

```bash
uv run --frozen --no-sync --offline --with pytest --with psycopg2-binary \
  --with duckdb --with fastapi --with httpx --with uvicorn --with 'pip==25.2' \
  --with wheel --with setuptools python -m pytest -q --tb=short \
  tests/test_file_lock.py tests/test_windows_lock_file.py \
  tests/test_plan084_migration_review.py tests/test_safe_migration.py \
  tests/test_safe_migration_postgresql.py tests/test_migration.py \
  tests/test_parquet_backend.py tests/test_partition.py tests/test_partition_string.py \
  tests/engines/sqlite/test_timeseries_contract.py \
  tests/engines/sqlite/test_partitioned_table_contract.py \
  tests/test_plan062_mysqlite.py tests/utils/mysqlite/test_mysqlite.py
```

两项失败均已记录在 [PLAN-084 的既有失败记录](../tests/20261018-15-WORKTREE/migration-review-verification.md)：

- `tests/test_plan062_mysqlite.py::test_group_rows_by_keys_groups_heterogeneous_rows`：字段顺序分组与旧断言不同。
- `tests/test_plan062_mysqlite.py::test_auto_commit_false_requires_transaction_or_explicit_commit`：事务内 `insert(commit=False)` 的旧断言未触发预期异常。

16 项跳过包括 12 项未配置 PostgreSQL DSN 的集成测试、2 项 mysqlite 原有跳过及 2 项新增 Windows 专属用例，未计为通过。对 `coordination.py` 使用当前继承的 Ruff 全量规则时，仍有本次改动之前存在的 I001 一处、SIM117 两处样式问题；本次未修改对应代码。

最初直接运行 `uv run --no-sync pytest` 使用了不包含项目的系统 pytest；改用 `uv run ... --with pytest python -m pytest` 后成功收集和运行。最终工作区未保留运行期间产生的 `uv.lock`，没有依赖声明变更。

## 自审与限制

- 检查了 Win32 宽字符路径、指针宽度、8 字节属性结构、错误后关闭及 CRT 句柄所有权。
- 确认共享和独占租约都经过安全打开；拒绝时不会登记描述符或尝试文件锁。
- Linux 上真实普通文件与正常/悬空符号链接验证通过；保留真实共享/独占、超时及进程租约回归。
- 当前环境没有 Windows/Wine。Win32 调用由确定性替身验证；Windows 条件测试已提供但未原机执行。
- 本次防护覆盖最终 `.lock` 文件，不代表所有父目录的重解析点检查，也不扩大断电持久性承诺。

实现依据：[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)、[msvcrt.open_osfhandle](https://docs.python.org/3/library/msvcrt.html#msvcrt.open_osfhandle)。
