"""防御式序列化层：把 iosql 结果对象转换为 JSON 安全的 dict。

设计目标（对应 PLAN-055 A2）：任何 iosql 返回对象进入本层后，
序列化过程绝不抛异常、绝不让请求 500。策略分三级：
1. 对象自带 ``to_dict()`` 时优先调用（探针确认：spec.py 中全部结果类型均有）；
2. dataclass 回退到 ``__dataclass_fields__`` 白名单逐字段 getattr；
3. 最终兜底 ``str(obj)``，bytes/Decimal/datetime 等做显式安全转换。
"""
from __future__ import annotations

import datetime
import decimal
import math
from dataclasses import fields as dataclass_fields
from typing import Any, Optional

from iosql.spec import TableContract, format_partition_spec


def to_jsonable(value: Any) -> Any:
    """递归转换为 JSON 安全结构；任何分支都不允许抛异常逃逸。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # Starlette JSONResponse 以 allow_nan=False 渲染，inf/nan 会在响应期
        # 抛 ValueError 造成 500，这里统一归一为 null（SQLite NaN 入库即为 NULL）
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, decimal.Decimal):
        converted = float(value)
        return converted if math.isfinite(converted) else None
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return to_jsonable(value.to_dict())
        except Exception:
            # 防御式序列化的最后防线：to_dict() 签名漂移时降级到
            # dataclass 字段白名单，而不是让请求失败
            pass
    dataclass_field_names = None
    if hasattr(value, "__dataclass_fields__"):
        dataclass_field_names = [f.name for f in dataclass_fields(value)]
    if dataclass_field_names is not None:
        return {
            name: to_jsonable(getattr(value, name, None))
            for name in dataclass_field_names
        }
    return str(value)


def serialize_partition(partition: Any) -> Optional[dict]:
    """PartitionSpec → dict，附规范字符串表达（前端/CLI 可直接复制）。"""
    if partition is None:
        return None
    data = to_jsonable(partition.to_dict())
    try:
        data["str"] = format_partition_spec(partition)
    except Exception:
        data["str"] = str(partition)
    return data


def serialize_contract(spec: Optional[TableContract]) -> Optional[dict]:
    """TableContract → 前端契约卡所需的固定形状 dict。

    探针结论：TableContract 自带 to_dict()，但 time/symbol_column/session_column
    在为 None 时会被省略；这里补齐为显式 null，保证前端字段稳定。
    """
    if spec is None:
        return None
    return {
        "primary_key": list(spec.primary_key or []),
        "time": to_jsonable(spec.time.to_dict()) if spec.time else None,
        "partition": serialize_partition(spec.partition),
        "segment": to_jsonable(spec.segment.to_dict()) if spec.segment else None,
        "columns": dict(spec.columns or {}),
        "schema_policy": spec.schema_policy,
        "order_key": spec.order_key,
        "conflict": spec.conflict,
        "symbol_column": spec.symbol_column,
        "session_column": spec.session_column,
    }


def serialize_plan(plan: Any) -> dict:
    """QueryPlan → dict（explain 端点用）。"""
    return to_jsonable(plan)


def serialize_layout_report(report: Any) -> dict:
    """LayoutReport → dict（含 partitions 明细与 recommendation）。"""
    return to_jsonable(report)


def serialize_result(result: Any) -> Any:
    """各操作结果对象（WriteResult/CompactResult/CleanupResult 等）→ dict。

    iosql 的 create_index 返回 None，这里原样透传 None。
    """
    return to_jsonable(result)


def serialize_segments(segments: list) -> list[dict]:
    """segments() 已返回 dict 列表（探针确认），仅做 JSON 安全兜底。"""
    return [to_jsonable(item) for item in segments]
