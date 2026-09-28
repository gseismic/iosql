# PLAN-067: 可视化管理台更名 ui → web

- 日期：2026-09-28
- 背景：用户认为 `iosql ui` 命名不合适——"ui" 泛指任何用户界面（TUI/桌面 GUI 均可称 ui），
  而本功能是**浏览器访问的本地 Web 管理台**，`web` 更准确；且社区先例（sqlite-web、
  datasette serve）均以技术面命名。更名决定：**主名 `web`，兼容别名保留**。

## 实施项

| # | 项 | 内容 |
| --- | --- | --- |
| P1 | 模块更名 | `git mv iosql/ui iosql/web`；`__init__.py` 安装提示与 docstring 同步；包内 docstring 的 iosql.ui 引用更新 |
| P2 | CLI 命令 | 主命令 `iosql web -s <库>`（选项不变）；保留 `iosql ui` 为兼容别名（打印一次性更名提示后执行同一逻辑） |
| P3 | pip extra | `[project.optional-dependencies]` 新增 `web = [fastapi, uvicorn]` 为主；保留 `ui = [...]` 同内容兼容别名（旧 `pip install "iosql[ui]"` 不破） |
| P4 | 测试 | `tests/test_ui.py → tests/test_web.py`、`tests/test_plan062_ui.py → tests/test_plan062_web.py`；import `iosql.ui` → `iosql.web`；`ui_app` → `web_app`；断言 `iosql[ui]` 安装提示同步 |
| P5 | 前端品牌 | index.html `<title>` 与顶栏品牌 `iosql UI → iosql Web`（favicon 不变） |
| P6 | 文档 | README：特性 bullet、可视化管理台章节、命令示例、安装 extra；skills/iosql-usage（SKILL.md + references/api-reference.md + references/ui.md） |
| P7 | 历史文档 | `docs/design/ui-*.md`、`docs/dev/PLAN-055/064…`、`docs/review/*` 为历史记录，**保留原名不改**；本计划即为更名记录 |

## 验收

1. `pytest tests/` 全量通过（重命名后测试文件与 import 同步）。
2. `python -m iosql.cli web -s <真实库>` 启动 + 浏览器打开验证；`iosql ui` 别名可用且提示更名。
3. `pip install "iosql[ui]"` 语义不破（extra 双写）。

## 结果文件

`docs/dev/PLAN-067-rename-ui-to-web-OUTCOME.md`
