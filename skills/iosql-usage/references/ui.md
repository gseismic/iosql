# iosql 可视化管理台（iosql web）

本地 Web 管理台：只读浏览 + 受控维护，无需写代码即可查看表契约、分区布局、物理 segment 和数据样本。

```bash
python -m pip install -e ".[ui]"   # fastapi + uvicorn（可选 extra）
iosql web -s database/market.iosql              # 默认只读，http://127.0.0.1:8686
iosql web -s database/market.iosql --write      # 开启维护操作
```

其他参数：`--host`（默认 127.0.0.1）、`--port`（默认 8686）、`--no-browser`。
绑定非 loopback `--host` 时会打印暴露警告，服务端只信任 `localhost`/`127.0.0.1`/`[::1]` 的 Host 头。
Python 侧入口是 `iosql.ui.create_app(source_uri, mode="readonly"|"write")`（惰性导入，未装依赖时抛带安装指引的 ImportError）。

| 工作区 | 只读 | 可修改 | 说明 |
|:--|:-:|:-:|---|
| Overview | ✅ | ✅ | 全库概览：表清单、行数、分区数、segment 数、partition 策略 |
| Browse Data | ✅ | ✅ | 表头每列过滤行（裸词精确 / `>` 比较 / `a~b` 范围 / 逗号多值，防抖即输即查）+ 条件构造器（AND/OR，与表头条件叠加）+ keyset 翻页 + 列头排序 + 时序表默认最新优先 + 毫秒时间戳可读化（可开关）+ 单元格详情（可一键以此值过滤）+ CSV/JSONL 导出 |
| Structure | ✅ | ✅ | 表契约 / 逻辑分区 / 物理 segment 三层透视 |
| Query | ✅ | ✅ | where 查询 + explain 裁剪可视化；时序表支持 start/end/date/session/latest 截面 |
| Charts | ✅ | ✅ | 时序快览折线图（服务端降采样） |
| Maintain | ❌ | ✅ | layout_report → compact → validate → cleanup_obsolete 步骤流；重分区 |

安全模型：

- 只读模式下服务端**不注册任何写路由**（写端点不存在而非被拒绝）。
- 可修改模式下 truncate/delete/cleanup_obsolete/repartition 等危险操作均需手输表名确认；
  重分区没有 dry-run 预览，确认后直接写入新库。
- 服务端只信任本机 Host（`localhost`/`127.0.0.1`/`[::1]`），绑定非 loopback `--host` 时 CLI 打印暴露警告。
- 定位是本机 localhost 工具，**不要暴露到公网**；排查 UI 问题时优先怀疑模式没开对（只读看不到 Maintain）。

启动为**非阻塞渐进加载**：首屏只读 manifest 级元数据（毫秒级），行数/分区数/segment 数由前端逐表异步拉取填充，
大库（HDD/冷缓存/网络盘上的大量 segment 文件）打开不再被全库统计阻塞。
无筛选行数统计走 manifest 求和零 I/O 路径（2000 万行 8ms）；带筛选计数为流式扫描，
超过 5,000,000 行扫描预算返回 422 提示缩小范围。
数据行按 keyset 分页按需读取，排序列含 NULL 时返回 422 并禁用该列排序（避免静默漏行）。
Browse Data 导出为流式下载，文件名兼容非 ASCII（RFC 5987），CSV 自动转义公式注入前缀，空结果也输出表头。
