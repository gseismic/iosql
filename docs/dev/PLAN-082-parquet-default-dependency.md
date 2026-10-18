# PLAN-082 Parquet 默认依赖

- 日期：2026-10-18
- 状态：已完成
- 范围：将 DuckDB 纳入 iosql 默认依赖，并统一当前安装文档和缺依赖提示。

## 目标

用户通过普通 `pip install iosql` 或仓库开发安装 `pip install -e .` 后即可使用 `parquet://` 后端，不再需要 Parquet extra。

## 实施步骤

1. 将 `duckdb>=1.1` 从可选依赖移入 `pyproject.toml` 的默认依赖；保留空的 `parquet` extra 作为旧安装命令的兼容别名。
2. 更新 Parquet 引擎模块说明和缺少 DuckDB 时的安装提示；同步调整现有缺依赖测试的名称与断言，不新增测试。
3. 更新 README、示例说明、iosql 使用指南、API/CLI/排错参考，以及 Parquet 设计概览中的当前安装策略。
4. 静态复核依赖配置、文档引用与此前新增示例的兼容性，生成结果文档；本轮不运行测试或构建。

## 约束

- PostgreSQL 和 Web 仍是可选依赖，不改变其安装方式。
- 保持 Parquet 引擎的延迟导入行为；默认安装 DuckDB 不代表 SQLite 使用时会导入 DuckDB。
- 保留此前工作区内的 `.zcodeignore` 和 SQLite → Parquet 示例变更，不改写历史计划/结果记录。
