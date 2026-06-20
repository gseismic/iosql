"""iosql 命令行工具。

命令:
  iosql doctor        — 诊断数据库健康状态
  iosql repartition   — 重分区表数据（含自动备份）

示例:
  iosql doctor -s data/day_kline.iosql
  iosql doctor -s data/day_kline.iosql -t daily_bars --fix
  iosql repartition -s data/day_kline.iosql -t daily_bars -e month
"""

import shutil
import time
from datetime import datetime
from pathlib import Path

import typer
from loguru import logger

from iosql import Database, Partition, TableSpec
from iosql.utils.mysqlite import Database as MyDatabase

app = typer.Typer(help="iosql 数据库管理工具")


def _parse_uri(raw: str) -> str:
    if raw.startswith("sqlite://"):
        return raw
    return f"sqlite://{raw}"


def _uri_to_path(uri: str) -> Path:
    return Path(uri.replace("sqlite://", ""))


def _backup_path(source_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return source_path.with_name(f"{source_path.name}.backup_{ts}")


@app.command()
def doctor(
    source: str = typer.Option(..., "--source", "-s", help="数据库 URI"),
    table: str = typer.Option("", "--table", "-t", help="表名，默认检查所有表"),
    fix: bool = typer.Option(False, "--fix", help="自动修复（添加缺失索引）"),
):
    """诊断数据库健康状态：分区大小、索引完整性。"""
    source_uri = _parse_uri(source)
    source_path = _uri_to_path(source_uri)
    if not source_path.exists():
        logger.error(f"数据库不存在: {source_path}")
        raise typer.Exit(1)

    db = Database(source_uri)
    table_names = [table] if table else db.table_names()
    if not table_names:
        logger.warning("数据库中没有表")
        db.close()
        return

    issues = 0
    fixed = 0
    for tname in sorted(table_names):
        t = db.table(tname)
        spec = t.spec()
        if not spec or not spec.primary_key:
            logger.info(f"[{tname}] 无表契约，跳过")
            continue

        report = t.layout_report()
        logger.info(
            f"[{tname}] {report.total_rows} 行, "
            f"{report.total_partitions} 个分区"
        )

        oversized = report.oversized_partitions
        undersized = report.undersized_partitions
        if oversized:
            issues += 1
            logger.warning(
                f"  ⚠ 分区过大 ({len(oversized)} 个 > {report.max_rows_per_file} 行): "
                f"{oversized[:5]}{'...' if len(oversized) > 5 else ''}"
            )
            logger.info(f"  修复: iosql repartition -s {source} -t {tname} -e <更小周期>")
        elif undersized:
            issues += 1
            logger.warning(
                f"  ⚠ 分区过小 ({len(undersized)} 个 < {report.min_rows_per_file} 行): "
                f"{undersized[:5]}{'...' if len(undersized) > 5 else ''}"
            )
            logger.info(f"  修复: iosql repartition -s {source} -t {tname} -e <更大周期>")
        else:
            logger.info(f"  ✓ 分区大小正常 ({report.min_rows_per_file}~{report.max_rows_per_file})")

        pk_missing = _missing_indexes(source_path, tname, spec.primary_key)
        if pk_missing:
            issues += 1
            logger.warning(f"  ⚠ 主键缺少索引: {pk_missing}")
            logger.info(f"  修复: iosql doctor -s {source} -t {tname} --fix")
            if fix:
                for col in pk_missing:
                    t.create_index(col)
                    fixed += 1
                    logger.info(f"  ✓ 已创建索引: {tname}.{col}")
        else:
            logger.info(f"  ✓ 主键索引完整")

    db.close()

    if issues == 0:
        logger.info("✓ 数据库健康状态正常")
    else:
        logger.warning(f"发现 {issues} 个问题" + (f"，已修复 {fixed} 个" if fix else ""))


def _missing_indexes(source_path: Path, table_name: str, pk_columns: list) -> list:
    """检查主键列是否有索引。检查第一个分区即可（索引在所有分区上创建）。"""
    try:
        for entry in source_path.iterdir():
            if entry.is_dir() and entry.name == table_name:
                data_dir = entry / "data"
                if not data_dir.exists():
                    return []
                parts = sorted(data_dir.glob("*.sqlite3"))
                if not parts:
                    return []
                pdb = MyDatabase(str(parts[0]), verbose=0)
                try:
                    indices = set(pdb.get_table_indices("data", exclude_autoindex=True))
                finally:
                    pdb.close()

                missing = []
                for col in pk_columns:
                    expected = f"data_{col}_index"
                    if expected not in indices:
                        missing.append(col)
                return missing
    except Exception:
        pass
    return []


@app.command()
def repartition(
    source: str = typer.Option(..., "--source", "-s", help="源数据库 URI"),
    table: str = typer.Option(..., "--table", "-t", help="表名"),
    every: str = typer.Option("quarter", "--every", "-e", help="新分区周期: month/quarter/week/biweek/biennium"),
    target: str = typer.Option("", "--target", help="目标数据库 URI，默认覆盖源库"),
    rm_backup: bool = typer.Option(False, "--rm-backup", help="完成后删除备份"),
):
    """重分区表数据。默认先备份源库，再创建新分区布局。"""
    source_uri = _parse_uri(source)
    source_path = _uri_to_path(source_uri)
    target_uri = target and _parse_uri(target)

    if not source_path.exists():
        logger.error(f"源数据库不存在: {source_path}")
        raise typer.Exit(1)

    backup = _backup_path(source_path)
    logger.info(f"备份源库: {source_path} → {backup}")
    shutil.copytree(source_path, backup, dirs_exist_ok=False)

    try:
        db = Database(source_uri)
        old_spec = db.table(table).spec()
        if not old_spec:
            logger.error(f"表不存在或无契约: {table}")
            raise typer.Exit(1)

        old_partition = old_spec.partition
        new_spec_data = old_spec.to_dict()
        if old_partition:
            new_partition = Partition.range(
                column=old_partition.column,
                every=old_partition.every,
            )
            new_spec_data["partition"] = new_partition.to_dict()

        new_spec = TableSpec.from_dict(new_spec_data)
        new_spec.force_partition = True
        db.close()

        final_target = target_uri or source_uri
        if not target_uri:
            tmp_target = str(source_path) + ".tmp"
            final_target = f"sqlite://{tmp_target}"
            logger.info(f"重分区到临时库: {tmp_target}")
        else:
            logger.info(f"重分区到: {final_target}")

        db2 = Database(source_uri)
        t = db2.table(table)
        t0 = time.monotonic()
        result = t.repartition_to(final_target, spec=new_spec)
        elapsed = time.monotonic() - t0
        db2.close()

        logger.info(
            f"重分区完成: {result.rows_copied} 行, "
            f"{len(result.partitions_created)} 个分区, "
            f"耗时 {elapsed:.1f}s"
        )

        if not target_uri:
            tmp_path = Path(tmp_target)
            logger.info(f"替换原库: {tmp_path} → {source_path}")
            shutil.rmtree(source_path)
            shutil.move(str(tmp_path), str(source_path))

        if rm_backup:
            logger.info(f"删除备份: {backup}")
            shutil.rmtree(backup)
        else:
            logger.info(f"备份保留在: {backup}")

    except Exception:
        logger.exception("重分区失败")
        logger.info(f"备份保留在: {backup}，可手动恢复")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
