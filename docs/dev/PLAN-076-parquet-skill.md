# PLAN-076：补齐 iosql Skill 的 Parquet 使用指引

## 背景

Parquet 后端已提供本地目录库、分区与 segment、查询和维护、流式读取及跨后端迁移能力。
当前 `skills/iosql-usage` 只在入口、API 和 CLI 概述中零散提及 Parquet，缺少完整的使用决策、
后端差异、迁移模式和错误排查导航；示例索引也未收录新 Parquet 示例。

## 目标

让加载 iosql Skill 的 agent 能根据归档/分析需求选择 Parquet，正确使用其公共 API，并能识别
与 SQLite 不同的写入成本、并发读取约束、未支持能力和迁移入口。

## 范围

1. 新增聚焦的 Parquet 参考文档，记录安装、URI、目录结构、读写/查询/维护、迁移及行为边界。
2. 更新 `SKILL.md` 的触发描述、入口决策、场景导航和详细资料索引。
3. 校准 API、CLI、迁移模式、排错和示例索引中涉及 Parquet 的描述；补充 Skill UI 元数据触发语句。
4. 修改 `skills/iosql-usage`、其镜像规则要求同步的 `examples/README.md` 与本计划/结果文档，
   不改动后端实现和其他仓库文档。

## 实施步骤

1. 对照 Parquet 设计、实现、现有 README 和测试确认真实接口与限制。
2. 写入本计划后，按渐进披露方式新增 `references/parquet.md`，将详细说明放在参考文档。
3. 修订 Skill 入口和相关参考文档，保持示例镜像完整一致。
4. Review 文案与链接，执行 Skill 结构校验和 diff 格式检查；不运行项目测试。
5. 生成本计划对应的结果文档。

## 验收标准

- Skill frontmatter 和 UI 元数据会在用户提到 Parquet/Parquet 迁移时触发。
- 主入口能说明何时选 Parquet，并链接到专门的 Parquet 指南与示例。
- 指南准确覆盖 `iosql[parquet]` 安装、`parquet://` URI、存储布局、可用 API、写入重写成本、
  segment 限制、读取流生命周期、SQLite/Parquet 迁移及不支持能力。
- CLI 说明准确体现仅 `migrate` 支持 Parquet URI。
- 引用的相对链接存在，Parquet 示例副本与根目录示例一致。
- Skill 校验和 `git diff --check` 通过。
