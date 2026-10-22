# PLAN-093 最终验证证据

所有写入均使用自动清理的临时库。真实 PostgreSQL 为本机独立 PG14.22 测试实例；没有访问业务库。

| 文件 | 内容 |
| --- | --- |
| `pytest-output.txt` | 最终全量回归、19 项跳过原因及最慢用例；1111 通过、0 失败 |
| `pytest.xml` | 每一项用例的机器可读结果，含新增 90 项回归及 wheel 隔离冒烟 |
| `ruff-output.txt` | 生产代码、CLI、示例、benchmark 与新增回归静态检查 |
| `build-output.txt` | 最终源码 wheel 构建输出 |
| `verification.py` | 可重复运行的语法、资产一致性、执行计数与迁移显式 append 检查 |
| `verification.json` | Python/SQLite/DuckDB/PG 版本、源码指纹、实际执行计数及迁移检查结果 |

复现（替换本机解释器路径和独立测试库 URI）：

```bash
POSTGRES_DSN=postgresql://iosql@127.0.0.1:55478/postgres uv run --no-sync /home/lsl/miniconda3/bin/python -m pytest -q tests --tb=short -ra --durations=10
uv run --no-sync /home/lsl/miniconda3/bin/ruff check iosql cli examples benchmarks tests/test_plan093_review.py tests/test_plan093_resources.py
PYTHONPATH=. POSTGRES_DSN=postgresql://iosql@127.0.0.1:55478/postgres uv run --no-sync /home/lsl/miniconda3/bin/python docs/tests/20261023-PLAN-093/verification.py
uv build --wheel
```

执行计数不是耗时倍数。旧报告 R13 使用无控制表 mock，当前 PG 记录使用真实服务且已有控制表，因此 SQL 次数不直接横向比较；新记录的三次独立 spec 调用共创建一条控制连接，首次检查目录后每次仍取锁、读 generation、解锁。Windows 原机 6 项及废弃接口 13 项跳过，具体原因保留在日志，不计为通过。
