"""示例以隔离目录运行；PostgreSQL 测试仅使用显式指定的测试库。"""

import ast
import importlib.util
import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import tracemalloc
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import pytest

from iosql import Database


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
SKILL_EXAMPLES = ROOT / "skills" / "iosql-usage" / "examples"
SCRIPTS = sorted(
    path.relative_to(EXAMPLES).as_posix()
    for path in EXAMPLES.rglob("*.py")
    if not {"runtime", "__pycache__"}.intersection(path.relative_to(EXAMPLES).parts)
)


def run_example(example_dir, script, extra_env=None):
    # 仅复制这一份脚本；不得借助其他示例文件。
    destination = example_dir / Path(script).name
    shutil.copy2(EXAMPLES / script, destination)
    env = dict(os.environ)
    for name in (
        "POSTGRES_DSN", "IOSQL_EXISTING_URI", "IOSQL_EXISTING_TABLE",
        "IOSQL_SOURCE_URI", "IOSQL_TARGET_URI", "IOSQL_POSTGRES_TARGET_TABLE",
        "IOSQL_POSTGRES_ROUNDTRIP_TABLE", "PGSERVICE", "PGSERVICEFILE",
    ):
        env.pop(name, None)
    env.update(extra_env or {})
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, destination.name],
        cwd=example_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def postgresql_example_env(uri, script):
    env = {"POSTGRES_DSN": uri}
    if script != "01_postgresql_env_password.py":
        return env
    parts = urlsplit(uri)
    params = parse_qsl(parts.query, keep_blank_values=True)
    password = next((value for key, value in params if key.lower() == "password"), None)
    password = password or (unquote(parts.password) if parts.password else None)
    password = password or os.environ.get("PGPASSWORD")
    if not password:
        pytest.skip("此示例需要测试 URI 中的密码或 PGPASSWORD")
    netloc = parts.netloc
    if parts.password is not None:
        netloc = netloc.split("@", 1)[0].split(":", 1)[0] + "@" + netloc.rsplit("@", 1)[1]
    query = urlencode(
        [(key, value) for key, value in params if key.lower() != "password"], quote_via=quote,
    )
    env["POSTGRES_DSN"] = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    env["PGPASSWORD"] = password
    return env


@pytest.fixture
def example_dir(tmp_path):
    path = tmp_path / "examples"
    path.mkdir()
    return path


@pytest.fixture
def pg_uri():
    base = os.environ.get("IOSQL_EXAMPLE_TEST_POSTGRES_DSN")
    if not base:
        pytest.skip("需要显式指定独立测试库 IOSQL_EXAMPLE_TEST_POSTGRES_DSN")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import sql
    from iosql.migration_postgresql import _stage_uri

    schema = "iosql_example_test_" + uuid.uuid4().hex
    with closing(psycopg2.connect(base)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield _stage_uri(base, schema)
    finally:
        with closing(psycopg2.connect(base)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def restricted_pg_uri(pg_uri):
    """仅在显式测试库创建临时角色，验证例子在主要逻辑前拒绝缺失权限。"""
    import psycopg2
    from psycopg2 import sql

    role = "iosql_example_role_" + uuid.uuid4().hex
    with closing(psycopg2.connect(pg_uri)) as conn, conn, conn.cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            pytest.skip("临时角色前置检查测试需要显式测试库的管理员账号")
        cursor.execute("SELECT current_schema()")
        schema = cursor.fetchone()[0]
        cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)))

    parts = urlsplit(pg_uri)
    params = parse_qsl(parts.query, keep_blank_values=True)
    options = next((value for key, value in params if key == "options"), "")
    params = [(key, value) for key, value in params if key != "options"]
    params.append(("options", options + " -crole=" + role))
    uri = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params, quote_via=quote), parts.fragment))
    try:
        yield uri, role, schema
    finally:
        with closing(psycopg2.connect(pg_uri)) as conn, conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_example_mirrors_are_identical():
    def files(root):
        return {
            path.relative_to(root): path
            for path in root.rglob("*")
            if path.is_file() and not {"runtime", "__pycache__"}.intersection(path.relative_to(root).parts)
        }

    examples, skill = files(EXAMPLES), files(SKILL_EXAMPLES)
    assert examples.keys() == skill.keys()
    for name in examples:
        assert examples[name].read_bytes() == skill[name].read_bytes(), str(name)


@pytest.mark.parametrize("script", SCRIPTS)
def test_single_copied_script_runs_without_other_examples(example_dir, request, script):
    header = ast.get_docstring(ast.parse((EXAMPLES / script).read_text()))
    assert f"python {Path(script).name}" in header
    assert "预期：" in header
    env = {}
    if script in ("01_postgresql_env_password.py", "01_quickstart_postgresql.py", "15_postgresql_timeseries_query.py"):
        env = postgresql_example_env(request.getfixturevalue("pg_uri"), script)
    result = run_example(example_dir, script, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_parquet_and_partial_update_examples(example_dir):
    for _ in range(2):
        for script in ("14_parquet_backend.py", "03_partial_upsert.py"):
            result = run_example(example_dir, script)
            assert result.returncode == 0, result.stderr
    parquet_paths = sorted((example_dir / "runtime").glob("*/14_parquet_quickstart.iosql"))
    sqlite_paths = sorted((example_dir / "runtime").glob("*/03_partial_upsert.iosql"))
    assert len(parquet_paths) == len(sqlite_paths) == 2
    for path in parquet_paths:
        with Database(f"parquet://{path}") as db:
            rows = db.table("bars").query(symbols="AAPL", order_by="ts").to_list()
            assert [row["close"] for row in rows] == [187.0, 188.0]
    for path in sqlite_paths:
        with Database(f"sqlite://{path}") as db:
            assert db.table("quotes").select() == [
                {"symbol": "AAPL", "venue": "NASDAQ", "price": 188.0}
            ]


@pytest.mark.parametrize("script", [
    "04_layout_report_and_repartition.py",
    "07_partition_string_repartition_cli.py",
    "10_existing_database_repartition.py",
    "16_sqlite_to_parquet.py",
    "18_save_as_compare_engines.py",
])
def test_repeated_local_migrations_preserve_previous_run(example_dir, script):
    first = run_example(example_dir, script)
    assert first.returncode == 0, first.stderr
    runtime = example_dir / "runtime"
    old_files = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    assert old_files
    second = run_example(example_dir, script)
    assert second.returncode == 0, second.stderr
    assert all(path.is_file() and path.read_bytes() == content for path, content in old_files.items())


def test_repartition_external_source_and_existing_target(example_dir, tmp_path):
    source_uri = f"sqlite://{tmp_path / 'external_source.iosql'}"
    with Database(source_uri) as db:
        table = db.timeseries(
            "ticks", time="ts", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition_every="1day",
        )
        table.write({"symbol": "AAPL", "ts": 1735689600000, "price": 1.0})

    result = run_example(example_dir, "10_existing_database_repartition.py", {
        "IOSQL_SOURCE_URI": source_uri,
    })
    assert result.returncode == 0, result.stderr
    assert "源/目标行数: 1 1" in result.stdout
    with Database(source_uri) as db:
        assert db.table("ticks").count_rows() == 1

    target_uri = f"sqlite://{tmp_path / 'existing_target.iosql'}"
    with Database(target_uri) as db:
        db.table("protected", pk=["id"], columns={"id": "integer"}).write({"id": 42})
    result = run_example(example_dir, "10_existing_database_repartition.py", {
        "IOSQL_SOURCE_URI": source_uri,
        "IOSQL_TARGET_URI": target_uri,
    })
    assert result.returncode != 0
    assert "目标库必须为空" in result.stderr
    with Database(target_uri) as db:
        assert db.table("protected").select() == [{"id": 42}]


def test_existing_repartition_counts_without_materializing_rows(example_dir, monkeypatch, capsys):
    from iosql.engines.sqlite.table import Table

    example = load_example(example_dir, "10_existing_database_repartition.py")

    def reject_select(*args, **kwargs):
        pytest.fail("重分区计数不应通过 select() 全量读取已有数据")

    monkeypatch.setattr(Table, "select", reject_select)
    example.run()
    assert "源/目标行数: 4 4" in capsys.readouterr().out


def test_existing_repartition_rejects_incompatible_time_before_work(example_dir, tmp_path):
    uri = f"sqlite://{tmp_path / 'non_timeseries.iosql'}"
    with Database(uri) as db:
        db.table("ticks", pk=["id"], columns={"id": "integer"}).write({"id": 1})
    result = run_example(example_dir, "10_existing_database_repartition.py", {"IOSQL_SOURCE_URI": uri})
    assert result.returncode != 0
    assert "time=ts、unit=ms" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (example_dir / "runtime").exists()


def test_mysqlite_transaction_example_commits_both_writes(example_dir):
    result = run_example(example_dir, "mysqlite-scenarios/01_crud.py")
    assert result.returncode == 0, result.stderr
    assert "回滚后库存: 5" in result.stdout
    assert "提交后库存: 3" in result.stdout
    path, = (example_dir / "runtime").glob("*/01_crud/crud.sqlite3")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT sku, qty FROM stock").fetchall() == [("A", 3)]
        assert conn.execute("SELECT id, qty FROM reservations").fetchall() == [(1, 2)]


def test_mysqlite_drop_column_example_refreshes_schema_cache(example_dir):
    result = run_example(example_dir, "mysqlite-scenarios/02_add_drop_column.py")
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("最终字段:"))
    fields = ast.literal_eval(line.split(":", 1)[1].strip())
    assert set(fields) == {"id", "kind", "source"}
    path, = (example_dir / "runtime").glob("*/02_add_drop_column/schema.sqlite3")
    with closing(sqlite3.connect(path)) as conn:
        assert [item[1] for item in conn.execute("PRAGMA table_info(events)")] == fields
        assert conn.execute("SELECT source FROM events ORDER BY id").fetchall() == [("web",), ("web",)]


def test_mysqlite_index_example_preserves_primary_key(example_dir):
    result = run_example(example_dir, "mysqlite-scenarios/05_index_operations.py")
    assert result.returncode == 0, result.stderr
    assert "按列删除数量: 1" in result.stdout
    path, = (example_dir / "runtime").glob("*/05_index_ops/index.sqlite3")
    with closing(sqlite3.connect(path)) as conn:
        indexes = conn.execute("PRAGMA index_list(ticks)").fetchall()
        assert len(indexes) == 1 and indexes[0][3] == "pk"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO ticks VALUES ('AAPL', 0, 99)")


@pytest.mark.parametrize(("script", "setting"), [
    ("09_existing_database_append.py", "IOSQL_EXISTING_URI"),
    ("10_existing_database_repartition.py", "IOSQL_SOURCE_URI"),
])
def test_existing_database_examples_reject_missing_source_before_writes(example_dir, tmp_path, script, setting):
    missing = tmp_path / "missing_source.iosql"
    result = run_example(example_dir, script, {setting: f"sqlite://{missing}"})
    assert result.returncode != 0
    assert "未找到指定表" in result.stderr
    assert not missing.exists()
    assert not (example_dir / "runtime").exists()


def test_postgresql_example_requires_connection(example_dir):
    result = run_example(example_dir, "01_quickstart_postgresql.py")
    assert result.returncode != 0
    assert "POSTGRES_DSN" in result.stderr


def test_postgresql_write_query_and_reopen(example_dir, pg_uri):
    for _ in range(2):
        result = run_example(example_dir, "01_quickstart_postgresql.py", {"POSTGRES_DSN": pg_uri})
        assert result.returncode == 0, result.stderr
        assert "提交的记录数: 3" in result.stdout
    with Database(pg_uri) as db:
        ticks = db.timeseries(
            "iosql_example_ticks", time="ts", pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition_every="1day",
        )
        assert ticks.count_rows() == 3
        assert ticks.query(symbols="AAPL", order_by="ts").to_list() == [
            {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
            {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        ]


@pytest.mark.parametrize("kind", ["table", "empty_table", "view", "partition"])
@pytest.mark.parametrize("script,setting", [
    ("11_sqlite_to_postgresql_migration.py", "IOSQL_POSTGRES_TARGET_TABLE"),
    ("12_sqlite_postgresql_roundtrip.py", "IOSQL_POSTGRES_ROUNDTRIP_TABLE"),
])
def test_migration_examples_refuse_existing_relations(example_dir, pg_uri, kind, script, setting):
    import psycopg2

    with closing(psycopg2.connect(pg_uri)) as conn, conn:
        with conn.cursor() as cursor:
            if kind in ("table", "empty_table"):
                cursor.execute("CREATE TABLE protected (id integer)")
                if kind == "table":
                    cursor.execute("INSERT INTO protected VALUES (42)")
                cursor.execute("CREATE VIEW dependent AS SELECT * FROM protected")
            elif kind == "view":
                cursor.execute("CREATE VIEW protected AS SELECT 42 AS id")
            else:
                cursor.execute("CREATE TABLE parent (id integer) PARTITION BY RANGE (id)")
                cursor.execute("CREATE TABLE protected PARTITION OF parent FOR VALUES FROM (0) TO (100)")
                cursor.execute("INSERT INTO parent VALUES (42)")

    result = run_example(example_dir, script, {"POSTGRES_DSN": pg_uri, setting: "protected"})
    assert result.returncode != 0
    assert "已存在" in result.stderr
    with closing(psycopg2.connect(pg_uri)) as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM protected")
            assert cursor.fetchall() == ([] if kind == "empty_table" else [(42,)])
            if kind in ("table", "empty_table"):
                cursor.execute("SELECT id FROM dependent")
                assert cursor.fetchall() == ([] if kind == "empty_table" else [(42,)])


@pytest.mark.parametrize("script", [
    "11_sqlite_to_postgresql_migration.py",
    "12_sqlite_postgresql_roundtrip.py",
])
def test_migration_examples_repeat_with_new_tables_and_hide_password(example_dir, pg_uri, script):
    import psycopg2

    password = urlsplit(pg_uri).password
    for _ in range(2):
        result = run_example(example_dir, script, {"POSTGRES_DSN": pg_uri})
        assert result.returncode == 0, result.stderr
        assert "行数校验: True" in result.stdout
        if password:
            assert unquote(password) not in result.stdout + result.stderr
    prefix = "iosql_example_migration_" if script.startswith("11_") else "iosql_example_roundtrip_"
    with closing(psycopg2.connect(pg_uri)) as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind = 'p' AND c.relname LIKE %s",
                (prefix + "%",),
            )
            assert cursor.fetchone()[0] == 2


@pytest.mark.parametrize(("script", "legacy_path"), [
    ("01_quickstart_table.py", "01_quickstart_table.iosql"),
    ("01_quickstart_timeseries.py", "01_quickstart.iosql"),
    ("02_calendar_events.py", "02_calendar.iosql"),
    ("02_calendar_events_table.py", "02_calendar_table.iosql"),
    ("03_buffered_ingest_and_query_plan.py", "03_buffered.iosql"),
    ("03_partial_upsert.py", "03_partial_upsert.iosql"),
    ("05_partition_segments_smoke.py", "05_partition_segments.iosql"),
    ("06_row_segment_late_data.py", "06_row_segment_late_data.iosql"),
    ("08_doctor_time_first_suggestions.py", "08_doctor_time_first.iosql"),
    ("09_existing_database_append.py", "09_existing_source.iosql"),
    ("13_streaming_ordered_query.py", "13_streaming_ordered.iosql"),
    ("14_parquet_backend.py", "14_parquet_quickstart.iosql"),
    ("finance/01_kline_bars.py", "01_kline.iosql"),
    ("finance/02_orderbook_snapshots.py", "02_orderbook.iosql"),
    ("finance/03_financial_reports.py", "03_reports.iosql"),
    ("finance/04_news_announcements.py", "04_news.iosql"),
    ("mysqlite/01_schemaless_crud.py", "01_schemaless_crud"),
    ("mysqlite/02_bulk_query_and_conflict.py", "02_bulk_query"),
    ("mysqlite/03_connection_pool_cleanup.py", "03_pool_cleanup"),
    ("mysqlite-scenarios/01_crud.py", "01_crud"),
    ("mysqlite-scenarios/02_add_drop_column.py", "02_add_drop_column"),
    ("mysqlite-scenarios/03_query_variants.py", "03_query_variants"),
    ("mysqlite-scenarios/04_rename_and_table_info.py", "04_rename_and_info"),
    ("mysqlite-scenarios/05_index_operations.py", "05_index_ops"),
    ("mysqlite-scenarios/06_backup.py", "06_backup"),
])
def test_local_examples_preserve_existing_and_previous_data(example_dir, script, legacy_path):
    runtime = example_dir / "runtime"
    marker = runtime / legacy_path / "existing.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("已有数据")

    first = run_example(example_dir, script)
    assert first.returncode == 0, first.stderr
    previous = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    assert previous[marker] == "已有数据".encode()
    assert len(previous) > 1

    second = run_example(example_dir, script)
    assert second.returncode == 0, second.stderr
    assert all(path.is_file() and path.read_bytes() == contents for path, contents in previous.items())


def load_example(example_dir, script):
    path = example_dir / Path(script).name
    shutil.copy2(EXAMPLES / script, path)
    spec = importlib.util.spec_from_file_location("iosql_review_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status", [1, 2])
def test_doctor_distinguishes_failure_from_diagnostic_findings(example_dir, monkeypatch, capsys, status):
    example = load_example(example_dir, "08_doctor_time_first_suggestions.py")
    monkeypatch.setattr(
        example.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, status, "诊断输出", "错误详情"),
    )
    if status == 1:
        with pytest.raises(SystemExit) as exc:
            example.run()
        assert exc.value.code == 1
    else:
        example.run()
    output = capsys.readouterr()
    assert "诊断输出" in output.out
    assert "错误详情" in output.err
    if status == 2:
        assert "待处理问题" in output.out
    else:
        assert "doctor 执行失败，退出码: 1" in output.err


def test_ordered_stream_consumer_has_bounded_memory(example_dir):
    example = load_example(example_dir, "13_streaming_ordered_query.py")
    rows = ({"open_time": value} for value in range(100_000))
    with redirect_stdout(io.StringIO()) as output:
        tracemalloc.start()
        try:
            example.show_ordered_rows(rows)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert "查询行数: 100000" in output.getvalue()
    assert peak < 2_000_000


def test_trading_session_includes_night_and_day(example_dir):
    result = run_example(example_dir, "02_calendar_events_table.py")
    assert result.returncode == 0, result.stderr
    rows = ast.literal_eval(result.stdout.split(":", 1)[1].strip())
    assert [(row["event_date"], row["name"]) for row in rows] == [
        ("2026-01-05", "night"), ("2026-01-06", "day"),
    ]


@pytest.mark.parametrize("script", [
    "01_postgresql_env_password.py", "01_quickstart_postgresql.py",
    "11_sqlite_to_postgresql_migration.py", "12_sqlite_postgresql_roundtrip.py",
    "15_postgresql_timeseries_query.py",
])
def test_postgresql_examples_explain_missing_driver_before_work(example_dir, script):
    (example_dir / "psycopg2.py").write_text("raise ModuleNotFoundError('No module named psycopg2')\n")
    result = run_example(example_dir, script, {
        "POSTGRES_DSN": "postgresql://localhost:1/testdb", "PGPASSWORD": "example-secret",
    })
    assert result.returncode != 0
    assert 'pip install "psycopg2-binary>=2.9"' in result.stderr
    assert "Traceback" not in result.stderr
    assert "example-secret" not in result.stdout + result.stderr
    assert not (example_dir / "runtime").exists()


@pytest.mark.parametrize("script", [
    "01_postgresql_env_password.py", "01_quickstart_postgresql.py",
    "11_sqlite_to_postgresql_migration.py", "12_sqlite_postgresql_roundtrip.py",
    "15_postgresql_timeseries_query.py",
])
def test_postgresql_examples_check_service_before_work(example_dir, script):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        result = run_example(example_dir, script, {
            "POSTGRES_DSN": f"postgresql://127.0.0.1:{port}/testdb?connect_timeout=1",
            "PGPASSWORD": "example-secret",
        })
    assert result.returncode != 0
    assert "无法连接 PostgreSQL" in result.stderr
    assert "example-secret" not in result.stdout + result.stderr
    assert not (example_dir / "runtime").exists()


@pytest.mark.parametrize("script", [
    "01_postgresql_env_password.py", "01_quickstart_postgresql.py",
    "11_sqlite_to_postgresql_migration.py", "12_sqlite_postgresql_roundtrip.py",
    "15_postgresql_timeseries_query.py",
])
def test_postgresql_examples_reject_missing_schema_create_before_work(
    example_dir, pg_uri, restricted_pg_uri, script,
):
    import psycopg2
    from psycopg2 import sql

    uri, role, _ = restricted_pg_uri
    # 让迁移例通过数据库权限检查，再验证缺失目标 schema CREATE 的情况。
    with closing(psycopg2.connect(pg_uri)) as conn, conn, conn.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database = cursor.fetchone()[0]
        cursor.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(database), sql.Identifier(role)))
    result = run_example(example_dir, script, postgresql_example_env(uri, script))
    assert result.returncode != 0
    assert "当前 schema 不可建表" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (example_dir / "runtime").exists()


@pytest.mark.parametrize("script", [
    "11_sqlite_to_postgresql_migration.py", "12_sqlite_postgresql_roundtrip.py",
])
def test_migration_examples_reject_missing_database_create_before_work(example_dir, restricted_pg_uri, script):
    uri, _, _ = restricted_pg_uri
    result = run_example(example_dir, script, {"POSTGRES_DSN": uri})
    assert result.returncode != 0
    assert "迁移需要数据库 CREATE 权限" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (example_dir / "runtime").exists()


@pytest.mark.parametrize(("script", "table"), [
    ("01_quickstart_postgresql.py", "iosql_example_ticks"),
    ("01_postgresql_env_password.py", "iosql_example_env_quotes"),
])
def test_postgresql_quickstarts_explain_foreign_table_owner_before_work(
    example_dir, pg_uri, restricted_pg_uri, script, table,
):
    import psycopg2
    from psycopg2 import sql

    uri, role, schema = restricted_pg_uri
    with closing(psycopg2.connect(pg_uri)) as conn, conn, conn.cursor() as cursor:
        cursor.execute(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)))
        cursor.execute(sql.SQL("CREATE TABLE {} (id integer)").format(sql.Identifier(table)))
    result = run_example(example_dir, script, postgresql_example_env(uri, script))
    assert result.returncode != 0
    assert "所有者权限" in result.stderr
    assert "Traceback" not in result.stderr
    with closing(psycopg2.connect(pg_uri)) as conn, conn.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        assert cursor.fetchone()[0] == 0


@pytest.mark.parametrize("script", [
    "01_postgresql_env_password.py", "01_quickstart_postgresql.py",
    "11_sqlite_to_postgresql_migration.py", "12_sqlite_postgresql_roundtrip.py",
    "15_postgresql_timeseries_query.py",
])
def test_postgresql_examples_reject_unsupported_server_before_work(example_dir, monkeypatch, script):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://localhost:5432/testdb")
    monkeypatch.setenv("PGPASSWORD", "example-secret")
    monkeypatch.delenv("PGSERVICE", raising=False)
    example = load_example(example_dir, script)
    connection = SimpleNamespace(server_version=130000, closed=False)
    connection.close = lambda: setattr(connection, "closed", True)
    driver = SimpleNamespace(connect=lambda uri: connection, Error=Exception)
    monkeypatch.setitem(sys.modules, "psycopg2", driver)

    with pytest.raises(SystemExit, match="PostgreSQL 14"):
        example.check_prerequisites()
    assert connection.closed
    assert not (example_dir / "runtime").exists()


@pytest.mark.parametrize("script", [
    "11_sqlite_to_postgresql_migration.py", "12_sqlite_postgresql_roundtrip.py",
])
def test_migration_table_reservation_is_atomic(example_dir, pg_uri, script):
    import psycopg2

    example = load_example(example_dir, script)
    barrier = threading.Barrier(2)

    def reserve():
        barrier.wait(timeout=10)
        try:
            example.reserve_postgresql_table(pg_uri, "concurrent_target")
            return "created"
        except SystemExit as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert results.count("created") == 1
    assert sum("已存在" in value for value in results) == 1
    with closing(psycopg2.connect(pg_uri)) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM concurrent_target")
        assert cursor.fetchone()[0] == 0
