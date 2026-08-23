"""iosql UI 包入口：惰性导出 create_app。

安全模型与职责见 app.py 模块 docstring。本包依赖 fastapi/uvicorn
（可选依赖 ``iosql[ui]``），未安装时 import 本包不炸，
仅在真正访问 create_app 时抛带安装指引的 ImportError。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["create_app"]

_INSTALL_HINT = 'iosql.ui 需要 FastAPI 运行环境，请安装可选依赖: pip install "iosql[ui]"'

if TYPE_CHECKING:
    # 仅为类型检查器与 IDE 提供符号；运行时走下方 __getattr__ 惰性导入
    from .app import create_app


def __getattr__(name: str) -> Any:
    # PEP 562 模块级 __getattr__：把 fastapi 的导入延迟到首次访问 create_app，
    # 保证未装 fastapi 的环境下 `import iosql.ui` 及 iosql 主包不受影响
    if name == "create_app":
        try:
            from .app import create_app as _create_app
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        return _create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
