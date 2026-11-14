"""使用公开 CLI 备份 SQLite 目录库。

运行：在本文件所在目录执行 python 17_safe_migration_and_paths.py。
准备：已安装 iosql 及其默认依赖。
预期：输出备份位置及备份中的一条记录；临时目录自动清理。
"""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from iosql import Database


def run():
    with TemporaryDirectory(prefix="iosql-backup-") as directory:
        source = Path(directory) / "source.iosql"
        backup = Path(directory) / "backup.iosql"
        with Database(f"sqlite://{source}") as db:
            db.table("events", pk=["id"], columns={"id": "integer"}).write({"id": 1})

        subprocess.run(
            [sys.executable, "-m", "iosql.cli", "backup", "--source", str(source), "--target", str(backup)],
            check=True,
        )
        with Database(f"sqlite://{backup}") as db:
            print("备份中的记录:", db.table("events").select())


if __name__ == "__main__":
    run()
