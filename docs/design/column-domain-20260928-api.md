# Column 类型与声明值域用户接口设计

日期：2026-09-28
状态：设计审查完成，按此方案实施

## 1. 目标

为量化与机器学习数据表持久化表说明、字段解释、数据库类型和声明值域，并可在重开表后查询。既支持建表时声明，也允许给旧数据库中的既有表补充这些信息。声明值域采用 `low` / `high`，表示列在业务或模型中的允许/预期值域，例如价格列 `low=0, high=+inf`。

此值域是列契约的一部分，不由已写入样本计算，也不代表当前数据的观测极值。现有分区、segment 统计中的 `min` / `max` 继续表示实际数据统计。

## 2. 现状与设计依据

- `Database.table(columns=...)` 已接受 `{列名: 数据库类型}`，`TableContract.columns` 持久化类型，`Table.spec()` 可读取契约。
- iosql 的 `column_info` 是物理表列信息；segment 中的 `min/max`、`time_min/time_max` 是数据统计。把列值域写进这些统计字段会混淆语义。
- `pyta2.utils.space.Box` 使用 `low/high`，边界比较为闭区间；其 `dtype`、`shape`、`null_value` 也是 Space 对象的属性。
- `Category`、`Discrete`、`Bool`、`Sign` 表达类别或离散空间，不具有与 `Box` 相同的 `low/high` 契约。
- 低层 `build_createtable_meta` 已有列 `comment` 概念，但 `Database.table()`、持久化 `TableContract` 和统一读取接口尚未形成可复用的表/字段说明 API。

因此第一版只定义**标量数值列的 low/high 值域**。数据库类型沿用 iosql 的 `columns`；不把 iosql 与 pyta2 绑定，也不将所有 Space 类型压成数值范围。

## 3. 代表场景与优先级

| 优先级 | 场景 | 典型声明 | 接口要求 |
|---|---|---|---|
| P0 | 价格、成交量等非负列 | `price: [0, +inf]` | 声明类型和值域，写入后保存在契约 |
| P0 | 模型因子、收益率等有界数值 | `return: [-1, 1]` | 重开表后能读取原边界 |
| P1 | 正负无界数值列 | `factor: [-inf, +inf]` | 能显式表达无界端点 |
| P1 | 已有表供训练/特征代码读取 | `table.spec()` | 类型和值域都从同一契约读取 |
| P2 | 类别、枚举、向量空间 | `Category(...)`、`Box(shape=...)` | 保留为后续独立的空间契约设计 |

## 4. 推荐的用户接口

### 4.1 声明列类型和值域

在 `Database.table()` 和 `Database.timeseries()` 增加可选参数 `description`、`column_descriptions`、`column_ranges`。`description` 是表介绍；`column_descriptions` 是字段解释。类型仍通过现有 `columns` 声明；字段解释和值域按列名映射：

```python
import numpy as np
from iosql import Database, Partition

with Database("sqlite://data/market.iosql") as db:
    bars = db.table(
        "bars",
        pk=["symbol", "ts"],
        columns={
            "symbol": "text",
            "ts": "integer",
            "price": "float",
            "volume": "integer",
            "return": "float",
        },
        description="日线行情。时间戳按 UTC 毫秒存储；价格为未复权价格。",
        column_descriptions={
            "symbol": "证券代码，使用交易所标准代码。",
            "ts": "K 线结束时间，Unix 毫秒时间戳。",
            "price": "收盘成交价，单位为人民币/股。",
            "volume": "成交量，单位为股。",
            "return": "本周期简单收益率。",
        },
        column_ranges={
            "price": {"low": 0, "high": np.inf},
            "volume": {"low": 0, "high": np.inf},
            "return": {"low": -1, "high": 1},
        },
        partition=Partition.duration("ts", every="1day"),
    )
```

对时序语法糖 `Database.timeseries()` 也提供同名参数并原样转交给表契约：

```python
bars = db.timeseries(
    "bars",
    time="ts",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    description="1 分钟行情。",
    column_descriptions={"price": "未复权收盘价，单位为人民币/股。"},
    column_ranges={"price": {"low": 0, "high": np.inf}},
)
```

若已用 pyta2 的标量 `Box` 描述数值域，可取出它的标量边界传入 `column_ranges`：

```python
from pyta2.utils.space import Box

price_space = Box(low=0, high=np.inf)
column_ranges = {
    "price": {
        "low": price_space.low.item(),
        "high": price_space.high.item(),
    }
}
```

`Box.dtype` 不替代 `columns` 中的数据库类型；两个定义各自表达存储类型和值域。第一版只接收标量边界，不接收数组形状或 `null_value`。

### 4.2 给旧表补充或修改说明

新增 `Table.update_metadata(*, description=_UNSET, column_descriptions=_UNSET, column_ranges=_UNSET) -> dict`，用于给已经存在的表补充元数据，不要求重建表或重写数据。`_UNSET` 是内部默认标记，调用方无需传入。参数省略表示不修改该项；方法返回更新后的 `describe()` 结果：

```python
with Database("sqlite://data/market.iosql") as db:
    bars = db.table("legacy_bars")
    bars.update_metadata(
        description="历史日线行情，price 为未复权价格。",
        column_descriptions={
            "price": "收盘成交价，单位为人民币/股。",
            "volume": "成交量，单位为股。",
        },
        column_ranges={
            "price": {"low": 0, "high": np.inf},
            "volume": {"low": 0, "high": np.inf},
        },
    )
```

更新规则：传入的 `description` 替换表介绍，`description=None` 清除表介绍；字段映射按列增量更新，字段值为 `None` 时清除该列对应项，整个映射参数传 `None` 时清空该类字段元数据。方法校验字段存在；声明值域时从旧 meta 的列类型读取类型并检查其为数值类型。更新在 metadata 事务中完成，不访问或改写行情数据文件。

对已有 SQLite 表也可用 `db.table("legacy_bars", description="...")` 只传元信息参数完成增补；需要清除信息时使用 `update_metadata()`，以区分省略参数和显式传入 `None`。

新建表可在 `db.table()` / `db.timeseries()` 声明相同参数；旧表可直接调用 `update_metadata()`，不必重新声明主键、分区或整份 `TableContract`。

### 4.3 读取列类型、值域和解释

保留现有 `Table.spec()`，并新增适合程序和 LLM 直接消费的 `Table.describe()`。`describe()` 返回 JSON 安全的规范结构，按列组织类型、解释和值域：

```python
info = bars.describe()
```

```json
{
  "name": "bars",
  "description": "日线行情。时间戳按 UTC 毫秒存储；价格为未复权价格。",
  "primary_key": ["symbol", "ts"],
  "columns": [
    {
      "name": "price",
      "type": "float",
      "description": "收盘成交价，单位为人民币/股。",
      "range": {"low": 0.0, "high": "+inf"}
    }
  ]
}
```

`columns` 按表 schema 顺序返回；每个字段固定提供 `name`、`type`、`description`、`range`，未定义的解释或值域用 `null` 表示。无穷边界输出 `"+inf"` / `"-inf"`。重开数据库后，`describe()` 和 `spec()` 返回相同的元数据：`spec().description` 是表介绍，`spec().column_descriptions` 和 `spec().column_ranges` 是字段映射，`spec().columns["price"]` 仍是原来的类型字符串。

`describe()` 只读取契约和表结构元数据，不扫描业务数据。PostgreSQL 当前实例可从调用方传入的 `spec` 读取说明和值域；由于 PostgreSQL 契约现阶段由调用方在重开时重新绑定，第一版不提供跨进程持久化的 `update_metadata()`。

读取旧表时，若新扩展元数据中没有字段解释，可将旧 `column_info[column].comment` 作为 `description` 回退值；新接口写入的说明只写扩展表，不覆盖旧 `comment`。

### 4.4 契约对象与序列化

`TableContract` 增加默认空的可选字段 `description`、`column_descriptions`、`column_ranges`，`to_dict()` / `from_dict()` 支持往返。`columns` 仍保持原来的 `{列名: 类型字符串}` 结构。字段为空时序列化省略新增键；旧契约没有新增键时按空元数据读取。该结构用于 Python 契约传递和 API 返回；SQLite 持久化 legacy `table_contract` 时必须剥离这三个可选字段，避免旧 meta JSON 的结构随本功能改变。建议的扩展结构：

```json
{
  "columns": {"price": "float"},
  "description": "日线行情。",
  "column_descriptions": {"price": "未复权收盘价，单位为人民币/股。"},
  "column_ranges": {
    "price": {"low": 0.0, "high": "+inf"}
  }
}
```

Python API 接受正负无穷；JSON 契约用 `"+inf"`、`"-inf"` 两个明确标记，避免输出非标准 JSON 的 `Infinity`，也避免把无穷误读成 `null`。读取契约时恢复为 Python `float('inf')` / `float('-inf')`。有限端点保持数值。

## 5. LLM 友好性约定

- `Table.describe()` 是读取入口，返回普通 dict/list/scalar，可直接 JSON 序列化或传给工具调用方；不需要让 LLM 解读 SQLite 内部 `meta` 行或组装多份映射。
- `description` 用于介绍整张表，包括数据主题、粒度、单位体系和关键业务口径；`column_descriptions` 用于解释每个字段的定义、单位、调整方式、空值含义等。
- `range` 是可选的模型/业务值域。若字段含义需要更丰富的语义，应写入 `description`，不从列名推断。
- 接口只返回用户显式提供的信息和 iosql 已知类型；不调用模型生成或改写说明。

## 6. 输入校验与行为契约

1. `column_ranges` 类型为 `dict[str, dict]`，每个值只接受 `low` 和 `high` 两个键；两个边界都要显式提供。无边界约束的列不放入该映射。
2. 每个值必须是标量数值或正负无穷；拒绝 `NaN`、布尔值、数组和无法比较的字符串。
3. 建表契约中的每个 `column_ranges` 键必须同时存在于 `columns`，且其声明类型属于 iosql 支持的数值类型。给旧表增补时，列名和类型从现有契约或 `column_info` 读取。类型仍按现有后端映射规则解释。
4. 必须满足 `low <= high`。端点按闭区间解释，与 `Box.contains()` 的边界语义一致。
5. 写入超出声明值域的数据仍按现有写入规则处理。`column_ranges` 只保存可查询的领域契约，不自动生成 SQL `CHECK`、不拒绝写入、不参与分区裁剪或 segment 统计。
6. 对已有表声明新值域或调整值域只更新扩展元数据，不改写历史数据；`spec()` 读取合并后的新元数据。已有列的数据库类型仍按 `SchemaConflict` 规则保护。
7. `column_descriptions` 中每个字段说明必须是字符串或 `None`；说明可为空字符串，但建议使用 `None` 清除。
8. `Table.update_metadata()` 要求逻辑表及其旧 meta 已存在；它不自动为没有数据/没有 schema meta 的空表造表。旧数据库中的类型从持久化 `column_info` 或已有契约读取。

校验失败使用 `BadArgument`，错误信息指出列名、问题和修复示例。声明中出现不在现有 schema 的名称时，提示可用列名；旧数据库里列名/类型无法解析时，允许补充表说明，但拒绝对该列写入范围。

## 7. 元数据落点与兼容性

为兼容旧版本会整条重写现有 meta JSON 的写入路径，SQLite 后端在每张逻辑表现有的 `meta.sqlite3` 中新增一个独立扩展表 `_iosql_metadata`。新增说明和值域只以此扩展表为持久化来源；`TableContract.to_dict()` 的可选字段不得写入 legacy `table_contract` / `table_spec` JSON。建议结构为：

```sql
CREATE TABLE _iosql_metadata (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (scope, name)
);
```

`scope` 取 `table` 或 `column`；表级行的 `name` 为空，字段级行的 `name` 是列名。JSON 值分别保存表说明，或字段说明与 `range`。此表是新元信息持久化的规范来源；`Table.spec()` 和 `Table.describe()` 将它与现有类型契约组合。`TableContract.to_dict()` 可携带可选扩展字段用于契约传递；SQLite 写入 legacy meta 前从 `table_contract` / `table_spec` 剥离扩展字段，读取时则以 `_iosql_metadata` 覆盖契约内存视图。已有 `column_info[column].comment` 可作为字段解释的只读回退值，新接口不改写该字段。

现有 `meta` 记录的必需字段、`column_info`、列类型映射、segment 的 `stats`、`time_min/max` 和分区边界结构均保持原样。旧版本只读写原有记录，不会读取或删除 `_iosql_metadata`；新版本再次打开时仍可从扩展表恢复新增信息。这样同一列的领域范围不会与实际写入数据极值混在一起。

新版本读旧库时，扩展表不存在表示没有新增说明/值域，读接口返回空值，不执行存量数据迁移。SQLite 以外的后端沿用现有契约生命周期：PostgreSQL 表重开时仍需由调用方传入 `spec`；通过 `update_metadata()` 持久化元信息到 PostgreSQL 不在本设计范围内。

## 8. 兼容性保证与边界

- `columns` 的键和值、旧 `table_contract` 必需字段及物理 `column_info` 不变；新增字段均可缺省。
- `description`、`column_descriptions`、`column_ranges` 不参与 DDL，也不改变旧读写的列类型、主键、分区和冲突策略。
- 旧版本打开新版本创建的 SQLite 库时，常规查询和写入仍使用原有业务表结构。扩展表不改变业务数据表 schema。
- 旧版本不理解也不返回新增元信息。常规旧版写入会重建 legacy meta JSON，但不会删除独立扩展表；新版本仍能读取这些信息。
- 旧版本执行整表迁移、重分区等“新建目标表”的操作时，不会携带它不认识的元信息；需要由支持扩展表的新版本执行此类操作。
- 新版本对旧库读取和写入时，未声明新增元信息的行为与现状一致，不要求迁移，也不改变默认写入校验。

## 9. 暂不纳入

- 写入时检查数据是否落在 `low/high` 内；若后续需要，应另设显式校验选项，避免把元数据声明变成隐式写入限制。
- `Category`、`Discrete`、`Bool`、`Sign` 的类别集合或离散空间持久化。
- 多维 `Box`、列级空值语义、抽样/`contains()` 行为，以及自动由 NumPy dtype 推导数据库类型。
- 对每行数据的范围统计、按声明范围查询或优化查询计划。

## 10. 设计审查记录

审查后确认并纳入实施约束：

1. SQLite 的 `_overwrite_table_meta()` 会整体重写 legacy meta JSON，因此新信息必须只存 `_iosql_metadata`；即使 `TableContract.to_dict()` 为 API 往返携带扩展字段，legacy `table_contract` / `table_spec` 落盘时也要剥离这些字段。
2. 增补旧表信息只写元数据侧表，不重建物理表、不扫描业务数据，也不触碰列类型、主键和分区定义。
3. `describe()` 提供单次调用即可使用的稳定 JSON 结构，并且只读取契约/结构元数据；旧 `column_info.comment` 只作为字段解释回退。
4. SQLite 的 `update_metadata()` 负责持久化增补；PostgreSQL 继续使用当前实例传入的契约，不承诺跨重开持久化。
