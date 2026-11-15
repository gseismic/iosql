"""使用普通表保存库存并按业务条件查询。

运行：在本文件所在目录执行 python 01_quickstart_table.py。
准备：已安装 iosql 及其默认依赖。
预期：输出写入行数和有库存的两种图书，按价格升序排列。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database


DB_PATH = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "01_quickstart_table.iosql"
DB_URI = f"sqlite://{DB_PATH}"


def run():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        products = db.table(
            "products",
            pk=["sku"],
            columns={"sku": "text", "category": "text", "stock": "integer", "price": "float"},
        )

        result = products.write([
            {"sku": "book-a", "category": "book", "stock": 3, "price": 39.0},
            {"sku": "book-b", "category": "book", "stock": 2, "price": 29.0},
            {"sku": "book-c", "category": "book", "stock": 0, "price": 19.0},
            {"sku": "pen-a", "category": "pen", "stock": 10, "price": 5.0},
        ])

        rows = products.select(
            fields=["sku", "stock", "price"],
            where="category == 'book' and stock > 0",
            order_by="price",
        )

        print("写入行数:", result.rows_written)
        print("有库存的图书:", rows)


if __name__ == "__main__":
    run()
