"""PLAN-052 用户接口统一回归测试。

对应 docs/design/user-api-unify-20260822-consolidated.md 第五节测试设计 1-11，
以及第八节"期望的用户接口"关键片段。
"""

import pytest

from iosql import (
    Database,
    Partition,
    Segment,
    parse_partition_spec,
)
from iosql.core.query import Case
from iosql.exception import BadArgument, MissingTimeColumn


def _uri(tmp_path, name="unify.iosql"):
    return f"sqlite://{tmp_path}/{name}"


_TICK_COLUMNS = {"symbol": "text", "ts": "integer", "price": "float"}

_TICK_ROWS = [
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
    {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
]


# ---------------------------------------------------------------
# 测试 1: timeseries(pk=...) 与 timeseries(primary_key=...) 契约等价
# ---------------------------------------------------------------

def test_timeseries_pk_alias_equivalent_to_primary_key(tmp_path):
    with Database(_uri(tmp_path)) as db:
        by_pk = db.timeseries(
            "a", "ts", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
        ).spec().to_dict()
        by_primary_key = db.timeseries(
            "b", "ts", primary_key=["symbol", "ts"], columns=_TICK_COLUMNS,
        ).spec().to_dict()
        assert by_pk == by_primary_key


# ---------------------------------------------------------------
# 测试 2: timeseries(...) 与等价 table(...) 的 spec 一致
# ---------------------------------------------------------------

def test_timeseries_equivalent_to_table_contract(tmp_path):
    with Database(_uri(tmp_path)) as db:
        sugar = db.timeseries(
            "a", "ts", pk=["symbol", "ts"],
            columns=_TICK_COLUMNS, partition_every="1day",
        ).spec().to_dict()
        explicit = db.table(
            "b", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
            partition=Partition.duration("ts", every="1day"),
            order_key="ts", time="ts",
        ).spec().to_dict()
        assert sugar == explicit


# ---------------------------------------------------------------
# 测试 3: dict 形式 op="between" 与 op="between_and" 结果一致
# ---------------------------------------------------------------

def test_dict_between_normalized_like_between_and(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
            partition=Partition.duration("ts", every="1day"),
        )
        ticks.write(_TICK_ROWS)

        user_form = ticks.select(where={
            "field": "ts", "op": "between",
            "value": [1735689600000, 1735776000000],
        }, to_dict=True, order_by="ts")
        internal_form = ticks.select(where={
            "field": "ts", "op": "between_and",
            "value": [1735689600000, 1735776000000],
        }, to_dict=True, order_by="ts")
        assert user_form == internal_form
        assert len(user_form) == 3


# ---------------------------------------------------------------
# 测试 4: 非法 op 在 Case 构造即抛 BadArgument（含合法列表）
# ---------------------------------------------------------------

def test_illegal_op_raises_bad_argument_at_case_construction():
    with pytest.raises(BadArgument) as excinfo:
        Case("ts", "betweeen", [1, 10])
    message = str(excinfo.value)
    assert "betweeen" in message
    assert "==" in message and "between" in message
    assert not isinstance(excinfo.value, AssertionError)


def test_op_aliases_normalized_to_canonical_forms():
    assert Case("f", "=", 1).op == "=="
    assert Case("f", "eq", 1).op == "=="
    assert Case("f", "gt", 1).op == ">"
    assert Case("f", "le", 1).op == "<="
    assert Case("f", "between", [1, 2]).op == "between_and"


def test_between_requires_pair_value_bad_argument_not_assert():
    with pytest.raises(BadArgument):
        Case("ts", "between", [1])
    with pytest.raises(BadArgument):
        Case("sym", "in", [])


# ---------------------------------------------------------------
# 测试 5: 字符串 partition tz= 与 timezone= 解析一致
# ---------------------------------------------------------------

def test_string_partition_tz_and_timezone_parse_equal():
    by_tz = parse_partition_spec("calendar(d,every=1day,tz=Asia/Shanghai)")
    by_timezone = parse_partition_spec(
        "calendar(d,every=1day,timezone=Asia/Shanghai)"
    )
    assert by_tz == by_timezone


def test_format_partition_spec_outputs_timezone_canonical_form():
    spec = parse_partition_spec("calendar(d,every=1day,tz=Asia/Shanghai)")
    assert spec.to_string() == "calendar(d,every=1day,timezone=Asia/Shanghai)"


# ---------------------------------------------------------------
# 测试 6: write(mode=...) 回落契约 conflict；replace/ignore 互斥
# ---------------------------------------------------------------

def test_write_mode_falls_back_to_contract_conflict(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
            conflict="ignore",
        )
        ticks.write([{"symbol": "AAPL", "ts": 1000, "price": 1.0}])
        result = ticks.write([{"symbol": "AAPL", "ts": 1000, "price": 9.9}])
        assert result.rows_seen == 1
        assert len(ticks.select()) == 1
        stored = ticks.select(to_dict=True)[0]
        assert stored["price"] == 1.0


def test_insert_all_replace_and_ignore_mutually_exclusive(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
        )
        with pytest.raises(BadArgument, match="互斥"):
            ticks.insert_all(_TICK_ROWS, pk=["symbol", "ts"],
                             replace=True, ignore=True)


# ---------------------------------------------------------------
# 测试 7: calendar 分区表反推 time，query(date=) 可用；range 不反推
# ---------------------------------------------------------------

def test_calendar_partition_infers_time_enables_date_query(tmp_path):
    with Database(_uri(tmp_path)) as db:
        events = db.table(
            "events",
            pk=["event_date", "name"],
            columns={"event_date": "text", "name": "text", "city": "text"},
            partition=Partition.calendar(
                "event_date", every="1day", timezone="Asia/Shanghai",
            ),
        )
        events.write([
            {"event_date": "2026-01-06", "name": "review", "city": "Beijing"},
            {"event_date": "2026-01-07", "name": "demo", "city": "Shanghai"},
        ])
        assert events.spec().time is not None
        assert events.spec().time.unit == "date"
        rows = events.query(date="2026-01-06").to_list()
        assert len(rows) == 1
        assert rows[0]["name"] == "review"


def test_range_partition_does_not_infer_time(tmp_path):
    with Database(_uri(tmp_path)) as db:
        items = db.table(
            "items", pk=["id"],
            columns={"id": "integer", "v": "float"},
            partition=Partition.range("id", every=100),
        )
        items.write([{"id": 1, "v": 0.5}])
        assert items.spec().time is None
        with pytest.raises(MissingTimeColumn, match="修复"):
            items.query(start=0, end=10).to_list()


# ---------------------------------------------------------------
# 测试 8: write() 与 insert_all() 对同一批数据结果一致
# ---------------------------------------------------------------

def test_write_delegates_to_insert_all_with_same_result(tmp_path):
    with Database(_uri(tmp_path)) as db:
        high = db.table(
            "high", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
            partition=Partition.duration("ts", every="1day"),
        )
        low = db.table(
            "low", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
            partition=Partition.duration("ts", every="1day"),
        )
        write_result = high.write(_TICK_ROWS)
        insert_result = low.insert_all(
            _TICK_ROWS, pk=["symbol", "ts"],
            partition=Partition.duration("ts", every="1day"),
        )
        assert write_result.rows_seen == insert_result.rows_seen == 3
        assert (
            high.select(order_by="symbol,ts", to_dict=True)
            == low.select(order_by="symbol,ts", to_dict=True)
        )


# ---------------------------------------------------------------
# 测试 9: evolve 三语义
# ---------------------------------------------------------------

def test_evolve_new_column_null_backfill_on_old_partitions(tmp_path):
    with Database(_uri(tmp_path)) as db:
        factors = db.table(
            "factors", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer"},
            partition=Partition.duration("ts", every="1day"),
        )
        factors.write([{"symbol": "AAPL", "ts": 1735689600000}])
        factors.write([
            {"symbol": "AAPL", "ts": 1735776000000, "f1": 0.3, "f2": 0.7},
        ])
        rows = factors.select(
            columns=["symbol", "ts", "f1"], to_dict=True, order_by="ts",
        )
        assert rows[0]["f1"] is None
        assert rows[1]["f1"] == 0.3


def test_evolve_contract_writeback_survives_reopen(tmp_path):
    uri = _uri(tmp_path)
    with Database(uri) as db:
        factors = db.table(
            "factors", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer"},
            partition=Partition.duration("ts", every="1day"),
        )
        factors.write([{"symbol": "AAPL", "ts": 1735689600000}])
        factors.write([
            {"symbol": "AAPL", "ts": 1735776000000, "f1": 0.3},
        ])
        assert "f1" in factors.spec().columns

    with Database(uri) as db:
        assert "f1" in db.table("factors").spec().columns


def test_evolve_type_conflict_raises_bad_argument_with_guidance(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = db.table(
            "t", pk=["id"], columns={"id": "integer"},
            partition=Partition.range("id", every=100),
        )
        table.write([{"id": 1, "x": 1}])
        with pytest.raises(BadArgument) as excinfo:
            table.write([{"id": 2, "x": "abc"}])
        message = str(excinfo.value)
        assert "`x`" in message
        assert "integer" in message and "text" in message
        assert "修复" in message


def test_evolve_numeric_widening_and_none_values_allowed(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = db.table(
            "t", pk=["id"],
            columns={"id": "integer", "v": "float", "flag": "integer"},
            partition=Partition.range("id", every=100),
        )
        table.write([{"id": 1, "v": 1.5, "flag": True}])
        table.write([{"id": 2, "v": 2, "flag": False}])
        table.write([{"id": 3, "v": None}])


def test_mixed_type_into_text_column_stays_allowed(tmp_path):
    """既有宽松用法：声明 text 的列写入数字由 TEXT affinity 转换。"""
    with Database(_uri(tmp_path)) as db:
        table = db.table(
            "mixed", pk=["event_id"],
            columns={"event_id": "integer", "value": "text"},
            segment=Segment.rows(max_rows=10),
        )
        table.write({"event_id": 1, "value": 10})
        table.write({"event_id": 2, "value": "text"})
        assert table.segments()[0]["stats"]["value"]["complete"] is False


# ---------------------------------------------------------------
# 测试 10: select/iter_select 的 columns= 别名与 fields= 等价
# ---------------------------------------------------------------

def test_select_columns_alias_equivalent_to_fields(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
            partition=Partition.duration("ts", every="1day"),
        )
        ticks.write(_TICK_ROWS)

        assert ticks.select() == ticks.select("*")
        assert ticks.select() != []
        by_columns = ticks.select(columns=["symbol", "price"], to_dict=True)
        by_fields = ticks.select(["symbol", "price"], to_dict=True)
        assert by_columns == by_fields

        alias_rows = list(ticks.iter_select(
            columns=["symbol"], to_dict=True,
        ))
        fields_rows = list(ticks.iter_select(["symbol"], to_dict=True))
        assert sorted(r["symbol"] for r in alias_rows) == \
            sorted(r["symbol"] for r in fields_rows)

        alias_plan = ticks.explain_select(columns=["symbol"])
        fields_plan = ticks.explain_select(["symbol"])
        assert alias_plan.to_dict()["columns"] == \
            fields_plan.to_dict()["columns"]

        with pytest.raises(BadArgument, match="只能传一个"):
            ticks.select(["symbol"], columns=["price"])


# ---------------------------------------------------------------
# 测试 11: 废弃参数触发 DeprecationWarning 且行为等价
# ---------------------------------------------------------------

def test_timeseries_deprecated_params_warn_and_behave_equivalently(tmp_path):
    with Database(_uri(tmp_path)) as db:
        with pytest.warns(DeprecationWarning, match="partition_kind"):
            calendar_ticks = db.timeseries(
                "cal", "event_date", pk=["event_date"],
                columns={"event_date": "text", "v": "float"},
                partition_every="1month", timezone="Asia/Shanghai",
                partition_kind="calendar",
            )
        assert calendar_ticks.spec().partition.kind == "calendar"
        assert calendar_ticks.spec().time.unit == "date"

        with pytest.warns(DeprecationWarning, match="time_unit"):
            date_ticks = db.timeseries(
                "dat", "event_date", pk=["event_date"],
                columns={"event_date": "text", "v": "float"},
                time_unit="date",
            )
        assert date_ticks.spec().partition.kind == "calendar"

        with pytest.warns(DeprecationWarning, match="rows_per_file"):
            segmented = db.timeseries(
                "seg", "ts", pk=["symbol", "ts"],
                columns=_TICK_COLUMNS,
                target_rows_per_file=400_000,
                min_rows_per_file=300_000,
                max_rows_per_file=500_000,
            )
        assert segmented.spec().segment is not None
        layout = segmented.spec().physical_layout
        assert layout.target_rows_per_file == 400_000
        assert layout.min_rows_per_file == 300_000
        assert layout.max_rows_per_file == 500_000


def test_latest_group_by_alias_warns_and_equals_by(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.timeseries(
            "ticks", "ts", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
        )
        ticks.write(_TICK_ROWS)
        with pytest.warns(DeprecationWarning, match="group_by"):
            legacy = ticks.latest(
                symbols="AAPL", as_of=1735776000000, group_by=["symbol"],
            ).to_list()
        modern = ticks.latest(
            symbols="AAPL", as_of=1735776000000, by=["symbol"],
        ).to_list()
        assert legacy == modern


def test_select_all_warns_and_matches_select_to_dict(tmp_path):
    with Database(_uri(tmp_path)) as db:
        ticks = db.table(
            "ticks", pk=["symbol", "ts"], columns=_TICK_COLUMNS,
        )
        ticks.write(_TICK_ROWS)
        with pytest.warns(DeprecationWarning, match="select_all"):
            legacy = ticks.select_all("*", where=None)
        modern = ticks.select("*", where=None, to_dict=True)
        assert modern == legacy


# ---------------------------------------------------------------
# 第八节验收片段: table() 直达 session_column 与 select 免样板
# ---------------------------------------------------------------

def test_table_shortcut_time_and_session_column(tmp_path):
    with Database(_uri(tmp_path)) as db:
        futs = db.table(
            "futures", pk=["symbol", "ts"],
            columns={
                "symbol": "text", "ts": "integer",
                "trading_date": "text", "price": "float",
            },
            partition=Partition.duration("ts", every="1day"),
            time="ts",
            session_column="trading_date",
        )
        futs.write([{
            "symbol": "AU", "ts": 1767661200000,
            "trading_date": "2026-01-06", "price": 700.0,
        }])
        rows = futs.query(session="2026-01-06", symbols="AU").to_list()
        assert len(rows) == 1
        assert rows[0]["trading_date"] == "2026-01-06"


def test_write_without_contract_error_contains_repair_guidance(tmp_path):
    with Database(_uri(tmp_path)) as db:
        table = db.table("no_contract")
        with pytest.raises(BadArgument) as excinfo:
            table.write([{"a": 1}])
        message = str(excinfo.value)
        assert "没有持久化契约" in message
        assert "重新声明" in message
        assert "spec=" in message
