"""FastAPI 依赖项：写模式守卫（纵深防御第二层）。

安全模型：只读模式的主防线是"写路由根本不注册"（404 而非 403）；
本依赖是第二层保险——防止未来维护者误把写路由挂到只读 app 上。
"""
from __future__ import annotations

from fastapi import Request

from .errors import AppError


def require_write_mode(request: Request) -> None:
    """校验当前 app 以 write 模式启动，否则按 404 处理。

    返回 404 而非 403：与主防线保持一致语义——端点"不存在"，
    不向调用方泄露服务端存在被禁功能这一事实。
    """
    mode = getattr(request.app.state, "mode", "readonly")
    if mode != "write":
        raise AppError(
            detail="端点不存在",
            hint="当前服务以只读模式启动，写端点未注册；如需修改数据请用 --write 模式重启",
            status=404,
        )
