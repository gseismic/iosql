# iosql Web 审查与界面优化结果

- 完成时间：2026-11-05 20:55（Asia/Shanghai）
- 执行模型：环境说明为 GPT-6，未提供可核实的完整模型 ID，沿用 `model-unknown`。
- 分析与修改范围：`WORKTREE`，基准提交 `47b8c796945aad7a710f11b93c77f88c5a2b6c00`；相关未提交文件为 `iosql/web/static/` 下的应用资源、`tests/test_web_browser.py`、`README.md`、`skills/iosql-usage/references/ui.md`、本计划及结果、`docs/plans/INDEX.md` 和 `docs/design/web-ui-20261105-review.md`。结果尚未固定为提交。

## 完成情况

- 沿用已确认的 Overview / Data / Structure / Charts / Maintain 工作区及暗色 tokens。修复 Data 切页后表格消失、快捷翻页作用域和刷新计数问题；加载、空结果与失败提供明确反馈，旧请求不会覆盖新页面，切离时取消分页和图表流式请求。
- 修复 DOM 构造器对对象式样式的处理，分区条按真实行数显示比例；仅为能够根据 duration 契约准确计算边界的分区提供跳转，使用左闭右开范围。大量分区分批显示，总览迷你图最多渲染 60 柱。
- 总览在统计不完整时不显示全库总数，失败可重试；显示来自 segment 统计的时间范围，以“分布均衡/偏斜”替代可能被误认为数据质量的“健康”，单分区不作比较。界面说明现有行数缓存可能延迟约 1 分钟。
- 增加窄屏表列表抽屉和可滚动标签栏，改善文字对比度、键盘访问、单元格详情和确认弹窗焦点；只读模式的 Maintain 显示开启方式，服务端写路由隔离及危险操作确认不变。
- 时间输入统一：无时区日期及日期时间按浏览器本地时间，带时区 ISO 和毫秒时间戳表示绝对时间；拒绝非法日期、夏令时跳变中不存在的本地时间及非 ISO 日期格式。
- 更新使用说明和设计文档。纯日期从原来的 UTC 零点改为本地零点，旧版浏览器宽松接受的斜杠日期等不再接受，属于有意的兼容行为变化。

## 验证结果

- `python -m pytest -q -rs tests/test_web_browser.py tests/test_web.py tests/test_plan062_web.py tests/test_chart_pushdown.py tests/test_distribution.py tests/test_plan072_fixes.py tests/test_plan084_migration_review.py tests/test_plan093_resources.py`：191 通过，1 跳过；跳过项 `tests/test_plan093_resources.py:286` 需要独立 `POSTGRES_DSN`。输出另有依赖及已有接口弃用警告。
- 加入最后一项触屏目标尺寸用例后，单独运行 `python -m pytest -q tests/test_web_browser.py`：15 通过。覆盖真实浏览器的筛选、分页和返回、失败重试、慢响应、请求取消、分区边界、1205 分区、特殊与长名称、图表、空态、时序及截面查询、两种时区、键盘焦点和可写确认；触屏测试使用 Chromium 的移动设备模拟。
- Chromium 中检查 1440×900 和 390×844 的界面与图表；所有前端 JS 通过 `node --check`，`git diff --check` 通过。验证使用临时示例库，没有操作用户数据库。

## 计划偏差与边界

计划实施中补充了请求生命周期清理、时间解析一致性和触屏目标验证；原因及行为变化已记录在计划。没有修改数据库 API、写操作权限或图表原始数据语义。只有可精确推导的 duration 分区提供数据跳转，其他分区保持展示；真实移动设备和其他浏览器未进行实机验证。独立 PostgreSQL 资源测试因环境未配置而未运行。

未执行 Git commit 或 push。
