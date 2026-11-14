"""按月度日历分区保存财报并按披露日期查询。

运行：在本文件所在目录执行 python 03_financial_reports.py。
准备：已安装 iosql 及其默认依赖。
预期：输出月度分区、扫描计划和二月财报。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database, Partition


EXAMPLE_DIR = Path(__file__).resolve().parent
DB_PATH = EXAMPLE_DIR / "runtime" / uuid4().hex / "03_reports.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        reports = db.timeseries(
            "financial_reports",
            time="published_date",
            partition=Partition.calendar(
                "published_date",
                every="1month",
                timezone="Asia/Shanghai",
            ),
            pk=["symbol", "fiscal_period", "statement"],
            columns={
                "symbol": "text",
                "published_date": "text",
                "fiscal_period": "text",
                "statement": "text",
                "revenue": "float",
                "net_income": "float",
            },
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


if __name__ == "__main__":
    run()
