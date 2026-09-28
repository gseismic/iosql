# PLAN-067 实施结果：可视化管理台更名 ui → web

- 日期：2026-09-28
- 计划：`docs/dev/PLAN-067-rename-ui-to-web.md`
- 结论：**完成，全量测试通过，真实库浏览器验收通过，兼容别名生效**

## 动机

"ui" 泛指任何用户界面（TUI/桌面 GUI 均可称 ui），而本功能是浏览器访问的本地
Web 管理台；社区同类项目（sqlite-web）亦以技术面命名。主名定为 **`iosql web`**。

## 变更

| 层 | 旧 | 新 |
| --- | --- | --- |
| CLI 命令 | `iosql ui` | **`iosql web`**（`iosql ui` 保留为 deprecated 别名，运行前打印更名提示） |
| Python 模块 | `iosql/ui/` | `iosql/web/`（`from iosql.web import create_app`） |
| pip extra | `iosql[ui]` | **`iosql[web]`**（`ui` extra 保留同内容兼容别名，旧安装命令不破） |
| 测试 | `tests/test_ui.py`、`tests/test_plan062_ui.py` | `tests/test_web.py`、`tests/test_plan062_web.py`（import 同步，`ui_app` → `web_app`） |
| 前端品牌 | title/顶栏 "iosql UI" | "iosql Web"（favicon 不变） |
| 文档 | README 特性 bullet/章节/命令示例、skills/iosql-usage（SKILL.md、references/ui.md、cli.md、api-reference.md） | 全部更新；历史设计/计划文档保留原名不改，本计划即更名记录 |

## 验证

1. 全量回归 `pytest tests/` → **690 passed, 16 skipped**（重命名后测试与 import 同步）。
2. `python -m iosql.cli web -s /home/lsl/Z1/.../kline.factor.iosql --port 8692` 启动，
   输出 `iosql Web: http://127.0.0.1:8692 (READ-ONLY)`；浏览器确认品牌与 5-Tab 新界面正常。
3. `iosql ui` 别名：打印 DeprecationWarning + 「已更名为 iosql web」提示后正常运行。
4. `pip install "iosql[ui]"` 语义不破（extra 双写）。

## 边界

- 历史文档（docs/design/ui-2026*.md、docs/dev/PLAN-055/064 等与 docs/review/*）为
  当时记录，保留原名；以本计划为准。
- Python 级旧导入路径 `iosql.ui` 不再保留（UI 功能发布约一个月、调用方仅本仓库
  测试与文档，已同步更名）；如后续有外部依赖再议 shim。
