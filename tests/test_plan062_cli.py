"""PLAN-062 CLI 修复回归测试（P0-4/P0-5/P0-6、L1-1~L1-5 与 CLI P2 批次）。"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from typer.testing import CliRunner

import iosql.cli as cli_module
from iosql import Database, Partition, Segment
from iosql.cli import app


runner = CliRunner()

_ROWS = [
    {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
    {"symbol": "MSFT", "ts": 1735689601000, "price": 420.3},
    {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
    {"symbol": "MSFT", "ts": 1735776001000, "price": 421.0},
]


def _uri(path) -> str:
    return f"sqlite://{path}"


def _invoke(*args):
    return runner.invoke(app, [str(arg) for arg in args])


def _make_db(tmp_path, name="market.iosql", rows=None):
    path = tmp_path / name
    db = Database(_uri(path))
    table = db.table(
        "ticks",
        pk=["symbol", "ts"],
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),
        segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
        order_key="ts",
    )
    table.write(rows if rows is not None else list(_ROWS))
    db.close()
    return path


def _make_backup(tmp_path, name="backup.iosql", source=None):
    source = source or _make_db(tmp_path)
    backup = tmp_path / name
    result = _invoke("backup", "-s", source, "--target", backup)
    assert result.exit_code == 0, result.output
    return backup


def _row_count(path) -> int:
    db = Database(_uri(path))
    try:
        return db.table("ticks").count_rows()
    finally:
        db.close()


def _failure_summary(output: str) -> dict:
    marker = '{\n  "error"'
    index = output.find(marker)
    assert index != -1, output
    return json.loads(output[index:])


# ---------------------------------------------------------------------------
# P0-4 restore 目标身份判定
# ---------------------------------------------------------------------------


def test_restore_replace_real_database(tmp_path):
    db_path = _make_db(tmp_path)
    backup_path = _make_backup(tmp_path, source=db_path)
    (db_path / "marker.txt").write_text("to be removed", encoding="utf-8")

    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        db_path,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    assert not (db_path / "marker.txt").exists()
    assert _row_count(db_path) == 4


def test_restore_replace_rejects_fake_meta_only_directory(tmp_path):
    backup_path = _make_backup(tmp_path)

    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "meta.sqlite3").touch()
    (fake / "important.txt").write_text("keep", encoding="utf-8")
    # 即使伪造表级目录+空 meta 文件也必须拒绝
    fake_table = fake / "table_like"
    fake_table.mkdir()
    (fake_table / "meta.sqlite3").touch()

    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        fake,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "不像 iosql 库" in result.output
    assert (fake / "important.txt").read_text(encoding="utf-8") == "keep"
    assert (fake / "meta.sqlite3").exists()


def test_restore_replace_rejects_symlink_target(tmp_path):
    real = _make_db(tmp_path, "real.iosql")
    backup_path = _make_backup(tmp_path, source=real)
    link = tmp_path / "link.iosql"
    link.symlink_to(real)

    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        link,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "符号链接" in result.output
    assert link.is_symlink()
    assert _row_count(real) == 4


def test_restore_rejects_root_and_home_targets(tmp_path, monkeypatch):
    backup_path = _make_backup(tmp_path)

    result = _invoke(
        "restore", "--backup", backup_path, "--target", "/", "--replace", "--yes"
    )
    assert result.exit_code == 1, result.output
    assert "特殊路径" in result.output

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        f"{home}/.",
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "特殊路径" in result.output


# ---------------------------------------------------------------------------
# P0-5 restore backup/target 重叠与可回滚替换
# ---------------------------------------------------------------------------


def test_restore_rejects_backup_target_same_path(tmp_path):
    db_path = _make_db(tmp_path)
    backup_path = _make_backup(tmp_path, source=db_path)

    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        backup_path,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "相同" in result.output
    assert _row_count(backup_path) == 4
    assert _row_count(db_path) == 4


def test_restore_rejects_target_inside_backup(tmp_path):
    backup_path = _make_backup(tmp_path)
    nested_target = backup_path / "nested"

    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        nested_target,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "内部" in result.output
    assert not nested_target.exists()


def test_restore_rejects_backup_inside_target(tmp_path):
    db_path = _make_db(tmp_path)
    inner_backup = db_path / "inner_backup"
    inner_backup.mkdir()
    (inner_backup / "keep.txt").write_text("keep", encoding="utf-8")

    result = _invoke(
        "restore",
        "--backup",
        inner_backup,
        "--target",
        db_path,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "内部" in result.output
    assert (inner_backup / "keep.txt").exists()
    assert _row_count(db_path) == 4


def test_restore_copy_failure_leaves_target_intact(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    (db_path / "keep.txt").write_text("keep", encoding="utf-8")
    backup_path = _make_backup(tmp_path, "other.iosql", source=db_path)

    def _boom(*args, **kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(cli_module.shutil, "copytree", _boom)

    result = _invoke(
        "restore",
        "--backup",
        backup_path,
        "--target",
        db_path,
        "--replace",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert (db_path / "keep.txt").exists()
    assert _row_count(db_path) == 4


# ---------------------------------------------------------------------------
# P0-6 rename-database --overwrite 目标防护
# ---------------------------------------------------------------------------


def test_rename_database_overwrite_rejects_ordinary_directory(tmp_path):
    db_path = _make_db(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep", encoding="utf-8")

    result = _invoke(
        "rename-database",
        "-s",
        db_path,
        "--target",
        victim,
        "--overwrite",
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "不像 iosql" in result.output
    assert (victim / "important.txt").read_text(encoding="utf-8") == "keep"
    assert _row_count(db_path) == 4


def test_rename_database_overwrite_valid_database(tmp_path):
    source = _make_db(tmp_path, "source.iosql")
    target = _make_db(tmp_path, "target.iosql")

    result = _invoke(
        "rename-database",
        "-s",
        source,
        "--target",
        target,
        "--overwrite",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    assert not source.exists()
    assert target.exists()
    assert _row_count(target) == 4


def test_rename_database_rejects_special_and_symlink_targets(tmp_path):
    db_path = _make_db(tmp_path)

    result = _invoke(
        "rename-database", "-s", db_path, "--target", "/", "--overwrite", "--yes"
    )
    assert result.exit_code == 1, result.output
    assert "特殊路径" in result.output

    link = tmp_path / "link.iosql"
    link.symlink_to(tmp_path / "link_target.iosql")
    result = _invoke(
        "rename-database", "-s", db_path, "--target", link, "--overwrite", "--yes"
    )
    assert result.exit_code == 1, result.output
    assert "符号链接" in result.output
    assert _row_count(db_path) == 4


# ---------------------------------------------------------------------------
# L1-1 migrate 同源/嵌套目标守卫与 verify row-count 预检
# ---------------------------------------------------------------------------


def test_migrate_rejects_same_source_and_target(tmp_path):
    db_path = _make_db(tmp_path)

    result = _invoke("migrate", "-s", db_path, "--target", db_path, "-t", "ticks")
    assert result.exit_code == 1, result.output
    assert "不能相同" in result.output
    assert _row_count(db_path) == 4


def test_migrate_rejects_target_inside_source(tmp_path):
    db_path = _make_db(tmp_path)
    nested = db_path / "nested.iosql"

    result = _invoke("migrate", "-s", db_path, "--target", nested, "-t", "ticks")
    assert result.exit_code == 1, result.output
    assert "不能位于源库内部" in result.output
    assert not nested.exists()


def test_migrate_verify_row_count_prechecks_preexisting_target(tmp_path):
    source = _make_db(tmp_path, "source.iosql")
    target = _make_db(tmp_path, "target.iosql")

    result = _invoke(
        "migrate",
        "-s",
        source,
        "--target",
        target,
        "-t",
        "ticks",
        "--verify",
        "row-count",
    )
    assert result.exit_code == 1, result.output
    assert "row-count" in result.output
    assert "已存在表" in result.output
    assert _row_count(target) == 4


def test_migrate_verify_row_count_on_fresh_target(tmp_path):
    source = _make_db(tmp_path, "source.iosql")
    target = tmp_path / "fresh.iosql"

    result = _invoke(
        "migrate",
        "-s",
        source,
        "--target",
        target,
        "-t",
        "ticks",
        "--verify",
        "row-count",
        "--batch-size",
        "2",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verified"] is True
    assert _row_count(target) == 4


# ---------------------------------------------------------------------------
# L1-2 export 原子写
# ---------------------------------------------------------------------------


def test_export_failure_keeps_old_file_and_does_not_create_parents(tmp_path):
    db_path = _make_db(tmp_path)
    old_output = tmp_path / "old.jsonl"
    old_output.write_text("old-content\n", encoding="utf-8")
    missing_where = tmp_path / "no_where.json"

    result = _invoke(
        "export",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-o",
        old_output,
        "--where",
        f"@{missing_where}",
        "--format",
        "jsonl",
    )
    assert result.exit_code == 1, result.output
    assert old_output.read_text(encoding="utf-8") == "old-content\n"

    nested_output = tmp_path / "newdir" / "out.jsonl"
    result = _invoke(
        "export",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-o",
        nested_output,
        "--where",
        f"@{missing_where}",
        "--format",
        "jsonl",
    )
    assert result.exit_code == 1, result.output
    assert not (tmp_path / "newdir").exists()


def test_export_midstream_failure_removes_temp_and_keeps_old_file(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    old_output = tmp_path / "old.jsonl"
    old_output.write_text("old-content\n", encoding="utf-8")

    def _boom(stream, batches, fmt):
        raise OSError("disk full")

    monkeypatch.setattr(cli_module, "_write_row_batches_to_stream", _boom)

    result = _invoke(
        "export",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-o",
        old_output,
        "--format",
        "jsonl",
    )
    assert result.exit_code == 1, result.output
    assert old_output.read_text(encoding="utf-8") == "old-content\n"
    leftovers = [item for item in tmp_path.iterdir() if item.name.startswith(".old.jsonl.")]
    assert leftovers == []


def test_export_success_still_writes(tmp_path):
    db_path = _make_db(tmp_path)
    output = tmp_path / "out.jsonl"
    output.write_text("old-content\n", encoding="utf-8")

    result = _invoke(
        "export",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-o",
        output,
        "--format",
        "jsonl",
    )
    assert result.exit_code == 0, result.output
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert "old-content" not in output.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# L1-3 import BOM / 坏行 / CSV 字段不匹配
# ---------------------------------------------------------------------------


def test_import_reads_utf8_bom_files(tmp_path):
    db_path = _make_db(tmp_path)

    jsonl_path = tmp_path / "bom.jsonl"
    jsonl_path.write_text(
        "\ufeff" + json.dumps({"symbol": "BOM1", "ts": 1735862400000, "price": 1.0}) + "\n",
        encoding="utf-8",
    )
    result = _invoke("import", "-s", db_path, "-t", "ticks", "-f", jsonl_path)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows_seen"] == 1

    csv_path = tmp_path / "bom.csv"
    csv_path.write_text(
        "\ufeffsymbol,ts,price\nBOM2,1735862401000,2.0\n",
        encoding="utf-8",
    )
    result = _invoke(
        "import", "-s", db_path, "-t", "ticks", "-f", csv_path, "--format", "csv"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows_seen"] == 1


def test_import_bad_jsonl_line_reports_line_number_and_partial_summary(tmp_path):
    db_path = _make_db(tmp_path)
    jsonl_path = tmp_path / "bad.jsonl"
    jsonl_path.write_text(
        "\n".join([
            json.dumps({"symbol": "OK1", "ts": 1735862400000, "price": 1.0}),
            json.dumps({"symbol": "OK2", "ts": 1735862401000, "price": 2.0}),
            "{not valid json",
            json.dumps({"symbol": "OK3", "ts": 1735862402000, "price": 3.0}),
        ]) + "\n",
        encoding="utf-8",
    )

    result = _invoke(
        "import",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-f",
        jsonl_path,
        "--batch-size",
        "1",
    )
    assert result.exit_code == 1, result.output
    summary = _failure_summary(result.output)
    assert "第 3 行" in summary["error"]
    assert summary["rows_seen"] == 2
    assert summary["rows_written"] == 2
    assert summary["batches_committed"] == 2


def test_import_csv_field_mismatch_errors(tmp_path):
    db_path = _make_db(tmp_path)

    extra = tmp_path / "extra.csv"
    extra.write_text(
        "symbol,ts,price\nX,1735862400000,1.0,stray\n",
        encoding="utf-8",
    )
    result = _invoke(
        "import", "-s", db_path, "-t", "ticks", "-f", extra, "--format", "csv"
    )
    assert result.exit_code == 1, result.output
    summary = _failure_summary(result.output)
    assert "行字段与表头不匹配（第 2 行）" in summary["error"]

    missing = tmp_path / "missing.csv"
    missing.write_text("symbol,ts,price\nY,1735862401000\n", encoding="utf-8")
    result = _invoke(
        "import", "-s", db_path, "-t", "ticks", "-f", missing, "--format", "csv"
    )
    assert result.exit_code == 1, result.output
    summary = _failure_summary(result.output)
    assert "行字段与表头不匹配（第 2 行）" in summary["error"]


# ---------------------------------------------------------------------------
# L1-5 repartition dry-run 保真
# ---------------------------------------------------------------------------


def test_repartition_dry_run_invalid_interval_fails_cleanly(tmp_path):
    db_path = _make_db(tmp_path)
    source_before = _row_count(db_path)

    result = _invoke(
        "repartition",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-e",
        "bogus",
        "--replace-source",
        "--dry-run",
    )
    assert result.exit_code == 1, result.output
    assert "bogus" in result.output
    assert _row_count(db_path) == source_before


def test_repartition_dry_run_invalid_timezone_fails_cleanly(tmp_path):
    db_path = _make_db(tmp_path)
    target = tmp_path / "new.iosql"

    result = _invoke(
        "repartition",
        "-s",
        db_path,
        "-t",
        "ticks",
        "--partition",
        "calendar(ts,every=1day,timezone=BadZone/Zone)",
        "--target",
        target,
        "--dry-run",
    )
    assert result.exit_code == 1, result.output
    assert "时区无效" in result.output
    assert not target.exists()


def test_repartition_dry_run_missing_target_parent_fails(tmp_path):
    db_path = _make_db(tmp_path)
    target = tmp_path / "missing_dir" / "new.iosql"

    result = _invoke(
        "repartition",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--target",
        target,
        "--dry-run",
    )
    assert result.exit_code == 1, result.output
    assert "父目录不存在" in result.output
    assert not target.exists()


def test_repartition_rejects_target_inside_source(tmp_path):
    db_path = _make_db(tmp_path)
    nested = db_path / "nested.iosql"

    result = _invoke(
        "repartition",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--target",
        nested,
    )
    assert result.exit_code == 1, result.output
    assert "不能位于源库内部" in result.output
    assert not nested.exists()


# ---------------------------------------------------------------------------
# P2 批次：统一错误处理 / 退出码 / 索引列校验 / partition 拼写 / bytes / backup
# ---------------------------------------------------------------------------


def test_cli_bad_inputs_fail_cleanly(tmp_path):
    db_path = _make_db(tmp_path)
    missing_where = tmp_path / "no_where.json"

    cases = [
        ("--limit -1", ["query", "-s", db_path, "-t", "ticks", "--limit", "-1"]),
        ("where @missing", ["query", "-s", db_path, "-t", "ticks", "--where", f"@{missing_where}"]),
        ("empty uri", ["tables", "-s", "sqlite://"]),
        ("bad order-by", ["query", "-s", db_path, "-t", "ticks", "--order-by", "bogus"]),
        ("bad target rows", ["compact", "-s", db_path, "-t", "ticks", "--target-rows", "-5"]),
    ]
    for label, args in cases:
        result = _invoke(*args)
        assert result.exit_code == 1, (label, result.output)
        assert "Traceback" not in result.output, (label, result.output)
        assert result.output.strip(), label


def test_cli_exit_codes_are_consistent(tmp_path):
    db_path = _make_db(tmp_path)

    result = _invoke("validate", "-s", db_path, "-t", "ticks")
    assert result.exit_code == 0, result.output

    result = _invoke("index", "drop", "-s", db_path, "-t", "ticks", "-c", "symbol")
    assert result.exit_code == cli_module.EXIT_OK, result.output

    result = _invoke("index", "check", "-s", db_path, "-t", "ticks")
    assert result.exit_code == cli_module.EXIT_VALIDATION_FAILED, result.output

    result = _invoke("doctor", "-s", db_path, "-t", "ticks")
    assert result.exit_code == cli_module.EXIT_VALIDATION_FAILED, result.output


def test_validate_failure_exit_code_is_2(tmp_path):
    db_path = _make_db(tmp_path)
    result = _invoke("compact", "-s", db_path, "-t", "ticks")
    assert result.exit_code == cli_module.EXIT_OK, result.output

    result = _invoke("segments", "-s", db_path, "-t", "ticks")
    segments = json.loads(result.output)
    os.unlink(segments[0]["path"])

    result = _invoke("validate", "-s", db_path, "-t", "ticks", "--deep")
    assert result.exit_code == cli_module.EXIT_VALIDATION_FAILED, result.output
    assert json.loads(result.output)["ok"] is False


def test_index_create_rejects_unknown_column(tmp_path):
    db_path = _make_db(tmp_path)

    result = _invoke("index", "create", "-s", db_path, "-t", "ticks", "-c", "nope")
    assert result.exit_code == 1, result.output
    assert "列不存在" in result.output

    result = _invoke("index", "list", "-s", db_path, "-t", "ticks")
    payload = json.loads(result.output)
    assert all("nope" not in index for part in payload["parts"] for index in part["indexes"])


def test_compact_and_cleanup_reject_unknown_partition(tmp_path):
    db_path = _make_db(tmp_path)

    result = _invoke("compact", "-s", db_path, "-t", "ticks", "--partition", "99999")
    assert result.exit_code == 1, result.output
    assert "分区不存在" in result.output

    result = _invoke(
        "cleanup-obsolete", "-s", db_path, "-t", "ticks", "--partition", "99999"
    )
    assert result.exit_code == 1, result.output
    assert "分区不存在" in result.output

    db = Database(_uri(db_path))
    try:
        valid_part = db.table("ticks").table_parts()[0]
    finally:
        db.close()
    result = _invoke(
        "compact", "-s", db_path, "-t", "ticks", "--partition", str(valid_part)
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["partitions"] == [valid_part]


def test_query_bytes_columns_are_base64(tmp_path):
    db_path = tmp_path / "blobs.iosql"
    payload = b"\x00\x01hi"
    db = Database(_uri(db_path))
    table = db.table("blobs", pk=["id"], columns={"id": "integer", "payload": "blob"})
    table.write([{"id": 1, "payload": payload}])
    db.close()

    result = _invoke("query", "-s", db_path, "-t", "blobs", "--format", "json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["payload"] == base64.b64encode(payload).decode("ascii")


def test_backup_rejects_target_overlapping_source(tmp_path):
    db_path = _make_db(tmp_path)
    inner = db_path / "inner"

    result = _invoke("backup", "-s", db_path, "--target", inner)
    assert result.exit_code == 1, result.output
    assert "内部" in result.output
    assert not inner.exists()

    result = _invoke(
        "repartition",
        "-s",
        db_path,
        "-t",
        "ticks",
        "-e",
        "1hour",
        "--replace-source",
        "--backup",
        inner,
        "--yes",
    )
    assert result.exit_code == 1, result.output
    assert "内部" in result.output
    assert _row_count(db_path) == 4


def test_backup_path_includes_microseconds_and_pid(tmp_path):
    source = tmp_path / "market.iosql"
    first = cli_module._backup_path(source)
    time.sleep(0.002)
    second = cli_module._backup_path(source)
    assert first != second
    assert f"_{os.getpid()}" in first.name


def test_module_entrypoint_help():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "cli", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "tables" in proc.stdout
    assert "restore" in proc.stdout
