"""金融高级：保存新闻/公告事件，并按事件时间和类型快速查询。"""

from pathlib import Path
import shutil

from iosql import Database


EXAMPLES_DIR = Path(__file__).resolve().parents[1]
DB_PATH = EXAMPLES_DIR / "runtime" / "finance" / "04_news.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    events = db.timeseries(
        "news_events",
        time="event_ts",
        primary_key=["symbol", "event_ts", "event_id"],
        columns={
            "symbol": "text",
            "event_ts": "integer",
            "event_id": "text",
            "event_type": "text",
            "source": "text",
            "title": "text",
        },
        partition_every="1day",
        target_rows_per_file=300_000,
    )

    day0 = 1772323200000
    events.write([
        {
            "symbol": "AAPL",
            "event_ts": day0 + 9 * 3_600_000,
            "event_id": "n1",
            "event_type": "news",
            "source": "wire",
            "title": "supplier capacity update",
        },
        {
            "symbol": "AAPL",
            "event_ts": day0 + 12 * 3_600_000,
            "event_id": "a1",
            "event_type": "announcement",
            "source": "exchange",
            "title": "annual meeting notice",
        },
        {
            "symbol": "MSFT",
            "event_ts": day0 + 86_400_000 + 10 * 3_600_000,
            "event_id": "n2",
            "event_type": "news",
            "source": "wire",
            "title": "cloud region expansion",
        },
    ])

    query = events.query(
        symbols="AAPL",
        date="2026-03-01",
        where={"field": "event_type", "op": "=", "value": "announcement"},
        order_by="event_ts desc",
    )

    print("事件分区:", events.table_parts())
    print("扫描分区:", query.explain().scanned_partitions)
    print("AAPL 当日公告:", query.to_list())
    db.close()


if __name__ == "__main__":
    run()
