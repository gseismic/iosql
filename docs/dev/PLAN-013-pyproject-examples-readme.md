# PLAN-013 pyproject 与示例文档整理

## 背景

当前项目仍使用 `setup.py` 声明包元数据，README 和 examples 目录中也保留了一批旧 API 和底层 mysqlite 工具示例。用户希望迁移到 `pyproject.toml`，并把示例整理成少而精的典型入口：入门示例编号靠前，同时保留高级场景，让用户通过示例即可理解主要用法。

## 目标

1. 使用 `pyproject.toml` 作为项目打包配置入口，移除旧式 `setup.py`。
2. 清理过期或偏离主线 API 的示例，尤其是大压测和底层 mysqlite 工具演示。
3. 保留少量编号示例，覆盖入门到高级：
   - 入门时序表写入与查询。
   - 入门日历分区事件表。
   - 批量写入、查询计划和全局排序。
   - 布局诊断与重分区。
4. 更新 README，使安装方式、快速开始、示例索引和 API 描述与当前实现一致。
5. 运行打包、示例和测试验证，完成 review 后生成 OUTCOME 并提交。

## 非目标

- 不重构核心存储逻辑。
- 不引入新的运行时依赖。
- 不继续维护 `examples/mysqlite` 作为用户主入口；底层工具仍在 `iosql/utils/mysqlite` 中，但不放在主示例目录展示。

## 验证

- `python -m pip wheel . --no-deps --no-build-isolation -w /tmp/iosql-wheel`
- 逐个运行 `examples/01_*.py` 到 `examples/04_*.py`
- `python -m pytest tests/sqlite/test_timeseries_contract.py -q`
- `python -m pytest tests -q`
- `git diff --check`
