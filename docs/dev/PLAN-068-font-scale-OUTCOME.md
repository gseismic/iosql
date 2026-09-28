# PLAN-068 实施结果：Web 界面字体调大 + 字号缩放开关

- 日期：2026-09-28
- 计划：`docs/dev/PLAN-068-font-scale.md`
- 结论：**完成，测试通过，真实库三档缩放逐屏验收通过**

## 变更（iosql/web/static/）

1. **默认字号整体上调一档**（+1px 映射，防链式替换脚本执行）：
   正文 13.5→14.5px、表格单元格 12→13px、表头 12.5→13.5px、
   次级标注 11→12px、微标签 11→12px、过滤输入 11→12px、
   徽章 10.5→11px、侧栏表名 13→14px 等；CSS 与 JS 内联 fontSize 全覆盖；
   大数字（20/24/26px）与图标字号保持不变。间距体系未动。
2. **状态栏字号缩放开关**：`A− / 标准 / A+` 三档（body zoom 1 / 1.1 / 1.2），
   localStorage `iosql_web_font_scale` 持久化，boot 时应用；默认落"标准"（1.1）。
3. **zoom 与视口单位冲突修复**：`body { zoom: var(--fs-zoom) }` 会放大
   `100vh`，导致 `#layout` 高度溢出、状态栏被挤出视口；引入 `--fs-zoom`
   CSS 变量，`#layout` 高度与 `.table-wrap` max-height 按变量除回原始值。
4. **布局随动**：侧栏 252→272px、列选择器面板 320→340px、
   `.side-meta` 允许折行，适配更大字号。

## 验证

- `node --check` 全部 JS；`pytest tests/test_web.py tests/test_plan062_web.py
  tests/test_cli.py` → **108 passed**。
- 真实库（kline_factor_iosql）浏览器验收：
  - 默认档（标准 1.1）：全站字号明显大于改前，状态栏回入视口；
  - A+ 档（1.2）：Data 表格/工具栏/分段控件/分页条不破版，
    刷新后档位保持（持久化生效）；
  - 侧栏表名与 meta 在新字号下折行自然。

## 边界

- 未做 px → rem 体系重构（zoom 方案已覆盖需求，churn 小）；
  Firefox 126+ 支持 zoom，旧浏览器下开关退化为无效果（不影响默认档）。
