"""写入 Parquet 表，重开后查询。

运行：在本文件所在目录执行 python 14_parquet_backend.py。
准备：已安装 iosql 及其默认依赖。
预期：输出两条 AAPL 行情。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database


RUNTIME = Path(__file__).resolve().parent / 'runtime'
PARQUET_PATH = RUNTIME / uuid4().hex / '14_parquet_quickstart.iosql'
BASE_TIME = 1735689600000


def run():
    uri = f'parquet://{PARQUET_PATH}'
    with Database(uri) as db:
        bars = db.timeseries(
            'bars', time='ts', pk=['symbol', 'ts'],
            columns={'symbol': 'text', 'ts': 'integer', 'close': 'float'},
            partition_every='1day',
        )
        bars.write([
            {'symbol': 'AAPL', 'ts': BASE_TIME, 'close': 187.0},
            {'symbol': 'MSFT', 'ts': BASE_TIME + 1, 'close': 410.0},
            {'symbol': 'AAPL', 'ts': BASE_TIME + 86_400_000, 'close': 188.0},
        ], mode='ignore')

    # Parquet 库保存表契约，重开时无需重复声明字段。
    with Database(uri) as db:
        bars = db.table('bars')
        rows = bars.query(symbols='AAPL', order_by='ts').to_list()
        print('重新打开后的 AAPL 行情:', rows)


if __name__ == '__main__':
    run()
