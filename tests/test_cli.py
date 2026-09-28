import csv
import json
import os

import iosql.cli as cli_module
from typer.testing import CliRunner

from iosql.cli import app
from iosql import (
    Database,
    Partition,
    PhysicalLayout,
    Segment,
    TableContract,
    TimeColumn,
)


runner = CliRunner()


def _db_uri(path):
    return f"sqlite://{path}"


def _make_sample_db(tmp_path):
    path = tmp_path / "market.iosql"
    db = Database(_db_uri(path))
    table = db.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    table.write([
        {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
        {"symbol": "MSFT", "ts": 1735689601000, "price": 420.3},
        {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
        {"symbol": "MSFT", "ts": 1735776001000, "price": 421.0},
    ])
    db.close()
    return path


def _invoke(*args):
    return runner.invoke(app, list(args))


def test_cli_help_lists_operational_commands():
    result = _invoke("--help")
    assert result.exit_code == 0, result.output
    assert "tables" in result.output, result.output
    assert "validate" in result.output
    assert "compact" in result.output
    assert "cleanup-obsolete" in result.output
    assert "migrate" in result.output
    assert "repartition" in result.output
    assert "index" in result.output


def test_cli_rejects_non_sqlite_uri():
    result = _invoke("tables", "-s", "postgresql://user:pass@localhost:5432/db")
    assert result.exit_code == 1, result.output
    assert "只支持" in result.output


def test_cli_read_only_inspection_commands(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("tables", "-s", str(db_path))
    assert result.exit_code == 0, result.output
    assert "ticks" in result.output

    result = _invoke("spec", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    spec = json.loads(result.output)
    assert spec["primary_key"] == ["symbol", "ts"]
    assert spec["partition"]["every"] == 86_400_000

    result = _invoke("layout", "-s", str(db_path), "-t", "ticks", "--summary")
    assert result.exit_code == 0, result.output
    layout = json.loads(result.output)
    assert layout["total_rows"] == 4
    assert layout["total_partitions"] == 2

    result = _invoke("parts", "-s", str(db_path), "-t", "ticks", "--json")
    assert result.exit_code == 0, result.output
    parts = json.loads(result.output)
    assert len(parts["parts"]) == 2

    result = _invoke("segments", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    segments = json.loads(result.output)
    assert len(segments) == 2


def test_cli_explain_query_export_and_import(tmp_path):
    db_path = _make_sample_db(tmp_path)
    where = json.dumps({"field": "symbol", "op": "==", "value": "AAPL"})

    result = _invoke(
        "explain",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        where,
        "--order-by",
        "ts",
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["table"] == "ticks"
    assert plan["scanned_partitions"]

    result = _invoke(
        "query",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        where,
        "--order-by",
        "ts",
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["symbol"] for row in rows] == ["AAPL", "AAPL"]

    export_path = tmp_path / "aapl.jsonl"
    result = _invoke(
        "export",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-o",
        str(export_path),
        "--where",
        where,
    )
    assert result.exit_code == 0, result.output
    assert export_path.exists()
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 2

    import_path = tmp_path / "new_rows.jsonl"
    import_path.write_text(
        json.dumps({"symbol": "GOOG", "ts": 1735862400000, "price": 150.0}) + "\n",
        encoding="utf-8",
    )
    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(import_path),
        "--mode",
        "append",
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["rows_seen"] == 1

    result = _invoke(
        "query",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        json.dumps({"field": "symbol", "op": "==", "value": "GOOG"}),
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["price"] == 150.0


def test_cli_compact_marks_old_segments_obsolete(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("compact", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["rows_read"] == 4
    assert summary["rows_written"] == 4
    assert summary["old_segments"] == 2
    assert summary["new_segments"] == 2
    assert summary["obsolete_segments"] == 2

    result = _invoke("segments", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    visible = json.loads(result.output)
    assert len(visible) == 2
    assert {item["state"] for item in visible} == {"sealed"}

    result = _invoke(
        "segments",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--include-obsolete",
    )
    assert result.exit_code == 0, result.output
    all_segments = json.loads(result.output)
    assert [item["state"] for item in all_segments].count("obsolete") == 2
    assert [item["state"] for item in all_segments].count("sealed") == 2

    result = _invoke("validate", "-s", str(db_path), "-t", "ticks", "--deep")
    assert result.exit_code == 0, result.output
    validation = json.loads(result.output)
    assert validation["ok"] is True
    assert validation["checked_segments"] == 2

    result = _invoke("cleanup-obsolete", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    assert preview["dry_run"] is True
    assert preview["candidate_segments"] == 2
    assert preview["files_deleted"] == 0
    assert preview["bytes_reclaimable"] > 0
    assert all(os.path.exists(path) for path in preview["files"])

    result = _invoke("cleanup-obsolete", "-s", str(db_path), "-t", "ticks", "--yes")
    assert result.exit_code == 0, result.output
    cleanup = json.loads(result.output)
    assert cleanup["dry_run"] is False
    assert cleanup["candidate_segments"] == 2
    assert cleanup["files_deleted"] >= 2
    assert cleanup["manifest_rows_deleted"] == 2
    assert all(not os.path.exists(path) for path in preview["files"])

    result = _invoke(
        "segments",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--include-obsolete",
    )
    assert result.exit_code == 0, result.output
    cleaned_segments = json.loads(result.output)
    assert [item["state"] for item in cleaned_segments] == ["sealed", "sealed"]


def test_cli_export_streams_jsonl_csv_and_json_batches(tmp_path):
    db_path = _make_sample_db(tmp_path)
    where = json.dumps({"field": "symbol", "op": "==", "value": "AAPL"})

    jsonl_path = tmp_path / "aapl.jsonl"
    result = _invoke(
        "export",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-o",
        str(jsonl_path),
        "--where",
        where,
        "--format",
        "jsonl",
        "--batch-size",
        "1",
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["rows"] == 2
    assert summary["batch_size"] == 1
    jsonl_rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["symbol"] for row in jsonl_rows] == ["AAPL", "AAPL"]

    csv_path = tmp_path / "aapl.csv"
    result = _invoke(
        "export",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-o",
        str(csv_path),
        "--where",
        where,
        "--fields",
        "symbol,ts",
        "--format",
        "csv",
        "--batch-size",
        "1",
    )
    assert result.exit_code == 0, result.output
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    assert [row["symbol"] for row in csv_rows] == ["AAPL", "AAPL"]
    assert set(csv_rows[0]) == {"symbol", "ts"}

    json_path = tmp_path / "aapl.json"
    result = _invoke(
        "export",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-o",
        str(json_path),
        "--where",
        where,
        "--format",
        "json",
        "--batch-size",
        "1",
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert [row["symbol"] for row in rows] == ["AAPL", "AAPL"]


def test_cli_import_streams_jsonl_csv_and_keeps_json_compat(tmp_path, monkeypatch):
    db_path = _make_sample_db(tmp_path)

    def fail_read_rows(*args, **kwargs):
        raise AssertionError("_read_rows must not be used by import")

    monkeypatch.setattr(cli_module, "_read_rows", fail_read_rows)

    jsonl_path = tmp_path / "new_rows.jsonl"
    jsonl_path.write_text(
        "\n".join([
            json.dumps({"symbol": "GOOG", "ts": 1735862400000, "price": 150.0}),
            json.dumps({"symbol": "AMZN", "ts": 1735862401000, "price": 151.0}),
            json.dumps({"symbol": "META", "ts": 1735862402000, "price": 152.0}),
        ]) + "\n",
        encoding="utf-8",
    )
    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(jsonl_path),
        "--mode",
        "append",
        "--batch-size",
        "2",
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary == {
        "rows_seen": 3,
        "rows_written": 3,
        "batches_committed": 2,
        "mode": "append",
        "batch_size": 2,
    }

    csv_path = tmp_path / "more_rows.csv"
    csv_path.write_text(
        "symbol,ts,price\nNFLX,1735862403000,153.0\nORCL,1735862404000,154.0\n",
        encoding="utf-8",
    )
    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(csv_path),
        "--format",
        "csv",
        "--batch-size",
        "1",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows_seen"] == 2

    json_path = tmp_path / "json_rows.json"
    json_path.write_text(
        json.dumps([
            {"symbol": "IBM", "ts": 1735862405000, "price": 155.0},
        ]),
        encoding="utf-8",
    )
    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(json_path),
        "--batch-size",
        "1",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows_seen"] == 1

    result = _invoke(
        "query",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--where",
        json.dumps({"field": "symbol", "op": "in", "value": ["GOOG", "ORCL", "IBM"]}),
        "--order-by",
        "ts",
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["symbol"] for row in rows] == ["GOOG", "ORCL", "IBM"]

    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(jsonl_path),
        "--batch-size",
        "0",
    )
    assert result.exit_code == 1
    assert "batch-size 必须大于 0" in result.output


def test_cli_import_ignore_mode_reports_rows_written_none(tmp_path):
    db_path = _make_sample_db(tmp_path)
    jsonl_path = tmp_path / "dup.jsonl"
    jsonl_path.write_text(
        "\n".join([
            json.dumps({"symbol": "AAPL", "ts": 1735689600000, "price": 999.0}),
            json.dumps({"symbol": "NEW", "ts": 1736000000000, "price": 1.0}),
        ]) + "\n",
        encoding="utf-8",
    )

    result = _invoke(
        "import",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-f",
        str(jsonl_path),
        "--mode",
        "ignore",
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["rows_seen"] == 2
    assert summary["rows_written"] is None


class _FakeTargetTable:
    def __init__(self, table_name):
        self.table_name = table_name
        self.batches = []

    def write(self, rows, mode=None):
        self.batches.append(list(rows))

    def count_rows(self, where=None):
        return sum(len(batch) for batch in self.batches)


class _FakeTargetDatabase:
    def __init__(self):
        self.tables = {}
        self.closed = False

    def table(self, table_name, spec=None):
        self.tables.setdefault(table_name, _FakeTargetTable(table_name))
        return self.tables[table_name]

    def close(self):
        self.closed = True


def test_cli_migrate_to_postgresql_uri_with_table_mapping(tmp_path, monkeypatch):
    db_path = _make_sample_db(tmp_path)
    fake_target = _FakeTargetDatabase()
    monkeypatch.setattr(
        Database,
        "_new_migration_target",
        lambda self, target_uri: fake_target,
    )

    result = _invoke(
        "migrate",
        "-s",
        str(db_path),
        "--target",
        "postgresql://user:pass@localhost:5432/market",
        "-t",
        "ticks:pg_ticks",
        "--batch-size",
        "2",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rows_copied"] == 4
    assert payload["batches"] == 2
    assert payload["tables"][0]["table"] == "ticks"
    assert payload["tables"][0]["target_table"] == "pg_ticks"
    assert [len(batch) for batch in fake_target.tables["pg_ticks"].batches] == [2, 2]
    assert fake_target.closed is True


def test_cli_migrate_dry_run_and_verify_row_count(tmp_path, monkeypatch):
    db_path = _make_sample_db(tmp_path)
    monkeypatch.setattr(
        Database,
        "_new_migration_target",
        lambda self, target_uri: (_ for _ in ()).throw(
            AssertionError("dry-run should not create target database")
        ),
    )

    result = _invoke(
        "migrate",
        "-s",
        str(db_path),
        "--target",
        "postgresql://user:pass@localhost:5432/market",
        "-t",
        "ticks:pg_ticks",
        "--batch-size",
        "3",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["rows_copied"] == 0
    assert payload["source_rows"] == 4
    assert payload["batches"] == 2
    assert payload["tables"][0]["target_table"] == "pg_ticks"

    fake_target = _FakeTargetDatabase()
    monkeypatch.setattr(
        Database,
        "_new_migration_target",
        lambda self, target_uri: fake_target,
    )
    result = _invoke(
        "migrate",
        "-s",
        str(db_path),
        "--target",
        "postgresql://user:pass@localhost:5432/market",
        "-t",
        "ticks:pg_ticks",
        "--batch-size",
        "3",
        "--verify",
        "row-count",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verified"] is True
    assert payload["source_rows"] == 4
    assert payload["target_rows"] == 4
    assert payload["tables"][0]["verify"] == "row-count"


def test_cli_doctor_fix_and_index_check(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("doctor", "-s", str(db_path), "-t", "ticks", "--fix")
    assert result.exit_code == 0, result.output
    assert "fix: ok" in result.output

    result = _invoke("index", "check", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["missing"] == []

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parts"]
    assert "data_symbol_index" in payload["parts"][0]["indexes"]


def test_cli_index_create_and_drop(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "index", "create", "-s", str(db_path), "-t", "ticks", "-c", "symbol"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["created"] is True

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    payload = json.loads(result.output)
    assert "data_symbol_index" in payload["parts"][0]["indexes"]

    result = _invoke(
        "index", "drop", "-s", str(db_path), "-t", "ticks", "-c", "symbol"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dropped"] > 0

    result = _invoke("index", "list", "-s", str(db_path), "-t", "ticks")
    payload = json.loads(result.output)
    assert "data_symbol_index" not in payload["parts"][0]["indexes"]


def test_cli_backup_and_restore(tmp_path):
    db_path = _make_sample_db(tmp_path)
    backup_path = tmp_path / "backup.iosql"
    restore_path = tmp_path / "restored.iosql"

    result = _invoke("backup", "-s", str(db_path), "--target", str(backup_path))
    assert result.exit_code == 0, result.output
    assert backup_path.exists()

    result = _invoke("restore", "--backup", str(backup_path), "--target", str(restore_path))
    assert result.exit_code == 0, result.output
    assert restore_path.exists()

    result = _invoke("tables", "-s", str(restore_path))
    assert result.exit_code == 0
    assert "ticks" in result.output


def test_cli_restore_replace_rejects_non_iosql_target(tmp_path):
    db_path = _make_sample_db(tmp_path)
    backup_path = tmp_path / "backup.iosql"
    result = _invoke("backup", "-s", str(db_path), "--target", str(backup_path))
    assert result.exit_code == 0, result.output

    target = tmp_path / "not_a_db"
    target.mkdir()
    (target / "random.txt").write_text("hello", encoding="utf-8")

    result = _invoke(
        "restore",
        "--backup",
        str(backup_path),
        "--target",
        str(target),
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1
    assert "不像 iosql 库" in result.output
    assert (target / "random.txt").exists()


def test_cli_repartition_default_behavior_and_flags(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour")
    assert result.exit_code == 1, result.output
    assert "请提供 --target 写入新库，或提供 --replace-source 明确替换源库" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--target", str(tmp_path / "target.iosql"), "--backup", str(tmp_path / "backup.iosql"),
    )
    assert result.exit_code == 1, result.output
    assert "--backup 只能与 --replace-source 同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--target", str(tmp_path / "target.iosql"), "--no-backup",
    )
    assert result.exit_code == 1, result.output
    assert "--no-backup 只能与 --replace-source 同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--backup", str(tmp_path / "backup.iosql"), "--no-backup",
        "--replace-source", "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "--backup 与 --no-backup 不能同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks",
        "--partition", "duration(ts,every=1hour)", "-e", "1day", "--dry-run",
    )
    assert result.exit_code == 1, result.output
    assert "--partition 不能与 --every/--kind/--timezone 同时使用" in result.output

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["plan"]["source_replaced"] is True
    assert payload["plan"]["new_partition_expr"] == "duration(ts,every=1hour)"


def test_cli_repartition_replace_source_with_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True
    assert payload["backup"] is not None
    assert os.path.isdir(payload["backup"])

    db = Database(_db_uri(db_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
        assert len(target_table.select("*", where=None, to_dict=True)) == 4
    finally:
        db.close()


def test_cli_repartition_replace_source_no_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--no-backup", "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True
    assert payload["backup"] is None

    db = Database(_db_uri(db_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
    finally:
        db.close()


def test_cli_repartition_replace_source_custom_backup(tmp_path):
    db_path = _make_sample_db(tmp_path)
    custom_backup = tmp_path / "my_backup.iosql"

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "ticks", "-e", "1hour",
        "--replace-source", "--backup", str(custom_backup), "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True
    assert str(custom_backup) in payload["backup"]
    assert custom_backup.exists()
    backup_db = Database(_db_uri(custom_backup))
    try:
        t = backup_db.table("ticks")
        assert t.spec().partition.every == 86400000
    finally:
        backup_db.close()


def test_cli_repartition_dry_run_and_target(tmp_path):
    db_path = _make_sample_db(tmp_path)
    target_path = tmp_path / "hourly.iosql"

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--target",
        str(target_path),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert not target_path.exists()

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--target",
        str(target_path),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backup"] is None
    assert target_path.exists()

    db = Database(_db_uri(target_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.every == "1hour"
        assert len(target_table.select("*", where=None, to_dict=True)) == 4
    finally:
        db.close()


def test_cli_repartition_partition_expr_dry_run_and_target(tmp_path):
    db_path = _make_sample_db(tmp_path)
    target_path = tmp_path / "partition_expr.iosql"

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--partition",
        "duration(ts,every=1hour)",
        "--target",
        str(target_path),
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["plan"]["old_partition_expr"] == "duration(ts,every=1day)"
    assert payload["plan"]["new_partition_expr"] == "duration(ts,every=1hour)"
    assert not target_path.exists()

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "ticks",
        "--partition",
        "duration(ts,every=1hour)",
        "--target",
        str(target_path),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["backup"] is None
    assert target_path.exists()

    db = Database(_db_uri(target_path))
    try:
        target_table = db.table("ticks")
        assert target_table.spec().partition.to_string() == "duration(ts,every=1hour)"
    finally:
        db.close()


def test_cli_repartition_range_partition(tmp_path):
    db_path = tmp_path / "range.iosql"
    db = Database(_db_uri(db_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "val": "float"},
        partition=Partition.range("id", every=1),
    )
    table.write([{"id": i, "val": i * 1.5} for i in range(20)])
    db.close()

    result = _invoke(
        "repartition", "-s", str(db_path), "-t", "items", "-e", "5",
        "--replace-source", "--yes",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source_replaced"] is True

    db = Database(_db_uri(db_path))
    try:
        t = db.table("items")
        assert t.spec().partition.kind == "range"
        rows = t.select("*", where=None, to_dict=True)
        assert len(rows) == 20
    finally:
        db.close()


def test_cli_repartition_range_partition_expr(tmp_path):
    db_path = tmp_path / "range_expr.iosql"
    db = Database(_db_uri(db_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "val": "float"},
        partition=Partition.range("id", every=1),
    )
    table.write([{"id": i, "val": i * 1.5} for i in range(20)])
    db.close()

    result = _invoke(
        "repartition",
        "-s",
        str(db_path),
        "-t",
        "items",
        "--partition",
        "range(id,width=5)",
        "--replace-source",
        "--yes",
    )
    assert result.exit_code == 0, result.output

    db = Database(_db_uri(db_path))
    try:
        t = db.table("items")
        assert t.spec().partition.to_string() == "range(id,width=5)"
        assert len(t.select("*", where=None, to_dict=True)) == 20
    finally:
        db.close()


def test_cli_doctor_recommends_partition_expr_for_range(tmp_path):
    db_path = tmp_path / "range_doctor.iosql"
    db = Database(_db_uri(db_path))
    table = db.table(
        "items",
        pk=["id"],
        columns={"id": "integer", "val": "float"},
        partition=Partition.range("id", every=1),
    )
    table.write([{"id": i, "val": i * 1.5} for i in range(20)])
    db.close()

    result = _invoke("doctor", "-s", str(db_path), "-t", "items")
    assert result.exit_code == 2, result.output
    assert "partition: range(id,width=1)" in result.output
    assert "suggestions:" in result.output
    assert "--partition 'range(id,width=500000)' --replace-source --yes" in result.output


def test_cli_doctor_prefers_time_partition_for_range_time_table(tmp_path):
    db_path = tmp_path / "time_range_doctor.iosql"
    db = Database(_db_uri(db_path))
    spec = TableContract(
        primary_key=["symbol", "date"],
        time=TimeColumn("date", unit="date", timezone="Asia/Shanghai"),
        partition="range(partition_key,width=1)",
        columns={
            "symbol": "text",
            "date": "text",
            "partition_key": "integer",
            "close": "float",
        },
        order_key="date",
        physical_layout=PhysicalLayout(
            target_rows_per_file=8,
            min_rows_per_file=5,
            max_rows_per_file=10,
        ),
    )
    table = db.table("daily_bars", spec=spec)
    rows = []
    for day in range(4):
        date = f"2025-01-{day + 1:02d}"
        rows.extend([
            {
                "symbol": "AAPL",
                "date": date,
                "partition_key": day,
                "close": 100.0 + day,
            },
            {
                "symbol": "MSFT",
                "date": date,
                "partition_key": day,
                "close": 200.0 + day,
            },
        ])
    table.write(rows)
    db.close()

    result = _invoke("doctor", "-s", str(db_path), "-t", "daily_bars")

    assert result.exit_code == 2, result.output
    assert "partition: range(partition_key,width=1)" in result.output
    assert "suggestions:" in result.output
    assert "1. 推荐: 按时间列 date 使用 5day" in result.output
    assert "max/avg≈5" in result.output
    assert "--partition 'calendar(date,every=5day,timezone=Asia/Shanghai)'" in result.output
    assert "--partition 'calendar(date,every=1week,timezone=Asia/Shanghai)'" in result.output
    assert "--partition 'range(partition_key,width=5)'" in result.output
    assert result.output.index("calendar(date,every=5day") < result.output.index(
        "range(partition_key,width=5)"
    )


def test_cli_doctor_duration_time_range_table_uses_day_baseline(tmp_path):
    db_path = tmp_path / "ts_range_doctor.iosql"
    db = Database(_db_uri(db_path))
    spec = TableContract(
        primary_key=["symbol", "ts"],
        time=TimeColumn("ts", unit="ms", timezone="UTC"),
        partition="range(partition_key,width=1)",
        columns={
            "symbol": "text",
            "ts": "integer",
            "partition_key": "integer",
            "price": "float",
        },
        order_key="ts",
        physical_layout=PhysicalLayout(
            target_rows_per_file=8,
            min_rows_per_file=5,
            max_rows_per_file=10,
        ),
    )
    table = db.table("ticks", spec=spec)
    base_ts = 1735689600000
    rows = []
    for day in range(4):
        rows.extend([
            {
                "symbol": "AAPL",
                "ts": base_ts + day * 86_400_000,
                "partition_key": day,
                "price": 100.0 + day,
            },
            {
                "symbol": "MSFT",
                "ts": base_ts + day * 86_400_000,
                "partition_key": day,
                "price": 200.0 + day,
            },
        ])
    table.write(rows)
    db.close()

    result = _invoke("doctor", "-s", str(db_path), "-t", "ticks")

    assert result.exit_code == 2, result.output
    assert "1. 推荐: 按时间列 ts 使用 5day" in result.output
    assert "--partition 'duration(ts,every=5day)'" in result.output
    assert "duration(ts,every=5ms)" not in result.output


def test_cli_doctor_range_partition_suggestion(tmp_path):
    from iosql.cli import (
        _recommended_calendar_every,
        _suggest_range_every_down,
        _suggest_range_every_up,
    )

    assert _suggest_range_every_up(avg_rows=316, target=350_000, current_every=1) > 1
    assert _suggest_range_every_down(avg_rows=500_000, target=200_000, current_every=100) < 100
    assert _suggest_range_every_down(avg_rows=500_000, target=200_000, current_every=1) == 1
    assert _recommended_calendar_every(4) == ("4day", 4)
    assert _recommended_calendar_every(1582) == ("5year", 1260)


def test_cli_describe_outputs_normalized_structure(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke("describe", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["name"] == "ticks"
    assert info["primary_key"] == ["symbol", "ts"]
    names = [c["name"] for c in info["columns"]]
    assert names == ["symbol", "ts", "price"]
    for col in info["columns"]:
        assert set(col) == {"name", "type", "description", "range"}
        assert col["description"] is None and col["range"] is None

    result = _invoke("describe", "-s", str(db_path), "-t", "nope")
    assert result.exit_code != 0
    assert "表不存在" in result.output


def test_cli_update_metadata_sets_descriptions_and_ranges(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--description", "1 分钟行情，UTC 毫秒时间戳。",
        "--column-desc", "price=收盘成交价。",
        "--column-desc", "symbol=证券代码。",
        "--column-range", "price=0:inf",
    )
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["description"] == "1 分钟行情，UTC 毫秒时间戳。"
    by_name = {c["name"]: c for c in info["columns"]}
    assert by_name["price"]["description"] == "收盘成交价。"
    assert by_name["price"]["range"] == {"low": 0, "high": "+inf"}
    assert by_name["symbol"]["description"] == "证券代码。"
    assert by_name["symbol"]["range"] is None

    # 重开数据库后 describe 仍可读到（SQLite sidecar 持久化）
    result = _invoke("describe", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code == 0, result.output
    again = json.loads(result.output)
    assert again["description"] == "1 分钟行情，UTC 毫秒时间戳。"

    # 有界值域与负无穷边界
    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-range", "price=-1:1",
    )
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    by_name = {c["name"]: c for c in info["columns"]}
    assert by_name["price"]["range"] == {"low": -1, "high": 1}


def test_cli_update_metadata_clear_semantics(tmp_path):
    db_path = _make_sample_db(tmp_path)

    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--description", "临时表介绍",
        "--column-desc", "price=临时解释",
        "--column-range", "price=0:100",
    )
    assert result.exit_code == 0, result.output

    # COL=（空文本）只清除该列解释；同时验证省略项不受影响
    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-desc", "price=",
    )
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    by_name = {c["name"]: c for c in info["columns"]}
    assert by_name["price"]["description"] is None
    assert by_name["price"]["range"] == {"low": 0, "high": 100}
    assert info["description"] == "临时表介绍"

    # 清空类操作与清除表介绍
    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--clear-column-ranges", "--clear-description",
    )
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["description"] is None
    assert all(c["range"] is None for c in info["columns"])


def test_cli_update_metadata_rejects_invalid_input(tmp_path):
    db_path = _make_sample_db(tmp_path)

    # 无任何修改
    result = _invoke("update-metadata", "-s", str(db_path), "-t", "ticks")
    assert result.exit_code != 0
    assert "没有任何修改" in result.output

    # 互斥选项
    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--description", "x", "--clear-description",
    )
    assert result.exit_code != 0
    assert "不能同时使用" in result.output

    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-desc", "price=x", "--clear-column-descs",
    )
    assert result.exit_code != 0

    # 非法值域格式 / low > high / 未知列
    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-range", "price=0",
    )
    assert result.exit_code != 0
    assert "缺少上界" in result.output

    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-range", "price=abc:1",
    )
    assert result.exit_code != 0
    assert "low 边界无效" in result.output

    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-range", "price=5:1",
    )
    assert result.exit_code != 0
    assert "low(5.0) > high(1.0)" in result.output

    # 未知列：核心校验 BadArgument → 错误信息含列名（经守卫转 _fail）
    result = _invoke(
        "update-metadata", "-s", str(db_path), "-t", "ticks",
        "--column-desc", "nope=解释",
    )
    assert result.exit_code != 0
    assert "nope" in result.output
