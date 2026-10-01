from iosql import Database, Partition, Segment
from iosql import TableContract
from iosql.exception import BadArgument
import iosql.engines.postgresql.table as pg_table_module
import pytest


def _uri(tmp_path, name="source.iosql"):
    return f"sqlite://{tmp_path}/{name}"


class FakeTargetTable:
    def __init__(self, table_name):
        self.table_name = table_name
        self.batches = []
        self.modes = []
        self.row_count_adjustment = 0

    def write(self, rows, mode=None):
        rows = list(rows)
        self.batches.append(rows)
        self.modes.append(mode)

    def count_rows(self, where=None):
        return sum(len(batch) for batch in self.batches) + self.row_count_adjustment


class FakeTargetDatabase:
    def __init__(self):
        self.tables = {}
        self.specs = {}
        self.closed = False

    def table(self, table_name, spec=None):
        self.specs[table_name] = spec
        self.tables.setdefault(table_name, FakeTargetTable(table_name))
        return self.tables[table_name]

    def close(self):
        self.closed = True


def test_database_migrate_to_batches_sqlite_rows_without_loading_whole_table(tmp_path):
    source = Database(_uri(tmp_path))
    ticks = source.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 2000, "price": 2.0},
        {"symbol": "AAPL", "ts": 3000, "price": 3.0},
        {"symbol": "AAPL", "ts": 4000, "price": 4.0},
        {"symbol": "AAPL", "ts": 5000, "price": 5.0},
    ])

    fake_target = FakeTargetDatabase()
    source._new_migration_target = lambda target_uri: fake_target

    result = source.migrate_to(
        "postgresql://user:pass@localhost:5432/market",
        tables={"ticks": "pg_ticks"},
        batch_size=2,
    )

    assert result.rows_copied == 5
    assert result.batches == 3
    assert result.tables[0].table == "ticks"
    assert result.tables[0].target_table == "pg_ticks"
    assert fake_target.closed is True
    assert fake_target.specs["pg_ticks"].primary_key == ["symbol", "ts"]
    assert [len(batch) for batch in fake_target.tables["pg_ticks"].batches] == [
        2,
        2,
        1,
    ]
    assert fake_target.tables["pg_ticks"].modes == ["append", "append", "append"]

    source.close()


def test_database_migrate_to_dry_run_counts_rows_without_target(tmp_path):
    source = Database(_uri(tmp_path))
    ticks = source.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 2000, "price": 2.0},
        {"symbol": "AAPL", "ts": 3000, "price": 3.0},
    ])
    source._new_migration_target = lambda target_uri: (_ for _ in ()).throw(
        AssertionError("dry-run should not create target database")
    )

    result = source.migrate_to(
        "postgresql://user:pass@localhost:5432/market",
        tables={"ticks": "pg_ticks"},
        batch_size=2,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.rows_copied == 0
    assert result.source_rows == 3
    assert result.batches == 2
    assert result.tables[0].target_table == "pg_ticks"
    assert result.tables[0].dry_run is True

    source.close()


def test_database_migrate_to_verify_row_count(tmp_path):
    source = Database(_uri(tmp_path))
    ticks = source.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 2000, "price": 2.0},
        {"symbol": "AAPL", "ts": 3000, "price": 3.0},
    ])

    fake_target = FakeTargetDatabase()
    source._new_migration_target = lambda target_uri: fake_target

    result = source.migrate_to(
        "postgresql://user:pass@localhost:5432/market",
        tables={"ticks": "pg_ticks"},
        batch_size=2,
        verify="row-count",
    )

    assert result.verified is True
    assert result.source_rows == 3
    assert result.target_rows == 3
    assert result.tables[0].verified is True
    assert result.tables[0].verify == "row-count"

    source.close()


def test_database_migrate_to_verify_row_count_raises_on_mismatch(tmp_path):
    source = Database(_uri(tmp_path))
    ticks = source.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    ticks.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
        {"symbol": "AAPL", "ts": 2000, "price": 2.0},
    ])

    fake_target = FakeTargetDatabase()
    source._new_migration_target = lambda target_uri: fake_target
    target = fake_target.table("pg_ticks")
    target.row_count_adjustment = 1

    with pytest.raises(BadArgument, match="row-count verification failed"):
        source.migrate_to(
            "postgresql://user:pass@localhost:5432/market",
            tables={"ticks": "pg_ticks"},
            batch_size=2,
            verify="row-count",
        )

    source.close()


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.description = []
        self.results = []
        self.fetchone_result = None
        self.itersize = None

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return list(self.results)

    def fetchmany(self, size=None):
        if size is None:
            rows = list(self.results)
            self.results = []
            return rows
        rows = self.results[:size]
        self.results = self.results[size:]
        return rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        return False

    def close(self):
        # 与 psycopg2 游标生命周期一致，供流式 finally 验证使用。
        pass

    def fetchone(self):
        return self.fetchone_result


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.autocommit = False
        self.cursor_obj = FakeCursor()
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self, name=None, withhold=False, **kwargs):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()
        return False


class FakePagingCursor(FakeCursor):
    # 目录/元数据查询关键字：这些查询返回空结果，不走数据分页模拟
    _CATALOG_MARKERS = (
        'pg_class',
        'pg_inherits',
        'information_schema',
        'current_schema',
        'pg_partitioned_table',
    )

    def __init__(self, rows, columns, catalog_names=()):
        super().__init__()
        self._rows = list(rows)
        self.catalog_names = list(catalog_names)
        self.description = [(column,) for column in columns]

    def execute(self, query, params=None):
        super().execute(query, params)
        text = str(query).strip().lower()
        if text.startswith('select count(*)'):
            self.fetchone_result = (len(self._rows),)
            self.results = []
            return
        if any(marker in text for marker in self._CATALOG_MARKERS):
            self.results = [(name,) for name in self.catalog_names]
            self.fetchone_result = None
            return
        if text.startswith('select'):
            limit = len(self._rows)
            offset = 0
            if params and len(params) >= 2:
                limit = int(params[-2])
                offset = int(params[-1])
            elif params:
                limit = int(params[-1])
            self.results = self._rows[offset:offset + limit]
            self.fetchone_result = None
            return
        self.results = []
        self.fetchone_result = None


class FakePagingConnection(FakeConnection):
    def __init__(self, rows, columns, catalog_names=()):
        super().__init__()
        self.cursor_obj = FakePagingCursor(rows, columns, catalog_names)


class FakeContextCursor(FakeCursor):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        return False


class FakeContextConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.cursor_obj = FakeContextCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()
        return False


def test_postgresql_table_names_filters_native_partition_children(monkeypatch):
    fake_conn = FakeContextConnection()
    fake_conn.cursor_obj.results = [("ticks",)]
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    db = Database("postgresql://user:pass@localhost:5432/market")
    assert db.table_names() == ["ticks"]

    query, params = fake_conn.cursor_obj.executed[-1]
    assert "pg_inherits" in query
    assert "relkind in ('r', 'p')" in query
    assert "n.nspname = coalesce(current_schema(), 'public')" in query
    assert params is None
    assert fake_conn.closed is True


def test_postgresql_iter_rows_flattens_batches():
    from iosql.engines.postgresql import Table

    table = object.__new__(Table)
    table.iter_select_batches = lambda *args, **kwargs: iter([
        [{"id": 1}, {"id": 2}], [{"id": 3}],
    ])
    assert list(table.iter_batches(batch_size=2)) == [
        [{"id": 1}, {"id": 2}], [{"id": 3}],
    ]
    assert list(table.iter_rows(batch_size=2)) == [
        {"id": 1}, {"id": 2}, {"id": 3},
    ]


def test_postgresql_database_table_accepts_contract_and_write(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition="duration(ts,every=1day)",
        order_key="ts",
    )
    db = Database("postgresql://user:pass@localhost:5432/market")
    table = db.table("ticks", spec=spec)
    result = table.write([
        {"symbol": "AAPL", "ts": 1000, "price": 1.0},
    ])

    assert result.rows_seen == 1
    assert table.spec().primary_key == ["symbol", "ts"]
    assert fake_conn.cursor_obj.executed
    db.close()
    assert fake_conn.closed is True


def test_postgresql_select_iter_batches_and_count_are_parameterized(monkeypatch):
    fake_conn = FakeConnection()
    fake_conn.cursor_obj.description = [
        ("symbol",),
        ("ts",),
        ("price",),
    ]
    fake_conn.cursor_obj.results = [
        ("AAPL", 1000, 1.0),
        ("MSFT", 2000, 2.0),
    ]
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=None,
        order_key="ts",
    )
    db = Database("postgresql://user:pass@localhost:5432/market")
    table = db.table("ticks", spec=spec)
    # 表创建完成后才注入 count 的 fetchone 结果，避免 relation/drift 探测误读
    fake_conn.cursor_obj.fetchone_result = (2,)

    rows = table.select(
        ["symbol", "ts", "price"],
        where={"field": "symbol", "op": "==", "value": "AAPL"},
        order_by="ts desc",
        limit=10,
    )
    assert rows[0]["symbol"] == "AAPL"
    select_sql, select_params = fake_conn.cursor_obj.executed[-1]
    assert 'WHERE ("symbol" = %s)' in select_sql
    assert "ORDER BY \"ts\" DESC" in select_sql
    assert select_params == ["AAPL", 10]

    batches = list(table.iter_select_batches(
        ["symbol", "ts"],
        where={"field": "ts", "op": ">=", "value": 1000},
        batch_size=2,
        limit=2,
    ))
    assert len(batches) == 1
    batch_sql, batch_params = fake_conn.cursor_obj.executed[-1]
    assert 'WHERE ("ts" >= %s)' in batch_sql
    assert "LIMIT %s" in batch_sql
    assert "OFFSET" not in batch_sql
    assert batch_params == [1000, 2]

    assert table.count_rows() == 2
    count_sql, count_params = fake_conn.cursor_obj.executed[-1]
    assert count_sql == 'SELECT count(*) FROM "ticks"'
    assert count_params == []

    db.close()


def test_postgresql_database_migrate_to_sqlite_batches_rows(tmp_path, monkeypatch):
    rows = [
        ("AAPL", 1735689600000, 187.4),
        ("MSFT", 1735689601000, 420.3),
        ("AAPL", 1735776000000, 189.1),
    ]
    fake_conn = FakePagingConnection(
        rows, ["symbol", "ts", "price"], catalog_names=["pg_ticks"]
    )
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition="duration(ts,every=1day)",
        order_key="ts",
    )
    source = Database("postgresql://user:pass@localhost:5432/market")
    source.table("pg_ticks", spec=spec)

    target_uri = f"sqlite://{tmp_path}/roundtrip.iosql"
    result = source.migrate_to(
        target_uri,
        tables={"pg_ticks": "ticks"},
        batch_size=2,
        verify="row-count",
    )

    assert result.rows_copied == 3
    assert result.batches == 2
    assert result.verified is True
    assert result.source_rows == 3
    assert result.target_rows == 3
    assert not any(
        'OFFSET' in str(query).upper()
        for query, _ in fake_conn.cursor_obj.executed
    )

    with Database(target_uri) as target:
        ticks = target.table("ticks")
        copied = ticks.select("*", where=None, order_by="ts", to_dict=True)
        assert [row["symbol"] for row in copied] == ["AAPL", "MSFT", "AAPL"]
        assert ticks.count_rows() == 3

    source.close()


def _executed_params(fake_conn):
    return [params for _, params in fake_conn.cursor_obj.executed]


def test_postgresql_calendar_partition_uses_iso_text_bounds(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["event_date", "name"],
        columns={"event_date": "text", "name": "text"},
        partition="calendar(event_date,every=1day,tz=UTC)",
    )
    db = Database("postgresql://user:pass@localhost:5432/market")
    table = db.table("events", spec=spec)
    table.write({"event_date": "2026-01-05", "name": "open"})

    assert ("2026-01-05", "2026-01-06") in _executed_params(fake_conn)
    db.close()


def test_postgresql_calendar_partition_uses_compact_text_bounds(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["event_date", "name"],
        columns={"event_date": "text", "name": "text"},
        partition="calendar(event_date,every=1day,tz=UTC)",
    )
    db = Database("postgresql://user:pass@localhost:5432/market")
    table = db.table("events", spec=spec)
    table.write({"event_date": "20260105", "name": "open"})

    assert ("20260105", "20260106") in _executed_params(fake_conn)
    db.close()


def test_postgresql_calendar_partition_uses_date_column_bounds(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["event_date", "name"],
        columns={"event_date": "date", "name": "text"},
        partition="calendar(event_date,every=1day,tz=UTC)",
    )
    db = Database("postgresql://user:pass@localhost:5432/market")
    table = db.table("events", spec=spec)
    table.write({"event_date": "20260105", "name": "open"})

    assert ("2026-01-05", "2026-01-06") in _executed_params(fake_conn)
    db.close()


def test_postgresql_duration_partition_keeps_numeric_bounds(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        pg_table_module.psycopg2,
        "connect",
        lambda uri: fake_conn,
    )

    spec = TableContract(
        primary_key=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition="duration(ts,every=1day)",
    )
    db = Database("postgresql://user:pass@localhost:5432/market")
    table = db.table("ticks", spec=spec)
    table.write({"symbol": "AAPL", "ts": 1735689600000, "price": 187.4})

    assert (1735689600000, 1735776000000) in _executed_params(fake_conn)
    db.close()
