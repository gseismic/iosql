"""发布产物冒烟：防止源码树通过、安装后 Web 静态资源缺失。"""

import os
import importlib.util
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def test_wheel_contains_web_assets_and_serves_them(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(root),
         "--no-deps", "--no-build-isolation", "--quiet", "-w", str(wheel_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("iosql-*.whl"))
    with ZipFile(wheel) as archive:
        assets = {name for name in archive.namelist() if name.startswith("iosql/web/static/")}
    assert {
        "iosql/web/static/index.html",
        "iosql/web/static/style.css",
        "iosql/web/static/app.js",
        "iosql/web/static/js/components.js",
        "iosql/web/static/vendor/uplot.min.js",
    } <= assets

    install_dir = tmp_path / "install"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--quiet",
         "--target", str(install_dir), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    if importlib.util.find_spec("fastapi") is None:
        return
    script = """
from pathlib import Path
from fastapi.testclient import TestClient
import iosql
from iosql.web import create_app
assert Path(iosql.__file__).is_relative_to(Path(INSTALL_DIR))
with TestClient(create_app(f'sqlite://{Path(INSTALL_DIR).parent / "smoke.iosql"}'), base_url='http://localhost') as client:
    for route in ('/', '/style.css', '/app.js', '/js/components.js', '/vendor/uplot.min.js'):
        assert client.get(route).status_code == 200, route
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(install_dir)
    env["INSTALL_DIR"] = str(install_dir)
    script = "import os\nINSTALL_DIR = os.environ['INSTALL_DIR']\n" + script
    subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        check=True, capture_output=True, text=True,
    )
