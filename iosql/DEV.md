# sqlutil
SQL utils (sqlite, mysql)

统一接口处理各类格式的数据、并可以相互转化
以数据存取为目的 sql操作

# 定位
文件系统，而非分布式系统
如果需要多进程/线程访问，使用clickhouse, redis等

# 设计
* 与`sqlite_utils`常用 python接口保持一致

# 不是什么
- [x] 不是应用于复杂的表间关系

# 使用说明
* `insert_all` 插入数据 partition/pk 不能改变，否则系统会检测到，raise
* 时间主要耗时在`partition_data`上，这里可以不用arrow.get这个形式
* columns: {'x': 'integer', 'y':float} 类型, fields 指: ['x', 'y'] 类型 (e.g. where)
* 统一用小写

# TODOs
- [ ] 扩展fn-partition, fn, {'date': {'min': 'xxx', 'max': 'xxx'}, 'symbol': {'min': 'SZ0000', 'max': 'SZ999999'}}
- [ ] 整理形成新的文件夹 reconstruct folder
- [ ] cache 查询 [要考虑数新插入，有更新问题]

# 稳定的接口和参数
* `insert_all([pk, alter, partition, replace, ignore, extra_data, batch_size, n_suggest_check])`
* insert 默认`n_suggest_check` None:  检查全部的数据类型，找到最宽松的类型
* 数据存放目录稳定: database.sqlite3/meta.sqlite3, database.sqlite3/data/part{i}.sqlite3
* 默认使用WAL数据安全考虑
* `select_all` 通过测试

# BugFixed
- [x] 修改了`insert_all`特性 保证了dict顺序不一致时，也能得到正确结果, 缺失的用default-value/None代替
- [x] 处理pk 为None 情况 [@2021-11-30 12:57:24]
- [x] `reorder_by_column_order` [@2021-12-01 15:39:33] 

# ChangeLog
* [@2021-11-28 00:26:42] v0.2.0 `insert_all` mysqlite 被几乎全面的测试，可用, iosql: `insert_all` 数据可用
* [@2021-11-28 02:15:54] `select_all` 常规使用通过测试（复杂语法未考虑)
* [@2021-11-28 04:10:39] BugFixed 解决了parts过多，没有关闭的问题，句柄用完的问题 （目前默认使用句柄不会超过20个)
* [@2021-11-29 12:10:34] partition=None 时处理, 重写了column
* [@2021-11-30 09:27:13] v0.3.0 解决mysqlite cursor管理问题
* [@2021-11-30 12:57:41] fix `pk` bug
* [@2021-12-01 15:39:46] fix bug
* [@2021-12-05 11:11:34] add: select
