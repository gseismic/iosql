# PLAN-065：列类型、声明值域与说明元数据接口

## 目标

在不改变旧数据表结构和旧版读取/写入路径的前提下，为 iosql 增加表介绍、字段解释、列声明值域的声明、持久化与查询能力。`low` / `high` 表示领域契约范围，不参与写入校验，也不与数据统计 `min` / `max` 混用。

## 设计审查结论

1. SQLite 的 legacy 表 meta 由 `_overwrite_table_meta()` 整体重写；新增信息必须持久化在每张表 `meta.sqlite3` 内的 `_iosql_metadata` sidecar，写 legacy `table_contract` / `table_spec` 前剥离扩展字段。
2. `Table.describe()` 返回可 JSON 序列化的稳定结构，只读契约与 schema 元数据，不扫描业务数据。
3. 允许对已有 SQLite 表调用 `Table.update_metadata()` 增补或清除说明和值域，不重建表、不重写行情数据。
4. `TableContract` 可在 Python 接口中携带新字段，空字段不进入旧契约序列化；SQLite 读取时以 sidecar 为规范来源。旧 `column_info.comment` 作为字段说明的只读回退值。
5. PostgreSQL 可通过当前绑定的契约查询元信息；第一版不增加 PostgreSQL 跨重开持久化能力。

## 实施范围

- `iosql/spec.py`：扩展 `TableContract` 的说明与范围字段、输入校验和 JSON 安全的无穷边界往返。
- `iosql/database.py`：在 `table()` / `timeseries()` 增加兼容性可选参数，并将元信息放入契约。
- `iosql/table.py`：提供通用 `describe()`，定义非 SQLite 后端的 `update_metadata()` 不支持行为。
- `iosql/engines/sqlite/table.py`：加载 sidecar、合并至 `spec()`；实现 `update_metadata()`、声明元信息持久化与旧 comment 回退；保证 legacy meta JSON 不携带扩展键。
- `iosql/engines/postgresql/table.py`：绑定契约时校验字段解释和值域键均对应已声明列，并拒绝非数值列的值域。
- 设计文档：记录审查结果和最终接口契约。

## 实施步骤

1. 完成 `TableContract` 元信息字段、数值范围规范化、`NaN` / 布尔 / 数组拒绝、`low <= high` 校验，以及 `+inf` / `-inf` JSON 编码。
2. 在数据库声明入口追加参数，不移动或改变任何既有参数的位置；支持 `spec` 与元信息参数组合，保留旧的核心快捷参数冲突规则。
3. 实现 `BaseTable.describe()` 的统一返回结构，并限制输出为 schema 顺序的列清单。
4. 为 SQLite 增加事务化 `_iosql_metadata` 读写；将声明 metadata patch 与显式 `update_metadata()` 合并到同一校验/持久化路径。
5. 用 sidecar 合并表介绍、列解释和值域到 SQLite `spec()`；对未设置新解释的列回退旧 comment。
6. 在 PostgreSQL 绑定契约时校验字段映射，避免未知列的说明和值域被静默忽略。
7. 在 `_overwrite_table_meta()` 的 legacy JSON 序列化边界剥离新契约键，保障旧格式与业务表 schema 不变。
8. 逐段 review 改动与差异，修复审查发现的问题，编写 outcome 文档。按当前任务约束不运行测试。

## 兼容性验收点

- 所有新增公开参数放在既有参数之后或关键词专用参数区，不改变旧调用的参数绑定。
- 无扩展元信息时，`TableContract.to_dict()`、SQLite legacy meta 行和业务表 schema 保持既有形状。
- sidecar 表不存在时，旧库读取不做迁移并返回空说明/范围；旧版整行覆盖 legacy meta 后 sidecar 内容仍可读。
- `describe()` 和 `spec()` 可返回列类型、字段解释和 `low/high`；无穷边界为标准 JSON 字符串标记。
- `update_metadata()` 对旧表只修改 sidecar，范围值不触发 DDL、数据扫描或写入拒绝。
- 不修改用户已进行中的 UI redesign 文件。

## 验证与结果文档

本任务未获授权添加或运行测试；完成后只进行静态代码 review 和变更差异审查，并在 `PLAN-065-column-metadata-api-OUTCOME.md` 记录实际完成内容、兼容性检查点和未运行测试这一限制。
