# PLAN-033-remove-compat-packages-and-bump-version-OUTCOME

## 结果

已删除旧兼容入口，重组测试目录，并将版本号更新到 `0.2.4`。

## 删除的旧入口

以下包已从源码中删除，不再作为兼容转发层保留：

```text
iosql.query
iosql.sqlite
iosql.postgresql
```

当前入口为：

```text
iosql.core.query
iosql.engines.sqlite
iosql.engines.postgresql
```

本地验证：

```text
iosql.__version__ == 0.2.4
import iosql.query       -> ModuleNotFoundError
import iosql.sqlite      -> ModuleNotFoundError
import iosql.postgresql  -> ModuleNotFoundError
```

## 测试目录重组

测试目录已按代码结构重组：

```text
tests/core/query
tests/engines/sqlite
tests/engines/postgresql
tests/utils/mysqlite
```

主要移动：

```text
tests/query/*              -> tests/core/query/*
tests/sqlite/*             -> tests/engines/sqlite/*
tests/test_postgresql.py   -> tests/engines/postgresql/test_postgresql.py
```

测试 import 已同步更新：

```python
from iosql.core.query...
from iosql.engines.postgresql import Table
```

## 文档更新

已更新：

- `README.md`
- `docs/design/PLAN-030-partitioned-sqlite-final-design.md`
- `docs/dev/PLAN-032-query-correctness-and-layout.md`
- `docs/dev/PLAN-032-query-correctness-and-layout-OUTCOME.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/troubleshooting.md`

## 验证

已通过：

```bash
python -m compileall iosql tests/core tests/engines -q
python -m pytest tests/core/query tests/engines/sqlite tests/engines/postgresql -q
python -m pytest tests/utils/mysqlite -q
git diff --check
```

结果：

```text
tests/core/query tests/engines/sqlite tests/engines/postgresql:
97 passed, 13 skipped

tests/utils/mysqlite:
41 passed
```

备注：工作区中 `iosql/DESIGN.md` 和 `iosql/DEV.md` 的删除在本轮开始前已经存在，本轮未恢复也不纳入本次提交。
