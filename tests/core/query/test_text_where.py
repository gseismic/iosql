import pytest

from iosql.exception import BadArgument
from iosql.core.query.where import Where


def _case_tuple(case):
    return case.field, case.op, case.value["value"]


def test_text_where_parses_simple_equality():
    where = Where("venue = 'NASDAQ'")

    assert [[_case_tuple(case) for case in cases]
            for cases in where.or_and_cases] == [
        [("venue", "==", "NASDAQ")]
    ]


def test_text_where_parses_and_conditions():
    where = Where("venue = 'NASDAQ' and level <= 3")

    assert [[_case_tuple(case) for case in cases]
            for cases in where.or_and_cases] == [
        [("venue", "==", "NASDAQ"), ("level", "<=", 3)]
    ]


def test_text_where_parses_or_with_parentheses():
    where = Where("(side = 'bid' or side = 'ask') and level = 1")

    assert [[_case_tuple(case) for case in cases]
            for cases in where.or_and_cases] == [
        [("side", "==", "bid"), ("level", "==", 1)],
        [("side", "==", "ask"), ("level", "==", 1)],
    ]


def test_text_where_parses_between_and_in():
    where = Where("ts between 1000 and 2000 and side in ('bid', 'ask')")

    assert [[_case_tuple(case) for case in cases]
            for cases in where.or_and_cases] == [
        [("ts", "between_and", [1000, 2000]), ("side", "in", ["bid", "ask"])]
    ]


@pytest.mark.parametrize("where", [
    "venue = 'NASDAQ'; drop table data",
    "lower(venue) = 'nasdaq'",
    "venue like 'NAS%'",
    "venue = NASDAQ",
])
def test_text_where_rejects_unsupported_sql_fragments(where):
    with pytest.raises(BadArgument):
        Where(where)
