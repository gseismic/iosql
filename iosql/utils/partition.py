from collections import defaultdict
from datetime import datetime
import arrow


def get_part_range(part, partition):
    fn_name, column = parse_partition(partition)
    start_dt, end_dt = None, None
    if fn_name is None:
        return start_dt, end_dt

    if fn_name in ['toYYYY', 'TStoYYYY', 'KTStoYYYY']:
        start_dt, end_dt = arrow.get(f'{part}-01').span('year')
    elif fn_name in ['TStoYYYYWW', 'KTStoYYYYWW']:
        year, week = int(part[:4]), int(part[4:6])
        start_dt = arrow.get(datetime.fromisocalendar(year, week, 1))
        end_dt = arrow.get(datetime.fromisocalendar(year, week, 7))
    elif fn_name == 'toYYYYMM' or fn_name in ['TStoYYYYMM', 'KTStoYYYYMM']:
        start_dt, end_dt = arrow.get(f'{part[:4]}-{part[4:6]}').span('month')
    elif fn_name == 'toYYYYMMDD' or fn_name in ['TStoYYYYMMDD', 'KTStoYYYYMMDD'] or fn_name.endswith('byDay'):
        start_dt, end_dt = arrow.get( f'{part[:4]}-{part[4:6]}-{part[6:8]}').span('day')
    elif fn_name == 'toYYYYMMDDHH' or fn_name in ['TStoYYYYMMDDHH', 'KTStoYYYYMMDDHH'] :
        start_dt, end_dt = arrow.get(f'{part[:4]}-{part[4:6]}-{part[6:8]}-{part[8:10]}').span('hour')
    elif fn_name.startswith('TStoTSx') or fn_name.startswith('KTStoTSx'):
        multi = int(fn_name.split('x')[1])
        start_dt = arrow.get(int(part) * multi)
        end_dt = arrow.get(int(part) * multi + multi - 1/1000000.0)
    else:
        raise Exception(f'unknown partition `{partition}`')

    return start_dt, end_dt


def parse_partition(partition):
    if partition is None:
        return None, None
    # partition toYYYY('date')
    # partition toYYYYMMDDHHm10('date')
    # partition toYYYY_5y('date')
    # support: toYYYY5 2000 每年5年
    # support: toYYYY 2000 每年
    # support: toYYYYMM 200010
    # support: toYYYYMMDD 20001013
    # support: toYYYYMMDDHH 2000101323
    # support: toYYYYMMDDHH3 每3小时
    # support: toYYYYMMDDHHMM10 每10分钟
    p1 = partition.find('(')
    p2 = partition.find(')')
    if p1 == -1 or p2 == -1:
        raise Exception('BadPartitionKey')
    fn_name = partition[:p1]
    column = partition[p1+1:p2].strip('"').strip("'").strip()
    return fn_name, column

def _partition_timestamp_div(partition, ts):
    # partition, KTStoTSx{1000}
    multi = int(partition.split('x')[1])
    return int(ts) // multi

def _partition_timestamp(partition, ts):
    dt = datetime.fromtimestamp(ts)
    if partition in ['toYYYYWW']:
        c = dt.isocalendar()
        return {'toYYYYWW': '%d%02d' % (c[0], c[1])}[partition]
    else:
        return {
            #'byYear': '%d' % dt.year,
            #'byMonth': '%d%02d' % (dt.year, dt.month),
            #'byWeek': '%d%02d' % (dt.isocalendar[0],dt.isocalendar[0]),
            #'byDay': '%d%02d%02d' % (dt.year, dt.month, dt.day),
            #'byHour': '%d%02d%02d%02d' % (dt.year, dt.month, dt.day, dt.hour),
            #'byMinute': '%d%02d%02d%02d%02d' % (dt.year, dt.month, dt.day, dt.hour, dt.minute),
            'toYYYY': '%d' % dt.year,
            'toYYYYMM': '%d%02d' % (dt.year, dt.month),
            'toYYYYMMDD': '%d%02d%02d' % (dt.year, dt.month, dt.day),
            'toYYYYMMDDHH': '%d%02d%02d%02d' % (dt.year, dt.month, dt.day, dt.hour),
            'toYYYYMMDDHHmm': '%d%02d%02d%02d%02d' % (dt.year, dt.month, dt.day, dt.hour, dt.minute)
        }[partition]


_BUILTIN_PARTITION_FN_DICT = {
    'toYYYY': lambda d: '%s' % d[:4],
    'toYYYYMM': lambda d: '%s%s' % (d[:4], d[5:7]),
    'toYYYYMMDD': lambda d: '%s%s%s' % (d[:4], d[5:7], d[8:10]),
    'toYYYYMMDDHH': lambda d: '%s%s%s%s' % (d[:4], d[5:7], d[8:10], d[11:13]),
    'toYYYYMMDDHHmm': lambda d: '%s%s%s%s%s' % (d[:4], d[5:7], d[8:10], d[11:13], d[14:16]),
    #'TSbyYear': lambda d: _partition_timestamp('byYear', d),
    #'KTSbyYear': lambda d: _partition_timestamp('byYear', d/1000),
    #'TSbyWeek': lambda d: _partition_timestamp('byWeek', d),
    #'KTSbyWeek': lambda d: _partition_timestamp('byWeek', d/1000),
    #'TSbyMonth': lambda d: _partition_timestamp('byMonth', d),
    #'KTSbyMonth': lambda d: _partition_timestamp('byMonth', d/1000),
    #'TSbyDay': lambda d: _partition_timestamp('byDay', d),
    #'KTSbyDay': lambda d: _partition_timestamp('byDay', d/1000),
    #'TSbyHour': lambda d: _partition_timestamp('byHour', d),
    #'KTSbyHour': lambda d: _partition_timestamp('byHour', d/1000),
    #'TSbyMinute': lambda d: _partition_timestamp('byMinute', d),
    #'KTSbyMinute': lambda d: _partition_timestamp('byMinute', d/1000),
    'TStoYYYY': lambda d: _partition_timestamp('toYYYY', d),
    'KTStoYYYY': lambda d: _partition_timestamp('toYYYY', d/1000),
    'TStoYYYYMM': lambda d: _partition_timestamp('toYYYYMM', d),
    'KTStoYYYYMM': lambda d: _partition_timestamp('toYYYYMM', d/1000),
    'TStoYYYYWW': lambda d: _partition_timestamp('toYYYYWW', d),
    'KTStoYYYYWW': lambda d: _partition_timestamp('toYYYYWW', d/1000),
    'TStoYYYYMMDD': lambda d: _partition_timestamp('toYYYYMMDD', d),
    'KTStoYYYYMMDD': lambda d: _partition_timestamp('toYYYYMMDD', d/1000),
    'TStoYYYYMMDDHH': lambda d: _partition_timestamp('toYYYYMMDDHH', d),
    'KTStoYYYYMMDDHH': lambda d: _partition_timestamp('toYYYYMMDDHH', d/1000),
    'TStoYYYYMMDDHHmm': lambda d: _partition_timestamp('toYYYYMMDDHHmm', d),
    'KTStoYYYYMMDDHHmm': lambda d: _partition_timestamp('toYYYYMMDDHHmm', d/1000),
}


# https://xin053.github.io/2016/07/04/arrow%E6%97%B6%E9%97%B4%E5%BA%93%E4%BD%BF%E7%94%A8%E8%AF%A6%E8%A7%A3/
def get_partition_fn(partition, use_builtin=True):
    fn = None
    fn_name, column = parse_partition(partition)

    if use_builtin:
        if fn_name.startswith('TStoTSx'):
            fn = lambda d: _partition_timestamp_div(fn_name, d)
        elif fn_name.startswith('KTStoTSx'):
            fn = lambda d: _partition_timestamp_div(fn_name, d//1000)
        else:
            fn = _BUILTIN_PARTITION_FN_DICT[fn_name]
    else:
        if fn_name in ['toYYYY', 'toYYYYMM', 'toYYYYMMDD', 'toYYYYMMDDHH', 'toYYYYMMDDHHmm']:
            _name = fn_name[2:]
            # partition 11.298309803009033
            # partition 0.10912513732910156
            fn = lambda d: arrow.get(d).format(_name)
            # 以下提速了几乎100倍
            # fn = lambda d: '%s%s' %(d[:4], d[5:7])
        else:
            raise Exception('PartitionNotFound')
    return fn, column


def partition_data(data, partition, use_builtin=True):
    if partition is None:
        partitioned_data = {str(None): data}
        return partitioned_data, str(None)

    fn_partition, column = get_partition_fn(partition, use_builtin)
    if fn_partition is None:
        raise Exception('Not supported partition')

    partitioned_data = defaultdict(list)
    for item in data:
        try:
            part = fn_partition(item[column])
            partitioned_data[part].append(item)
        except KeyError:
            raise Exception(f'partition key `{column}` Not in data')

    return partitioned_data, column
