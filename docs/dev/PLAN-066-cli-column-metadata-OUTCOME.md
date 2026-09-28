# PLAN-066 实施结果：CLI 暴露列元数据（describe / update-metadata）

- 日期：2026-09-28
- 计划：`docs/dev/PLAN-066-cli-column-metadata.md`
- 依据设计：`docs/design/column-domain-20260928-api.md`（PLAN-065 的 CLI 补齐）
- 结论：**完成，测试通过，端到端冒烟通过**

## 变更（iosql/cli.py，README.md）

1. `iosql describe -s DB -t TABLE`：输出 `Table.describe()` JSON——表名、表介绍、
   主键、按 schema 顺序的逐列 `name/type/description/range`；无穷边界 `+inf/-inf`。
   只读，与 `iosql spec`（原始契约）互补。
2. `iosql update-metadata -s DB -t TABLE`：增量更新元信息，成功输出更新后的 describe。
   - `--description TEXT` 设置表介绍；`--clear-description` 清除（映射 `None`）
   - `--column-desc COL=TEXT`（可重复）增量更新字段解释；空文本 `COL=` 清除该列；
     `--clear-column-descs` 清空
   - `--column-range COL=LOW:HIGH`（可重复）声明闭区间值域，边界支持数值与
     `inf/+inf/-inf`；`--clear-column-ranges` 清空
   - 互斥校验（设置 vs 清除三对）；全空参数 `_fail("没有任何修改…")`
   - 值域解析错误 `_fail` 消息含列名与修复示例（如 `price=0:inf`）；`NaN` 拒绝；
     `low > high` 拒绝；未知列由核心 `BadArgument` 经错误守卫转 `_fail`（含列名）
3. README 新增「表说明、字段解释与声明值域」小节（Python API + CLI 用法）。

## 验证

- `pytest tests/test_cli.py` → **30 passed**（新增 4 个测试函数：describe 结构与
  缺表报错、设置/持久化复读/有界与负值域、清除语义三态、非法输入全路径）
- 全量回归 `pytest tests/` → **690 passed, 16 skipped**（686 + 新增 4，零失败）
- 端到端冒烟：`update-metadata` 设置 → 输出含 `"+inf"` 值域；重开库 `describe`
  可复读；`price=5:1` 报 `low(5.0) > high(1.0)` 且退出码 1

## 边界

- 不提供 `create-table` 建表声明元数据的 CLI 参数（设计建议建表走 Python API）
- PostgreSQL 的 `update_metadata` 不在 PLAN-065 范围，CLI 亦不提供
