"""只更新已有行的部分字段。

运行：在本文件所在目录执行 python 03_partial_upsert.py。
准备：已安装 iosql 及其默认依赖。
预期：输出价格已更新、venue 保持原值的记录。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database


def run():
    path = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "03_partial_upsert.iosql"
    with Database(f"sqlite://{path}") as db:
        quotes = db.table(
            "quotes",
            pk=["symbol"],
            columns={"symbol": "text", "venue": "text", "price": "float"},
        )
        quotes.write({"symbol": "AAPL", "venue": "NASDAQ", "price": 187.4}, mode="ignore")

        # 只更新价格；未传入的 venue 保留，显式传 None 则写入 NULL。
        quotes.upsert_all([{"symbol": "AAPL", "price": 188.0}], pk=["symbol"])
        print("更新后:", quotes.select())


if __name__ == "__main__":
    run()
