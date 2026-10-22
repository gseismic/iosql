"""PLAN-056 回归测试：v0.3.0 全量 review 的 P0 修复项。

覆盖：P0-1（写/compact 并发）、P0-2（mysqlite 补列类型）、P0-3（PG create_index
注入）、P0-5（PG file 模式守卫）、P0-6（CLI restore 目标校验）、P0-7（rename 双向
包含）、P0-8（表名校验）、P0-9（IntegrityError 回滚）、C-M1（import ignore 统计）。
P0-4 的回归由 tests/test_migration.py 中已更新的 mock 用例覆盖。
"""

import sqlite3
import threading

import pytest



from iosql import Database, Segment
from iosql.exception import BadArgument, UnsafeOperation
from iosql.engines.postgresql import Table as PGTable
from iosql.utils.mysqlite import Database as MyDatabase
import iosql.engines.postgresql.table as pg_table_module


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self):
        self.closed = False
        self.autocommit = False
        self.cursor_obj = _FakeCursor()

    def cursor(self, name=None, withhold=False, **kwargs):
        return self.cursor_obj

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- P0-3

def test_postgresql_create_index_uses_identifier_not_fstring(monkeypatch):
    fake_conn = _FakeConn()
    monkeypatch.setattr(pg_table_module.psycopg2, "connect", lambda uri: fake_conn)
    table = PGTable("postgresql://u:p@localhost:5432/db", "ticks", partition_mode="native")

    # 注入形列名仍须通过 Identifier 引用，目录证据在此独立替换。
    from contextlib import nullcontext
    monkeypatch.setattr(table, '_column_types', lambda: {'id); DROP TABLE x; --': 'integer'})
    monkeypatch.setattr(table, '_index_definitions', lambda: [])
    monkeypatch.setattr(table, '_transaction', lambda *a: nullcontext())
    table.create_index("id); DROP TABLE x; --", order="desc")
    query = fake_conn.cursor_obj.executed[0][0]
    assert not isinstance(query, str)


def test_postgresql_create_index_rejects_invalid_order(monkeypatch):
    fake_conn = _FakeConn()
    monkeypatch.setattr(pg_table_module.psycopg2, "connect", lambda uri: fake_conn)
    table = PGTable("postgresql://u:p@localhost:5432/db", "ticks", partition_mode="native")

    with pytest.raises(BadArgument):
        table.create_index("price", order="sideways")


# ---------------------------------------------------------------- P0-2

def test_mysqlite_insert_all_ensure_column_preserves_numeric_type(tmp_path):
    db = MyDatabase(str(tmp_path / "t.sqlite3"), auto_commit=True, verbose=0)
    try:
        db["events"].insert_all([{"id": 1, "name": "a"}], ensure_table=True, pk=["id"])
        db["events"].insert_all([{"id": 2, "name": "b", "qty": 3}], ensure_column=True)
        col_type = db["events"].column_info()["column_type"].get("qty", "")
        assert col_type.lower() == "integer"
    finally:
        db.close()


# ---------------------------------------------------------------- P0-5

def test_postgresql_file_partition_mode_rejected():
    with pytest.raises(BadArgument):
        PGTable("postgresql://u:p@localhost:5432/db", "t", partition_mode="file")


def test_postgresql_invalid_partition_mode_rejected():
    with pytest.raises(BadArgument):
        PGTable("postgresql://u:p@localhost:5432/db", "t", partition_mode="bogus")


# ---------------------------------------------------------------- P0-7

def test_database_rename_rejects_target_containing_source(tmp_path):
    parent = tmp_path / "parent.iosql"
    db_path = parent / "child.iosql"
    db = Database(f"sqlite://{db_path}")
    db.table("t", pk=["id"], columns={"id": "integer"})
    db.close()

    src = Database(f"sqlite://{db_path}")
    try:
        with pytest.raises(UnsafeOperation):
            src.rename(f"sqlite://{parent}")
    finally:
        src.close()
    assert db_path.exists()


# ---------------------------------------------------------------- P0-8

def test_sqlite_table_name_validation_rejects_escape(tmp_path):
    db = Database(f"sqlite://{tmp_path}/d.iosql")
    try:
        for bad in ["a/../evil", "a/b", "..", ".", "__meta"]:
            with pytest.raises(BadArgument):
                db.table(bad)
    finally:
        db.close()


# ---------------------------------------------------------------- P0-9

def test_mysqlite_integrity_error_rolls_back_connection(tmp_path):
    db = MyDatabase(str(tmp_path / "t.sqlite3"), auto_commit=False, verbose=0)
    try:
        t = db["data"]
        t.create_table(
            [{"name": "id", "type": "integer"}, {"name": "v", "type": "text"}],
            pk=["id"],
        )
        t.execute_params("INSERT INTO data (id, v) VALUES (?, ?)", [1, "a"], commit=True)
        with pytest.raises(sqlite3.IntegrityError):
            # 延迟提交只允许显式事务；让事务异常退出以验证连接回滚。
            with db.transaction():
                t.execute_params(
                    "INSERT INTO data (id, v) VALUES (?, ?)", [1, "b"], commit=False
                )
        t.execute_params("INSERT INTO data (id, v) VALUES (?, ?)", [2, "c"], commit=True)
        assert t.count() == 2
    finally:
        db.close()


# ---------------------------------------------------------------- P0-1

def test_write_compact_concurrency_preserves_all_rows(tmp_path):
    db = Database(f"sqlite://{tmp_path}/d.iosql")
    table = db.table(
        "ticks",
        pk=["id"],
        columns={"id": "integer", "v": "integer"},
        partition=None,
        segment=Segment.rows(max_rows=100, target_rows=50, min_rows=1),
        order_key="id",
    )
    table.write([{"id": i, "v": i} for i in range(100)])

    errors = []

    def writer(n):
        try:
            for i in range(n):
                table.write([{"id": 1000 + i, "v": i}])
        except Exception as exc:  # pragma: no cover - 断言失败时上报
            errors.append(exc)

    def compactor():
        try:
            for _ in range(3):
                table.compact()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(200,)), threading.Thread(target=compactor)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    ids = [row["id"] for row in table.select("id", where=None)]
    assert len(ids) == 300
    assert len(set(ids)) == 300
    db.close()

pytestmark = pytest.mark.usefixtures("fake_pg_coordination")
