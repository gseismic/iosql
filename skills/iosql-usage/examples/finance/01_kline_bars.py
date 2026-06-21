"""金融入门：写入 K 线 OHLCV，并按标的和时间段查询。"""

from pathlib import Path
import shutil

from iosql import Database


EXAMPLES_DIR = Path(__file__).resolve().parents[1]
DB_PATH = EXAMPLES_DIR / "runtime" / "finance" / "01_kline.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    bars = db.timeseries(
        "kline_1m",
        time="ts",
        primary_key=["symbol", "timeframe", "ts"],
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
        target_rows_per_file=400_000,
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
    db.close()


if __name__ == "__main__":
    run()
