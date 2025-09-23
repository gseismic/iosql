# iosql
sqlite wrapper for time series data supporting datetime-based partition 

## Usage
```py
import arrow
from iosql import Database

db = Database('sqlite://database/finance.sqlite3')
d = arrow.get('2020-01-01')
min_id, max_id = 0, 60*12*2*3

data = []
for i in range(min_id, max_id):
    date = d.shift(minutes=i)
    dic = {'id': i, 'date': date.timestamp(), 'iii': i*i*i}
    # print(i, dic)
    data.append(dic)

db['demo'].insert_all(data, pk=['id','date'], 
                       replace=True, 
                       partition='TStoYYYYMMDD(date)')
print(f"{db['demo'].table_parts()=}")
# assert len(db['demo'].table_parts()) == 3

values = db['demo'].select(
    'id,date', 
    where={'field': 'id', 'op': '<', 'value': 3},
    to_dict=True
)
print(values, len(values))
assert len(values) == 3

values = db['demo'].select(
    'id,date', 
    where={'field': 'date', 'op': '<', 'value': arrow.get('2020-01-02').timestamp()}
)
print([dict(item) for item in values], len(values))

values = db['demo'].select(
    'id,date', 
    where={'field': 'date', 'op': 'between_and', 
           'value':
           [arrow.get('2020-01-02 00').timestamp(),
            arrow.get('2020-01-02 01').timestamp()]}
)
print([dict(item) for item in values], len(values))
print(arrow.get(dict(values[0])['date']))
print(arrow.get(dict(values[-1])['date']))
```

## Notes
- 被作为partition关键字的时间必须以timestamp*1000方式表示输入, `[{'datatime': arrow.utcnow().timestamp()*1000, ...}]`

## KnownIssue
- 时间戳转为YYYYMMDDHH后，如果写入地和读取地时区不一致，可能导致错误的partition分区，应该放弃这种数据分区方法

## TODOs
- better data-query design

## KnownIssues
- [@2024-03-30 22:24:07] 不能自动区分大小写，应检测{'u': 1, 'U': 2}

# ChangeLog
- [@2023-10-21 15:57:35] v0.1.0
- [@2023-10-23 15:37:25] v0.1.1 Add `TStoTSx1000`-like and `KTStoTSx1000`-like partition
- [@2023-10-25 00:45:58] v0.1.2 db[table].create_index
