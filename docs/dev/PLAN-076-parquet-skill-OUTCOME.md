# PLAN-076 结果：补齐 iosql Skill 的 Parquet 使用指引

## 完成内容

- 新增 `skills/iosql-usage/references/parquet.md`，说明可选依赖、URI、目录布局、契约 API、
  分区/segment、写入重写成本、流式并发约束、SQLite/Parquet 迁移和后端限制。
- 更新 `SKILL.md` 与 `agents/openai.yaml`，把 Parquet 使用和 SQLite/Parquet 迁移纳入技能触发、
  场景路由和示例导航；将 Parquet 的详细边界集中链接到参考文档。
- 更新 API、CLI、迁移模式和故障排查参考页，说明 Parquet 的支持范围与可选依赖。
- 更新仓库与 Skill 两份示例索引，收录 `14_parquet_backend.py`，保持两份 README 和示例脚本一致。
- 移除 Skill frontmatter 中校验器不支持的 `version` 字段；正文仍保留 v0.3.x 契约说明。

## Review 与验证

- Skill 校验：`python /home/lsl/.codex-chatgpt/skills/.system/skill-creator/scripts/quick_validate.py skills/iosql-usage` 通过，输出 `Skill is valid!`。
- `git diff --check` 通过。
- Skill 中所有相对 Markdown 链接均存在。
- `examples/README.md` 与 `skills/iosql-usage/examples/README.md` 内容一致。
- `examples/14_parquet_backend.py` 与 Skill 中的镜像内容一致。
- 未运行项目测试；本计划只更新技能文档、元数据和示例索引。

## 文件范围

- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/agents/openai.yaml`
- `skills/iosql-usage/references/{api-reference,cli,parquet,troubleshooting,usage-patterns}.md`
- `examples/README.md` 与其 Skill 镜像
- `docs/dev/PLAN-076-parquet-skill.md`
- `docs/dev/PLAN-076-parquet-skill-OUTCOME.md`
