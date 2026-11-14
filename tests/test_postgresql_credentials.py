"""只使用显式提供的隔离 PostgreSQL 测试库验证 libpq 凭据来源。"""

import os
import shutil
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def auth_database():
    uri = os.environ.get("IOSQL_AUTH_TEST_POSTGRES_DSN")
    user = os.environ.get("IOSQL_AUTH_TEST_POSTGRES_USER")
    password = os.environ.get("IOSQL_AUTH_TEST_POSTGRES_PASSWORD")
    if not all((uri, user, password)):
        pytest.skip("需要显式配置独立的 PostgreSQL 密码认证测试库")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import sql
    from iosql.migration_postgresql import _stage_uri

    schema = "iosql_auth_test_" + uuid.uuid4().hex
    with closing(psycopg2.connect(uri, user=user, password=password)) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield _stage_uri(uri, schema), user, password
    finally:
        with closing(psycopg2.connect(uri, user=user, password=password)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def with_auth(uri, user, password=None):
    parts = urlsplit(uri)
    auth = quote(user, safe="")
    if password is not None:
        auth += ":" + quote(password, safe="!")
    return urlunsplit((parts.scheme, auth + "@" + parts.netloc, parts.path, parts.query, parts.fragment))


def make_passfile(tmp_path, uri, user, password):
    parts = urlsplit(uri)
    def escape(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")
    path = tmp_path / "pgpass"
    fields = [parts.hostname, str(parts.port), parts.path.lstrip("/"), user, password]
    path.write_text(":".join(escape(item) for item in fields) + "\n")
    path.chmod(0o600)
    return path


def run_example(tmp_path, script, env):
    path = tmp_path / script
    shutil.copy2(ROOT / "examples" / script, path)
    child_env = dict(os.environ)
    for key in list(child_env):
        if key.startswith(("PG", "IOSQL_")) or key == "POSTGRES_DSN":
            child_env.pop(key, None)
    child_env.update(env)
    child_env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, str(path)], cwd=tmp_path, env=child_env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(("source", "script"), [
    ("environment", "01_quickstart_postgresql.py"),
    ("environment", "01_postgresql_env_password.py"),
    ("environment", "11_sqlite_to_postgresql_migration.py"),
    ("passfile", "15_postgresql_timeseries_query.py"),
    ("passfile", "12_sqlite_postgresql_roundtrip.py"),
])
def test_password_sources_cover_reads_writes_and_migration(tmp_path, auth_database, source, script):
    uri, user, password = auth_database
    env = {"POSTGRES_DSN": uri, "PGUSER": user}
    if source == "environment":
        env["PGPASSWORD"] = password
        # 显式禁用默认密码文件，确保连接确实使用环境变量。
        env["PGPASSFILE"] = str(tmp_path / "missing-pgpass")
    else:
        env["POSTGRES_DSN"] = with_auth(uri, user)
        env["PGPASSFILE"] = str(make_passfile(tmp_path, uri, user, password))

    result = run_example(tmp_path, script, env)
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout + result.stderr
    if script.startswith(("11_", "12_")):
        assert "行数校验: True" in result.stdout
    elif script.startswith("01_"):
        assert "AAPL 查询结果" in result.stdout
    else:
        assert "PostgreSQL 查询示例校验通过" in result.stdout


def test_password_environment_takes_precedence_over_passfile(tmp_path, auth_database):
    uri, user, password = auth_database
    env = {
        "POSTGRES_DSN": with_auth(uri, user),
        "PGPASSFILE": str(make_passfile(tmp_path, uri, user, password)),
        "PGPASSWORD": password + "-invalid",
    }
    denied = run_example(tmp_path, "01_quickstart_postgresql.py", env)
    assert denied.returncode != 0
    assert "无法连接 PostgreSQL" in denied.stderr

    del env["PGPASSWORD"]
    accepted = run_example(tmp_path, "01_quickstart_postgresql.py", env)
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.skipif(os.name == "nt", reason="libpq 不在 Windows 检查密码文件权限")
def test_password_file_requires_private_permissions(tmp_path, auth_database):
    uri, user, password = auth_database
    path = make_passfile(tmp_path, uri, user, password)
    env = {"POSTGRES_DSN": with_auth(uri, user), "PGPASSFILE": str(path)}
    path.chmod(0o644)
    denied = run_example(tmp_path, "01_quickstart_postgresql.py", env)
    assert denied.returncode != 0

    path.chmod(0o600)
    accepted = run_example(tmp_path, "01_quickstart_postgresql.py", env)
    assert accepted.returncode == 0, accepted.stderr


def test_uri_password_is_literal_and_overrides_environment(tmp_path, auth_database):
    uri, user, password = auth_database
    literal = "!password" if password != "!password" else "!other"
    env = {
        "POSTGRES_DSN": with_auth(uri, user, literal),
        "PGPASSWORD": password,
        "PGPASSFILE": str(tmp_path / "missing-pgpass"),
        "password": password,
    }
    denied = run_example(tmp_path, "01_quickstart_postgresql.py", env)
    assert denied.returncode != 0
    assert "无法连接 PostgreSQL" in denied.stderr

    env["POSTGRES_DSN"] = with_auth(uri, user, password)
    env["PGPASSWORD"] = password + "-invalid"
    accepted = run_example(tmp_path, "01_quickstart_postgresql.py", env)
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(("env", "expected"), [
    ({}, "请设置 POSTGRES_DSN"),
    ({"POSTGRES_DSN": "postgresql://user@localhost:5432/testdb"}, "请将 PostgreSQL 密码设置到 PGPASSWORD"),
    ({"POSTGRES_DSN": "postgresql://user:example-secret@localhost:5432/testdb", "PGPASSWORD": "example-secret"}, "POSTGRES_DSN 不应包含密码"),
    ({"POSTGRES_DSN": "postgresql://user@localhost:5432/testdb?password=example-secret", "PGPASSWORD": "example-secret"}, "POSTGRES_DSN 不应包含密码"),
])
def test_environment_example_rejects_missing_or_embedded_credentials(tmp_path, env, expected):
    result = run_example(tmp_path, "01_postgresql_env_password.py", env)
    assert result.returncode != 0
    assert expected in result.stderr
    assert "example-secret" not in result.stdout + result.stderr


def test_environment_example_is_repeatable(tmp_path, auth_database):
    import psycopg2

    uri, user, password = auth_database
    env = {"POSTGRES_DSN": with_auth(uri, user), "PGPASSWORD": password}
    for _ in range(2):
        result = run_example(tmp_path, "01_postgresql_env_password.py", env)
        assert result.returncode == 0, result.stderr
        assert "AAPL 查询结果: [{'symbol': 'AAPL', 'price': 187.4}]" in result.stdout
        assert password not in result.stdout + result.stderr
    with closing(psycopg2.connect(uri, user=user, password=password)) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM iosql_example_env_quotes")
        assert cursor.fetchone()[0] == 2


@pytest.mark.parametrize("source", ["environment", "uri"])
def test_environment_example_rejects_service_credentials(tmp_path, auth_database, source):
    uri, user, password = auth_database
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[example]\npassword=" + password + "\n")
    env = {
        "POSTGRES_DSN": with_auth(uri, user),
        "PGPASSWORD": "incorrect-password",
        "PGSERVICEFILE": str(service_file),
    }
    if source == "environment":
        env["PGSERVICE"] = "example"
    else:
        env["POSTGRES_DSN"] += "&service=example" if "?" in env["POSTGRES_DSN"] else "?service=example"

    result = run_example(tmp_path, "01_postgresql_env_password.py", env)
    assert result.returncode != 0
    assert "PGSERVICE" in result.stderr
    assert password not in result.stdout + result.stderr
    assert "incorrect-password" not in result.stdout + result.stderr
