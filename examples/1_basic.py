import arrow
from iosql import Database


def run():
    db = Database('sqlite://database/finance.iosql')
    d = arrow.get('2020-01-01')
    min_id, max_id = 0, 60*12*2*3

    data = []
    for i in range(min_id, max_id):
        date = d.shift(minutes=i) 
        dic = {'id': i, 'date': date.timestamp()*1000, 'iii': i*i*i}
        # print(i, dic)
        data.append(dic)

    db['demo'].insert_all(data, pk=['id','date'], 
                           replace=True, 
                           partition='KTStoYYYYMMDD(date)')
    print(f"{db['demo'].table_parts()=}")
    # assert len(db['demo'].table_parts()) == 3

    values = db['demo'].select(
        'id,date', 
        where={'field': 'id', 'op': '<', 'value': 3},
        to_dict=True
    )
    print(values, len(values))
    # raise
    print(values, len(values))
    assert len(values) == 3

    values = db['demo'].select(
        'id,date', 
        where={'field': 'date', 'op': '<', 'value': arrow.get('2020-01-02').timestamp()*1000}
    )
    print([dict(item) for item in values], len(values))

    values = db['demo'].select(
        'id,date', 
        where={'field': 'date', 'op': 'between_and', 
               'value': 
               [arrow.get('2020-01-02 00').timestamp()*1000,
                arrow.get('2020-01-02 01').timestamp()*1000]}
    )
    # print(values)
    # raise 
    print([dict(item) for item in values], len(values))
    print(arrow.get(dict(values[0])['date'])) 
    print(arrow.get(dict(values[-1])['date'])) 


if __name__ == '__main__':
    run()
