# PLAN-082 Parquet 默认依赖结果

- 日期：2026-10-18
- 计划：[PLAN-082-parquet-default-dependency.md](PLAN-082-parquet-default-dependency.md)
- 状态：完成

## 实施结果

1. 将 `duckdb>=1.1` 移入 `pyproject.toml` 默认依赖。普通 `pip install iosql` 和仓库开发安装 `pip install -e .` 都包含 Parquet 后端运行时。
2. 保留空的 `parquet` extra 作为兼容别名；DuckDB 已由默认依赖安装，用户无需再写 `iosql[parquet]` 或 `.[parquet]`。
3. 保持 Parquet 引擎的按需导入行为；SQLite 使用路径不因安装 DuckDB 而主动导入它。
4. 更新 Parquet 缺依赖提示、宽表 benchmark 提示、README、示例说明、使用指南和 API/CLI/排错参考；在 Parquet 设计概览标明当前安装策略。
5. 同步调整既有缺依赖测试的名称和预期提示，没有新增测试。

## 静态复核与验证范围

- 已静态复核默认依赖、compatibility extra、延迟导入说明及当前用户文档；旧的可选依赖表述仅保留在历史计划/结果记录中。
- `git diff --check` 通过。
- 本轮未运行测试、示例或构建，没有运行时或 wheel 元数据验证结论。
- `.zcodeignore` 及工作区内此前的 SQLite → Parquet 示例修改保持保留。
