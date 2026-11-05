# iosql 可视化管理台（iosql web）

本地 Web 管理台：只读浏览 + 受控维护，无需写代码即可查看表契约、分区布局、物理 segment 和数据样本。

```bash
python -m pip install -e ".[web]"  # fastapi + uvicorn（可选 extra）
iosql web -s database/market.iosql              # 默认只读，http://127.0.0.1:8686
iosql web -s database/archive.iosql             # 自动识别 Parquet；支持 Charts
iosql web -s database/market.iosql --write      # 开启维护操作
```

其他参数：`--host`（默认 127.0.0.1）、`--port`（默认 8686）、`--no-browser`。
绑定非 loopback `--host` 时会打印暴露警告；绑定具体非回环地址时该地址加入 Host 白名单，绑定 `0.0.0.0` 时必须显式指定 `--allowed-host`。
Python 侧入口是 `iosql.web.create_app(source_uri, mode="readonly"|"write")`（惰性导入，未装依赖时抛带安装指引的 ImportError）。`.[ui]` 仍是兼容安装别名。

| 工作区 | 只读 | 可修改 | 说明 |
|:--|:-:|:-:|---|
| Overview | ✅ | ✅ | 全库概览：表清单、行数、分区数、segment 数、partition 策略 |
| Data | ✅ | ✅ | Browse 与 Query 合并后的工作区：表头每列过滤行（裸词精确 / `>` 比较 / `a~b` 范围 / 逗号多值，防抖即输即查）+ 条件构造器（AND/OR，与表头条件叠加）+ keyset 翻页 + 列头排序 + Explain 裁剪可视化 + 时序表支持 start/end/date/session/latest + 单元格详情 + CSV/JSONL 导出 |
| Structure | ✅ | ✅ | 表契约 / 逻辑分区 / 物理 segment 三层透视 |
| Charts | ✅ | ✅ | 可选 X/Y、分组和筛选条件，逐行流式绘制原始点；最多 32 组 |
| Maintain | 查看开启方式 | ✅ | layout_report → compact → validate → cleanup_obsolete 步骤流；重分区 |

安全模型：

- 只读模式下服务端**不注册任何写路由**（写端点不存在而非被拒绝）。
- 可修改模式下 truncate/delete/cleanup_obsolete/repartition 等危险操作均需手输表名确认；
  重分区没有 dry-run 预览，确认后直接写入新库。
- 默认只信任本机 Host（`localhost`/`127.0.0.1`/`[::1]`），非回环访问需匹配配置的 Host 白名单。
- 定位是本机 localhost 工具，**不要暴露到公网**；只读模式下 Maintain 显示开启方式，不显示写操作。

启动时只读取库和表清单，进入 Data 再读取表契约及当前数据页。总览的「统计本表」/「统计全部表」计算行数等摘要，
「加载分布」读取指定表的物理 segment 统计；Data 的「统计总行数」计算当前筛选结果。进入 Structure 时按需读取分区和物理 segment 明细。
这些统计不会在进入 Data 时自动执行；旧式 SQLite 分区可能需要扫描数据文件。统计失败可重试，不完整时不显示全库合计；行数可能受服务端 60 秒缓存影响。窄屏使用顶栏「表」按钮切换表列表。
Data 的表头筛选、可见列和当前视图保存在地址中，切换工作区再返回时保持一致；高级条件、排序及页位不包含在链接中。Structure 对可确定边界的毫秒 duration 分区提供跳转，包括 `6hour` 这类固定时长，跳转条件为左闭右开区间。
时序查询、截面和图表支持 `YYYY-MM-DD`、`YYYY-MM-DD HH:mm[:ss]`、带时区的 ISO 日期时间和毫秒时间戳；无时区的输入按浏览器本地时间解析。纯日期相对旧版从 UTC 零点改为本地零点，斜杠日期等非 ISO 格式不再接受。
无筛选行数统计优先使用已持久化的 segment 行数；旧式分区需要计算各文件行数。带筛选计数为流式查询，
单次迭代执行时间预算为 5000ms，超时返回 422 并提示缩小筛选范围或添加分区/时间条件。
数据行按 keyset 分页按需读取，排序列含 NULL 时返回 422 并禁用该列排序（避免静默漏行）。
Data 导出为流式下载，文件名兼容非 ASCII（RFC 5987），CSV 自动转义公式注入前缀，空结果也输出表头。
Charts 的 `POST /chart` 以 NDJSON 流式返回筛选范围内的全部原始记录；分组可选，
null 指标值显示为断点，数值或毫秒时间轴缩放后重新读取可视范围。大表应先筛选时间和标的。
