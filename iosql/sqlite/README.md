# SQLite compatibility package

`iosql.sqlite` 现在只是兼容转发层。

真实 SQLite 引擎实现位于：

```python
iosql.engines.sqlite
```

通用查询 AST 和 where 解析位于：

```python
iosql.core.query
```
