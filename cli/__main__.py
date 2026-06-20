"""iosql 命令行工具。

命令:
  iosql repartition  — 重分区表数据（含自动备份）

示例:
  iosql repartition --source sqlite://data/day_kline.iosql --table daily_bars --every quarter
  iosql repartition -s data/day_kline.iosql -t daily_bars -e month --rm-backup
"""

import shutil
import time
from datetime import datetime
from pathlib import Path

import typer
from loguru import logger

from iosql import Database, Partition, TableSpec

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
