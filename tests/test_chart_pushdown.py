"""PLAN-070 chart 大表分桶聚合下推测试。

覆盖：
- sqlite 引擎 aggregate_by_bucket 的正确性（与 Python 逐步计算对拍）；
- web /chart line 路径接下推后的契约、错误语义与"大表不再受扫描行数
  上限"回归（PLAN-069 报告的 chart 绘制曲线失败根因）。
"""
import shutil
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
    db.close()
    return db_dir


def _client(db_dir: Path) -> TestClient:
    return TestClient(
        create_app(f"sqlite://{db_dir}", "readonly"),
        base_url="http://localhost",
    )


class TestChartEndpointPushdown:
    def test_line_contract_no_filter(self, chart_db_dir):
        """无任何筛选全范围绘图 200：PLAN-069 报告的失败场景基线。"""
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart", params={"col": "price", "buckets": 5},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["kind"] == "line"
        assert {"x", "y", "col", "symbol"} <= set(payload)
        assert len(payload["x"]) == len(payload["y"]) > 0

    def test_line_ignores_scan_cap(self, chart_db_dir, monkeypatch):
        """line 路径不受扫描行数上限约束（上限仅保留给 candle 流式路径）。"""
        from iosql.web import app as web_app

        monkeypatch.setattr(web_app, "MAX_CHART_SCAN_ROWS", 10)
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart", params={"col": "price", "buckets": 3},
            )
        assert resp.status_code == 200
        assert len(resp.json()["x"]) > 0

    def test_line_values_match_python(self, chart_db_dir):
        rows = make_tick_rows()
        n = 5
        _, width, sums, counts = expected_buckets(rows, "ts", "price", n)
        x_min = min(r["ts"] for r in rows)
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart",
                params={"col": "price", "buckets": n},
            )
        payload = resp.json()
        expect_mids = [
            x_min + i * width + width // 2
            for i in range(n) if counts[i]
        ]
        expect_ys = [sums[i] / counts[i] for i in range(n) if counts[i]]
        assert payload["x"] == expect_mids
        assert payload["y"] == pytest.approx(expect_ys)

    def test_line_symbol_filter(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/ticks/chart",
                params={"col": "price", "symbol": "AAPL", "buckets": 4},
            )
        assert resp.status_code == 200
        assert resp.json()["symbol"] == "AAPL"
        assert len(resp.json()["x"]) > 0

    def test_candle_still_works(self, chart_db_dir):
        with _client(chart_db_dir) as client:
            resp = client.get(
                "/api/table/bars/chart",
                params={"col": "close", "kind": "candle", "buckets": 3},
            )
        assert resp.status_code == 200
        payload = resp.json()
        assert {"x", "open", "high", "low", "close"} <= set(payload)
        assert payload["x"]

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
        payload = resp.json()
        assert payload["kind"] == "line"
        assert payload["col"] == "price"
        assert payload["symbol"] == "AAPL"
        assert len(payload["x"]) == len(payload["y"]) > 0
