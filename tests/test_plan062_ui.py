"""PLAN-062 UI 修复回归测试（P0-10 / U1-1~U1-7 / P2 批次）。

前端 JS 无测试运行器：涉及前端的行为用「后端契约测试 + JS 静态内容断言」
覆盖；静态断言只检查关键的不可回退标记（如不再发送 dry_run）。
"""

import csv
import io
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from iosql import Database, Segment
from iosql.ui import create_app
from fastapi.testclient import TestClient

import iosql.ui.app as ui_app

DAY_MS = 86_400_000
BASE_TS = 1735689600000

STATIC_DIR = Path(ui_app.__file__).parent / "static"


def _build_db(db_dir: Path) -> None:
    """构建测试库：ticks 时序 + prices（排序列含 NULL / 公式注入载荷）
    + 中文/引号/换行表名。"""
    db = Database(f"sqlite://{db_dir}")

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
        for i in range(12):
            rows.append({
                "symbol": "AAPL",
                "ts": BASE_TS + day * DAY_MS + i * 1000,
                "price": 100.0 + day + i * 0.1,
                "volume": 1000 + i,
            })
    ticks.write(rows)

    prices = db.table(
        "prices",
        pk=["id"],
        columns={"id": "integer", "score": "integer", "name": "text"},
    )
    prices.write([
        {"id": 1, "score": None, "name": "=SUM(A1)"},
        {"id": 2, "score": 5, "name": "+1"},
        {"id": 3, "score": None, "name": "-2"},
        {"id": 4, "score": 7, "name": "@x"},
        {"id": 5, "score": 5, "name": "\tTAB"},
        {"id": 6, "score": 9, "name": "\rCR"},
        {"id": 7, "score": 3, "name": "普通"},
    ])

    for name, txt in (
        ("空 白", "值"),
        ('引"号', "含引号"),
        ("换\n行", "含换行"),
    ):
        t = db.table(name, pk=["id"], columns={"id": "integer", "txt": "text"})
        t.write([{"id": 1, "txt": txt}])

    db.close()


@pytest.fixture(scope="module")
def demo_uri(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("plan062_ui")
    db_dir = root / "demo.iosql"
    _build_db(db_dir)
    return f"sqlite://{db_dir}"


@pytest.fixture
def copy_uri(demo_uri: str, tmp_path: Path) -> str:
    src = Path(demo_uri[len("sqlite://"):])
    target = tmp_path / "copy.iosql"
    shutil.copytree(src, target)
    return f"sqlite://{target}"


def _client(uri: str, mode: str = "readonly", **kwargs) -> TestClient:
    return TestClient(
        create_app(uri, mode),
        base_url="http://localhost",
        **kwargs,
    )


# ---------------------------------------------------------------- P0-10


class TestStrictWriteBodies:
    """P0-10：写请求模型 extra=forbid；repartition 不再吞 dry_run。"""

    EXTRA_BODIES = [
        ("/api/table/prices/write", {"rows": [{"id": 99}], "bogus": 1}),
        ("/api/table/prices/delete", {"where": [], "bogus": 1}),
        ("/api/table/prices/truncate", {"confirm": "prices", "bogus": 1}),
        ("/api/table/prices/create_index", {"column": "score", "bogus": 1}),
        ("/api/table/prices/compact", {"bogus": 1}),
        ("/api/table/prices/cleanup_obsolete", {"dry_run": True, "bogus": 1}),
        (
            "/api/table/ticks/repartition",
            {
                "target_uri": "sqlite:///tmp/never.iosql",
                "partition_str": "duration(ts,every=1hour)",
                "dry_run": True,
            },
        ),
    ]

    def test_unknown_fields_rejected_422(self, copy_uri):
        with _client(copy_uri, mode="write") as client:
            for path, body in self.EXTRA_BODIES:
                resp = client.post(path, json=body)
                assert resp.status_code == 422, (path, resp.status_code, resp.text)
                assert "detail" in resp.json()

    def test_repartition_dry_run_key_rejected(self, copy_uri):
        """旧前端的 dry_run:true 必须 422，而不是静默变成真实写库。"""
        with _client(copy_uri, mode="write") as client:
            resp = client.post("/api/table/ticks/repartition", json={
                "target_uri": "sqlite:///tmp/never.iosql",
                "partition_str": "duration(ts,every=1hour)",
                "dry_run": True,
            })
        assert resp.status_code == 422
        assert "dry_run" in resp.json()["hint"] or "Extra" in resp.json()["hint"]

    def test_repartition_works_without_preview(self, copy_uri, tmp_path):
        target_uri = f"sqlite://{tmp_path / 'hourly.iosql'}"
        with _client(copy_uri, mode="write") as client:
            resp = client.post("/api/table/ticks/repartition", json={
                "target_uri": target_uri,
                "partition_str": "duration(ts,every=1hour)",
            })
        assert resp.status_code == 200
        assert resp.json()["result"]["rows_copied"] == 36

    def test_frontend_drops_false_preview(self):
        text = (STATIC_DIR / "js" / "tab-maintain.js").read_text(encoding="utf-8")
        assert "Dry-run 预览" not in text
        assert "bodyOf(true)" not in text
        assert "执行重分区（写入新库）" in text
        # 与其他破坏性操作一致：手输表名确认
        assert "typeName: ctx.table" in text


# ---------------------------------------------------------------- U1-1


class TestNullKeysetPagination:
    """U1-1：排序列含 NULL 时 keyset 分页从首页即 422；过滤 NULL 后
    翻页仍能不重不漏覆盖全部行。"""

    def test_null_sort_asc_rejected_with_hint(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.post("/api/table/prices/rows", json={
                "order_by": "score", "limit": 2,
            })
        assert resp.status_code == 422
        payload = resp.json()
        assert "NULL" in payload["detail"]
        assert "rows" not in payload  # 明确拒绝，不返回半截页

    def test_null_sort_desc_rejected_with_hint(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.post("/api/table/prices/rows", json={
                "order_by": "score desc", "limit": 2,
            })
        assert resp.status_code == 422
        assert "NULL" in resp.json()["detail"]

    def _collect(self, uri: str, order_by: str, limit: int) -> list:
        collected = []
        cursor = None
        pages = 0
        with _client(uri) as client:
            while True:
                body = {
                    "where": [{"field": "score", "op": ">=", "value": 0}],
                    "order_by": order_by,
                    "limit": limit,
                }
                if cursor is not None:
                    body["cursor"] = cursor
                resp = client.post("/api/table/prices/rows", json=body)
                assert resp.status_code == 200, resp.text
                payload = resp.json()
                collected.extend(row["id"] for row in payload["rows"])
                pages += 1
                cursor = payload["next_cursor"]
                if cursor is None:
                    break
                assert pages < 10, "翻页未收敛"
        return collected

    def test_filtered_nulls_still_fully_paginated_asc(self, demo_uri):
        collected = self._collect(demo_uri, "score", 2)
        assert sorted(collected) == [2, 4, 5, 6, 7]  # NULL 已被 where 过滤
        assert len(collected) == len(set(collected))

    def test_filtered_nulls_still_fully_paginated_desc(self, demo_uri):
        collected = self._collect(demo_uri, "score desc", 2)
        assert sorted(collected) == [2, 4, 5, 6, 7]
        assert len(collected) == len(set(collected))

    def test_frontend_disables_bad_sort_column(self):
        text = (STATIC_DIR / "js" / "tab-data.js").read_text(encoding="utf-8")
        assert "badCols" in text
        assert "sort-bad" in text
        assert "排序列" in text


# ---------------------------------------------------------------- U1-2 / U1-3


class TestResponseEnvelopes:
    """U1-2/U1-3：后端包裹键固定；前端逐一解包。"""

    def test_maintain_envelopes(self, copy_uri):
        with _client(copy_uri, mode="write") as client:
            report = client.get("/api/table/ticks/layout_report").json()
            assert set(report) == {"report"}
            assert report["report"]["total_rows"] == 36

            validation = client.post(
                "/api/table/ticks/validate", json={"deep": False}
            ).json()
            assert set(validation) == {"validation"}
            assert validation["validation"]["ok"] is True

            compact = client.post("/api/table/ticks/compact", json={}).json()
            assert set(compact) == {"result"}
            assert compact["result"]["rows_read"] == 36

            cleanup = client.post(
                "/api/table/ticks/cleanup_obsolete", json={"dry_run": True}
            ).json()
            assert set(cleanup) == {"result"}

    def test_explain_envelope(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.post("/api/table/ticks/explain", json={
                "where": [{"field": "symbol", "op": "==", "value": "AAPL"}],
            })
        assert resp.status_code == 200
        assert set(resp.json()) == {"plan"}

    def test_frontend_unwraps_all_envelopes(self):
        maintain = (STATIC_DIR / "js" / "tab-maintain.js").read_text(encoding="utf-8")
        assert "IU.unwrap(resp, 'report')" in maintain
        assert "IU.unwrap(resp, 'validation')" in maintain
        assert "IU.unwrap(resp, 'result')" in maintain

        query = (STATIC_DIR / "js" / "tab-data.js").read_text(encoding="utf-8")
        assert "IU.unwrap(resp, 'plan')" in query


# ---------------------------------------------------------------- U1-4


class TestExportFilename:
    """U1-4：Content-Disposition 转义；非 ASCII/引号/换行表名导出 200。"""

    def _disposition(self, client, name: str) -> str:
        resp = client.get(f"/api/table/{name}/export", params={"format": "csv"})
        assert resp.status_code == 200, (name, resp.status_code, resp.text)
        assert "内部错误" not in resp.text
        return resp.headers["content-disposition"]

    def test_chinese_table_name(self, demo_uri):
        import urllib.parse
        with _client(demo_uri) as client:
            header = self._disposition(client, urllib.parse.quote("空 白"))
        assert 'filename="' in header
        assert "filename*=UTF-8''" in header
        assert "%E7%A9%BA" in header  # "空" 的 UTF-8 百分号编码

    def test_quote_table_name(self, demo_uri):
        import urllib.parse
        with _client(demo_uri) as client:
            header = self._disposition(client, urllib.parse.quote('引"号'))
        # ASCII 降级名里不允许出现裸引号（否则畸形 header）
        ascii_part = header.split('filename="', 1)[1].split('"', 1)[0]
        assert '"' not in ascii_part and "\n" not in ascii_part

    def test_newline_table_name(self, demo_uri):
        import urllib.parse
        with _client(demo_uri) as client:
            header = self._disposition(client, urllib.parse.quote("换\n行"))
        assert "\n" not in header and "\r" not in header


# ---------------------------------------------------------------- P2 CSV


class TestCsvSafetyAndEmptyHeader:
    def test_formula_injection_prefix(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.get(
                "/api/table/prices/export", params={"format": "csv"},
            )
        assert resp.status_code == 200
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        by_id = {int(r["id"]): r["name"] for r in rows}
        assert by_id[1] == "'=SUM(A1)"
        assert by_id[2] == "'+1"
        assert by_id[3] == "'-2"
        assert by_id[4] == "'@x"
        assert by_id[5].startswith("'\t")
        assert by_id[6].startswith("'\r")
        assert by_id[7] == "普通"

    def test_empty_result_still_writes_header(self, demo_uri):
        where = json.dumps({"field": "id", "op": "==", "value": 9999})
        with _client(demo_uri) as client:
            resp = client.get(
                "/api/table/prices/export",
                params={"format": "csv", "where": where},
            )
        assert resp.status_code == 200
        first_line = resp.text.splitlines()[0]
        assert first_line == "id,score,name"


# ---------------------------------------------------------------- P2 422


class TestValidation422:
    """P2：非法 body 一律 422（此前 dict/list 直达核心引发 500）。"""

    def test_query_start_end_non_int(self, demo_uri):
        with _client(demo_uri) as client:
            for key in ("start", "end"):
                resp = client.post("/api/table/ticks/query", json={key: {"a": 1}})
                assert resp.status_code == 422, (key, resp.status_code)

    def test_latest_unknown_column(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.post("/api/table/ticks/latest", json={
                "columns": ["symbol", "nope"],
            })
        assert resp.status_code == 422
        payload = resp.json()
        assert "nope" in payload["detail"]
        assert "symbol" in payload["hint"]

    def test_where_in_between_non_scalar_elements(self, demo_uri):
        cases = [
            {"where": [{"field": "id", "op": "in", "value": [{"x": 1}]}]},
            {"where": [{"field": "id", "op": "between", "value": [{"x": 1}, 2]}]},
        ]
        with _client(demo_uri) as client:
            for body in cases:
                resp = client.post("/api/table/prices/rows", json=body)
                assert resp.status_code == 422, (body, resp.status_code, resp.text)

    def test_partition_dict_rejected(self, copy_uri):
        with _client(copy_uri, mode="write") as client:
            for path in ("compact", "cleanup_obsolete"):
                resp = client.post(f"/api/table/ticks/{path}", json={
                    "partition": {"a": 1}, "dry_run": True,
                })
                assert resp.status_code == 422, (path, resp.status_code)

    def test_500_handler_hides_exception_text(self, demo_uri, monkeypatch):
        def _boom(table):
            raise RuntimeError("secret-path-/tmp/private")

        monkeypatch.setattr(ui_app, "fast_row_count", _boom)
        with _client(demo_uri, raise_server_exceptions=False) as client:
            resp = client.post("/api/table/prices/count", json={})
        assert resp.status_code == 500
        payload = resp.json()
        assert payload["detail"] == "内部错误"
        assert "secret-path" not in json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- P2 Host


class TestHostHeader:
    """P2：TrustedHost 只放行本机回环 Host（无 CORS）。"""

    def test_evil_host_rejected(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.get("/api/db", headers={"Host": "evil.example.com"})
        assert resp.status_code == 400

    def test_loopback_hosts_allowed(self, demo_uri):
        with _client(demo_uri) as client:
            for host in ("localhost", "127.0.0.1:8000", "[::1]:8000"):
                resp = client.get("/api/db", headers={"Host": host})
                assert resp.status_code == 200, (host, resp.status_code)


# ---------------------------------------------------------------- P2 预算


class TestCountBudget:
    """P2：/count 过滤统计扫描超预算 422；无筛选走 manifest 不受限。"""

    def test_filtered_count_budget(self, demo_uri, monkeypatch):
        monkeypatch.setattr(ui_app, "MAX_COUNT_SCAN_ROWS", 2)
        with _client(demo_uri) as client:
            resp = client.post("/api/table/prices/count", json={
                "where": [{"field": "id", "op": ">=", "value": 0}],
            })
        assert resp.status_code == 422
        assert "缩小" in resp.json()["hint"]

    def test_unfiltered_count_unaffected(self, demo_uri, monkeypatch):
        monkeypatch.setattr(ui_app, "MAX_COUNT_SCAN_ROWS", 0)
        with _client(demo_uri) as client:
            resp = client.post("/api/table/prices/count", json={})
        assert resp.status_code == 200
        assert resp.json()["count"] == 7


# ---------------------------------------------------------------- U1-5 / U1-6


class TestRowsLimitAndPaging:
    def test_rows_limit_passthrough(self, demo_uri):
        with _client(demo_uri) as client:
            resp = client.post("/api/table/prices/rows", json={
                "order_by": "id", "limit": 3,
            })
        assert resp.status_code == 200
        payload = resp.json()
        assert [r["id"] for r in payload["rows"]] == [1, 2, 3]
        assert payload["next_cursor"] == [3]

    def test_frontend_prev_page_and_seq(self):
        text = (STATIC_DIR / "js" / "tab-data.js").read_text(encoding="utf-8")
        assert "s.pos--" in text
        assert "pageSeq" in text
        assert "spec.primary_key" in text and "spec.pk" not in text


# ---------------------------------------------------------------- P2 truncated


class TestQueryTruncated:
    def test_truncated_only_when_extra_row(self, demo_uri):
        with _client(demo_uri) as client:
            small = client.post("/api/table/ticks/query", json={"limit": 5})
            large = client.post("/api/table/ticks/query", json={"limit": 5000})
        assert small.status_code == 200
        assert len(small.json()["rows"]) == 5
        assert small.json()["truncated"] is True

        assert large.status_code == 200
        assert len(large.json()["rows"]) == 36
        assert large.json()["truncated"] is False


# ---------------------------------------------------------------- U1-7


class TestImportBufferAbsence:
    """U1-7：import_buffer 从未实现，读写两种模式都不存在该端点。

    设计文档 docs/design/ui-20260823-overview.md:276 仍列出该端点，
    需文档侧删除（本任务不修改设计文档）。
    """

    def test_import_buffer_absent_in_write_mode(self, copy_uri):
        with _client(copy_uri, mode="write") as client:
            resp = client.post("/api/table/prices/import_buffer", json={})
        assert resp.status_code == 404


# ---------------------------------------------------------------- 前端清理


class TestFrontendHygiene:
    def test_menu_listener_is_delegated_once(self):
        text = (STATIC_DIR / "js" / "util.js").read_text(encoding="utf-8")
        assert text.count("document.addEventListener('click'") == 1
        assert "querySelectorAll('.menu.open')" in text

    def test_chart_resize_listener_cleanup(self):
        text = (STATIC_DIR / "js" / "tab-charts.js").read_text(encoding="utf-8")
        assert "resizeHandler" in text
        assert "removeEventListener('resize'" in text

    def test_sidebar_failure_placeholder_unified(self):
        text = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        assert "统计失败" in text
