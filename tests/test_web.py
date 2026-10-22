"""iosql Web 后端契约测试（PLAN-055 Phase D）。

后端 iosql.web 由并行团队开发中；本文件基于冻结契约编写，
未集成时整个模块自动 skip，集成后无需改动即可运行。
"""

import csv
import io
import json
import shutil
from pathlib import Path

import pytest

# fastapi 属于 [ui] extra，核心环境可能未安装
pytest.importorskip("fastapi")

from typer.testing import CliRunner

from iosql import Database, Segment
from iosql.cli import app as cli_app

try:
    from iosql.web import create_app
except ImportError:
    pytest.skip("backend not integrated yet", allow_module_level=True)

from fastapi.testclient import TestClient


runner = CliRunner()

DAY_MS = 86_400_000
BASE_TS = 1735689600000  # 2025-01-01 00:00:00 UTC
TICKS_TOTAL = 72         # ticks：3 天 × 2 symbol × 12 行
EVENTS_TOTAL = 6         # events：2 天 × 3 行（满足 ≥5）
NOTES_TOTAL = 3          # notes：无分区普通表，无 time 契约
TABLE_TOTAL = 3


def _build_demo_db(db_dir: Path) -> None:
    """构建演示库：ticks（duration 分区 + row segment 滚动）、
    events（calendar 分区普通表）、notes（无分区，用于能力门控）。"""
    db = Database(f"sqlite://{db_dir}")

    # 每个逻辑分区（天）写入 24 行 > max_rows=10，保证 segment 滚动出多文件
    ticks = db.timeseries(
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
    rows = []
    for day in range(3):
        for sym_idx, symbol in enumerate(["AAPL", "MSFT"]):
            for i in range(12):
                rows.append({
                    "symbol": symbol,
                    "ts": BASE_TS + day * DAY_MS + i * 1000 + sym_idx,
                    "price": 100.0 + day + i * 0.1,
                    "volume": 1000 + i,
                })
    ticks.write(rows)

    events = db.table(
        "events",
        pk=["event_date", "name"],
        columns={"event_date": "text", "name": "text", "city": "text"},
        partition="calendar(event_date,every=1day,timezone=Asia/Shanghai)",
    )
    for day in range(2):
        date = f"2026-01-{5 + day:02d}"
        events.write([
            {"event_date": date, "name": f"evt-{day}-{i}", "city": "Beijing"}
            for i in range(3)
        ])

    # iosql 只对 duration/calendar 分区自动反推 time 契约；
    # notes 无分区 → spec().time 为 None，用于验证 /query 能力门控 409
    notes = db.table("notes", pk=["id"], columns={"id": "integer", "text": "text"})
    notes.write([{"id": i, "text": f"note-{i}"} for i in range(NOTES_TOTAL)])

    db.close()


@pytest.fixture(scope="module")
def demo_db_path(tmp_path_factory) -> Path:
    """模块级共享 demo 库：只读组直接使用。"""
    root = tmp_path_factory.mktemp("ui_demo")
    db_dir = root / "demo.iosql"
    _build_demo_db(db_dir)
    return db_dir


@pytest.fixture
def copy_db_uri(demo_db_path: Path, tmp_path: Path) -> str:
    """函数级独立库副本：写模式等需要隔离的用例使用。"""
    target = tmp_path / "copy.iosql"
    shutil.copytree(demo_db_path, target)
    return f"sqlite://{target}"


def _client(uri: str, mode: str = "readonly") -> TestClient:
    """统一以上下文管理器使用，保证 shutdown 执行 db.close()。

    base_url 用 localhost：生产 app 挂了 TrustedHostMiddleware（只信任
    本机回环 Host），TestClient 默认的 testserver 会被 400 拒绝。
    """
    return TestClient(create_app(uri, mode), base_url="http://localhost")


class TestReadOnlyApi:
    """D2 · 只读模式：全部只读端点可用，写端点不存在（404）。"""

    def test_api_db_info(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/db")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["mode"] == "readonly"
        assert payload["table_count"] == TABLE_TOTAL
        assert payload["uri"].endswith("demo.iosql")

    def test_list_tables_summary(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/tables?with_stats=1")
        assert resp.status_code == 200
        tables = {item["name"]: item for item in resp.json()["tables"]}
        assert set(tables) == {"ticks", "events", "notes"}

        ticks = tables["ticks"]
        assert ticks["row_count"] == TICKS_TOTAL
        assert ticks["part_count"] == 3
        assert ticks["segment_count"] > 1  # max_rows=10 触发滚动
        assert ticks["has_time"] is True
        assert ticks["pk"] == ["symbol", "ts"]
        assert ticks["order_key"] == "ts"
        assert ticks["partition_str"].startswith("duration(")
        assert {col["name"] for col in ticks["columns"]} == {
            "symbol", "ts", "price", "volume",
        }
        assert all(col["type"] for col in ticks["columns"])

        events = tables["events"]
        assert events["row_count"] == EVENTS_TOTAL
        assert events["part_count"] >= 1  # calendar 分区非空

        # 能力位：无分区表无 time 契约（驱动 D5 门控）
        assert tables["notes"]["has_time"] is False

    def test_list_tables_light_default(self, demo_db_path):
        """PLAN-060 · 默认轻量契约：不返回统计字段，首屏不被全库统计阻塞。"""
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/tables")
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        assert {t["name"] for t in tables} == {"ticks", "events", "notes"}
        for t in tables:
            assert "row_count" not in t
            assert "part_count" not in t
            assert "segment_count" not in t
            # manifest 级元数据仍然可用
            assert "partition_str" in t and "has_time" in t and "pk" in t

    def test_table_stats_endpoint(self, demo_db_path):
        """PLAN-060 · 单表渐进统计端点。"""
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/ticks/stats")
            assert resp.status_code == 200
            stats = resp.json()
            assert stats["name"] == "ticks"
            assert stats["row_count"] == TICKS_TOTAL
            assert stats["part_count"] == 3
            assert stats["segment_count"] > 1

            resp = client.get("/api/table/nope/stats")
            assert resp.status_code == 404

    def test_stats_single_file_table(self, demo_db_path):
        """PLAN-061 · 无分区单文件表行数（manifest 回退路径覆盖）。"""
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/notes/stats")
            assert resp.status_code == 200
            assert resp.json()["row_count"] == NOTES_TOTAL

    def test_stats_after_delete(self, copy_db_uri):
        """PLAN-061 · 删除后 stats 行数同步（manifest 求和仍精确）。"""
        with _client(copy_db_uri, mode="write") as client:
            before = client.get("/api/table/notes/stats").json()["row_count"]
            client.post("/api/table/notes/delete", json={
                "where": [{"field": "id", "op": "==", "value": 0}],
            })
            after = client.get("/api/table/notes/stats").json()["row_count"]
            assert after == before - 1

    def test_count_endpoint_uses_manifest_sum(self, demo_db_path):
        """PLAN-061 · /count 无筛选走 manifest 求和（与 count_rows 一致）。"""
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/count", json={})
            assert resp.status_code == 200
            assert resp.json()["count"] == TICKS_TOTAL

    def test_table_spec(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/ticks/spec")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["name"] == "ticks"
        assert payload["spec"]["primary_key"] == ["symbol", "ts"]

    def test_table_parts(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/events/parts")
        assert resp.status_code == 200
        parts = resp.json()["parts"]
        assert isinstance(parts, list) and parts

    def test_table_segments(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/ticks/segments")
            assert resp.status_code == 200
            segments = resp.json()["segments"]
            assert len(segments) > 1

            # include_obsolete 查询参数可接受
            resp_all = client.get(
                "/api/table/ticks/segments",
                params={"include_obsolete": "true"},
            )
        assert resp_all.status_code == 200
        assert len(resp_all.json()["segments"]) >= len(segments)

    def test_rows_first_page(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/rows", json={"limit": 7})
        assert resp.status_code == 200
        payload = resp.json()
        assert {"columns", "rows", "next_cursor", "order_by"} <= set(payload)
        assert len(payload["rows"]) == 7

    def test_explain(self, demo_db_path):
        body = {
            "where": [{"field": "symbol", "op": "==", "value": "AAPL"}],
            "order_by": "ts",
        }
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/explain", json=body)
        assert resp.status_code == 200
        assert resp.json()["plan"]

    def test_count(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/count", json={})
        assert resp.status_code == 200
        assert resp.json()["count"] == TICKS_TOTAL

    def test_query_timeseries(self, demo_db_path):
        body = {
            "start": BASE_TS,
            "end": BASE_TS + DAY_MS,  # 第一天
            "symbols": "AAPL",
        }
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/query", json=body)
        assert resp.status_code == 200
        payload = resp.json()
        assert {"rows", "columns"} <= set(payload)
        assert payload["rows"]
        assert all(row["symbol"] == "AAPL" for row in payload["rows"])

    def test_latest(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post(
                "/api/table/ticks/latest",
                json={"symbols": ["AAPL", "MSFT"]},
            )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert rows
        assert {row["symbol"] for row in rows} == {"AAPL", "MSFT"}

    def test_chart_line(self, demo_db_path):
        params = {
            "col": "price",
            "symbol": "AAPL",
            "start": BASE_TS,
            "end": BASE_TS + DAY_MS,
            "kind": "line",
        }
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/ticks/chart", params=params)
        assert resp.status_code == 200
        # PLAN-074 起图表输出完整原始点的 NDJSON 流。
        events = [json.loads(line) for line in resp.text.splitlines()]
        assert events[0]['kind'] == 'line'
        assert events[0]['value_cols'] == ['price']
        points = [point for event in events if event['type'] == 'data'
                  for point in event['points'] if event['symbol'] == 'AAPL']
        assert events[-1]['type'] == 'done'
        assert events[-1]['rows'] == len(points) > 0
        assert all(len(point) == 2 for point in points)

    def test_chart_candle_shape(self, demo_db_path):
        # ticks 表无 OHLC 列；后端可降级为 line 或以 422+hint 明确拒绝
        params = {
            "col": "price",
            "symbol": "AAPL",
            "start": BASE_TS,
            "end": BASE_TS + DAY_MS,
            "kind": "candle",
        }
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/ticks/chart", params=params)
        assert resp.status_code in (200, 422)
        payload = resp.json()
        if resp.status_code == 422:
            # 错误体契约：{"detail": str, "hint": str|null}
            assert isinstance(payload["detail"], str)
        elif payload["kind"] == "candle":
            assert {"x", "open", "high", "low", "close"} <= set(payload)
        else:
            assert payload["kind"] == "line"
            assert {"x", "y"} <= set(payload)

    def test_export_csv_and_jsonl(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp_csv = client.get(
                "/api/table/events/export", params={"format": "csv"},
            )
            resp_jsonl = client.get(
                "/api/table/events/export", params={"format": "jsonl"},
            )
            where = json.dumps(
                {"field": "event_date", "op": "==", "value": "2026-01-05"},
            )
            resp_filtered = client.get(
                "/api/table/events/export",
                params={"format": "jsonl", "where": where},
            )
        assert resp_csv.status_code == 200
        csv_rows = list(csv.DictReader(io.StringIO(resp_csv.text)))
        assert len(csv_rows) == EVENTS_TOTAL

        assert resp_jsonl.status_code == 200
        lines = [
            json.loads(line)
            for line in resp_jsonl.text.splitlines()
            if line.strip()
        ]
        assert len(lines) == EVENTS_TOTAL

        filtered = [
            json.loads(line)
            for line in resp_filtered.text.splitlines()
            if line.strip()
        ]
        assert len(filtered) == EVENTS_TOTAL // 2

    def test_export_order_by_streams_sorted(self, demo_db_path):
        # 引擎有序流式归并就位后，export 可带 order_by 且仍为流式产出
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get(
                "/api/table/ticks/export",
                params={"format": "jsonl", "order_by": "ts desc"},
            )
        assert resp.status_code == 200
        rows = [
            json.loads(line)
            for line in resp.text.splitlines()
            if line.strip()
        ]
        assert len(rows) == TICKS_TOTAL
        ts_list = [row["ts"] for row in rows]
        assert ts_list == sorted(ts_list, reverse=True)

    def test_layout_report(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/ticks/layout_report")
        assert resp.status_code == 200
        report = resp.json()["report"]
        assert report["total_rows"] == TICKS_TOTAL

    def test_validate(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/validate", json={"deep": False})
        assert resp.status_code == 200
        validation = resp.json()["validation"]
        assert validation["ok"] is True

    def test_error_body_contract(self, demo_db_path):
        # 错误体统一 {"detail": str, "hint": str|null}
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get("/api/table/nope/spec")
        assert resp.status_code == 404
        payload = resp.json()
        assert isinstance(payload["detail"], str)
        assert "hint" in payload

    def test_write_endpoints_absent_in_readonly(self, demo_db_path):
        # 只读模式下写路由不注册：7 个写端点全部 404 而非 403
        # （import_buffer 从未实现，PLAN-055 OUTCOME 已确认 v1 仅 7 个写端点）
        write_endpoints = [
            ("/api/table/ticks/write", {"rows": []}),
            ("/api/table/ticks/delete", {}),
            ("/api/table/ticks/truncate", {"confirm": "ticks"}),
            ("/api/table/ticks/create_index", {"column": "ts"}),
            ("/api/table/ticks/compact", {}),
            ("/api/table/ticks/cleanup_obsolete", {"dry_run": True}),
            (
                "/api/table/ticks/repartition",
                {
                    "target_uri": "sqlite:///tmp/should_not_exist.iosql",
                    "partition_str": "duration(ts,every=1hour)",
                },
            ),
        ]
        with _client(f"sqlite://{demo_db_path}") as client:
            for path, body in write_endpoints:
                resp = client.post(path, json=body)
                assert resp.status_code == 404, (path, resp.status_code)


class TestWriteMode:
    """D3 · 写模式：写端点注册且防呆校验生效（各自使用独立库副本）。"""

    def test_write_increases_count(self, copy_db_uri):
        new_row = {
            "symbol": "GOOG",
            "ts": BASE_TS + 10 * DAY_MS,
            "price": 199.9,
            "volume": 1,
        }
        with _client(copy_db_uri, mode="write") as client:
            before = client.post("/api/table/ticks/count", json={}).json()["count"]
            resp = client.post("/api/table/ticks/write", json={"rows": [new_row]})
            assert resp.status_code == 200
            assert "result" in resp.json()
            after = client.post("/api/table/ticks/count", json={}).json()["count"]
        assert after == before + 1

    def test_delete_requires_where(self, copy_db_uri):
        with _client(copy_db_uri, mode="write") as client:
            missing = client.post("/api/table/ticks/delete", json={})
            assert missing.status_code == 422

            # where 与 /rows 契约同构：条件列表 [{field,op,value}]
            resp = client.post("/api/table/ticks/delete", json={
                "where": [{"field": "symbol", "op": "==", "value": "MSFT"}],
            })
            assert resp.status_code == 200
            assert resp.json()["deleted"] >= 1

    def test_truncate_requires_matching_confirm(self, copy_db_uri):
        with _client(copy_db_uri, mode="write") as client:
            wrong = client.post(
                "/api/table/events/truncate", json={"confirm": "ticks"},
            )
            assert wrong.status_code == 422

            ok = client.post(
                "/api/table/events/truncate", json={"confirm": "events"},
            )
            assert ok.status_code == 200

            count = client.post("/api/table/events/count", json={}).json()["count"]
        assert count == 0

    def test_compact_then_cleanup_obsolete_two_phase(self, copy_db_uri):
        with _client(copy_db_uri, mode="write") as client:
            compact = client.post("/api/table/ticks/compact", json={})
            assert compact.status_code == 200
            assert compact.json()["result"]["rows_read"] == TICKS_TOTAL

            # 第一步：dry-run 预览，不删除任何文件
            preview = client.post(
                "/api/table/ticks/cleanup_obsolete", json={"dry_run": True},
            )
            assert preview.status_code == 200
            assert preview.json()["result"] is not None

            # 第二步：显式确认删除 obsolete 文件
            cleanup = client.post(
                "/api/table/ticks/cleanup_obsolete", json={"dry_run": False},
            )
            assert cleanup.status_code == 200
            assert cleanup.json()["result"]["files_deleted"] >= 1

    def test_create_index(self, copy_db_uri):
        with _client(copy_db_uri, mode="write") as client:
            resp = client.post(
                "/api/table/ticks/create_index", json={"column": "symbol"},
            )
        assert resp.status_code == 200

    def test_repartition_to_new_target_keeps_source(self, copy_db_uri, tmp_path):
        target_uri = f"sqlite://{tmp_path / 'hourly.iosql'}"
        with _client(copy_db_uri, mode="write") as client:
            resp = client.post("/api/table/ticks/repartition", json={
                "target_uri": target_uri,
                "partition_str": "duration(ts,every=1hour)",
            })
            assert resp.status_code == 200
            result = resp.json()["result"]
            assert result["rows_copied"] == TICKS_TOTAL

        # 源库不受影响：repartition 只写新库
        source = Database(copy_db_uri)
        try:
            assert "ticks" in source.table_names()
            assert source.table("ticks").count_rows() == TICKS_TOTAL
        finally:
            source.close()

        target = Database(target_uri)
        try:
            assert target.table("ticks").count_rows() == TICKS_TOTAL
        finally:
            target.close()


class TestKeysetPagination:
    """D4 · keyset 分页：limit=7 连续翻页不重不漏直至耗尽。"""

    def test_pagination_no_dup_no_loss(self, demo_db_path):
        uri = f"sqlite://{demo_db_path}"
        collected = []
        cursor = None
        pages = 0
        with _client(uri) as client:
            while True:
                body = {"limit": 7, "order_by": "ts"}
                if cursor is not None:
                    body["cursor"] = cursor
                resp = client.post("/api/table/ticks/rows", json=body)
                assert resp.status_code == 200
                payload = resp.json()
                assert "next_cursor" in payload
                collected.extend(
                    (row["symbol"], row["ts"]) for row in payload["rows"]
                )
                pages += 1
                cursor = payload["next_cursor"]
                if cursor is None:
                    break
                assert pages < 50, "翻页未收敛，疑似 next_cursor 永不为空"

        assert pages == (TICKS_TOTAL + 6) // 7  # 11 页：10 页整 + 尾页 2 行
        assert len(collected) == TICKS_TOTAL
        assert len(set(collected)) == TICKS_TOTAL  # (symbol, ts) 无重复

        with _client(uri) as client:
            total = client.post("/api/table/ticks/count", json={}).json()["count"]
        assert total == TICKS_TOTAL

    def test_pagination_survives_duplicate_sort_values(self, tmp_path):
        """Oracle H1 回归：排序列存在大量并列值时 keyset 不得丢行。

        ticks 夹具的 ts 因 sym_idx 偏移而全局唯一，无法暴露"同值并列跳行"
        缺陷；本用例构造 ts 大量重复的表（多 symbol 同时刻），逐页翻取必须
        不重不漏覆盖全部 (symbol, ts) 组合。
        """
        db_dir = tmp_path / "tied.iosql"
        db = Database(f"sqlite://{db_dir}")
        tied = db.table(
            "tied",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "px": "real"},
        )
        # 5 个时间点 × 3 个 symbol = 15 行；ts 仅 5 个唯一值，并列密度极高
        rows = [
            {"symbol": sym, "ts": 1000 + i, "px": float(i)}
            for i in range(5)
            for sym in ("A", "B", "C")
        ]
        tied.write(rows)
        total = 15
        db.close()

        collected = []
        cursor = None
        pages = 0
        with _client(f"sqlite://{db_dir}") as client:
            while True:
                body = {"limit": 4, "order_by": "ts"}
                if cursor is not None:
                    body["cursor"] = cursor
                resp = client.post("/api/table/tied/rows", json=body)
                assert resp.status_code == 200
                payload = resp.json()
                collected.extend(
                    (row["symbol"], row["ts"]) for row in payload["rows"]
                )
                pages += 1
                cursor = payload.get("next_cursor")
                if cursor is None:
                    break
                assert pages < 20, "翻页未收敛"

        assert pages == (total + 3) // 4
        assert len(collected) == total
        assert set(collected) == {
            (sym, 1000 + i) for i in range(5) for sym in ("A", "B", "C")
        }


class TestNestedWhere:
    """PLAN-059 · or/and 嵌套 where：修复前端 OR 序列化被 422 拒绝的坏损。"""

    def test_or_group_union(self, demo_db_path):
        """{"or":[…]} 两组 == 条件应返回并集（此前 422）。"""
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/rows", json={
                "where": [{"or": [
                    {"field": "symbol", "op": "==", "value": "AAPL"},
                    {"field": "symbol", "op": "==", "value": "MSFT"},
                ]}],
                "limit": 5000,
            })
        assert resp.status_code == 200
        payload = resp.json()
        symbols = {row["symbol"] for row in payload["rows"]}
        assert symbols == {"AAPL", "MSFT"}
        assert payload["next_cursor"] is None  # 两 symbol 全量命中

    def test_flat_and_or_mix(self, demo_db_path):
        """平铺普通条件与 or 分组混用：AND 连接 + 组内 OR。"""
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/rows", json={
                "where": [
                    {"field": "price", "op": ">=", "value": 100.0},
                    {"or": [
                        {"field": "symbol", "op": "==", "value": "MSFT"},
                        {"field": "volume", "op": ">", "value": 1005},
                    ]},
                ],
                "limit": 5000,
            })
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert rows
        assert all(
            (r["symbol"] == "MSFT" or r["volume"] > 1005) and r["price"] >= 100.0
            for r in rows
        )

    def test_and_group_nested(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/rows", json={
                "where": [{"and": [
                    {"field": "symbol", "op": "==", "value": "AAPL"},
                    {"field": "ts", "op": ">=", "value": BASE_TS},
                ]}],
                "limit": 5000,
            })
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert rows
        assert all(r["symbol"] == "AAPL" and r["ts"] >= BASE_TS for r in rows)

    def test_rows_cursor_with_or_where(self, demo_db_path):
        """or 筛选与 keyset 游标组合：翻页不重不漏直至耗尽。"""
        uri = f"sqlite://{demo_db_path}"
        expected_symbols = {"AAPL", "MSFT"}
        collected = []
        cursor = None
        pages = 0
        with _client(uri) as client:
            while True:
                body = {
                    "where": [{"or": [
                        {"field": "symbol", "op": "==", "value": "AAPL"},
                        {"field": "symbol", "op": "==", "value": "MSFT"},
                    ]}],
                    "limit": 7, "order_by": "ts",
                }
                if cursor is not None:
                    body["cursor"] = cursor
                resp = client.post("/api/table/ticks/rows", json=body)
                assert resp.status_code == 200
                payload = resp.json()
                collected.extend(row["symbol"] for row in payload["rows"])
                pages += 1
                cursor = payload["next_cursor"]
                if cursor is None:
                    break
                assert pages < 50

        # ticks 全部行都属于 AAPL/MSFT → 命中数等于全表行数
        assert len(collected) == TICKS_TOTAL
        assert set(collected) == expected_symbols

    def test_count_with_or_group(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/count", json={
                "where": [{"or": [
                    {"field": "symbol", "op": "==", "value": "AAPL"},
                    {"field": "symbol", "op": "==", "value": "MSFT"},
                ]}],
            })
        assert resp.status_code == 200
        assert resp.json()["count"] == TICKS_TOTAL

    def test_query_latest_accept_or_group(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/query", json={
                "where": [{"or": [
                    {"field": "symbol", "op": "==", "value": "AAPL"},
                    {"field": "symbol", "op": "==", "value": "MSFT"},
                ]}],
                "limit": 10,
            })
            assert resp.status_code == 200
            assert len(resp.json()["rows"]) == 10

            resp = client.post("/api/table/ticks/latest", json={
                "by": ["symbol"],
                "columns": ["symbol", "ts"],
                "where": [{"or": [
                    {"field": "symbol", "op": "==", "value": "AAPL"},
                    {"field": "symbol", "op": "==", "value": "MSFT"},
                ]}],
            })
            assert resp.status_code == 200
            assert len(resp.json()["rows"]) == 2

    def test_invalid_nested_groups_rejected(self, demo_db_path):
        cases = [
            # or 内叶子缺 field
            {"where": [{"or": [{"op": "==", "value": 1}]}]},
            # or 与 and 同组
            {"where": [{"or": [], "and": []}]},
            # or 值不是数组
            {"where": [{"or": {"field": "symbol", "op": "==", "value": "AAPL"}}]},
            # 嵌套深层叶子非法 op
            {"where": [{"or": [
                {"and": [{"field": "ts", "op": "~", "value": 1}]},
            ]}]},
            # 非法 between 形态藏在嵌套里
            {"where": [{"or": [
                {"field": "ts", "op": "between", "value": [1]},
            ]}]},
        ]
        with _client(f"sqlite://{demo_db_path}") as client:
            for body in cases:
                resp = client.post("/api/table/ticks/rows", json=body)
                assert resp.status_code == 422, f"{body} 应 422，得到 {resp.status_code}"
                assert "detail" in resp.json()

    def test_delete_with_or_group(self, copy_db_uri):
        with _client(copy_db_uri, mode="write") as client:
            resp = client.post("/api/table/notes/delete", json={
                "where": [{"or": [
                    {"field": "id", "op": "==", "value": 0},
                    {"field": "id", "op": "==", "value": 2},
                ]}],
            })
            assert resp.status_code == 200
            assert resp.json()["deleted"] == 2

            remain = client.post("/api/table/notes/rows", json={"limit": 100})
            assert [r["id"] for r in remain.json()["rows"]] == [1]

    def test_export_with_or_group(self, demo_db_path):
        import urllib.parse
        where = [{"or": [
            {"field": "symbol", "op": "==", "value": "AAPL"},
            {"field": "symbol", "op": "==", "value": "MSFT"},
        ]}]
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.get(
                "/api/table/ticks/export?format=jsonl&where="
                + urllib.parse.quote(json.dumps(where))
            )
        assert resp.status_code == 200
        lines = [ln for ln in resp.text.strip().splitlines() if ln]
        assert len(lines) == TICKS_TOTAL


class TestCapabilityGating:
    """D5 · 能力门控：无 time 契约的表禁用 timeseries 快捷查询。"""

    def test_query_gated_without_time_contract(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/notes/query", json={})
            assert resp.status_code == 409

            resp = client.post(
                "/api/table/notes/latest", json={"symbols": ["x"]},
            )
            assert resp.status_code == 409

    def test_query_allowed_for_timeseries_table(self, demo_db_path):
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post("/api/table/ticks/query", json={
                "start": BASE_TS,
                "end": BASE_TS + DAY_MS,
            })
        assert resp.status_code == 200

    def test_calendar_partition_infers_time_capability(self, demo_db_path):
        # calendar 分区自动反推 time 契约，events 可按 date 查询而非 409
        with _client(f"sqlite://{demo_db_path}") as client:
            resp = client.post(
                "/api/table/events/query", json={"date": "2026-01-05"},
            )
        assert resp.status_code == 200
        assert len(resp.json()["rows"]) == EVENTS_TOTAL // 2


class TestCliUiCommand:
    """D6 · CLI 冒烟：--help 输出与 source 校验，不真正启动服务。"""

    def test_ui_help_lists_options(self):
        result = runner.invoke(cli_app, ["ui", "--help"])
        assert result.exit_code == 0, result.output
        assert "--write" in result.output
        assert "--port" in result.output
        assert "--no-browser" in result.output
        assert "--source" in result.output

    def test_ui_rejects_missing_source(self, tmp_path):
        result = runner.invoke(cli_app, ["ui", "-s", str(tmp_path / "nope.iosql")])
        assert result.exit_code != 0, result.output
        assert "数据库不存在" in result.output
