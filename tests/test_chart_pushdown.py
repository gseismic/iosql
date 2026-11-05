"""Charts 原始点流与引擎分桶原语测试。

覆盖：
- sqlite 引擎 aggregate_by_bucket 的正确性（与 Python 逐步计算对拍）；
- web /chart line 与 candle 路径逐行输出、不跨 symbol 聚合、窗口过滤和 null gap。
"""
import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

# fastapi 属于 [ui] extra，核心环境可能未安装
pytest.importorskip("fastapi")

from iosql import Database, Segment
from iosql.exception import BadArgument

try:
    from iosql.web import create_app
except ImportError:
    pytest.skip("backend not integrated yet", allow_module_level=True)

from fastapi.testclient import TestClient

DAY_MS = 86_400_000
BASE_TS = 1735689600000  # 2025-01-01 00:00:00 UTC


def make_tick_rows(symbols=("AAPL", "MSFT"), days=3, per_day=12):
    """与 tests/test_web.py 演示库同构的行集，供 Python 逐步对拍复用。"""
    rows = []
    for day in range(days):
        for sym_idx, symbol in enumerate(symbols):
            for i in range(per_day):
                rows.append({
                    "symbol": symbol,
                    "ts": BASE_TS + day * DAY_MS + i * 1000 + sym_idx,
                    "price": 100.0 + day + i * 0.1,
                    "volume": 1000 + i,
                })
    return rows


def chart_events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def chart_series(response):
    events = chart_events(response)
    result = {}
    for event in events:
        if event.get("type") == "data":
            result.setdefault(event.get("symbol"), []).extend(event["points"])
    return events, result


def expected_buckets(rows, x_col, y_col, n_buckets, predicate=None):
    """与旧 web 流式路径完全一致的 Python 分桶期望值。"""
    xs = [r[x_col] for r in rows if r[x_col] is not None]
    x_min, x_max = min(xs), max(xs)
    span = x_max - x_min
    width = span // n_buckets + 1 if span >= 0 else 1
    sums = [0.0] * n_buckets
    counts = [0] * n_buckets
    for r in rows:
        if predicate is not None and not predicate(r):
            continue
        xv = r[x_col]
        if xv is None:
            continue
        idx = int((xv - x_min) // width)
        idx = min(max(idx, 0), n_buckets - 1)
        if r[y_col] is not None:
            sums[idx] += r[y_col]
            counts[idx] += 1
    return x_min, width, sums, counts


# ---------------------------------------------------------------- 引擎级


@pytest.fixture
def ticks_db(tmp_path):
    """duration 月分区 × 多 segment 的数值表，覆盖跨分区聚合路径。"""
    db_dir = tmp_path / "ticks.iosql"
    db = Database(f"sqlite://{db_dir}")
    t = db.timeseries(
        "ticks",
        time="ts",
        pk=["symbol", "ts"],
        columns={
            "symbol": "text",
            "ts": "integer",
            "price": "float",
            "volume": "integer",
        },
        partition_every="1day",
        segment=Segment.rows(max_rows=10),
    )
    t.write(make_tick_rows())
    yield db.table("ticks")
    db.close()


class TestSqliteAggregateByBucket:
    def test_numeric_parity_full_range(self, ticks_db):
        rows = make_tick_rows()
        n = 5
        agg = ticks_db.aggregate_by_bucket("ts", ["price", "volume"], n)
        x_min, width, sums, counts = expected_buckets(rows, "ts", "price", n)
        assert agg["x_mode"] == "numeric"
        assert agg["scanned"] == len(rows)
        assert agg["x_min"] == x_min
        assert agg["bucket_min"] == x_min
        assert agg["bucket_width"] == width
        assert agg["counts"]["price"] == counts
        assert all(
            abs(a - b) < 1e-9
            for a, b in zip(agg["sums"]["price"], sums)
        )
        # 第二列同样聚合且计数一致
        assert agg["counts"]["volume"] == counts
        vol_sums = expected_buckets(rows, "ts", "volume", n)[2]
        assert all(
            abs(a - b) < 1e-9
            for a, b in zip(agg["sums"]["volume"], vol_sums)
        )

    def test_where_filter_parity(self, ticks_db):
        rows = make_tick_rows()
        n = 4
        agg = ticks_db.aggregate_by_bucket(
            "ts", ["price"], n,
            where=[{"field": "symbol", "op": "==", "value": "AAPL"}],
        )
        x_min, width, sums, counts = expected_buckets(
            rows, "ts", "price", n,
            predicate=lambda r: r["symbol"] == "AAPL",
        )
        assert agg["counts"]["price"] == counts
        assert agg["bucket_min"] == x_min
        assert all(
            abs(a - b) < 1e-9 for a, b in zip(agg["sums"]["price"], sums)
        )

    def test_between_where_and_unknown_column(self, ticks_db):
        rows = make_tick_rows()
        agg = ticks_db.aggregate_by_bucket(
            "ts", ["price"], 3,
            where=[{"field": "ts", "op": "between",
                    "value": [BASE_TS, BASE_TS + DAY_MS]}],
        )
        expected = expected_buckets(
            rows, "ts", "price", 3,
            predicate=lambda r: BASE_TS <= r["ts"] <= BASE_TS + DAY_MS,
        )
        assert sum(agg["counts"]["price"]) == sum(expected[3])
        with pytest.raises(BadArgument):
            ticks_db.aggregate_by_bucket("nope", ["price"], 3)
        with pytest.raises(BadArgument):
            ticks_db.aggregate_by_bucket("ts", ["nope"], 3)
        with pytest.raises(BadArgument):
            ticks_db.aggregate_by_bucket("ts", ["price"], 0)

    def test_ordered_stream_can_close_on_another_thread(self, ticks_db):
        """ASGI 客户端取消流时，迭代器清理由不同工作线程执行。"""
        stream = ticks_db.iter_select_batches(
            ["symbol", "ts", "price"],
            order_by=[("symbol", "asc"), ("ts", "asc")],
            batch_size=1,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_batch = executor.submit(next, stream).result()

        assert len(first_batch) == 1
        # 有序归并已打开 SQLite 只读连接；跨线程 close 必须完成且释放资源。
        stream.close()
        remaining = list(ticks_db.iter_select_batches(
            ["symbol", "ts"],
            order_by=[("symbol", "asc"), ("ts", "asc")],
            batch_size=50,
        ))
        assert sum(map(len, remaining)) == len(make_tick_rows())

    def test_empty_table(self, tmp_path):
        db = Database(f"sqlite://{tmp_path}/empty.iosql")
        t = db.timeseries(
            "empty_t", time="ts", pk=["ts"],
            columns={"ts": "integer", "v": "float"},
            partition_every="1day",
        )
        agg = t.aggregate_by_bucket("ts", ["v"], 4)
        db.close()
        assert agg["x_mode"] == "empty"
        assert agg["scanned"] == 0
        assert agg["sums"] is None

    def test_digit_text_x(self, tmp_path):
        """YYYYMMDD 纯数字文本 x：text 模式，网格按整数化值计算。"""
        db_dir = tmp_path / "dtxt.iosql"
        db = Database(f"sqlite://{db_dir}")
        t = db.table(
            "dtxt", pk=["d", "k"],
            columns={"d": "text", "k": "integer", "v": "float"},
            order_key="d",
        )
        rows = [
            {"d": f"202501{10 + i:02d}", "k": i, "v": float(i)}
            for i in range(1, 9)
        ]
        t.write(rows)
        agg = t.aggregate_by_bucket("d", ["v"], 3)
        db.close()
        assert agg["x_mode"] == "text"
        assert agg["bucket_min"] == 20250111
        assert agg["scanned"] == 8
        x_min, width, sums, counts = expected_buckets(
            [{**r, "d_num": int(r["d"])} for r in rows],
            "d_num", "v", 3,
        )
        assert agg["bucket_width"] == width
        assert agg["counts"]["v"] == counts
        assert all(
            abs(a - b) < 1e-9 for a, b in zip(agg["sums"]["v"], sums)
        )

    def test_non_numeric_text_x(self, tmp_path):
        db_dir = tmp_path / "iso.iosql"
        db = Database(f"sqlite://{db_dir}")
        t = db.table(
            "iso", pk=["d", "k"],
            columns={"d": "text", "k": "integer", "v": "float"},
            order_key="d",
        )
        t.write([
            {"d": f"2026-01-{10 + i:02d}", "k": i, "v": float(i)}
            for i in range(5)
        ])
        agg = t.aggregate_by_bucket("d", ["v"], 3)
        db.close()
        assert agg["x_mode"] == "non_numeric"
        assert agg["sums"] is None

    def test_float_x_and_null_y(self, tmp_path):
        db_dir = tmp_path / "fx.iosql"
        db = Database(f"sqlite://{db_dir}")
        t = db.table(
            "fx", pk=["k"], columns={"k": "integer", "x": "float", "v": "float"},
        )
        rows = [{"k": i, "x": i * 0.5, "v": float(i) if i % 2 else None}
                for i in range(10)]
        t.write(rows)
        agg = t.aggregate_by_bucket("x", ["v"], 4)
        db.close()
        assert agg["x_mode"] == "numeric"
        x_min, width, sums, counts = expected_buckets(rows, "x", "v", 4)
        assert agg["bucket_width"] == width
        assert agg["counts"]["v"] == counts
        assert all(
            abs(a - b) < 1e-9 for a, b in zip(agg["sums"]["v"], sums)
        )

    def test_dirty_value_column_counted(self, tmp_path):
        """契约声明 text 的 y 列：非数值行按 value_bad_rows 透出。"""
        db_dir = tmp_path / "dirty.iosql"
        db = Database(f"sqlite://{db_dir}")
        t = db.table(
            "dirty", pk=["k"],
            columns={"k": "integer", "ts": "integer", "v": "text"},
        )
        t.write([{"k": i, "ts": i, "v": str(i * 2)} for i in range(5)])
        agg = t.aggregate_by_bucket("ts", ["v"], 2)
        db.close()
        assert agg["value_bad_rows"]["v"] == 5


# ---------------------------------------------------------------- web 级


@pytest.fixture(scope="module")
def chart_db_dir(tmp_path_factory) -> Path:
    db_dir = tmp_path_factory.mktemp("chart") / "demo.iosql"
    db = Database(f"sqlite://{db_dir}")
    t = db.timeseries(
        "ticks", time="ts", pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition_every="1day", segment=Segment.rows(max_rows=10),
    )
    t.write(make_tick_rows())
    # OHLC 表：candle 分支回归
    k = db.timeseries(
        "bars", time="ts", pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "open": "float",
                 "high": "float", "low": "float", "close": "float"},
        partition_every="1day",
    )
    k.write([
        {"symbol": "AAPL", "ts": BASE_TS + i * 60_000,
         "open": 1.0 + i, "high": 2.0 + i, "low": 0.5 + i, "close": 1.5 + i}
        for i in range(10)
    ])
    gaps = db.timeseries(
        "gaps", time="ts", pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "value": "float"},
        partition_every="1day",
    )
    gaps.write([
        {"symbol": "AAPL", "ts": BASE_TS + i * 60_000,
         "value": None if i == 1 else float(i)}
        for i in range(3)
    ])
    # ISO 文本 x + 数值 y：非数字文本 422 回归
    iso = db.table(
        "iso", pk=["d", "k"],
        columns={"d": "text", "k": "integer", "v": "float"},
        order_key="d",
    )
    iso.write([
        {"d": f"2026-01-{10 + i:02d}", "k": i, "v": float(i)}
        for i in range(5)
    ])
    metrics = db.table(
        "metrics", time="ts", symbol_column=None, pk=["id"],
        columns={"id": "integer", "ts": "integer", "device": "text",
                 "state": "text", "reading": "float"},
    )
    metrics.write([
        {"id": 1, "ts": BASE_TS, "device": "pump-a", "state": "ok", "reading": 1.0},
        {"id": 2, "ts": BASE_TS + 1000, "device": "pump-b", "state": "warn", "reading": 2.0},
        {"id": 3, "ts": BASE_TS + 2000, "device": "pump-a", "state": "idle", "reading": None},
        {"id": 4, "ts": BASE_TS + 3000, "device": "", "state": "ok", "reading": 4.0},
        {"id": 5, "ts": BASE_TS + 4000, "device": None, "state": "ok", "reading": 5.0},
    ])
    many = db.table(
        "many_devices", time="ts", symbol_column=None, pk=["id"],
        columns={"id": "integer", "ts": "integer", "device": "text", "reading": "float"},
    )
    many.write([
        {"id": i, "ts": BASE_TS + i, "device": f"sensor-{i:02}", "reading": float(i)}
        for i in range(33)
    ])
    flags = db.table(
        "flags", pk=["id"], columns={"id": "integer", "enabled": "boolean"},
    )
    flags.write([{"id": 1, "enabled": True}, {"id": 2, "enabled": False}])
    ids = db.table(
        "big_ids", pk=["id"],
        columns={"id": "integer", "device_id": "integer", "reading": "float"},
    )
    ids.write([
        {"id": 1, "device_id": 9007199254740992, "reading": 1.0},
        {"id": 2, "device_id": 9007199254740993, "reading": 2.0},
    ])
    daily = db.table(
        "daily", pk=["d"], columns={"d": "text", "v": "float"},
        partition="calendar(d,every=1day,timezone=UTC)",
    )
    daily.write([{"d": "2025-01-01", "v": 1.0}, {"d": "2025-01-02", "v": 2.0}])
    bad_date = db.table("bad_date", pk=["id"], columns={"id": "integer", "d": "date", "v": "float"})
    bad_date.write([{"id": 1, "d": "2025-02-30", "v": 1.0}])
    compact_date = db.table("compact_date", pk=["id"], columns={"id": "integer", "d": "date", "v": "float"})
    compact_date.write([{"id": 1, "d": "20250101", "v": 1.0}, {"id": 2, "d": "20250102", "v": 2.0}])
    daily_ohlc = db.table("daily_ohlc", pk=["d"], columns={
        "d": "date", "open": "float", "high": "float", "low": "float", "close": "float",
    })
    daily_ohlc.write([{"d": "2025-01-01", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}])
    db.close()
    return db_dir


def _client(db_dir: Path) -> TestClient:
    return TestClient(
        create_app(f"sqlite://{db_dir}", "readonly"),
        base_url="http://localhost",
    )


class TestChartEndpointPushdown:
    def test_line_contract_no_filter(self, chart_db_dir):
        """全范围返回原始行，空 symbol 按标的分线。"""
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart", params={"col": "price", "buckets": 5},
            )
        assert resp.status_code == 200
        events, groups = chart_series(resp)
        assert events[0]["kind"] == "line"
        assert events[-1]["type"] == "done"
        assert events[-1]["rows"] == len(make_tick_rows())
        assert set(groups) == {"AAPL", "MSFT"}
        assert len(groups["AAPL"]) == len(groups["MSFT"]) == 36

    def test_buckets_parameter_does_not_drop_rows(self, chart_db_dir):
        """兼容旧 buckets 参数，但其值不会影响原始点数量。"""
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart", params={"col": "price", "buckets": 3},
            )
        assert resp.status_code == 200
        events, groups = chart_series(resp)
        assert events[-1]["rows"] == len(make_tick_rows())
        assert sum(map(len, groups.values())) == len(make_tick_rows())

    def test_line_values_match_python(self, chart_db_dir):
        rows = make_tick_rows()
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart", params={"col": "price"},
            )
        _, groups = chart_series(resp)
        for symbol in ("AAPL", "MSFT"):
            expected = [
                [row["ts"], row["price"]]
                for row in sorted(rows, key=lambda item: item["ts"])
                if row["symbol"] == symbol
            ]
            assert groups[symbol] == expected

    def test_line_symbol_filter(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart",
                params={"col": "price", "symbol": "AAPL", "buckets": 4},
            )
        assert resp.status_code == 200
        events, groups = chart_series(resp)
        assert set(groups) == {"AAPL"}
        assert events[-1]["rows"] == len(groups["AAPL"]) == 36

    def test_candle_preserves_each_ohlc_row(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/bars/chart",
                params={"col": "close", "kind": "candle", "buckets": 3},
            )
        assert resp.status_code == 200
        events, groups = chart_series(resp)
        assert events[0]["kind"] == "candle"
        assert events[-1]["rows"] == 10
        assert groups["AAPL"] == [
            [BASE_TS + i * 60_000, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i]
            for i in range(10)
        ]

    def test_null_y_is_kept_as_a_gap(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/gaps/chart", params={"col": "value", "symbol": "AAPL"},
            )
        assert resp.status_code == 200
        _, groups = chart_series(resp)
        assert groups["AAPL"] == [
            [BASE_TS, 0.0],
            [BASE_TS + 60_000, None],
            [BASE_TS + 120_000, 2.0],
        ]

    def test_iso_text_x_rejected_with_hint(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/iso/chart", params={"col": "v", "buckets": 3},
            )
        assert resp.status_code == 422
        body = resp.json()
        assert "非数字文本" in body["detail"]
        assert "duration" in body["hint"]

    def test_text_y_rejected_before_scan(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart", params={"col": "symbol"},
            )
        assert resp.status_code == 422
        assert "文本类型" in resp.json()["detail"]

    def test_chart_line_existing_contract(self, chart_db_dir):
        """PLAN-055 D 系契约回归：带筛选窗口的 line 请求保持原语义。"""
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart",
                params={
                    "col": "price", "symbol": "AAPL",
                    "start": BASE_TS, "end": BASE_TS + DAY_MS,
                    "kind": "line",
                },
            )
        assert resp.status_code == 200
        events, groups = chart_series(resp)
        assert events[0]["kind"] == "line"
        assert events[-1]["rows"] == 13
        assert len(groups["AAPL"]) == 13
        assert groups["AAPL"][0][0] == BASE_TS
        assert groups["AAPL"][-1][0] == BASE_TS + DAY_MS


class TestGenericChartEndpoint:
    def test_time_line_filters_and_groups_nonfinancial_data(self, chart_db_dir):
        body = {
            "x": "ts", "y": "reading", "group_by": "device",
            "where": [{"or": [
                {"field": "device", "op": "==", "value": "pump-a"},
                {"field": "device", "op": "==", "value": "pump-b"},
            ]}],
        }
        with _client(chart_db_dir) as client:
            resp = client.post("/api/table/metrics/chart", json=body)
        assert resp.status_code == 200
        events = chart_events(resp)
        assert (events[0]["kind"], events[0]["x_type"], events[0]["y_type"]) == (
            "line", "time", "numeric",
        )
        points = {event["group"]: event["points"] for event in events if event["type"] == "data"}
        assert points == {
            "pump-a": [[BASE_TS, 1.0], [BASE_TS + 2000, None]],
            "pump-b": [[BASE_TS + 1000, 2.0]],
        }
        assert events[-1]["rows"] == 3

    @pytest.mark.parametrize("x,y,x_type,y_type", [
        ("id", "reading", "numeric", "numeric"),
        ("state", "reading", "category", "numeric"),
        ("id", "state", "numeric", "category"),
        ("state", "device", "category", "category"),
    ])
    def test_type_driven_scatter_preserves_observations(self, chart_db_dir, x, y, x_type, y_type):
        with _client(chart_db_dir) as client:
            resp = client.post("/api/table/metrics/chart", json={"x": x, "y": y})
        events = chart_events(resp)
        assert resp.status_code == 200
        assert (events[0]["kind"], events[0]["x_type"], events[0]["y_type"]) == (
            "scatter", x_type, y_type,
        )
        points = [point for event in events if event["type"] == "data" for point in event["points"]]
        assert len(points) == events[-1]["rows"] == 5
        if y == "reading":
            assert sum(point[1] is None for point in points) == 1

    def test_null_and_empty_group_are_distinct(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.post("/api/table/metrics/chart", json={
                "x": "ts", "y": "reading", "group_by": "device",
            })
        events = chart_events(resp)
        assert {event["group"] for event in events if event["type"] == "data"} == {
            None, "", "pump-a", "pump-b",
        }
        assert events[-1]["rows"] == 5

    def test_many_groups_need_explicit_filter(self, chart_db_dir):
        body = {"x": "ts", "y": "reading", "group_by": "device"}
        with _client(chart_db_dir) as client:
            too_many = chart_events(client.post("/api/table/many_devices/chart", json=body))
            body["where"] = [{"field": "device", "op": "==", "value": "sensor-01"}]
            filtered = chart_events(client.post("/api/table/many_devices/chart", json=body))
        assert too_many[-1]["type"] == "error"
        assert "32" in too_many[-1]["hint"]
        assert filtered[-1]["type"] == "done" and filtered[-1]["rows"] == 1
        assert next(event for event in filtered if event["type"] == "data")["group"] == "sensor-01"

    def test_boolean_categories_follow_contract(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            response = client.post("/api/table/flags/chart", json={"x": "enabled", "y": "id"})
        events = chart_events(response)
        points = [point for event in events if event["type"] == "data" for point in event["points"]]
        assert points == [[False, 2.0], [True, 1.0]]

    def test_calendar_contract_uses_date_axis_and_rejects_invalid_date(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            response = client.post("/api/table/daily/chart", json={"x": "d", "y": "v"})
            invalid = client.post("/api/table/bad_date/chart", json={"x": "d", "y": "v"})
            compact = client.post("/api/table/compact_date/chart", json={"x": "d", "y": "v"})
            compact_groups = client.post("/api/table/compact_date/chart", json={
                "x": "id", "y": "v", "group_by": "d",
            })
            compact_y = client.post("/api/table/compact_date/chart", json={"x": "id", "y": "d"})
            ohlc = client.post("/api/table/daily_ohlc/chart", json={"x": "d", "y": "close", "kind": "ohlc"})
        events = chart_events(response)
        assert (events[0]["kind"], events[0]["x_type"]) == ("line", "date")
        assert next(event for event in events if event["type"] == "data")["points"] == [
            ["2025-01-01", 1.0], ["2025-01-02", 2.0],
        ]
        invalid_events = chart_events(invalid)
        assert invalid_events[-1]["type"] == "error" and "无效日期" in invalid_events[-1]["detail"]
        compact_events = chart_events(compact)
        assert compact_events[0]["x_type"] == "date" and compact_events[-1]["rows"] == 2
        assert next(event for event in compact_events if event["type"] == "data")["points"] == [
            [20250101, 1.0], [20250102, 2.0],
        ]
        assert {event["group"] for event in chart_events(compact_groups) if event["type"] == "data"} == {
            20250101, 20250102,
        }
        assert next(event for event in chart_events(compact_y) if event["type"] == "data")["points"] == [
            [1, "20250101"], [2, "20250102"],
        ]
        ohlc_events = chart_events(ohlc)
        assert (ohlc_events[0]["kind"], ohlc_events[0]["x_type"]) == ("ohlc", "date")
        assert next(event for event in ohlc_events if event["type"] == "data")["points"] == [
            ["2025-01-01", 1.0, 2.0, 0.5, 1.5],
        ]

    @pytest.mark.parametrize("axis", ["x", "y"])
    def test_excess_categories_fail_instead_of_truncating(self, tmp_path, axis):
        uri = f"sqlite://{tmp_path / 'categories.iosql'}"
        with Database(uri) as db:
            table = db.table("observations", pk=["id"], columns={
                "id": "integer", "category": "text", "value": "float",
            })
            table.write([
                {"id": i, "category": f"c{i:04}", "value": float(i)} for i in range(1001)
            ])
        with TestClient(create_app(uri, "readonly"), base_url="http://localhost") as client:
            body = {"x": "category", "y": "value"} if axis == "x" else {"x": "id", "y": "category"}
            too_many = chart_events(client.post("/api/table/observations/chart", json=body))
            body["where"] = [{"field": "category", "op": "<=", "value": "c0004"}]
            filtered = chart_events(client.post("/api/table/observations/chart", json=body))
        assert too_many[-1]["type"] == "error" and "类别过多" in too_many[-1]["detail"]
        assert filtered[-1]["type"] == "done" and filtered[-1]["rows"] == 5

    def test_large_integer_groups_are_not_merged_or_rounded(self, chart_db_dir):
        body = {"x": "id", "y": "reading", "group_by": "device_id"}
        with _client(chart_db_dir) as client:
            events = chart_events(client.post("/api/table/big_ids/chart", json=body))
            body["where"] = [{"field": "device_id", "op": "==", "value": "9007199254740993"}]
            selected = chart_events(client.post("/api/table/big_ids/chart", json=body))
            unsafe_axis = chart_events(client.post("/api/table/big_ids/chart", json={
                "x": "device_id", "y": "reading",
            }))
        assert {event["group"] for event in events if event["type"] == "data"} == {
            "9007199254740992", "9007199254740993",
        }
        assert selected[-1]["rows"] == 1
        assert next(event for event in selected if event["type"] == "data")["group"] == "9007199254740993"
        assert unsafe_axis[-1]["type"] == "error" and "安全整数" in unsafe_axis[-1]["detail"]

    def test_invalid_columns_and_filter_fail_before_streaming(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            invalid_axis = client.post("/api/table/metrics/chart", json={"x": "missing", "y": "reading"})
            invalid_filter = client.post("/api/table/metrics/chart", json={
                "x": "ts", "y": "reading", "where": [{"field": "missing", "op": "==", "value": 1}],
            })
        assert invalid_axis.status_code == invalid_filter.status_code == 422

    def test_ohlc_retains_original_values_with_generic_filter(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.post("/api/table/bars/chart", json={
                "x": "ts", "y": "close", "kind": "ohlc", "group_by": "symbol",
                "where": [{"field": "ts", "op": "<=", "value": BASE_TS + 60_000}],
            })
        events = chart_events(resp)
        assert events[0]["kind"] == "ohlc"
        assert events[-1]["rows"] == 2
        assert next(event for event in events if event["type"] == "data")["points"] == [
            [BASE_TS, 1.0, 2.0, 0.5, 1.5],
            [BASE_TS + 60_000, 2.0, 3.0, 1.5, 2.5],
        ]

    @pytest.mark.parametrize("engine", ["sqlite", "parquet"])
    def test_generic_query_uses_common_engine_contract(self, tmp_path, engine):
        uri = f"{engine}://{tmp_path / 'generic.iosql'}"
        with Database(uri) as db:
            table = db.table(
                "measurements", time="ts", symbol_column=None, pk=["id"],
                columns={"id": "integer", "ts": "integer", "sensor": "text", "v": "float"},
            )
            table.write([
                {"id": 1, "ts": BASE_TS, "sensor": "A", "v": 1.0},
                {"id": 2, "ts": BASE_TS + 1000, "sensor": "B", "v": 2.0},
            ])
        with TestClient(create_app(uri, "readonly"), base_url="http://localhost") as client:
            response = client.post("/api/table/measurements/chart", json={
                "x": "ts", "y": "v", "group_by": "sensor",
                "where": [{"field": "sensor", "op": "==", "value": "B"}],
            })
        events = chart_events(response)
        assert events[-1]["rows"] == 1
        assert next(event for event in events if event["type"] == "data")["points"] == [[BASE_TS + 1000, 2.0]]

    @pytest.mark.parametrize("engine", ["sqlite", "parquet"])
    def test_large_integer_filter_remains_exact_across_engines(self, tmp_path, engine):
        uri = f"{engine}://{tmp_path / 'large.iosql'}"
        with Database(uri) as db:
            table = db.table("measurements", pk=["id"], columns={
                "id": "integer", "device_id": "bigint", "v": "float",
            })
            table.write([
                {"id": 1, "device_id": 9007199254740992, "v": 1.0},
                {"id": 2, "device_id": 9007199254740993, "v": 2.0},
            ])
        with TestClient(create_app(uri, "readonly"), base_url="http://localhost") as client:
            response = client.post("/api/table/measurements/chart", json={
                "x": "id", "y": "v", "group_by": "device_id",
                "where": [{"field": "device_id", "op": "==", "value": "9007199254740993"}],
            })
        events = chart_events(response)
        assert events[-1]["rows"] == 1
        assert next(event for event in events if event["type"] == "data")["group"] == "9007199254740993"

    def test_generic_chart_postgresql_when_test_instance_is_configured(self):
        base = os.environ.get("POSTGRES_DSN")
        if not base:
            pytest.skip("需要显式配置独立 PostgreSQL 测试实例")
        psycopg2 = pytest.importorskip("psycopg2")
        from psycopg2 import sql
        from iosql.migration_postgresql import _stage_uri

        schema = "iosql_chart_test_" + uuid.uuid4().hex
        with closing(psycopg2.connect(base)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
        try:
            uri = _stage_uri(base, schema)
            with Database(uri) as db:
                table = db.table("measurements", pk=["id"], columns={
                    "id": "integer", "device_id": "bigint", "v": "float",
                })
                table.write([
                    {"id": 1, "device_id": 9007199254740992, "v": 1.0},
                    {"id": 2, "device_id": 9007199254740993, "v": 2.0},
                ])
            with closing(psycopg2.connect(base)) as connection:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL(
                        'CREATE TABLE {}.decimals (id bigint PRIMARY KEY, g numeric, v double precision)'
                    ).format(sql.Identifier(schema)))
                    cursor.execute(sql.SQL(
                        'INSERT INTO {}.decimals (id, g, v) VALUES (%s, %s, %s), (%s, %s, %s)'
                    ).format(sql.Identifier(schema)), (1, "1.0000000000000001", 1.0,
                                                       2, "1.0000000000000002", 2.0))
                    cursor.execute(sql.SQL(
                        'CREATE TABLE {}.dates (id bigint PRIMARY KEY, d date, v double precision)'
                    ).format(sql.Identifier(schema)))
                    cursor.execute(sql.SQL(
                        'INSERT INTO {}.dates (id, d, v) VALUES (%s, %s, %s)'
                    ).format(sql.Identifier(schema)), (1, "2025-01-01", 3.0))
            with TestClient(create_app(uri, "readonly"), base_url="http://localhost") as client:
                spec_response = client.get("/api/table/measurements/spec")
                response = client.post("/api/table/measurements/chart", json={
                    "x": "id", "y": "v", "group_by": "device_id",
                    "where": [{"field": "device_id", "op": "==", "value": "9007199254740993"}],
                })
                decimal_response = client.post("/api/table/decimals/chart", json={
                    "x": "id", "y": "v", "group_by": "g",
                })
                date_response = client.post("/api/table/dates/chart", json={"x": "d", "y": "v"})
            assert spec_response.json()["spec"] is None
            assert spec_response.json()["columns"]["device_id"] == "bigint"
            assert response.status_code == 200, response.text
            events = chart_events(response)
            assert events[-1]["type"] == "done", events
            assert events[-1]["rows"] == 1
            assert next(event for event in events if event["type"] == "data")["group"] == "9007199254740993"
            decimal_events = chart_events(decimal_response)
            assert {event["group"] for event in decimal_events if event["type"] == "data"} == {
                "1.0000000000000001", "1.0000000000000002",
            }
            date_events = chart_events(date_response)
            assert (date_events[0]["kind"], date_events[0]["x_type"]) == ("line", "date")
            assert next(event for event in date_events if event["type"] == "data")["points"] == [
                ["2025-01-01", 3.0],
            ]
        finally:
            with closing(psycopg2.connect(base)) as connection:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
