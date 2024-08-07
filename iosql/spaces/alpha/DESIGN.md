# 设计原则
* 接口稳定，增量增加
* 所有抽象类都不应放根目录(开发可以) Database 可放根目录

## 数据格式
date[timestamp]: int(timestamp * 1000 * 1000)
date[timestamp-1M]

## Partition设计
timestamp -> date(YYYY): 2020    [2020-01-01 00:00:00, 2020-01-02:00:00)
timestamp -> date(YYYYMM): 202001 [2020-01-01 00:00:00, 2020-02-01:00:00)
int -> int(1000): 1920     -> [1920*1000, 1920*1000 + 1000)
int -> float(1000): 1920.0     -> [1920.0*1000, 1920.0*1000 + 1000)
text([AA, AZ]):   -> [AA, AB, AC, .., AZ]
text([SH60, SH69])
category/cat([1,2,3])

TS:date(YYYY)
ts:date(YYYY)
int: int(1000)
us_timestamp: int(1000)

## 使用
# TODOs
