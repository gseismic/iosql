"""写入 K 线 OHLCV，查询时间窗口与最新截面。

运行：在本文件所在目录执行 python 01_kline_bars.py。
准备：已安装 iosql 及其默认依赖。
预期：输出写入行数、窗口 K 线和最新价格。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database


EXAMPLE_DIR = Path(__file__).resolve().parent
DB_PATH = EXAMPLE_DIR / "runtime" / uuid4().hex / "01_kline.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        bars = db.timeseries(
            "kline_1m",
            time="ts",
            pk=["symbol", "timeframe", "ts"],
            columns={
                "symbol": "text",
                "timeframe": "text",
                "ts": "integer",
                "open": "float",
                "high": "float",
                "low": "float",
                "close": "float",
                "volume": "float",
            },
            partition_every="1day",
        )

        base_ts = 1735689600000
        rows = []
        for minute in range(6):
            rows.append({
                "symbol": "AAPL",
                "timeframe": "1m",
                "ts": base_ts + minute * 60_000,
                "open": round(187.0 + minute * 0.1, 2),
                "high": round(187.2 + minute * 0.1, 2),
                "low": round(186.8 + minute * 0.1, 2),
                "close": round(187.1 + minute * 0.1, 2),
                "volume": 10_000 + minute * 100,
            })
            rows.append({
                "symbol": "MSFT",
                "timeframe": "1m",
                "ts": base_ts + minute * 60_000,
                "open": round(420.0 + minute * 0.2, 2),
                "high": round(420.4 + minute * 0.2, 2),
                "low": round(419.7 + minute * 0.2, 2),
                "close": round(420.2 + minute * 0.2, 2),
                "volume": 8_000 + minute * 80,
            })

        result = bars.write(rows)
        query = bars.query(
            symbols="AAPL",
            start=base_ts + 2 * 60_000,
            end=base_ts + 4 * 60_000,
            order_by="ts",
        )
        snapshot = bars.latest(
            symbols=["AAPL", "MSFT"],
            as_of=base_ts + 4 * 60_000,
            by=["symbol", "timeframe"],
            columns=["symbol", "timeframe", "ts", "close"],
        ).to_list()

        print("写入行数:", result.rows_seen)
        print("扫描分区:", query.explain().scanned_partitions)
        print("窗口 K 线:", query.to_list())
        print("截面最新价:", snapshot)


if __name__ == "__main__":
    run()
