"""金融进阶：用日期/日历分区保存财报指标，并按披露日期查询。"""

from pathlib import Path
import shutil

from iosql import Database


EXAMPLES_DIR = Path(__file__).resolve().parents[1]
DB_PATH = EXAMPLES_DIR / "runtime" / "finance" / "03_reports.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def reset_database():
    shutil.rmtree(DB_PATH, ignore_errors=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(DB_URI)
    reports = db.timeseries(
        "financial_reports",
        time="published_date",
        time_unit="date",
        partition_kind="calendar",
        partition_every="1month",
        timezone="Asia/Shanghai",
        primary_key=["symbol", "fiscal_period", "statement"],
        columns={
            "symbol": "text",
            "published_date": "text",
            "fiscal_period": "text",
            "statement": "text",
            "revenue": "float",
            "net_income": "float",
        },
        symbol_column="symbol",
    )

    reports.write([
        {
            "symbol": "AAPL",
            "published_date": "2026-01-31",
            "fiscal_period": "2025Q4",
            "statement": "income",
            "revenue": 124_300.0,
            "net_income": 36_300.0,
        },
        {
            "symbol": "MSFT",
            "published_date": "2026-02-02",
            "fiscal_period": "2026Q2",
            "statement": "income",
            "revenue": 69_600.0,
            "net_income": 24_100.0,
        },
        {
            "symbol": "NVDA",
            "published_date": "2026-02-25",
            "fiscal_period": "2026Q4",
            "statement": "income",
            "revenue": 39_300.0,
            "net_income": 22_100.0,
        },
    ])

    query = reports.query(
        start="2026-02-01",
        end="2026-02-28",
        order_by="published_date",
    )

    print("月度分区:", reports.table_parts())
    print("扫描分区:", query.explain().scanned_partitions)
    print("2 月披露财报:", query.to_list())
    db.close()


if __name__ == "__main__":
    run()
