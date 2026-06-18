from iosql import Database
from iosql.core.query import Where
from iosql.engines.sqlite import Table as SQLiteTable


def _uri(tmp_path):
    return f"sqlite://{tmp_path / 'query_correctness.sqlite3'}"


def _items_table(tmp_path):
    db = Database(_uri(tmp_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={
            "id": "integer",
            "symbol": "text",
            "value": "integer",
            "order": "integer",
            "score": "integer",
        },
    )
    table.write([
        {
            "id": 1,
            "symbol": "AAPL",
            "value": 10,
            "order": 7,
            "score": None,
        },
        {
            "id": 2,
            "symbol": "symbol",
            "value": 20,
            "order": 8,
            "score": 5,
        },
    ])
    return table


def test_text_literal_matching_column_name_is_not_identifier(tmp_path):
    table = _items_table(tmp_path)

    rows = table.select(
        "*",
        where={"field": "symbol", "op": "==", "value": "symbol"},
        to_dict=True,
    )

    assert [row["id"] for row in rows] == [2]


def test_numeric_field_string_value_is_bound_parameter_not_sql(tmp_path):
    table = _items_table(tmp_path)

    rows = table.select(
        "*",
        where={"field": "id", "op": "==", "value": "1 OR 1=1"},
        to_dict=True,
    )

    assert rows == []


def test_or_where_does_not_duplicate_rows(tmp_path):
    table = _items_table(tmp_path)
    where = [
        [{"field": "id", "op": "==", "value": 1}],
        [{"field": "value", "op": "==", "value": 10}],
    ]

    rows = table.select("*", where=where, order_by="id", to_dict=True)

    assert [row["id"] for row in rows] == [1]


def test_list_projection_quotes_reserved_identifier(tmp_path):
    table = _items_table(tmp_path)

    rows = table.select(["order"], where=None, to_dict=True)

    assert rows == [{"order": 7}, {"order": 8}]


def test_python_order_by_null_matches_sqlite_default(tmp_path):
    table = _items_table(tmp_path)

    asc = table.select("*", where=None, order_by="score", to_dict=True)
    desc = table.select("*", where=None, order_by="score desc", to_dict=True)

    assert [row["id"] for row in asc] == [1, 2]
    assert [row["id"] for row in desc] == [2, 1]


def test_latest_projection_accepts_star_and_string_columns(tmp_path):
    db = Database(_uri(tmp_path))
    ticks = db.timeseries(
        "ticks",
        time="ts",
        primary_key=["ts", "symbol"],
        columns={"ts": "integer", "symbol": "text", "price": "real"},
    )
    ticks.write([
        {"ts": 1000, "symbol": "AAPL", "price": 1.0},
        {"ts": 2000, "symbol": "AAPL", "price": 2.0},
    ])

    assert ticks.latest(columns="*").to_list() == [
        {"ts": 2000, "symbol": "AAPL", "price": 2.0}
    ]
    assert ticks.latest(columns="symbol,price").to_list() == [
        {"symbol": "AAPL", "price": 2.0}
    ]


def test_new_package_layout_exports_core_query_and_sqlite_engine():
    assert Where("id = 1").or_and_cases
    assert SQLiteTable.__name__ == "Table"
