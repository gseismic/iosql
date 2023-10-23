import config
import arrow
from datetime import datetime
from iosql.utils.column import (
    suggest_column_types,
    sqlite_types_of_data,
    sqlite_type_of_value
)
from iosql.utils.createtable import reorder_by_column_order
from iosql.utils.partition import (
    get_part_range,
    get_partition_fn,
    partition_data
)
from iosql.utils.partition import partition_data, get_part_range


def test_basic():
    assert type(1.0) == float
    assert type(1) == int
    assert type('xxx') == str
    assert type(b'xxx') == bytes
    assert int in [int]
    assert not isinstance(int, int)
    assert sqlite_type_of_value(1) == 'integer'
    assert sqlite_type_of_value(1.2) == 'float'
    assert sqlite_type_of_value('TEXT') == 'text'
    assert sqlite_type_of_value(b'bytesss') == 'blob'

    data = [
        {'a': 1, 'b': 1.2, 'c': 3},
        {'a': '2', 'b': 1.2, 'c': 3}
    ]
    print(f'{sqlite_types_of_data(data)=}')


def test_createtable_order():
    out = reorder_by_column_order({'b': 3, 'a': 9, 'd': 5},
                            ['a', 'd', 'b'])
    assert list(out.keys()) == ['a', 'd', 'b']


def test_partition():
    print(f"{get_part_range('1990', 'toYYYY(date)')=}")
    print(f"{get_part_range('1990', 'TStoYYYY(ts)')=}")
    print(f"{get_part_range('1990', 'KTStoYYYY(ts)')=}")

    my = get_part_range('199005', 'KTStoYYYYWW(ts)') 
    ref_start = datetime.fromisocalendar(1990, 5, 1)
    ref_end = datetime.fromisocalendar(1990, 5, 7)
    assert my[0] == arrow.get(ref_start)
    assert my[1] == arrow.get(ref_end)
    # assert get_part_range('199005', 'KTStoYYYYWW(ts)') == datetime.fromisocalendar(1990, 5, 1), datetime.fromisocalendar(1990, 5, 7)
    # assert get_part_range('199005', 'TStoYYYYWW(ts)') == datetime.fromisocalendar(1990, 5, 1), datetime.fromisocalendar(1990, 5, 7)

    data = [
        {'date': '2015-01-01'},
        {'date': '2016-01-01'},
        {'date': '2016-03-01'},
        {'date': '2017-01-01'},
        {'date': '2017-02-01'},
        {'date': '2017-02-03'},
        {'date': '2017-02-09'},
        {'date': '2017-02-16'},
        {'date': '2018-01-01'},
        {'date': '2019-01-01'}
    ]

    pdata, column = partition_data(data, 'toYYYY(date)')
    print(pdata)

    print('='*30)
    pdata, column= partition_data(data, 'toYYYYMM(date)')
    print(pdata)

    start_dt, end_dt = get_part_range('2015', 'toYYYY(date)')
    print(start_dt, end_dt)

    part = int(arrow.get('2000-01-01 00:00:00').timestamp()//3600)
    start_dt, end_dt = get_part_range(part, 'TStoTSx3600(date)')
    print(f'{part=}')
    print(f'{start_dt=}, {end_dt=}')

    part = int(arrow.get('2000-01-01 00:00:00').timestamp()//3600)
    start_dt, end_dt = get_part_range(part, 'TStoTSx3600(date)')

    data = [
            {'date': 10000},
            {'date': 20000},
            {'date': 30000}
           ]
    pdata, column = partition_data(data, 'TStoTSx100000(date)')
    print(pdata)

if __name__ == '__main__':
    if 1:
        test_basic()
    if 1:
        test_createtable_order()
    if 1:
        test_partition()
