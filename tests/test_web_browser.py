"""浏览器回归：覆盖 Web 的真实交互和异步状态，而非仅检查静态资源。"""

import json
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
playwright = pytest.importorskip("playwright.sync_api")

from iosql import Database, Segment
from iosql.web import create_app


BASE = 1735689600000
DAY = 86400000


def _demo_database(path):
    with Database(f"sqlite://{path}") as db:
        ticks = db.timeseries(
            "ticks", time="ts", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition_every="1day", segment=Segment.rows(max_rows=50),
        )
        rows = []
        for day, count in [(0, 150), (1, 20), (3, 10)]:
            for i in range(count):
                rows.append({"symbol": "AAA" if i % 2 == 0 else "BBB",
                             "ts": BASE + day * DAY + i * 1000, "price": 100.0 + i})
        ticks.write(rows)
        notes = db.table("notes", pk=["id"], columns={
            "id": "integer", "text": "text", "flag": "boolean", "date": "date", "compact_date": "date",
        })
        notes.write([{"id": i, "text": f"note-{i}", "flag": i % 2 == 0,
                      "date": f"2025-01-0{i + 1}", "compact_date": f"2025010{i + 1}"} for i in range(3)])


@pytest.fixture(scope="module")
def web_servers(tmp_path_factory):
    """使用独立临时库及回环端口，测试不会访问用户库。"""
    db_path = tmp_path_factory.mktemp("browser-db") / "demo.iosql"
    _demo_database(db_path)
    running = []
    urls = {}
    try:
        for mode in ("readonly", "write"):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            server = uvicorn.Server(uvicorn.Config(
                create_app(f"sqlite://{db_path}", mode), log_level="error", lifespan="on"
            ))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            running.append((server, thread, sock))
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                pytest.fail("测试 Web 服务未能启动")
            urls[mode] = f"http://127.0.0.1:{sock.getsockname()[1]}"
        yield urls
    finally:
        for server, thread, _ in running:
            server.should_exit = True
            thread.join(timeout=10)
        for _, _, sock in running:
            sock.close()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("未安装 Playwright Chromium 浏览器")
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, timezone_id="Asia/Shanghai")
    page = context.new_page()
    yield page
    context.close()


def test_data_and_overview_load_expensive_statistics_only_on_demand(page, web_servers):
    requests = []
    page.on("request", lambda request: requests.append(request.url.split("?", 1)[0]))
    page.goto(web_servers["readonly"])
    playwright.expect(page.get_by_role("link", name="打开 ticks 的数据")).to_be_visible()
    playwright.expect(page.locator("#stat-total-rows")).to_have_text("—")
    playwright.expect(page.locator('[data-part-signal="ticks"]')).to_contain_text("未加载")
    page.wait_for_load_state("networkidle")

    def requests_to(endpoint):
        return [url for url in requests if url.endswith("/" + endpoint)]

    assert not requests_to("stats")
    assert not requests_to("segments")
    assert not requests_to("count")
    assert not requests_to("mchart3-0.5.0.min.js")

    page.get_by_role("link", name="打开 ticks 的数据").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：未统计")
    page.get_by_role("combobox", name="每页行数").select_option("100")
    page.get_by_role("button", name="下一页").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(80)
    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.wait_for_load_state("networkidle")
    assert not requests_to("stats")
    assert not requests_to("segments")
    assert not requests_to("count")
    assert not requests_to("mchart3-0.5.0.min.js")

    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_have_text("总行数：90 · 1 个表头过滤已生效")
    assert len(requests_to("count")) == 1
    page.get_by_role("tab", name="3Structure").click()
    playwright.expect(page.locator(".bar-line")).to_have_count(3)
    playwright.expect(page.get_by_text("物理 Segment")).to_be_visible()
    page.wait_for_load_state("networkidle")
    assert len(requests_to("segments")) == 1
    assert not requests_to("stats")
    page.get_by_role("tab", name="2Data").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("tab", name="3Structure").click()
    playwright.expect(page.locator(".bar-line")).to_have_count(3)
    page.wait_for_load_state("networkidle")
    assert len(requests_to("segments")) == 2  # 再次进入时刷新，共享给明细和分布


def test_data_count_failure_can_retry(page, web_servers):
    calls = []

    def count(route):
        calls.append(1)
        if len(calls) == 1:
            route.fulfill(status=503, json={"detail": "统计暂不可用"})
        else:
            route.continue_()

    page.route("**/api/table/ticks/count", count)
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("统计暂不可用")
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (url, opts) => String(url).endsWith('/count')
        ? new Promise((resolve, reject) => setTimeout(() => original(url, opts).then(resolve, reject), 1200))
        : original(url, opts);
    }""")
    page.get_by_role("button", name="重试统计").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("统计中…")
    playwright.expect(page.locator(".pager-bar").last).not_to_contain_text("null")
    playwright.expect(page.locator(".pager-bar").last).to_have_text("总行数：180")
    assert len(calls) == 2


def test_statusbar_remains_readable_at_different_sizes(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    assert " · · " not in page.locator("#status-left").inner_text()
    for width in (390, 801, 901, 1101, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        for zoom in ("A−", "标准", "A+"):
            page.get_by_role("button", name=zoom, exact=True).click()
            metrics = page.locator("#statusbar").evaluate("""bar => {
              const bounds = bar.getBoundingClientRect();
              return {scrollWidth: bar.scrollWidth, clientWidth: bar.clientWidth,
                statusWidth: document.getElementById('status-left').getBoundingClientRect().width,
                children: Array.from(bar.children).filter(c => getComputedStyle(c).display !== 'none')
                  .map(c => { const r = c.getBoundingClientRect(); return {
                    top: r.top - bounds.top, bottom: r.bottom - bounds.bottom,
                    left: r.left - bounds.left, right: r.right - bounds.right
                  }; })};
            }""")
            assert metrics["scrollWidth"] <= metrics["clientWidth"] + 1, (width, zoom, metrics)
            assert metrics["statusWidth"] > 0, (width, zoom, metrics)
            assert all(c["top"] >= -1 and c["bottom"] <= 1 and c["left"] >= -1 and c["right"] <= 1
                       for c in metrics["children"]), (width, zoom, metrics)


def test_data_reentry_keyboard_paging_and_clear(page, web_servers):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(web_servers["readonly"])
    page.get_by_role("button", name="统计全部表").click()
    playwright.expect(page.locator("#stat-total-rows")).to_have_text("183")
    page.get_by_role("link", name="打开 ticks 的数据").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("combobox", name="每页行数").select_option("100")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(100)
    playwright.expect(page.get_by_text("第 1 页", exact=False)).to_be_visible()
    page.locator("body").press("ArrowRight")
    playwright.expect(page.get_by_text("第 2 页", exact=False)).to_be_visible()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(80)
    page.locator("body").press("ArrowLeft")
    playwright.expect(page.get_by_text("第 1 页", exact=False)).to_be_visible()

    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：90")
    page.get_by_role("tab", name="2Data").focus()
    page.get_by_role("tab", name="2Data").press("ArrowRight")
    playwright.expect(page.get_by_role("tab", name="3Structure")).to_have_attribute("aria-selected", "true")
    page.get_by_role("tab", name="2Data").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")
    assert "f.symbol=AAA" in page.url
    fresh = page.context.new_page()
    try:
        fresh.goto(page.url)
        playwright.expect(fresh.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")
        playwright.expect(fresh.locator(".browse-table tbody tr")).to_have_count(90)
    finally:
        fresh.close()
    page.get_by_role("button", name="清空", exact=True).click()
    playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("")
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：180")
    assert "f.symbol" not in page.url
    assert not errors


def test_partition_navigation_keeps_filter_and_column_url_shareable(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("button", name="⚙ 列").click()
    page.locator('.col-picker label').filter(has_text="price").locator('input').uncheck()
    page.get_by_role("tab", name="3Structure").click()
    playwright.expect(page.locator(".bar-line")).to_have_count(3)
    page.locator(".bar-line").first.click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(75)
    playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")
    assert "f.symbol=AAA" in page.url
    assert "f.ts=" in page.url
    assert "cols=" in page.url
    fresh = page.context.new_page()
    try:
        fresh.goto(page.url)
        playwright.expect(fresh.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")
        playwright.expect(fresh.locator(".browse-table tbody tr")).to_have_count(75)
        playwright.expect(fresh.get_by_role("columnheader", name="price", exact=True)).to_have_count(0)
    finally:
        fresh.close()
    page.locator('[data-nav="table:notes"]').click()
    playwright.expect(page.get_by_role("textbox", name="筛选列 id")).to_have_value("")
    assert "f.symbol" not in page.url and "f.ts" not in page.url and "cols=" not in page.url


def test_data_view_switch_keeps_url_in_sync(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("button", name="时序", exact=True).click()
    assert "f.symbol=AAA" in page.url and "view=timeseries" in page.url
    fresh = page.context.new_page()
    try:
        fresh.goto(page.url)
        playwright.expect(fresh.get_by_role("button", name="时序", exact=True)).to_have_attribute("aria-pressed", "true")
    finally:
        fresh.close()
    page.get_by_role("button", name="浏览", exact=True).click()
    assert "view=" not in page.url and "f.symbol=AAA" in page.url
    playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")


def test_browse_survives_repeated_latest_view_switches(page, web_servers):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("button", name="截面", exact=True).click()
    page.get_by_role("button", name="浏览", exact=True).click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)

    for iteration in range(4):
        page.get_by_role("button", name="截面", exact=True).click()
        if iteration == 0:
            page.get_by_placeholder("逗号分隔：AAPL,MSFT").fill("AAA")
            with page.expect_response(lambda response: response.url.endswith("/latest")) as response:
                page.get_by_role("button", name="最新截面").click()
            assert response.value.ok
            playwright.expect(page.get_by_text("/latest 截面结果")).to_be_visible()
        page.get_by_role("button", name="浏览", exact=True).click()
        playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
        playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")
        assert "f.symbol=AAA" in page.url and "view=" not in page.url
        assert page.locator(".browse-table tbody tr").evaluate_all(
            "rows => rows.every(row => row.cells[0].textContent === 'AAA')"
        )

    assert not errors


def test_browse_recovers_when_switching_views_during_a_request(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)

    # 模拟请求离开视图后才完成，即使底层响应未及时停止也不能覆盖新条件。
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (url, opts) => {
        if (String(url).endsWith('/rows') && !window.__oldRowsSignal) {
          window.__oldRowsSignal = opts.signal;
          return new Promise((resolve, reject) => {
            window.__releaseOldRows = () => original(url, {...opts, signal: undefined})
              .then(response => {
                const read = response.text.bind(response);
                response.text = async () => {
                  const result = await read();
                  window.__oldRowsParsed = true;
                  return result;
                };
                resolve(response);
              }, reject);
          });
        }
        return original(url, opts);
      };
    }""")
    page.get_by_role("button", name="刷新", exact=True).click()
    page.wait_for_function("window.__oldRowsSignal")
    page.get_by_role("button", name="截面", exact=True).click()
    page.wait_for_function("window.__oldRowsSignal.aborted")
    page.get_by_role("button", name="浏览", exact=True).click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("textbox", name="筛选列 symbol").fill("BBB")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    playwright.expect(page.locator(".browse-table tbody tr").first.locator("td").first).to_have_text("BBB")
    page.evaluate("() => window.__releaseOldRows()")
    page.wait_for_function("window.__oldRowsParsed === true")
    assert page.locator(".browse-table tbody tr").evaluate_all(
        "rows => rows.every(row => row.cells[0].textContent === 'BBB')"
    )


@pytest.mark.parametrize("navigation", ["structure", "timeseries"])
def test_filter_before_debounce_is_applied_when_returning(page, web_servers, navigation):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("combobox", name="每页行数").select_option("100")
    page.get_by_role("button", name="下一页").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(80)
    page.evaluate("""navigation => {
      const input = document.querySelector('[aria-label="筛选列 symbol"]');
      input.value = 'AAA';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      if (navigation === 'structure') document.getElementById('tab-structure').click();
      else Array.from(document.querySelectorAll('.seg-control button'))
        .find(button => button.textContent === '时序').click();
    }""", navigation)
    if navigation == "structure":
        playwright.expect(page.get_by_role("tab", name="3Structure")).to_have_attribute("aria-selected", "true")
        page.get_by_role("tab", name="2Data").click()
    else:
        playwright.expect(page.get_by_role("button", name="时序", exact=True)).to_have_attribute("aria-pressed", "true")
        page.get_by_role("button", name="浏览", exact=True).click()
    playwright.expect(page.get_by_text("第 1 页", exact=False)).to_be_visible()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("AAA")
    assert "f.symbol=AAA" in page.url
    assert page.locator(".browse-table tbody tr").evaluate_all(
        "rows => rows.every(row => row.cells[0].textContent === 'AAA')"
    )


def test_structure_proportions_and_exclusive_partition_boundary(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/structure")
    bars = page.locator(".bar-line")
    playwright.expect(bars).to_have_count(3)
    widths = bars.locator(".bar-fill").evaluate_all("els => els.map(e => parseFloat(e.style.width))")
    assert widths == pytest.approx([100, 100 * 20 / 150, 100 * 10 / 150], abs=0.001)
    bars.first.click()
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：150")
    playwright.expect(page.get_by_role("textbox", name="筛选列 ts")).to_have_value(
        f"{BASE}~<{BASE + DAY}"
    )
    assert "ts <" in page.locator(".where-preview").inner_text()


@pytest.mark.parametrize("every,width", [
    ("6hour", 6 * 3600000), ("2hours", 2 * 3600000), ("90min", 90 * 60000),
    ("42ms", 42), ("2s", 2000), ("1day", DAY), ("2week", 14 * DAY),
    ("1month", 30 * DAY), ("1year", 365 * DAY), ("hour", 3600000),
    ("0hour", None), ("1M", None), ("1.5hour", None), (True, None),
    ("9007199254740000ms", None),
])
def test_duration_partition_links_accept_only_safe_intervals(page, web_servers, every, width):
    def spec(route):
        response = route.fetch()
        payload = response.json()
        payload["spec"]["partition"]["every"] = every
        route.fulfill(response=response, json=payload)

    page.route("**/api/table/ticks/spec", spec)
    page.goto(web_servers["readonly"] + "/#/table/ticks/structure")
    playwright.expect(page.locator(".bar-line")).to_have_count(3)
    links = page.locator("a.bar-line")
    if width is None:
        playwright.expect(links).to_have_count(0)
    else:
        playwright.expect(links).to_have_count(3)
        query = links.first.get_attribute("href").split("?", 1)[1]
        assert parse_qs(query)["f.ts"] == [f"{BASE}~<{BASE + width}"]


def test_statistics_failure_retry_and_incomplete_total(page, web_servers):
    calls = {"stats": 0, "segments": 0}

    def stats(route):
        calls["stats"] += 1
        if calls["stats"] == 1:
            route.fulfill(status=503, json={"detail": "统计暂不可用"})
        else:
            route.continue_()

    def segments(route):
        calls["segments"] += 1
        if calls["segments"] == 1:
            route.fulfill(status=503, json={"detail": "分布暂不可用"})
        else:
            route.continue_()

    page.route("**/api/table/ticks/stats", stats)
    page.route("**/api/table/ticks/segments", segments)
    page.goto(web_servers["readonly"])
    page.get_by_role("button", name="统计全部表").click()
    actions = page.locator('[data-overview-actions="ticks"]')
    retry = actions.get_by_role("button", name="重试统计")
    playwright.expect(retry).to_be_visible()
    playwright.expect(page.locator("#stat-total-rows")).to_have_text("—")
    playwright.expect(page.locator("#stat-total-hint")).to_contain_text("1 张失败")
    playwright.expect(page.locator('[data-part-signal="ticks"]')).to_contain_text("未加载")
    retry.click()
    playwright.expect(page.locator("#stat-total-rows")).to_have_text("183")
    actions.get_by_role("button", name="加载分布").click()
    retry_dist = actions.get_by_role("button", name="重试分布")
    playwright.expect(retry_dist).to_be_visible()
    retry_dist.click()
    playwright.expect(page.locator('[data-part-signal="ticks"]')).to_contain_text("偏斜")
    playwright.expect(page.locator('[data-part-signal="ticks"] .tc-signal')).to_have_count(0)
    page.locator('[data-overview-actions="notes"]').get_by_role("button", name="加载分布").click()
    playwright.expect(page.locator('[data-part-signal="notes"]')).to_contain_text("单分区")


def test_mobile_navigation_empty_and_readonly_maintain(page, web_servers):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    playwright.expect(page.get_by_role("tab", name="4Charts")).to_have_attribute("aria-selected", "true")
    assert page.get_by_role("tab", name="4Charts").evaluate("""el => {
      const tab = el.getBoundingClientRect(), bar = el.parentElement.getBoundingClientRect();
      return tab.left >= bar.left - 1 && tab.right <= bar.right + 1;
    }""")
    menu = page.get_by_role("button", name="打开表列表")
    playwright.expect(menu).to_be_visible()
    menu.click()
    playwright.expect(page.locator("#sidebar")).to_be_visible()
    page.get_by_role("link", name="▸ ticks", exact=False).click()
    playwright.expect(page.locator("#sidebar")).to_be_hidden()
    playwright.expect(page.locator(".browse-table")).to_be_visible()
    page.get_by_role("textbox", name="筛选列 symbol").fill("NOT-FOUND")
    playwright.expect(page.get_by_text("没有匹配的行。", exact=False)).to_be_visible()
    page.get_by_role("button", name="清空筛选").click()
    playwright.expect(page.get_by_role("textbox", name="筛选列 symbol")).to_have_value("")
    page.get_by_role("tab", name="5Maintain").click()
    playwright.expect(page.get_by_text("维护台在只读模式下不可用")).to_be_visible()
    playwright.expect(page.get_by_text("iosql web -s <库路径> --write")).to_be_visible()


def test_data_loading_and_failure_offer_retry(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.evaluate("""() => {
      const original = window.fetch;
      let fail = true;
      window.fetch = (url, opts) => {
        if (String(url).endsWith('/rows') && fail) {
          fail = false;
          return new Promise(resolve => setTimeout(() => resolve(new Response(
            JSON.stringify({detail: '模拟失败'}), {status: 503, headers: {'Content-Type':'application/json'}})), 500));
        }
        return original(url, opts);
      };
    }""")
    page.get_by_role("button", name="刷新", exact=True).click()
    playwright.expect(page.get_by_role("button", name="上一页")).to_be_disabled()
    playwright.expect(page.get_by_role("button", name="下一页")).to_be_disabled()
    playwright.expect(page.get_by_role("button", name="重试", exact=True)).to_be_visible()
    playwright.expect(page.locator(".pager-bar").last).to_have_text("查询未完成")
    page.get_by_role("button", name="重试", exact=True).click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)


def test_delayed_responses_do_not_overwrite_current_view(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：180")
    page.evaluate("""() => {
      const original = window.fetch;
      window.__delayedCount = false;
      window.fetch = (url, opts) => {
        if (String(url).endsWith('/count') && opts && String(opts.body).includes('AAA')) {
          window.__delayedCount = true;
          return new Promise((resolve, reject) => setTimeout(() => original(url, opts).then(resolve, reject), 1200));
        }
        return original(url, opts);
      };
    }""")
    page.get_by_role("textbox", name="筛选列 symbol").fill("AAA")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("button", name="统计总行数").click()
    page.wait_for_function("window.__delayedCount === true")
    page.get_by_role("button", name="清空", exact=True).click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：180")
    page.wait_for_timeout(1400)
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：180")

    page.evaluate("""() => {
      const original = window.fetch;
      window.__delayedRows = false;
      window.fetch = (url, opts) => {
        if (String(url).endsWith('/rows') && !window.__delayedRows) {
          window.__delayedRows = true;
          return new Promise((resolve, reject) => setTimeout(() => original(url, opts).then(resolve, reject), 1200));
        }
        return original(url, opts);
      };
    }""")
    page.get_by_role("button", name="刷新", exact=True).click()
    page.get_by_role("tab", name="3Structure").click()
    page.get_by_role("tab", name="2Data").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.wait_for_timeout(1400)
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)


def test_cell_keyboard_detail_and_write_confirmation(page, web_servers):
    writes = []
    page.on("request", lambda request: writes.append(request.url) if request.url.endswith("/truncate") else None)
    page.goto(web_servers["write"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    cell = page.locator('.browse-table tbody td[tabindex="0"]')
    cell.focus()
    cell.press("ArrowRight")
    focused = page.locator('.browse-table tbody td:focus')
    playwright.expect(focused).to_have_attribute("aria-label", re.compile(r"^ts："))
    focused.press("Enter")
    playwright.expect(page.locator("#detail-panel")).to_have_attribute("aria-hidden", "false")
    page.keyboard.press("Escape")
    playwright.expect(page.locator("#detail-panel")).to_have_attribute("aria-hidden", "true")
    playwright.expect(focused).to_be_focused()
    focused.press("ArrowLeft")
    page.locator('.browse-table tbody td:focus').press("Enter")
    page.get_by_role("button", name="以此值过滤").click()
    playwright.expect(page.locator("#detail-panel")).to_have_attribute("aria-hidden", "true")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(90)
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：90")

    page.get_by_role("tab", name="5Maintain").click()
    trigger = page.get_by_role("button", name="清空整表…")
    trigger.click()
    dialog = page.get_by_role("dialog", name="清空整张表？")
    playwright.expect(dialog).to_be_visible()
    confirm = page.get_by_role("button", name="确认清空")
    playwright.expect(confirm).to_be_disabled()
    page.keyboard.press("Shift+Tab")
    playwright.expect(dialog.get_by_role("button", name="取消")).to_be_focused()
    page.get_by_placeholder('请输入 "ticks" 以确认').fill("ticks")
    playwright.expect(confirm).to_be_enabled()
    page.get_by_role("button", name="取消", exact=True).click()
    playwright.expect(trigger).to_be_focused()
    assert writes == []


def test_charts_and_overview_empty_state(page, web_servers):
    requests = []
    engine_requests = []
    page.on("request", lambda request: requests.append(request.url) if request.url.endswith("/chart") else None)
    page.on("request", lambda request: engine_requests.append(request.url)
            if request.url.endswith("/mchart3-0.5.0.min.js") else None)
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    playwright.expect(page.get_by_text("选择坐标并按需设置筛选条件", exact=False)).to_be_visible()
    assert not requests and not engine_requests
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-canvas")).to_be_visible()
    playwright.expect(page.locator(".chart-note").last).to_contain_text("180 条原始记录")
    assert len(engine_requests) == 1
    page.route("**/api/tables", lambda route: route.fulfill(json={"tables": []}))
    page.goto(web_servers["readonly"])
    playwright.expect(page.get_by_text("这个库还没有表")).to_be_visible()
    playwright.expect(page.get_by_role("button", name="复制示例")).to_be_visible()


def test_chart_engine_load_failure_can_retry(page, web_servers):
    calls = []
    requests = []

    def engine(route):
        calls.append(1)
        if len(calls) == 1:
            route.fulfill(status=503, body="unavailable")
        else:
            route.continue_()

    page.route("**/vendor/mchart3-0.5.0.min.js", engine)
    page.on("request", lambda request: requests.append(request.url) if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.get_by_text("图表引擎加载失败", exact=False)).to_be_visible()
    assert not requests
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("180 条原始记录")
    assert len(calls) == 2 and len(requests) == 1


def test_leaving_while_chart_engine_loads_does_not_start_query(page, web_servers):
    requests = []
    page.on("request", lambda request: requests.append(request.url) if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.evaluate("""() => {
      const append = document.head.appendChild.bind(document.head);
      document.head.appendChild = node => {
        if (String(node.src || '').includes('mchart3-0.5.0.min.js')) {
          window.__releaseChartEngine = () => append(node);
          return node;
        }
        return append(node);
      };
    }""")
    page.get_by_role("button", name="绘制", exact=True).click()
    page.wait_for_function("typeof window.__releaseChartEngine === 'function'")
    assert page.evaluate("typeof window.IU.tabs.charts.cleanup") == "function"
    page.get_by_role("tab", name="2Data").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    assert page.evaluate("window.IU.tabs.charts.cleanup === null")
    assert not requests
    page.evaluate("() => window.__releaseChartEngine()")
    page.wait_for_function("window.MChart && typeof window.MChart.Figure === 'function'")
    assert not requests


@pytest.mark.parametrize("x,y", [("id", "text"), ("text", "id"), ("text", "text"), ("id", "compact_date")])
def test_generic_chart_axes_work_for_a_table_without_symbol(page, web_servers, x, y):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    page.get_by_role("combobox", name="X 轴").select_option(x)
    page.get_by_role("combobox", name="Y 轴").select_option(y)
    playwright.expect(page.get_by_text("当前图型：散点图", exact=False)).to_be_visible()
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-canvas")).to_be_visible()
    playwright.expect(page.locator(".chart-note")).to_contain_text("3 条原始记录")
    assert not errors


def test_chart_accepts_physical_columns_when_contract_is_absent(page, web_servers):
    page.route("**/api/table/notes/spec", lambda route: route.fulfill(json={
        "name": "notes", "spec": None, "columns": {"id": "bigint", "text": "text"},
    }))
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    playwright.expect(page.get_by_role("combobox", name="X 轴")).to_have_value("id")
    playwright.expect(page.get_by_role("combobox", name="Y 轴")).to_have_value("text")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("3 条原始记录")


@pytest.mark.parametrize("column", ["date", "compact_date"])
def test_date_axis_preserves_calendar_days_and_zooms_locally(page, web_servers, column):
    requests = []
    page.on("request", lambda request: requests.append(request.url) if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    page.add_script_tag(url=web_servers["readonly"] + "/vendor/mchart3-0.5.0.min.js")
    page.evaluate("""() => {
      const Original = window.MChart.Figure;
      window.MChart = { Figure: class extends Original {
        addSubplot(...args) {
          const axes = super.addSubplot(...args), addLine = axes.addLineSeries.bind(axes);
          axes.addLineSeries = (x, y, options) => {
            window.__dateXs = [...x];
            return addLine(x, y, options);
          };
          return axes;
        }
      }};
    }""")
    page.get_by_role("combobox", name="X 轴").select_option(column)
    page.get_by_role("combobox", name="Y 轴").select_option("id")
    playwright.expect(page.get_by_text("当前图型：折线图", exact=False)).to_be_visible()
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("本地缩放")
    assert page.evaluate("window.__dateXs") == [BASE, BASE + DAY, BASE + 2 * DAY]
    page.locator(".chart-canvas").hover()
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(600)
    assert len(requests) == 1


def test_chart_zoom_reuses_drawn_conditions_and_double_click_resets(page, web_servers):
    requests = []
    page.on("request", lambda request: requests.append(request.post_data_json)
            if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.locator(".cond-row .field-sel").select_option("symbol")
    page.locator(".cond-row .val-input").fill("AAA")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("90 条原始记录")
    assert requests[0]["where"] == [{"field": "symbol", "op": "==", "value": "AAA"}]

    # 未点击绘制前修改条件，缩放仍应使用上一次绘制的筛选快照。
    page.locator(".cond-row .val-input").fill("BBB")
    page.locator(".chart-canvas").hover()
    with page.expect_request(lambda request: request.url.endswith("/chart")):
        page.mouse.wheel(0, -300)
    playwright.expect(page.locator(".chart-note")).to_contain_text("可视区")
    playwright.expect(page.locator(".chart-note")).not_to_contain_text("加载中")
    playwright.expect(page.locator(".chart-note")).not_to_contain_text("失败")
    assert requests[1]["where"][0]["value"] == "AAA"
    assert {item["op"] for item in requests[1]["where"][1:]} == {">=", "<="}
    with page.expect_request(lambda request: request.url.endswith("/chart")):
        page.locator(".chart-canvas").dblclick()
    playwright.expect(page.locator(".chart-note")).to_contain_text("90 条原始记录")
    assert requests[-1]["where"] == [{"field": "symbol", "op": "==", "value": "AAA"}]
    page.wait_for_timeout(600)
    assert len(requests) == 3


def test_chart_preserves_nulls_and_duplicate_x_without_downsampling(page, web_servers):
    events = [
        {"type": "meta", "kind": "line", "x_type": "time", "y_type": "numeric",
         "x_col": "ts", "value_cols": ["price"], "group_by": "symbol"},
        {"type": "data", "group": "AAA", "points": [[BASE, 1], [BASE, None], [BASE + 1000, 2]]},
        {"type": "done", "rows": 3, "series": [{"group": "AAA", "rows": 3}]},
    ]
    page.route("**/api/table/ticks/chart", lambda route: route.fulfill(
        status=200, content_type="application/x-ndjson",
        body="\n".join(json.dumps(event) for event in events) + "\n",
    ))
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.add_script_tag(url=web_servers["readonly"] + "/vendor/mchart3-0.5.0.min.js")
    page.evaluate("""() => {
      const Original = window.MChart.Figure;
      window.__lineData = [];
      window.MChart = { Figure: class extends Original {
        addSubplot(...args) {
          const axes = super.addSubplot(...args);
          const addLine = axes.addLineSeries.bind(axes);
          axes.addLineSeries = (x, y, options) => {
            window.__lineData.push({x: [...x], y: [...y], downsample: options.downsample});
            return addLine(x, y, options);
          };
          return axes;
        }
      }};
    }""")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("3 条原始记录")
    assert page.evaluate("window.__lineData") == [{
        "x": [BASE, BASE, BASE + 1000], "y": [1, None, 2], "downsample": False,
    }]


def test_chart_rejects_incomplete_stream(page, web_servers):
    page.route("**/api/table/ticks/chart", lambda route: route.fulfill(
        status=200, content_type="application/x-ndjson",
        body=json.dumps({"type": "meta", "kind": "line", "x_type": "time",
                         "y_type": "numeric", "value_cols": ["price"]}) + "\n",
    ))
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.get_by_text("图表数据流未正常结束", exact=False)).to_be_visible()
    playwright.expect(page.locator(".chart-canvas")).to_have_count(0)


def test_chart_rejects_null_numeric_x_instead_of_drawing_zero(page, web_servers):
    events = [
        {"type": "meta", "kind": "scatter", "x_type": "numeric", "y_type": "numeric",
         "value_cols": ["id"], "group_by": None},
        {"type": "data", "group": None, "points": [[None, 2]]},
        {"type": "done", "rows": 1, "series": [{"group": None, "rows": 1}]},
    ]
    page.route("**/api/table/notes/chart", lambda route: route.fulfill(
        status=200, content_type="application/x-ndjson",
        body="\n".join(json.dumps(event) for event in events) + "\n",
    ))
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.get_by_text("图表 X 轴包含无效值", exact=False)).to_be_visible()
    playwright.expect(page.locator(".chart-canvas")).to_have_count(0)


def test_chart_log_axis_reports_hidden_nonpositive_values(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    page.get_by_role("combobox", name="Y 轴").select_option("id")
    page.get_by_role("checkbox", name="对数轴").check()
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("对数轴不显示 1 个非正值")


def test_chart_converts_time_inputs_and_condition_values(page, web_servers):
    requests = []
    page.on("request", lambda request: requests.append(request.post_data_json)
            if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.locator(".cond-row .field-sel").select_option("ts")
    page.locator(".cond-row .op-sel").select_option(">=")
    page.locator(".cond-row .val-input").fill("2025-01-01T00:00:00Z")
    page.get_by_role("textbox", name="时间结束").fill("2025-01-01T00:00:10Z")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("11 条原始记录")
    assert requests[0]["where"] == [
        {"field": "ts", "op": ">=", "value": BASE},
        {"field": "ts", "op": "<=", "value": BASE + 10000},
    ]


def test_chart_filter_keeps_large_integer_input_exact(page, web_servers):
    requests = []
    page.on("request", lambda request: requests.append(request.post_data_json)
            if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    page.get_by_role("combobox", name="条件字段 1").select_option("id")
    page.get_by_role("textbox", name="条件值 1").fill("9007199254740993")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.get_by_text("没有匹配的数据", exact=False)).to_be_visible()
    assert requests[0]["where"][0]["value"] == "9007199254740993"


def test_chart_boolean_filter_and_category_axis(page, web_servers):
    requests = []
    page.on("request", lambda request: requests.append(request.post_data_json)
            if request.url.endswith("/chart") else None)
    page.goto(web_servers["readonly"] + "/#/table/notes/charts")
    page.get_by_role("combobox", name="Y 轴").select_option("flag")
    page.get_by_role("combobox", name="条件字段 1").select_option("flag")
    page.get_by_role("textbox", name="条件值 1").fill("true")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("2 条原始记录")
    assert requests[0]["where"][0]["value"] is True


def test_chart_new_draw_cannot_be_overwritten_by_late_old_response(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (url, opts) => {
        if (String(url).endsWith('/chart') && !window.__oldChartStarted) {
          window.__oldChartStarted = true;
          return new Promise((resolve, reject) => {
            window.__releaseOldChart = () => original(url, {...opts, signal: undefined})
              .then(async response => {
                const bytes = await response.arrayBuffer();
                const stream = new ReadableStream({start(controller) {
                  controller.enqueue(new Uint8Array(bytes));
                  controller.close();
                  window.__oldChartDelivered = true;
                }});
                resolve(new Response(stream, {status: response.status, headers: response.headers}));
              }, reject);
          });
        }
        return original(url, opts);
      };
    }""")
    page.get_by_role("button", name="绘制", exact=True).click()
    page.wait_for_function("window.__oldChartStarted")
    page.locator(".cond-row .field-sel").select_option("symbol")
    page.locator(".cond-row .val-input").fill("AAA")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("90 条原始记录")
    page.evaluate("() => window.__releaseOldChart()")
    page.wait_for_function("window.__oldChartDelivered === true")
    playwright.expect(page.locator(".chart-note")).to_contain_text("90 条原始记录")


def test_chart_reset_cancels_pending_viewport_request(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/charts")
    page.get_by_role("button", name="绘制", exact=True).click()
    playwright.expect(page.locator(".chart-note")).to_contain_text("180 条原始记录")
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (url, opts) => {
        if (String(url).endsWith('/chart') && !window.__viewportSignal) {
          window.__viewportSignal = opts.signal;
          return new Promise((resolve, reject) => {
            opts.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
          });
        }
        return original(url, opts);
      };
    }""")
    page.locator(".chart-canvas").hover()
    page.mouse.wheel(0, -300)
    page.wait_for_function("window.__viewportSignal")
    playwright.expect(page.locator(".chart-note")).to_contain_text("可视区加载中")
    page.locator(".chart-canvas").dblclick()
    page.wait_for_function("window.__viewportSignal.aborted")
    playwright.expect(page.locator(".chart-note")).to_contain_text("全范围 · 180 条原始记录")


def test_refresh_recounts_and_arbitrary_names_remain_usable(page, web_servers):
    count_requests = []
    page.on("request", lambda request: count_requests.append(request.url) if request.url.endswith("/count") else None)
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    page.get_by_role("button", name="统计总行数").click()
    playwright.expect(page.locator(".pager-bar").last).to_contain_text("总行数：180")
    with page.expect_response(lambda response: response.url.endswith("/count")):
        page.locator("body").press("r")
    assert len(count_requests) >= 2

    long_name = "很长的表名" * 40
    segments = [{"logical_part": key, "row_count": 1}
                for key in ["__proto__", "constructor"] + [f"part-{i}" for i in range(1200)]]
    page.route("**/api/tables", lambda route: route.fulfill(json={"tables": [
        {"name": "constructor"}, {"name": long_name}
    ]}))
    page.route("**/api/table/*/stats", lambda route: route.fulfill(json={
        "row_count": 1202, "part_count": 1202, "segment_count": 1202
    }))
    page.route("**/api/table/*/segments", lambda route: route.fulfill(json={"segments": segments}))
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(web_servers["readonly"])
    page.get_by_role("button", name="统计全部表").click()
    playwright.expect(page.locator("#stat-total-rows")).to_have_text("2,404")
    page.locator('[data-overview-actions="constructor"]').get_by_role("button", name="加载分布").click()
    signal = page.locator('[data-part-signal="constructor"]')
    playwright.expect(signal).to_contain_text("分布均衡")
    assert signal.locator(".mb").count() <= 60
    assert page.locator("#content").evaluate("el => el.scrollWidth <= el.clientWidth")


def test_structure_limits_initial_dom_for_many_partitions(page, web_servers):
    keys = [BASE + i * DAY for i in range(1205)]
    segments = [{"logical_part": key, "row_count": 1, "time_min": key, "time_max": key}
                for key in keys]
    page.route("**/api/table/ticks/parts", lambda route: route.fulfill(json={"parts": keys}))
    page.route("**/api/table/ticks/segments*", lambda route: route.fulfill(json={"segments": segments}))
    page.goto(web_servers["readonly"] + "/#/table/ticks/structure")
    bars = page.locator(".bar-line")
    playwright.expect(bars).to_have_count(200)
    page.get_by_role("button", name="显示更多分区", exact=False).click()
    playwright.expect(bars).to_have_count(400)


def test_leaving_data_and_charts_cancels_inflight_requests(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.evaluate("""() => {
      const original = window.fetch;
      window.fetch = (url, opts) => {
        const address = String(url);
        const key = address.endsWith('/rows') ? '__rowSignal' :
                    address.endsWith('/chart') ? '__chartSignal' : null;
        if (key && !window[key]) {
          window[key] = opts.signal;
          return new Promise((resolve, reject) => {
            opts.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
          });
        }
        return original(url, opts);
      };
    }""")
    page.get_by_role("button", name="刷新", exact=True).click()
    page.wait_for_function("window.__rowSignal")
    page.get_by_role("tab", name="3Structure").click()
    page.wait_for_function("window.__rowSignal.aborted")
    page.get_by_role("tab", name="2Data").click()
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    page.get_by_role("tab", name="4Charts").click()
    page.get_by_role("button", name="绘制", exact=True).click()
    page.wait_for_function("window.__chartSignal")
    page.get_by_role("tab", name="2Data").click()
    page.wait_for_function("window.__chartSignal.aborted")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)


@pytest.mark.parametrize("retry_button", ["重试分布", "重试"])
def test_structure_can_retry_failed_distribution(page, web_servers, retry_button):
    calls = []

    def segments(route):
        calls.append(1)
        if len(calls) == 1:
            route.fulfill(status=503, json={"detail": "统计暂不可用"})
        else:
            route.continue_()

    page.route("**/api/table/ticks/segments", segments)
    page.goto(web_servers["readonly"] + "/#/table/ticks/structure")
    page.get_by_role("button", name=retry_button, exact=True).click()
    playwright.expect(page.locator(".bar-line")).to_have_count(3)
    playwright.expect(page.get_by_text("segment 加载失败。", exact=False)).to_have_count(0)
    assert len(calls) == 2


def test_time_inputs_local_timezone_invalid_dates_and_quick_views(page, web_servers):
    page.goto(web_servers["readonly"] + "/#/table/ticks/data")
    playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
    values = page.evaluate("""() => Object.fromEntries([
      '2025-01-01', '2025-01-01 00:00', '2025-01-01T00:00:00Z',
      '2025-02-30', '2025-02-30T00:00:00Z', '2025-2-30', '2025/01/01', '2024-02-29', '1735689600000'
    ].map(value => [value, IU.parseTimeInput(value)]))""")
    assert values["2025-01-01"]["ms"] == BASE - 8 * 3600000
    assert values["2025-01-01 00:00"]["ms"] == values["2025-01-01"]["ms"]
    assert values["2025-01-01T00:00:00Z"]["ms"] == BASE
    assert values["1735689600000"]["ms"] == BASE
    assert values["2024-02-29"]["ok"]
    assert not values["2025-02-30"]["ok"]
    assert not values["2025-02-30T00:00:00Z"]["ok"]
    assert not values["2025-2-30"]["ok"]
    assert not values["2025/01/01"]["ok"]

    page.get_by_role("button", name="时序", exact=True).click()
    fields = page.locator(".time-field input")
    fields.nth(0).fill("2025-02-30")
    playwright.expect(page.locator(".tf-parsed.bad")).to_contain_text("无法解析")
    fields.nth(0).fill("2025-01-01")
    fields.nth(1).fill("2025-01-02")
    with page.expect_request(lambda request: request.url.endswith("/query")) as request_info:
        page.get_by_role("button", name="时间窗查询").click()
    body = request_info.value.post_data_json
    assert body["start"] == BASE - 8 * 3600000
    assert body["end"] == BASE + 16 * 3600000
    playwright.expect(page.get_by_text("/query 结果")).to_be_visible()

    page.get_by_role("button", name="截面", exact=True).click()
    page.get_by_role("button", name="最新截面", exact=True).click()
    playwright.expect(page.get_by_text("/latest 截面结果")).to_be_visible()


def test_time_input_rejects_nonexistent_dst_time(browser, web_servers):
    context = browser.new_context(timezone_id="America/New_York")
    try:
        page = context.new_page()
        page.goto(web_servers["readonly"])
        page.wait_for_function("window.IU && IU.parseTimeInput")
        values = page.evaluate("""() => [
          IU.parseTimeInput('2025-03-09 01:30').ok,
          IU.parseTimeInput('2025-03-09 02:30').ok,
          IU.parseTimeInput('2025-03-09T02:30:00-05:00').ok
        ]""")
        assert values == [True, False, True]
    finally:
        context.close()


def test_touch_controls_and_table_cells_have_usable_targets(browser, web_servers):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    try:
        page = context.new_page()
        page.goto(web_servers["readonly"] + "/#/table/ticks/data")
        playwright.expect(page.locator(".browse-table tbody tr")).to_have_count(180)
        for locator in [page.get_by_role("button", name="打开表列表"),
                        page.get_by_role("button", name="应用筛选"),
                        page.locator(".browse-table tbody td").first]:
            box = locator.bounding_box()
            assert box is not None and box["height"] >= 44 and box["width"] >= 44
    finally:
        context.close()
