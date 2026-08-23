# -*- coding: utf-8 -*-
"""iosql UI 统一异常体系。

安全模型：所有错误响应统一为 ``{"detail": str, "hint": str|null}``，
detail 面向用户描述发生了什么，hint 给出可执行的修复指引。
本模块只定义异常类型；FastAPI handler 在 app.py 中注册。
"""
from __future__ import annotations

from typing import Optional


class AppError(Exception):
    """UI 服务层业务异常：携带 HTTP 状态码与修复指引。

    - status: HTTP 状态码（404=资源不存在，409=能力门控冲突，422=参数非法）
    - detail: 用户可读的错误描述
    - hint:   可选的修复指引（如何做才能成功）
    """

    def __init__(self, detail: str, hint: Optional[str] = None,
                 status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint
        self.status = status
