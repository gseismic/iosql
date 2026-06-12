# PLAN-013 实施结果

## 完成内容

1. 打包入口迁移
   - 新增 `pyproject.toml`，使用 setuptools 的 PEP 621 配置。
   - 版本号改为从 `iosql.version.__version__` 动态读取。
   - 删除旧式 `setup.py`。
   - 将 `requires-python` 设为 `>=3.9`，匹配当前代码使用的类型注解语法。

2. 示例整理
   - 删除旧的顶层分区示例、大压测示例和连接池清理示例。
   - 删除 `examples/mysqlite` 旧示例，避免把底层工具 API 作为用户主入口。
   - 新增 4 个按学习顺序编号的主线示例：
     - `examples/01_quickstart_timeseries.py`
     - `examples/02_calendar_events.py`
     - `examples/03_buffered_ingest_and_query_plan.py`
     - `examples/04_layout_report_and_repartition.py`
   - 更新 `examples/README.md`，说明示例定位、运行方式和运行时数据目录。

3. README 更新
   - 改为中文文档。
   - 安装方式更新为 pyproject 路径。
   - 快速开始改为 `Database.timeseries()`、`Table.write()`、`Table.query()` 和 `query().explain()`。
   - 增加 `TableSpec` 日历分区、`write_buffer()`、`layout_report()`、`repartition_to()` 的简明用法。
   - 移除旧的多分区不支持 `order_by` / `limit` 描述。

## Review 修复

- 将 `pyproject.toml` 的 Python 版本要求从 `>=3.8` 调整为 `>=3.9`，避免和当前代码的内置泛型类型注解不一致。
- 修正高级示例中目标库的关闭方式，显式持有并关闭 `target_db`。

## 验证

已运行：

```bash
PYTHONPATH=. python examples/01_quickstart_timeseries.py
PYTHONPATH=. python examples/02_calendar_events.py
PYTHONPATH=. python examples/03_buffered_ingest_and_query_plan.py
PYTHONPATH=. python examples/04_layout_report_and_repartition.py
python -m pip wheel . --no-deps --no-build-isolation -w /tmp/iosql-wheel
python -m compileall -q iosql examples
git diff --check
python -m pytest tests -q
```

结果：

- 4 个示例均可从空库运行并输出预期结果。
- wheel 构建成功，产物版本为 `iosql-0.2.1-py3-none-any.whl`。
- `compileall` 通过。
- `git diff --check` 通过。
- 全量测试通过：`116 passed, 14 skipped`。
- pytest 仍有上层目录 `.pytest_cache` 不可写的权限警告，不影响测试结果。
