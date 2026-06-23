import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (
            "examples/09_existing_database_append.py",
            "已有契约主键",
        ),
        (
            "examples/10_existing_database_repartition.py",
            "源/目标行数: 4 4",
        ),
        (
            "skills/iosql-usage/examples/09_existing_database_append.py",
            "已有契约主键",
        ),
        (
            "skills/iosql-usage/examples/10_existing_database_repartition.py",
            "源/目标行数: 4 4",
        ),
    ],
)
def test_existing_database_example_is_self_contained(script, expected):
    env = dict(os.environ)
    for name in [
        "IOSQL_EXISTING_URI",
        "IOSQL_EXISTING_TABLE",
        "IOSQL_SOURCE_URI",
        "IOSQL_TARGET_URI",
    ]:
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
