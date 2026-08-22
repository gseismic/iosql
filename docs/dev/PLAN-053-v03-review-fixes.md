# PLAN-053: v0.3.0 代码 Review 修复

日期: 2026-08-22
来源: v0.3.0（99a3caa..HEAD）全量代码 review，5 项确认属实并需修复

## 背景

review 覆盖 PLAN-052 用户接口统一与并发演进竞争修复的代码变更。
复核方式：最小复现脚本实证 + 双引擎代码对照 + 设计文档意图核对。

## 修复项

### T1 类型归一化缺口（sqlite 引擎，P0）

现象：`columns={"x": "int"}` 声明后写入 int 数据被
`_ensure_contract_type_compatible` 误拒（`契约已固化为 int，本次数据为 integer`）。
`_canon_sqlite_type` 只归一 `real/double/float`，不识别 `int/bigint/smallint/
varchar/double precision` 等 SQLite 合法声明名。

方案：
- `_canon_sqlite_type` 改为基于别名表 `_SQLITE_TYPE_ALIASES` 归一到库规范类型
  （integer/float/text/blob 四族，含 `boolean→integer`、`numeric/decimal→float`、
  截断 `(n)` 参数）；无法识别的类型返回 `None`。
- 校验循环中任一侧归一结果为 `None` 时**跳过该列**而非拒绝——未知自定义类型
  （如 timestamp/json）回落到 0.3.0 之前的宽松行为。存储库宁可漏检不可误拒。

验收：声明 `int/bigint/varchar(255)/double precision/boolean` 后写入对应数据成功；
声明 `text` 写入 dict 数据仍报错；既有类型冲突测试不回归。

### T2 PG delete 注入面与双引擎不一致（postgresql 引擎，P0）

现象：`delete(where)` 用 f-string 直接拼 where 与表名（pg table.py:1006-1008），
绕过 `Where`/op 白名单；sqlite 侧同一接口走 `Where` 参数化编译。

方案：改用 `sql.Identifier` 引用表名；where 经 `self._where_sql(where, column_types)`
参数化编译（复用现有路径，天然支持 dict/list/Case/文本四种形式，与 select 一致）；
`where=None` 保持 delete_all 行为不变。

验收：dict 条件删除正确；文本条件可用；非法 op 报 `BadArgument`；
`delete_all()` 不回归（PG 测试无 DSN 时跳过，需本地验证语法路径）。

### T3 calendar 分区 + 显式字符串 time 的 unit/tz 错配（spec.py，P1）

现象：`db.table(partition="calendar(date,...,timezone=Asia/Shanghai)", time="date")`
得到 `TimeColumn(unit='ms', timezone='UTC')`；不传 time 时反推为
`unit='date', timezone='Asia/Shanghai'`。裸字符串 time 只表达列名，
却按 TimeColumn 默认值固化了语义。

方案：`__post_init__` 记录 time 是否来自裸字符串；当来自裸字符串且 partition 为
duration/calendar 且 `partition.column == time.name` 时，以分区语义推导
unit/timezone（规则与 `time is None` 反推完全一致）。显式 `TimeColumn` /
dict 形式不受影响；持久化契约经 to_dict/from_dict 往返后 unit 已固化，
重开不会二次漂移。

验收：calendar+str time 得到 `date/tz`；duration+str time 尊重分区 `time_unit`；
time 列名与分区列不同时保持现行为；既有 spec 测试不回归。

### T4 _schema_lock 覆盖数据写入（sqlite 引擎，P1）

现象：NoSuchTable / NoSuchColumn 冷分支把最终数据 insert 重试也纳入
`_schema_lock`；不同分区上两条初始批量导入线程整个写入期互相串行。

方案：锁体收缩到 schema 演进段（meta 重读 → suggest → build meta → 补列 →
建表 → meta 回写），数据 insert 重试移出锁体。正确性依据：重试 insert
`ensure_table=False` 不再触发演进；insert 间并发与稳态热路径模型一致
（SQLite 文件锁兜底）。注释同步更新。

验收：`tests/test_plan052_concurrency.py` 13 例全绿（含 barrier 双线程加列 ×3 轮）；
全量回归通过。

### T5 契约类型校验改为采样式（sqlite 引擎，P1）

现象：`_ensure_contract_type_compatible` 每次 insert_all 全量 O(rows×cols)
扫描（5 万行×20 列 ≈ 百万次 isinstance/批），位于分区路由之前的热路径。

方案：新增模块常量 `_TYPE_CHECK_SAMPLE_ROWS = 1000`，校验仅扫描前 N 行。
理由：类型冲突几乎必然在批次头部显形；且同函数依赖的
`suggest_column_types` 本身就按 `n_suggest_check` 采样推断，深批量尾部异常
从未被完整覆盖，采样不降低既有保障水位。

验收：构造首行 int、第 2000 行 str 的批次——行为与现状一致由后续路径处理，
不因采样引入新的错误类别；性能上大批次不再全量扫描。

## 明确不做

- **range 分区不反推 time**：PLAN-052 L47/L102 与 README 注意事项均已文档化，
  属故意决策，不改行为。
- **PG select `to_dict=True` 默认值**：改动会破坏既有 PG 调用方，属接口决策，
  留给用户裁决。
- **BaseTable.insert_all 死代码下沉**：结构优化，非本轮缺陷修复范围。

## 实施顺序

T1 → T3 → T5 → T4（均在 sqlite/spec 侧，逐项跑相关测试）→ T2（PG 侧）→
全量回归 → 自审 → OUTCOME 文档。
