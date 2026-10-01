# PLAN-079 PostgreSQL 示例的本机连接配置结果

- 日期：2026-10-02
- 状态：实现、自审与本机验证完成
- 计划：`docs/dev/PLAN-079-postgresql-local-example.md`

## 问题与处理

此前示例只认 POSTGRES_DSN；PLAN-078 验证时环境变量仅存在于测试进程，其临时数据库在结束后清理。因此用户直接运行 p3 时没有可用连接配置。

1. 新增 `examples/start_postgresql_test.sh`，复用本机 PostgreSQL 14.22，在 `examples/runtime/postgresql-test/data` 保留独立数据目录，监听 127.0.0.1:55478，使用本机无密码测试身份 iosql。重复执行只检查/启动已有目录；程序主版本不符时明确报错，不删除数据。
2. 启动后验证真实数据库连接，保存 `postgresql-test.dsn` 和可 source 的 `postgresql-test.env`。示例优先使用环境变量，没有时按脚本位置读取本地连接文件；缺少配置时给出具体启动命令并返回非零状态。
3. 更新根目录 README、示例说明与仓库 skill，逐文件同步示例和启动脚本副本；忽略两份 examples/runtime，避免提交数据目录与本机配置。
4. 本机 Python 为 p3 别名指向的 `/home/lsl/miniconda3/bin/python3` 3.12.8，iosql 与 psycopg2 已可导入。没有安装任务，无需委派 luna。

## 验证

| 检查 | 结果 |
| --- | --- |
| 在 examples 内，移除 POSTGRES_DSN 后执行 `p3 15_postgresql_timeseries_query.py` | 退出 0，时间窗、截面、字段更新、分区裁剪、实际执行指标全部通过 |
| 在仓库根目录，移除 POSTGRES_DSN 后执行 `p3 examples/15_postgresql_timeseries_query.py` | 退出 0，证明配置路径不依赖当前目录 |
| 显式 POSTGRES_DSN 下真实运行示例，同时禁止本地 Path 分支 | 校验通过，没有读取本地配置，证明环境变量优先 |
| 重复执行启动脚本 | 复用已有实例，连接验证成功 |
| 停止本机测试库，再运行启动脚本 | 原目录成功重启，未重复初始化 |
| 查询本机实例的 `iosql_example_query_%` 关系数 | 0，示例及分区清理成功 |
| 两份启动脚本 bash -n、两份示例 ruff check、git diff --check | 通过 |
| 示例、启动脚本、示例 README 与 skill 副本 cmp | 全部一致 |

这次仅调整示例连接与本机启动方式，没有修改 iosql 引擎代码；运行真实示例作为此次验收。

## 当前本机状态与后续运行

测试实例保持运行，数据与连接文件保留。本机原有 PostgreSQL 18 服务未修改；用户原有 .zcodeignore 未纳入提交。

在 examples 内直接执行：

```bash
p3 15_postgresql_timeseries_query.py
```

电脑重启后先执行 `bash start_postgresql_test.sh`。其他 PG 示例/基准先执行 `source runtime/postgresql-test.env`。本地连接是 `postgresql://iosql@127.0.0.1:55478/postgres`，用于独立本机测试。

停止命令：

```bash
/usr/lib/postgresql/14/bin/pg_ctl -D runtime/postgresql-test/data -m fast -w stop
```

计划、结果、脚本与说明一并中文提交并推送。
