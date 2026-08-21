# auto-partition-20260822-overview

## 背景

iosql 的布局管理采用**双模式并存**定位：

- **模式 A · 显式分区（金融主力模式，中期目标主体）**：建表时声明 `partition=`，用 `layout_report()`/`doctor` 检查，需要时 `repartition_to()` 修复。金融场景数据一次写入基本不变，布局决策是**偶发的**——已知 workload 的表应在创建时把粒度定对，之后长期稳定。
- **模式 B · auto 模式（免决策，可选层）**：不设 `partition`，数据少时不分区，数据多了系统自动升级布局，用户全程不感知分区词汇。

两种模式长期并存、同为一流接口：auto 不是显式分区的替代或终局，而是"不想/不能在创建时做布局决策"的表的另一选项。读侧透明（查询永远是一张逻辑表）与布局变更透明（迁移期间旧读有效、不停服）对两种模式同等适用。

本文评估该定位的合理性、与现有机制的距离，并给出分阶段实施路线。本文是 P1→P3 路线的起点；P0 为 `user-api-unify-20260822-consolidated.md`（接口统一），两者衔接关系见"分阶段路线"。

## 结论

方向合理，且是业界收敛验证过的形态（Apache Iceberg hidden partitioning + partition evolution、Google F1 online schema change 的交集）。但有两个概念必须修正，否则实现会走进死胡同：

1. **"全局锁"是错误原语 → 正确答案是快照交换 + 双读**。读请求在迁移全程永不等待；唯一需要短暂等待的是写路由切换的原子提交瞬间（毫秒级、单表粒度）。锁会把迁移时长和并发读耦合在一起，且锁状态本身是故障源；快照交换把两者解耦，迁移可以很慢、可断点续传。
2. **"分区太多/太少"不是目标变量 → 真正的不变量是文件大小和文件数量**。分区粒度是从 `target_file_rows / 观测行速率` 推导出的结果，不是第一性的。用户怕的不是"分区多"，是"文件碎导致扫描打开过多句柄、元数据膨胀"；怕的不是"分区少"，是"文件大导致单文件无限膨胀"。

补充一条定位修正：**auto 不是显式分区的终局替代，而是并存的第二模式**。金融数据一次写入基本不变，布局决策是偶发事件而非持续过程——显式分区（创建时定对一次 + 偶尔 repartition 修复）是已知 workload 的最优解；auto 服务于"创建时不知道量级/不想决策"的表。因此中期目标就是模式 A 的体验闭环（检查→重分区），模式 B 是其上的可选层。

## 现状盘点（已核实的基础设施）

以下机制均已与源码核对，是本路线的"胚胎"：

| 目标机制 | iosql 已有基础 | 位置 |
|---|---|---|
| 旧读有效直到切换 | `compact()` 的 generation 机制：新 generation 写好后旧 segment 仅标 `obsolete`，查询自动读新代 | `engines/sqlite/table.py:1075-1141` |
| 中间态不可见 | segment 状态机 `active/sealed/obsolete`，默认查询/`segments()`/`layout_report()` 过滤 obsolete | `table.py:1791-1831` |
| 迁移期新写入去向 | 晚到数据进 delta segment（不破坏已 sealed 文件） | README/例 06 |
| 原子元数据提交 | `meta.sqlite3` 清单；mysqlite 层已有 `BEGIN IMMEDIATE` 事务 | `const.py`、`mysqlite/database.py:256` |
| 数据少不分区 | 不配 `partition` 时走 `__all__` 逻辑分区 + `Segment.rows(max_rows=...)` 按行滚动文件 | README 四种组合 |
| 布局健康信号 | `layout_report()` 的 oversized/undersized 水位（oversized = 行数 > `max_rows_per_file`；undersized = < `min_rows_per_file` 且多分区） | `table.py:3653-3689` |
| 粒度推导 | `doctor` 建议器：`max/avg≈4, 目标<=500,000` 的 every 推荐（当前是"建议给人看"） | `cli.py` |
| 迁移结果验证 | `verify="row-count"` 已存在于迁移路径 | 例 11/12 |
| 影子写新布局 | `repartition_to()` 全量写新目标库、源库不动 | `table.py:3691` |
| 删除安全门 | `cleanup_obsolete()` 默认 dry-run + 删前 `validate(deep=True)` 拒绝删除 | `table.py:700-764` |

**缺口只有三块**：① 清单中的"双布局过渡状态"（一表同时持有旧 spec 分区 + 新 spec 分区）；② 查询计划器对双布局的融合读（两侧各自裁剪后 union）；③ 写路由切换 + 后台增量迁移的状态机。

## 概念修正详述

### 修正一：全局锁 → 快照交换

| | 全局锁 | 快照交换 |
|---|---|---|
| 读请求 | 迁移期间排队等待 | 永不等待，继续读旧快照 |
| 迁移时长 | 与并发读冲突 | 与读完全解耦，可以很慢 |
| 故障恢复 | 锁状态是故障源 | 清单状态机，重启续传 |
| 实现复杂度 | 锁超时/死锁地狱 | 状态机 + 双读，复杂但可测试 |

先例：Iceberg snapshot 元数据原子切换、SQLite WAL 读写不互阻。用户直觉里的"处于某个特定状态需要等待"，正确落点是：**MIGRATING 状态下读不等待；CUTOVER 提交瞬间写路径短暂串行**（单事务内完成，毫秒级）。

### 修正二：分区数 → 文件不变量

- 控制变量：单文件行数上限（已有 `max_rows_per_file` 语义）、单表打开文件数预算（已有 `max_open_partitions` FD 预算）。
- 推导规则：`every ≈ target_file_rows / 观测行速率`。日入 50 万行 → `1day`；日入 5 亿行 → `1hour`。
- 自动策略必须**双向**：分裂（升分区密度）与合并（降密度重组碎分区），且带迟滞防震荡——越过高水位持续 N 个观测窗口才分裂，跌破低水位持续 N 个窗口才合并，每次变更后冷却期内不再触发。没有迟滞，数据速率波动会让布局在 `1day`↔`1hour` 间反复自我重写，磁盘先被后台迁移写爆。

## 目标与非目标

### 目标

1. 模式 A 为推荐默认（尤其金融 write-once 场景）：显式 `partition=` 一等公民，"显式建表 → 检查 → 一键重分区"闭环体验打磨（P1 重点）。
2. 模式 B 同为一等公民：`db.table("ticks", pk=..., columns=..., auto=True)` —— 不出现任何分区词汇，布局由系统演进（P3，可选层）。
3. 在线布局变更：迁移期间读不断、写不断（仅 cutover 毫秒级写串行）。模式 A 的 repartition 与模式 B 的 evolve 均受益（P2）。
4. 崩溃安全：任何时刻断电/杀进程，重启后按清单状态续传或回滚，旧布局始终可读。
5. 可观测性补偿：布局对用户不可见后，性能退化不能也不可见（见"难点 4"）。

### 非目标

1. 不改变存储层目录结构、分区 key 计算、segment 物理布局格式（复用现有）。
2. 不做多进程多 writer 并发迁移（保持单 writer 假设，见"难点 5"）。
3. 不在本轮实现跨 segment 全局 upsert/tombstone（与迁移期写入语义合并设计，见"难点 1"）。
4. 不引入新分区类型（hash/value 等）。

## 目标用户接口（双模式示意）

```python
with Database("sqlite://database/market.iosql") as db:
    # ── 模式 A：显式分区（金融主力，中期目标形态）──────────────────
    daily = db.table(
        "daily", pk=["symbol", "date"], columns={...},
        partition="calendar(date,every=1year,timezone=Asia/Shanghai)",
    )
    daily.write(rows)                            # write-once，布局终身稳定
    daily.layout_report()                        # 偶发检查
    daily.repartition_to(hourly_uri, spec=...)   # 偶发修复（P2 后在线执行）

    # ── 模式 B：auto（免决策，可选）────────────────────────────────
    ticks = db.table("ticks", pk=["symbol", "ts"], columns={...}, auto=True)

    ticks.write(rows)                                  # 用户视角永远是这一张表
    ticks.select(where="symbol == 'AAPL' and ts between ...")
    ticks.query(symbols="AAPL", start=..., end=...)    # auto 反推 time 契约（依赖 P0 的 partition→time）

    # 可观测性（补偿黑盒，两种模式共用）：
    ticks.layout_status()   # 当前布局、是否迁移中、进度、下次评估时间
    ticks.explain_select(...).pruning_quality   # 裁剪不锋利时给出原因
```

## 架构设计：在线布局变更状态机

```
STABLE ──(水位信号+策略批准)──→ PLANNING
   │        计算新 spec，写入清单(事务)
   ▼
[旧布局 sole]
   │                              PREPARING ── 新分区按新 spec 开放写入
   │                              │   (新写入 → 新布局；存量仍在旧布局)
   │                              ▼
   │                           MIGRATING ── 后台分批搬旧数据(限流/断点续传)
   │                              │   读 = 旧布局 ∪ 新布局 (双读融合)
   │                              ▼
   │                           CUTOVER ── 原子提交清单：旧布局 segment 标 obsolete
   │                              │      (唯一等待点：写路径毫秒级串行)
   ▼                              ▼
STABLE(新布局) ←────────── CLEANUP ── 旧文件 lazy GC（沿用 dry-run+validate 门）
```

关键设计约束：

1. **清单是唯一事实源**：每次状态跃迁 = `meta.sqlite3` 一个事务（`BEGIN IMMEDIATE`）。崩溃恢复 = 读清单状态续传或安全回退；20 年日线的全量迁移要跑数小时，中断可续。
2. **双读融合**：MIGRATING 期间查询计划器把新旧两套分区都纳入候选，各自裁剪后 union；每批迁移完成做 row-count 不变式断言（能力已有）。
3. **迁移预算**：后台搬迁限流（并发 ≤1 分区、IO 带宽上限、可配置）。生产盘是 USB HDD（G1），不限流会把正在跑的导入打爆。
4. **写路由**：PREPARING 起新写入按新 spec 路由；迁移期的"晚到"行落入旧布局侧 delta segment（机制已有），最终一批统一搬入。
5. **回退路径**：CUTOVER 前任何时刻可 `abort`，清单回退 STABLE，新布局文件标 obsolete 走 GC——失败不是灾难。

## 自动化策略（P3 层，模式 B 专属）

关键定性：金融数据 write-once 特性使 auto 的本质是**延迟的一次性布局决策**——预期每表生命周期仅触发 1-3 次布局变更（初始回灌期定布局，之后长期稳定），状态机偶发触发，不是 LSM 式常驻再平衡。这显著简化 P3：迟滞机制仍需（回灌期速率爬升会造成假信号），但转换频次压力远低于持续再平衡场景。

- 信号源：`layout_report()` 水位 + 滚动窗口行速率观测（写入路径顺带累计，零额外扫描）。
- 决策：迟滞 + 冷却期 + 最小迁移间隔（见"概念修正二"）。
- 触发分级：`auto="off"|"suggest"|"apply"`。`suggest` = doctor 升级版（默认），`apply` = 真自动。自动档必须可一键降回手动（降级后即模式 A，显式接管）。

## 先例对照

| 系统 | 对应部分 | 借鉴点 |
|---|---|---|
| Apache Iceberg | hidden partitioning + partition evolution | 旧数据留旧布局、新数据走新布局、元数据层统一读；不加锁不重写历史 |
| ClickHouse MergeTree | 数据多自动分文件 | size-tiered 自动 merge、part 对用户不可见、merge 限流可配置 |
| Google F1/Spanner | "特定状态需要等待" | online schema change 多状态机：锁 → 可逆小步进 |
| RocksDB/LSM | 数据少不分层，多了自动分层 | size-tier + leveled，天然迟滞 |
| MongoDB | 自动分裂 chunk | 分裂只短暂锁 range 而非全局 |
| DuckDB | 反面参照：完全不分区 | 单文件 B-tree 自身有自适应力——印证"数据少不分区"是正确默认 |

## 难点与风险

1. **迁移期写入语义**：append-only（K线/tick/orderbook）天然无冲突，最简单；跨分区 upsert/replace 在双布局期语义难定义。README 已声明 segment 表暂不支持 upsert，全局 pk index/tombstone 本就是后续项——**两者必须合并设计**，不要各自独立演进。
2. **双读正确性**：union 的去重、全局排序、limit 下推是最大 bug 温床（迁移中查出一行两次/漏一行）。必须内建 row-count + checksum 不变式，每批迁移完自动断言，断言失败冻结迁移并告警。
3. **策略震荡**：无迟滞的自动机制会自我折磨（见修正二）。P3 上线前必须在仿真负载（周期性速率波动）下验证不震荡。
4. **可观测性**：布局不可见后性能退化不可见。补偿：`layout_status()`/`explain` 展示布局与迁移进度/裁剪质量；`doctor` 从"建议器"升级为"自动系统仪表盘"。
5. **并发与租约**：自动迁移引入后台执行者后，需要清单级迁移租约（同一时刻单进程持有），防止两个进程同时迁移同一张表。

## 分阶段路线（每步独立可交付）

| 阶段 | 内容 | 用户感知 | 依赖 |
|---|---|---|---|
| **P0**（已有设计） | `user-api-unify-20260822-consolidated.md`：接口统一 + partition 反推 time | 查询不感知分区；建表仍要选 every | — |
| **P1 半自动（中期主体）** | `doctor` 建议一键执行：`iosql autotune --apply`（内部仍是影子库 repartition，离线单命令）。**模式 A 闭环成型**：显式建表 → 检查 → 一键重分区 | 模式 A 主工作流：不再手写 repartition 命令；变更仍需停写窗口 | P0 |
| **P2 布局内联演进** | 状态机 + 双布局清单 + 双读融合 + 原子 cutover，**手动触发**：`table.evolve(spec=...)` 在线执行 | 换布局不停读不停写（模式 A 的 repartition 升级为在线执行） | P0（清单/裁剪改造自本阶段起） |
| **P3 模式 B（auto，可选层）** | 水位信号 + 迟滞策略引擎自动触发 P2，`auto=True` | 模式 B 上线：免决策建表；模式 A 完全不受影响 | P2 稳定运行一个周期 |

P2 是核心技术攻坚；`compact()` 的 generation 机制已是它的"单分区内版本"，等于做过一次排练。P3 只是 P2 之上加策略层。

## 测试设计要点

1. **双读正确性**：MIGRATING 各阶段（0%、50%、99%）并发读 + 写，断言结果与 STABLE 等价（row-count + 内容 checksum）。
2. **崩溃恢复**：在 PLANNING/PREPARING/MIGRATING/CUTOVER 各状态杀进程，重启后：旧布局可读、状态可续传或可回退、无孤儿文件泄漏。
3. **迁移预算**：限流配置生效性（后台迁移不打爆前台导入吞吐，基准阈值）。
4. **策略迟滞**：仿真速率波动负载（日/周周期），断言布局变更次数 ≤ 阈值（不震荡）。
5. **不变式断言**：每批迁移后 row-count 断言失败时，迁移冻结且数据零丢失。
6. **P1 回归**：`autotune --apply` 与手工 `repartition` 产出的目标库逐字节等价（或行级等价）。

## 开放问题

1. `evolve()` 是否允许同时变更 partition 列本身（如 `ts` → `trading_date`），还是首版只允许同列变更粒度/类型（duration↔calendar）？倾向：首版限同列。
2. CUTOVER 写串行的粒度：整表 vs 单逻辑分区？倾向：单逻辑分区逐个提交（更细、更平滑），代价是过渡期更长。
3. 迁移租约的过期与抢占策略（持有者崩溃后另一进程多久可接管）。
4. `auto="apply"` 的默认触发门槛是否需要按盘速自适应（HDD vs SSD 的 IO 预算差异）。
5. 是否需要在 `spec()` 中暴露布局历史（lineage），供诊断与审计。

## 变更记录

- 2026-08-22 初稿：基于终局目标（用户无感知分区 + 自动分区 + 在线布局变更）的可行性评估与 P1→P3 路线设计；现状机制已与源码核对（generation/obsolete/delta/meta 清单/doctor/layout_report/cleanup 门）。
- 2026-08-22 修订：应用户定位调整，改为**双模式并存**——模式 A（显式分区，金融 write-once 主力，中期目标主体）与模式 B（auto，可选层）同为一等接口，auto 不再表述为"终局形态/显式分区的替代"。补充 auto 的设计定性：延迟的一次性布局决策（预期每表生命周期 1-3 次布局变更），非常驻再平衡。
