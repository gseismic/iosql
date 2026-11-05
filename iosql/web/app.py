"""iosql Web 后端服务层：FastAPI app 工厂与全部 REST 端点。

职责：把 iosql Python API 包装为前端契约（设计文档第 7 节冻结版）可用的
本地 HTTP 服务；Database 实例为进程单例，lifespan 中创建、shutdown 时 close。

安全模型（设计文档第 5 节双模式防御）：
1. 主防线——mode="readonly" 时写路由**不注册**，写端点在只读进程中不存在
   （请求 404），而非被拒（403）；结构性隔离，审计一眼可信。
2. 纵深——写路由统一挂 ``require_write_mode`` Depends，防未来误挂。
3. 错误统一 ``{"detail": str, "hint": str|null}``；任何异常路径都不裸奔。

探针实验结论（/tmp/opencode/probe_iosql.py，2026-08-23）：
- ``select`` 支持 ``"ts desc"`` 方向与逗号分隔多列 order_by（Python 层排序）；
  keyset 分页因此可透传用户方向：asc 用 cursor op ">"，desc 用 "<"。
- 全部结果对象（WriteResult/QueryPlan/LayoutReport/CompactResult/CleanupResult/
  SegmentValidationResult/RepartitionResult/TableContract）均有 to_dict() 且 JSON 安全。
- 无 time 契约的表调 query()/latest() 会抛 MissingTimeColumn → 端点层先按
  spec().time is None 门控为 409，避免异常穿透。
- ``db.table(未知表名)`` 返回 ghost 对象（spec=None/count=0/select=[]），
  因此存在性校验必须走 db.table_names()，不能依赖 table 方法自身报错。
- ``with_partition(partition=...)`` 同时接受字符串与 parse_partition_spec 结果对象。
- where 契约（PLAN-059）：除平铺 {field,op,value} 外，接受 {"or":[…]} /
  {"and":[…]} 递归嵌套；build_case() 负责转为核心 Case/CaseExpression，
  keyset 复合游标与用户 or 链通过 CaseExpression `*` 运算组合。
"""
from __future__ import annotations

import csv
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from itertools import chain
from io import StringIO
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional, Union
from urllib.parse import quote

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from iosql import Case
from iosql.exception import (
    BadArgument,
    IosqlError,
    MissingTimeColumn,
    PartitionSpecError,
    SchemaConflict,
    TableSpecConflict,
    UnsafeOperation,
)
from iosql.spec import parse_partition_spec

from .deps import require_write_mode
from .errors import AppError
from .serializers import (
    to_jsonable,
    serialize_contract,
    serialize_layout_report,
    serialize_partition,
    serialize_plan,
    serialize_result,
    serialize_segments,
)

# ---------------------------------------------------------------- 常量

logger = logging.getLogger(__name__)

MODES = ("readonly", "write")

# where 条件合法 op 白名单（与 iosql Case 归一化入口一致）
ALLOWED_OPS = ("==", "!=", ">", ">=", "<", "<=", "between", "in")

WRITE_MODES = ("append", "upsert", "replace", "ignore")

# rows 端点单页响应上限：只约束返回行数，防止 limit=10^9 撑爆响应体。
# 内存真正的大头是核心查询的全量物化排序（U1-5），由核心侧
# ORDER BY + LIMIT 下推解决，本上限不是内存保护。
MAX_PAGE_LIMIT = 5000
MAX_QUERY_LIMIT = 50_000

# 过滤统计（/count 带 where）的查询执行时间预算；无筛选统计走 manifest 求和，
# 不读取数据行，因此不受此限制。
MAX_COUNT_QUERY_MS = 5_000

# 大表告警阈值：未过滤首页仅记录服务端日志，不改变响应契约
BIG_TABLE_WARN_ROWS = 1_000_000

# chart 数据以 NDJSON 按批传输；每批最多 2048 个原始记录。
CHART_STREAM_BATCH_ROWS = 2048

# 最多同时绘制 32 个 symbol；超限时显式报错，不静默截断。
MAX_CHART_SYMBOLS = 32

EXPORT_BATCH_SIZE = 5000

# 行数缓存 TTL（秒）：Overview 首屏懒加载 count_rows，60s 内复用
ROW_COUNT_TTL_SECONDS = 60.0

# 模块级行数缓存：key=(db_uri, table_name) → (到期时间戳, 行数)
_ROW_COUNT_CACHE: dict[tuple[str, str], tuple[float, int]] = {}

_PARTITION_HINT = (
    "合法示例: duration(ts,every=1hour) / "
    "calendar(date,every=1day,timezone=Asia/Shanghai) / range(id,width=10000)"
)


# ---------------------------------------------------------------- 请求体模型


class _StrictBody(BaseModel):
    """写请求体基类：未知字段一律 422，避免 dry_run 之类的字段被静默吞掉。"""

    model_config = ConfigDict(extra="forbid")


class RowsBody(BaseModel):
    """POST /rows 与 /explain 共用请求体。"""

    where: Optional[list[dict[str, Any]]] = None
    order_by: Optional[str] = None
    limit: int = 200
    cursor: Optional[Any] = None


class CountBody(BaseModel):
    where: Optional[list[dict[str, Any]]] = None


class QueryBody(BaseModel):
    symbols: Optional[Any] = None
    start: Optional[int] = None
    end: Optional[int] = None
    date: Optional[str] = None
    session: Optional[str] = None
    columns: Optional[Any] = None
    where: Optional[list[dict[str, Any]]] = None
    order_by: Optional[str] = None
    limit: int = 1000


class LatestBody(BaseModel):
    symbols: Optional[Any] = None
    as_of: Optional[Any] = None
    by: Optional[list[str]] = None
    columns: Optional[list[str]] = None
    where: Optional[list[dict[str, Any]]] = None
    session: Optional[str] = None


class ValidateBody(BaseModel):
    deep: bool = False


class WriteBody(_StrictBody):
    rows: list[dict[str, Any]]
    mode: Optional[str] = None


class DeleteBody(_StrictBody):
    where: Optional[list[dict[str, Any]]] = None


class TruncateBody(_StrictBody):
    confirm: str = ""


class CreateIndexBody(_StrictBody):
    column: str
    order: str = "asc"


class CompactBody(_StrictBody):
    # 分区键为字符串（calendar/duration token）或数值（range）；dict 等非法
    # 形态由 pydantic 直接 422，绝不允许静默成功
    partition: Optional[Union[str, int, float]] = None
    target_rows: Optional[int] = None


class CleanupBody(_StrictBody):
    dry_run: bool = True
    partition: Optional[Union[str, int, float]] = None


class RepartitionBody(_StrictBody):
    target_uri: str
    partition_str: str


# ---------------------------------------------------------------- 通用 helper


def _bad_request(detail: str, hint: str) -> AppError:
    return AppError(detail=detail, hint=hint, status=422)


def normalize_where(raw: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
    """where 归一化校验：list of {field,op,value} 或 {"or"/"and":[…]} 嵌套。

    叶子条件非法、嵌套结构非法均 422 带 hint；归一化输出保留嵌套结构，
    由 _build_case 负责转成核心 Case/CaseExpression。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _bad_request(
            "where 必须是条件数组",
            '示例: [{"field":"symbol","op":"==","value":"AAPL"}]',
        )
    comps: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and ("or" in item or "and" in item) \
                and "field" not in item:
            comps.append(_normalize_group(item))
        else:
            comps.append(normalize_leaf(item))
    return comps or None


def _normalize_group(item: dict[str, Any]) -> dict[str, Any]:
    """递归校验 {"or":[…]} / {"and":[…]} 分组节点。"""
    if "or" in item and "and" in item:
        raise _bad_request(
            "or 与 and 不能同时出现在同一分组",
            hint='示例: {"or":[{"field":"symbol","op":"==","value":"AAPL"}, …]}',
        )
    key = "or" if "or" in item else "and"
    children = item[key]
    if not isinstance(children, list) or not children:
        raise _bad_request(
            f"`{key}` 分组的值必须是非空条件数组",
            hint='示例: {"or":[{"field":"symbol","op":"==","value":"AAPL"},'
                 '{"field":"symbol","op":"==","value":"MSFT"}]}',
        )
    kids = [
        _normalize_group(child) if isinstance(child, dict)
        and ("or" in child or "and" in child) and "field" not in child
        else normalize_leaf(child)
        for child in children
    ]
    return {key: kids}


# 标量值白名单：where 的 between/in 元素只允许标量，防止 dict/None
# 直达 SQL 参数绑定引发底层 500
_SCALAR_TYPES = (int, float, str, bool)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALAR_TYPES)


def normalize_leaf(item: Any) -> dict[str, Any]:
    """单个叶子条件的校验与归一化（原 normalize_where 主体逻辑）。"""
    if not isinstance(item, dict) or "field" not in item or "op" not in item:
        raise _bad_request(
            "where 条件必须包含 field 与 op",
            '示例: {"field":"ts","op":">=","value":1735689600000}；'
            'OR 条件请用 {"or":[条件,…]} 分组',
        )
    field = str(item["field"]).strip()
    op = item["op"]
    value = item.get("value")
    if not field:
        raise _bad_request("where 条件的 field 不能为空", hint="请填写列名")
    if op not in ALLOWED_OPS:
        raise _bad_request(
            f"非法 where 运算符 `{op}`",
            hint=f"合法 op: {', '.join(ALLOWED_OPS)}；"
                 '示例: {"field":"symbol","op":"==","value":"AAPL"}',
        )
    if op == "between" and (
        not isinstance(value, (list, tuple)) or len(value) != 2
    ):
        raise _bad_request(
            "between 条件的 value 必须是二元列表 [min, max]",
            hint='示例: {"field":"ts","op":"between","value":[start,end]}',
        )
    if op in ("between", "in"):
        if op == "in" and (not isinstance(value, list) or not value):
            raise _bad_request(
                "in 条件的 value 必须是非空列表",
                hint='示例: {"field":"symbol","op":"in","value":["AAPL","MSFT"]}',
            )
        if not all(_is_scalar(v) for v in value):
            raise _bad_request(
                f"{op} 条件的元素必须是标量（int/float/str）",
                hint='示例: {"field":"symbol","op":"in","value":["AAPL","MSFT"]}',
            )
    return {"field": field, "op": op, "value": value}


def build_case(comps: Optional[list[dict[str, Any]]]) -> Any:
    """把 normalize_where 输出转为核心 Case / CaseExpression（AND 平铺）。

    嵌套 {"or":[…]} / {"and":[…]} 递归转换；None/空列表返回 None。
    """
    if not comps:
        return None
    expr: Any = None
    for comp in comps:
        node = _build_case_node(comp)
        expr = node if expr is None else expr * node
    return expr


def _build_case_node(comp: dict[str, Any]) -> Any:
    if "or" in comp:
        expr: Any = None
        for child in comp["or"]:
            node = _build_case_node(child)
            expr = node if expr is None else expr + node
        return expr
    if "and" in comp:
        expr = None
        for child in comp["and"]:
            node = _build_case_node(child)
            expr = node if expr is None else expr * node
        return expr
    return Case(comp["field"], comp["op"], comp["value"])


def get_table(db: Any, name: str) -> Any:
    """统一的表引用获取：先校验存在性，再裸 db.table(name)。

    绝不传契约参数——避免对已有表重复声明契约触发 TableSpecConflict。
    探针确认：db.table(未知表名) 会返回 ghost 对象而不报错，所以
    存在性必须用 db.table_names() 校验。
    """
    names = db.table_names()
    if name not in names:
        summary = ", ".join(names[:20]) + ("…" if len(names) > 20 else "")
        raise AppError(
            detail="表不存在",
            hint=f"当前库中的表: {summary or '(空库)'}",
            status=404,
        )
    return db.table(name)


def table_columns(table: Any, spec: Any) -> list[str]:
    """表列名清单：优先契约 columns，无契约时抽样一行推断。"""
    if spec is not None and spec.columns:
        return list(spec.columns.keys())
    sample = table.select(limit=1, to_dict=True)
    return list(sample[0].keys()) if sample else []


def resolve_sort_col(
    table: Any,
    spec: Any,
    order_by_raw: Optional[str],
) -> tuple[str, str, str]:
    """解析排序列与方向，返回 (col, direction, applied_str)。

    默认列优先级（探针+设计文档）：spec.time.name > order_key > pk[0] > 第一列。
    keyset 分页只支持单列；多列 order_by 直接 422 说明原因。
    """
    if order_by_raw:
        text = str(order_by_raw).strip()
        segments = [seg.strip() for seg in text.split(",") if seg.strip()]
        if len(segments) != 1 or len(text.split(',')) != 1:
            raise _bad_request(
                "keyset 分页仅支持单列排序",
                hint=f"order_by 请传单列（可带 asc/desc），例如 `ts` 或 `ts desc`；收到: {text}",
            )
        tokens = segments[0].split()
        if len(tokens) not in (1, 2):
            raise _bad_request(
                "非法 order_by 格式",
                hint="请传单列及可选方向，例如 `ts` 或 `ts desc`",
            )
        col = tokens[0]
        direction = tokens[1].lower() if len(tokens) > 1 else "asc"
        if direction not in ("asc", "desc"):
            raise _bad_request(
                f"非法排序方向 `{direction}`",
                hint="方向仅支持 asc / desc",
            )
        return col, direction, (col if direction == "asc" else f"{col} desc")

    columns = table_columns(table, spec)
    if spec is not None and spec.time is not None:
        col = spec.time.name
    elif spec is not None and spec.order_key:
        col = spec.order_key
    elif spec is not None and spec.primary_key:
        col = spec.primary_key[0]
    elif columns:
        col = columns[0]
    else:
        raise AppError(
            detail="无法确定默认排序列",
            hint="该表没有契约也没有数据，请显式传 order_by",
            status=409,
        )
    return col, "asc", col


def fast_row_count(table: Any) -> int:
    """segment 复用持久计数；legacy 只执行 COUNT，不触发列范围统计。"""
    from ..coordination import operation
    with operation(table):
        total = 0
        for ref in table._physical_part_refs(lightweight=True):
            table._ensure_ref_file_present(ref)
            total += ref.row_count if ref.segment_id >= 0 else table._data_file_row_count(ref.part_key)
        return total


def physical_page(table, order_cols, direction, user_expr, cursor, limit):
    """局部主键布局把物理文件加入排序身份，合法重复行仍能逐一翻页。"""
    import hashlib
    from ..coordination import operation
    from ..engines.sqlite.query_util import build_select_or_cases_query

    reverse = direction == 'desc'
    order = [(column, direction) for column in order_cols]
    with operation(table):
        refs = table._physical_part_refs(lightweight=True)
        tokens = [(str(ref.part_key), ref.generation) for ref in refs]
        version = (getattr(table, '_iosql_generation', None), getattr(table, '_iosql_namespace_revision', None))
        layout = hashlib.sha256(json.dumps((tokens, version)).encode()).hexdigest()
        values = None
        part = None
        if cursor is not None:
            if (not isinstance(cursor, dict) or cursor.get('version') != 1
                    or cursor.get('layout') != layout
                    or cursor.get('order') != order_cols or cursor.get('direction') != direction):
                raise AppError(detail='分页游标已失效或不匹配当前排序',
                               hint='文件布局已变化或游标无效，请重新从首页浏览', status=409)
            values, part = cursor.get('values'), cursor.get('part')
            if (not isinstance(values, list) or len(values) != len(order_cols)
                    or any(value is None for value in values) or not isinstance(part, str)):
                raise _bad_request('非法分页游标', hint='请原样回传上一页 next_cursor')
        columns = table.spec().columns
        candidates = []
        sort_key = table._row_sort_key(table._parse_order_by(order))
        order_sql = table._order_by_sql(order, '*', columns)
        for ref in refs:
            token = str(ref.part_key)
            expr = None
            prefix = None
            if values is not None:
                for column, value in zip(order_cols, values):
                    branch = Case(column, '<' if reverse else '>', value)
                    if prefix is not None:
                        branch = prefix * branch
                    expr = branch if expr is None else expr + branch
                    equal = Case(column, '=', value)
                    prefix = equal if prefix is None else prefix * equal
                if (token < part if reverse else token > part):
                    expr = expr + prefix
            if user_expr is not None:
                expr = user_expr if expr is None else user_expr * expr
            where = table._build_where(expr)
            query = build_select_or_cases_query(
                'data', '*', where.or_and_cases, columns,
                order_by_sql=order_sql, limit=limit + 1,
            )
            table._ensure_ref_file_present(ref)
            with table._part_db(ref.path, auto_commit=False) as db:
                candidates.extend((dict(row), token) for row in db['data'].query(query.sql, params=query.params))
        # 先按文件方向稳定排序，再按 SQL 键排序，形成完整的物理身份顺序。
        candidates.sort(key=lambda item: item[1], reverse=reverse)
        candidates.sort(key=lambda item: sort_key(item[0]))
        selected = candidates[:limit]
        next_cursor = None
        if len(candidates) > limit:
            row, token = selected[-1]
            next_cursor = {'version': 1, 'layout': layout, 'order': order_cols,
                           'direction': direction, 'values': [row[column] for column in order_cols],
                           'part': token}
        rows = [row for row, _ in selected]
        return {'columns': list(rows[0]) if rows else list(columns), 'rows': rows,
                'next_cursor': next_cursor,
                'order_by': ', '.join(f'{column} {direction}' for column in order_cols)}


def cached_row_count(db_uri: str, table: Any, name: str) -> int:
    """带 TTL 缓存的行数：防止 Overview 首屏被大表拖慢。"""
    cache_key = (db_uri, name)
    now = time.monotonic()
    hit = _ROW_COUNT_CACHE.get(cache_key)
    if hit is not None and hit[0] > now:
        return hit[1]
    count = fast_row_count(table)
    _ROW_COUNT_CACHE[cache_key] = (now + ROW_COUNT_TTL_SECONDS, count)
    return count


def invalidate_row_count(db_uri: str, name: str) -> None:
    """写操作成功后失效对应表的行数缓存。"""
    _ROW_COUNT_CACHE.pop((db_uri, name), None)


def require_time_contract(spec: Any) -> None:
    """timeseries 快捷查询能力门控：无 time 契约 → 409。"""
    if spec is None or spec.time is None:
        raise AppError(
            detail="该表无 time 契约",
            hint="timeseries 快捷查询仅适用于时序表；普通表请使用 where 条件查询(/rows)",
            status=409,
        )


def _parse_int_param(value: Optional[str], label: str) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise _bad_request(
            f"{label} 必须是整数",
            hint=f"收到: {value!r}；时间戳请传毫秒整数",
        ) from exc


# ---------------------------------------------------------------- 错误 handler


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"detail": exc.detail, "hint": exc.hint},
        )

    @app.exception_handler(IosqlError)
    async def handle_iosql_error(request: Request, exc: IosqlError) -> JSONResponse:
        # iosql 异常映射：参数类→422，冲突/危险操作→409，其余→400；
        # hint 给类型化修复指引，避免用户面对裸异常字符串
        if isinstance(exc, BadArgument):
            status = 422
        elif isinstance(exc, (UnsafeOperation, TableSpecConflict, SchemaConflict)):
            status = 409
        else:
            status = 400
        hints: dict[type, str] = {
            MissingTimeColumn: "timeseries 快捷查询仅适用于声明了 time 契约的表",
            TableSpecConflict: "不要对已存在的表重复声明不同契约；打开表请用不带契约参数的 db.table(name)",
            UnsafeOperation: "该操作有数据安全风险，请按界面预览-确认流程执行",
        }
        hint = next((h for t, h in hints.items() if isinstance(exc, t)), None)
        return JSONResponse(
            status_code=status,
            content={"detail": str(exc), "hint": hint},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[:3]
        summary = "; ".join(
            f"{'.'.join(str(loc) for loc in err.get('loc', []))}: {err.get('msg', '')}"
            for err in first
        )
        return JSONResponse(
            status_code=422,
            content={"detail": "请求体校验失败", "hint": summary},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 兜底防线：未预期异常保持 {detail,hint} 形状，细节只进服务端日志，
        # 不把裸异常文本（可能含路径/数据）回传给前端
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "内部错误", "hint": "请查看服务端日志定位问题"},
        )


# ---------------------------------------------------------------- lifespan


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from iosql import Database

    # 进程单例：uvicorn 单 worker 下整个进程只有这一个 Database 连接池
    db = Database(app.state.db_uri)
    app.state.db = db
    try:
        yield
    finally:
        db.close()
        app.state.db = None


# ---------------------------------------------------------------- 只读端点


def _register_read_routes(app: FastAPI) -> None:
    @app.get("/api/db")
    def get_db_info(request: Request) -> dict:
        db = request.app.state.db
        return {
            "uri": db.uri,
            "mode": request.app.state.mode,
            "table_count": len(db.table_names()),
        }

    @app.get("/api/tables")
    def list_tables(request: Request, with_stats: bool = False) -> dict:
        """表清单。默认轻量（manifest 级，毫秒级返回，不碰物理文件）——
        启动契约要求首屏不被全库统计阻塞（PLAN-060）。
        with_stats=1 保留旧的阻塞式全量行为（脚本/测试兼容）。"""
        db = request.app.state.db
        db_uri = db.uri
        names = db.table_names()
        tables = []
        for name in names:
            table = get_table(db, name)
            spec = table.spec()
            partition = spec.partition if spec is not None else None
            partition_data = serialize_partition(partition)
            entry = {
                "name": name,
                "partition_str": (
                    partition_data["str"] if partition_data else None
                ),
                "has_time": spec is not None and spec.time is not None,
                "columns": [
                    {"name": col, "type": str(typ)}
                    for col, typ in (spec.columns or {}).items()
                ] if spec is not None else [],
                "order_key": spec.order_key if spec is not None else None,
                "pk": list(spec.primary_key) if spec is not None else [],
            }
            if with_stats:
                entry["row_count"] = cached_row_count(db_uri, table, name)
                entry["part_count"] = len(table.table_parts())
                entry["segment_count"] = len(table._physical_part_refs(lightweight=True))
            tables.append(entry)
        return {"tables": tables}

    @app.get("/api/table/{name}/stats")
    def get_stats(name: str, request: Request) -> dict:
        """单表统计（行数带 TTL 缓存；parts/segments 为目录列举）。
        前端按需请求；旧式分区的行数统计可能访问数据文件。"""
        db = request.app.state.db
        table = get_table(db, name)
        return {
            "name": name,
            "row_count": cached_row_count(db.uri, table, name),
            "part_count": len(table.table_parts()),
            "segment_count": len(table._physical_part_refs(lightweight=True)),
        }

    @app.get("/api/table/{name}/spec")
    def get_spec(name: str, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        return {"name": name, "spec": serialize_contract(table.spec())}

    @app.get("/api/table/{name}/parts")
    def get_parts(name: str, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        return {"parts": table.table_parts()}

    @app.get("/api/table/{name}/segments")
    def get_segments(name: str, request: Request,
                     include_obsolete: bool = False) -> dict:
        table = get_table(request.app.state.db, name)
        return {
            "segments": serialize_segments(
                table.segments(include_obsolete=include_obsolete)
            )
        }

    @app.post("/api/table/{name}/rows")
    def post_rows(name: str, body: RowsBody, request: Request) -> dict:
        db = request.app.state.db
        table = get_table(db, name)
        spec = table.spec()
        comps = normalize_where(body.where)
        col, direction, _applied = resolve_sort_col(table, spec, body.order_by)

        # 复合 keyset：排序列后追加全部主键列做次级排序，消除同值并列丢行。
        # 游标为 [各排序列值...]，条件展开为 OR 链：
        #   (c1 > v1) OR (c1 == v1 AND c2 > v2) OR ...（desc 时比较方向反转）
        pk_cols = list(spec.primary_key or []) if spec is not None else []
        if not pk_cols:
            raise _bad_request(
                "无主键表不能安全使用 keyset 分页",
                hint="请为表声明唯一主键后使用数据浏览；导出可使用 /export",
            )
        order_cols = [col] + [p for p in pk_cols if p != col]
        word = "desc" if direction == "desc" else "asc"
        applied = ", ".join(f"{c} {word}" for c in order_cols)

        limit = max(1, min(int(body.limit), MAX_PAGE_LIMIT))
        # 用户条件（含 or/and 嵌套）统一转 CaseExpression：
        # 平铺 dict 列表虽可直传 select，但嵌套分组核心不识别
        user_expr = build_case(comps)

        # 大表未过滤首页：只告警不改契约（真正的慢点是核心全量物化排序，
        # 由核心侧 ORDER BY+LIMIT 下推解决；这里用 manifest 求和无 I/O）
        if body.cursor is None and user_expr is None:
            try:
                # 首页告警只使用已持久化的行数，不为告警扫描 legacy 文件。
                big_rows = sum(ref.row_count for ref in table._segment_infos())
            except Exception:
                big_rows = 0
            if big_rows > BIG_TABLE_WARN_ROWS:
                logger.warning(
                    "大表未过滤分页 table=%s rows=%s limit=%s："
                    "首页排序依赖核心 ORDER BY+LIMIT 下推",
                    name, big_rows, limit,
                )

        # NULL 排序列探测（U1-1）：keyset 游标无法表达 IS NULL（Case 拒绝
        # None，SQL 比较对 NULL 恒为 NULL），排序列一旦含 NULL，ASC 的
        # NULL 段与 DESC 的 NULL 尾段都不可达/半截。首页即 422 明确拒绝，
        # 保证不出现"看起来翻完了其实漏行"的静默部分分页。
        if body.cursor is None:
            probe = table.select(
                [col], where=user_expr, order_by=col, limit=1, to_dict=True,
            )
            if probe and probe[0].get(col) is None:
                raise _bad_request(
                    f"排序列 `{col}` 含 NULL，keyset 分页无法安全遍历",
                    hint="请改用不含 NULL 的列排序（如主键/时间列），"
                         "或先在 where 中对排序列加比较条件（如 >=）过滤 NULL 行",
                )

        where_expr: Any = user_expr
        if spec.segment or (spec.partition and spec.partition.column not in pk_cols):
            return physical_page(table, order_cols, direction, user_expr, body.cursor, limit)
        if body.cursor is not None:
            if not isinstance(body.cursor, (list, tuple)):
                raise _bad_request(
                    "cursor 必须是数组",
                    hint=f"游标为首页响应 next_cursor 原样回传的列表，长度应为 {len(order_cols)}",
                )
            vals = list(body.cursor)
            if len(vals) != len(order_cols):
                raise _bad_request(
                    f"cursor 长度 {len(vals)} 与排序键 {len(order_cols)} 列不一致",
                    hint=f"order_cols={order_cols}；请原样回传上一页 next_cursor",
                )
            if any(v is None for v in vals):
                raise _bad_request(
                    "cursor 含 NULL，keyset 分页无法继续",
                    hint=f"排序键 {order_cols} 的游标值不能为 NULL；请更换排序列或过滤 NULL 行",
                )
            strict_op = "<" if direction == "desc" else ">"
            eq_op = "=="
            expr: Any = None
            prefix_eq: Any = None
            for i, c in enumerate(order_cols):
                branch: Any = Case(c, strict_op, vals[i])
                if prefix_eq is not None:
                    branch = prefix_eq * branch
                expr = branch if expr is None else expr + branch
                eq_i = Case(c, eq_op, vals[i])
                prefix_eq = eq_i if prefix_eq is None else prefix_eq * eq_i
            if user_expr is not None:
                # CaseExpression 的 `*` 已覆盖 Case×CaseExpression 组合，
                # 用户条件（含 or 嵌套）与 keyset 游标链 AND 相乘
                expr = user_expr * expr
            where_expr = expr
        rows = table.select(
            "*", where=where_expr, order_by=applied,
            limit=limit + 1, to_dict=True,
        )
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = [rows[-1].get(c) for c in order_cols]
        columns = list(rows[0].keys()) if rows else table_columns(table, spec)
        return {
            "columns": columns,
            "rows": rows,
            "next_cursor": next_cursor,
            "order_by": applied,
        }

    @app.post("/api/table/{name}/explain")
    def post_explain(name: str, body: RowsBody, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        comps = normalize_where(body.where)
        limit = max(1, min(int(body.limit), MAX_PAGE_LIMIT))
        plan = table.explain_select("*", where=build_case(comps),
                                    order_by=body.order_by, limit=limit)
        return {"plan": serialize_plan(plan)}

    @app.post("/api/table/{name}/count")
    def post_count(name: str, body: CountBody, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        comps = build_case(normalize_where(body.where))
        if comps is not None:
            try:
                total = sum(
                    len(batch)
                    for batch in table.iter_select_batches(
                        "*", where=comps, to_dict=True,
                        batch_size=EXPORT_BATCH_SIZE,
                        timeout_ms=MAX_COUNT_QUERY_MS,
                    )
                )
            except TimeoutError as exc:
                raise _bad_request(
                    f"筛选统计超过查询时间预算 {MAX_COUNT_QUERY_MS} ms",
                    hint="请缩小筛选范围或添加分区/时间条件后重试；无筛选统计走 manifest 求和",
                ) from exc
            return {"count": total}
        return {"count": fast_row_count(table)}

    @app.post("/api/table/{name}/query")
    def post_query(name: str, body: QueryBody, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        require_time_contract(table.spec())
        comps = build_case(normalize_where(body.where))
        effective_limit = max(1, min(int(body.limit), MAX_QUERY_LIMIT))
        # 多取一行判断是否被截断，避免"恰好取满 limit"的假阳性
        fluent = table.query(
            symbols=body.symbols,
            start=body.start,
            end=body.end,
            date=body.date,
            session=body.session,
            columns=body.columns if body.columns is not None else "*",
            where=comps,
            order_by=body.order_by,
            limit=effective_limit + 1,
        )
        rows = fluent.to_list()
        truncated = len(rows) > effective_limit
        if truncated:
            rows = rows[:effective_limit]
        columns = (
            list(rows[0].keys()) if rows
            else (list(body.columns) if isinstance(body.columns, list) else [])
        )
        return {"rows": rows, "columns": columns, "truncated": truncated}

    @app.post("/api/table/{name}/latest")
    def post_latest(name: str, body: LatestBody, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        spec = table.spec()
        require_time_contract(spec)
        if body.columns:
            known = table_columns(table, spec)
            unknown = [c for c in body.columns if c not in known]
            if unknown:
                raise _bad_request(
                    f"columns 含不存在的列: {', '.join(unknown)}",
                    hint=f"可用列: {', '.join(known)}",
                )
        comps = build_case(normalize_where(body.where))
        fluent = table.latest(
            symbols=body.symbols,
            as_of=body.as_of,
            by=body.by,
            columns=body.columns if body.columns is not None else "*",
            where=comps,
            session=body.session,
        )
        return {"rows": fluent.to_list()}

    @app.get("/api/table/{name}/chart")
    def get_chart(name: str, request: Request,
                  col: str = Query(...),
                  symbol: str = Query(""),
                  start: Optional[str] = Query(None),
                  end: Optional[str] = Query(None),
                  kind: str = Query("line")) -> StreamingResponse:
        # 兼容旧客户端传入 buckets 参数，但 Charts 始终返回范围内全部原始行。
        return _chart_endpoint(name, request, col, symbol, start, end, kind)

    @app.get("/api/table/{name}/export")
    def get_export(name: str, request: Request,
                   format: str = Query(...),
                   where: Optional[str] = Query(None),
                   order_by: Optional[str] = Query(None),
                   limit: Optional[int] = Query(None)) -> StreamingResponse:
        return _export_endpoint(name, request, format, where, order_by, limit)

    @app.get("/api/table/{name}/layout_report")
    def get_layout_report(name: str, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        return {"report": serialize_layout_report(table.layout_report())}

    @app.post("/api/table/{name}/validate")
    def post_validate(name: str, body: ValidateBody, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        validation = table.validate(deep=body.deep)
        return {"validation": serialize_result(validation)}


# ---------------------------------------------------------------- chart 实现


def _detect_ohlc(columns: list[str]) -> Optional[tuple[str, str, str, str]]:
    """检测 OHLC 列名集合：open/high/low/close 或 o/h/l/c 全存在才支持 candle。"""
    lowered = {c.lower(): c for c in columns}
    for group in (("open", "high", "low", "close"), ("o", "h", "l", "c")):
        if all(g in lowered for g in group):
            return tuple(lowered[g] for g in group)
    return None


def _chart_endpoint(name: str, request: Request, col: str, symbol: str,
                    start: Optional[str], end: Optional[str], kind: str
                    ) -> StreamingResponse:
    if kind not in ("line", "candle"):
        raise _bad_request(
            f"非法图型 `{kind}`",
            hint="kind 仅支持 line（折线）或 candle（OHLC 四线近似显示）",
        )
    db = request.app.state.db
    table = get_table(db, name)
    spec = table.spec()
    columns = table_columns(table, spec)
    if col not in columns:
        raise _bad_request(
            f"数值列 `{col}` 不存在",
            hint=f"可用列: {', '.join(columns)}",
        )

    ohlc = _detect_ohlc(columns) if kind == "candle" else None
    if kind == "candle" and ohlc is None:
        raise _bad_request(
            "该表缺少 OHLC 列，无法绘制 K 线",
            hint="candle 需要 open/high/low/close（或 o/h/l/c）四列齐全；请改用 kind=line",
        )

    x_col, _, _ = resolve_sort_col(table, spec, None)
    x_is_text = False
    if spec is not None and spec.columns:
        col_type = str(spec.columns.get(col, "")).lower()
        if any(t in col_type for t in ("text", "char", "blob")):
            raise _bad_request(
                f"数值列 `{col}` 是文本类型，无法绘图",
                hint="chart 仅支持 integer/real 等数值列",
            )
        x_type = str(spec.columns.get(x_col, "")).lower()
        x_is_text = any(t in x_type for t in ("text", "char", "blob"))

    sym_col = (spec.symbol_column if spec is not None else None) or "symbol"
    group_by_symbol = bool(spec and spec.symbol_column and sym_col in columns)
    symbols: list[str] = []
    comps: list[dict[str, Any]] = []
    if symbol:
        symbols = list(dict.fromkeys(s.strip() for s in symbol.split(",") if s.strip()))
        if len(symbols) > MAX_CHART_SYMBOLS:
            raise _bad_request(
                f"symbol 数量超过上限 {MAX_CHART_SYMBOLS}",
                hint=f"请一次绘制不超过 {MAX_CHART_SYMBOLS} 个 symbol",
            )
        if not group_by_symbol:
            raise _bad_request(
                "该表没有 symbol 契约，无法按 symbol 筛选或分线",
                hint="清空 symbol 输入后按单序列绘图",
            )
        if len(symbols) == 1:
            comps.append({"field": sym_col, "op": "==", "value": symbols[0]})
        elif symbols:
            comps.append({"field": sym_col, "op": "in", "value": symbols})

    start_val = _parse_int_param(start, "start")
    end_val = _parse_int_param(end, "end")
    if start_val is not None:
        comps.append({
            "field": x_col, "op": ">=",
            "value": str(start_val) if x_is_text else start_val,
        })
    if end_val is not None:
        comps.append({
            "field": x_col, "op": "<=",
            "value": str(end_val) if x_is_text else end_val,
        })

    if kind == "candle":
        assert ohlc is not None
        if spec is not None and spec.columns:
            for ohlc_col in ohlc:
                col_type = str(spec.columns.get(ohlc_col, "")).lower()
                if any(t in col_type for t in ("text", "char", "blob")):
                    raise _bad_request(
                        f"OHLC 列 `{ohlc_col}` 是文本类型，无法绘图",
                        hint="candle 要求 open/high/low/close 列均为数值类型",
                    )
        return _chart_candle_stream(
            table, x_col, ohlc, sym_col, group_by_symbol, comps,
            time_col=spec.time.name if spec and spec.time else None,
        )

    # text x 只允许 YYYYMMDD 等纯数字日期字符串。探测仅在文本轴启用，
    # 继续在响应开始前给 ISO 日期与混合类型返回标准 HTTP 422。
    if x_is_text:
        probe = table.aggregate_by_bucket(
            x_col, [col], 1, where=comps or None,
        )
        if probe["x_mode"] == "mixed":
            raise _bad_request(
                f"x 轴列 `{x_col}` 含混合类型数据",
                hint="同一列内文本与数值混存无法绘图，请先清洗该列",
            )
        if probe["x_mode"] == "non_numeric":
            raise _bad_request(
                f"x 轴列 `{x_col}` 是非数字文本，无法绘图",
                hint="支持整数/浮点与 YYYYMMDD 等纯数字日期字符串；ISO 日期请改用 duration 毫秒时间戳分区表",
            )
        if probe["value_bad_rows"].get(col):
            raise _bad_request(
                f"列 `{col}` 含非数值数据，无法绘图",
                hint="chart 仅支持数值列；请换一列或检查数据质量",
            )

    return _chart_line_stream(
        table, x_col, col, sym_col, group_by_symbol, comps,
        time_col=spec.time.name if spec and spec.time else None,
    )


def _chart_stream_response(events: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _chart_event(payload: dict[str, Any]) -> str:
    return json.dumps(
        to_jsonable(payload), ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ) + "\n"


def _chart_stream_x(value: Any, x_col: str) -> int | float:
    if isinstance(value, bool) or value is None:
        raise _bad_request(
            f"x 轴列 `{x_col}` 含空值或非数值数据",
            hint="图表要求每条记录都有数值型 x；请先检查时间列",
        )
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.lstrip("-").isdigit() or stripped in ("", "-"):
            raise _bad_request(
                f"x 轴列 `{x_col}` 是非数字文本，无法绘图",
                hint="支持整数/浮点与 YYYYMMDD 等纯数字日期字符串",
            )
        return int(stripped)
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _bad_request(
            f"x 轴列 `{x_col}` 含非数值数据",
            hint="图表要求整数、浮点或纯数字日期字符串",
        ) from exc
    if not math.isfinite(numeric):
        raise _bad_request(
            f"x 轴列 `{x_col}` 含非有限数值",
            hint="请检查时间列中的 NaN/Infinity 数据",
        )
    if isinstance(value, int):
        return value
    return numeric


def _chart_stream_y(value: Any, col: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _bad_request(
            f"列 `{col}` 含非数值数据，无法绘图",
            hint="chart 仅支持数值列；请检查数据质量",
        )
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _bad_request(
            f"列 `{col}` 含非数值数据，无法绘图",
            hint="chart 仅支持数值列；请检查数据质量",
        ) from exc
    return numeric if math.isfinite(numeric) else None


def _numeric_text_chart_batches(table, fields, x_col, order, comps):
    """数字文本轴在文件内转换再归并，避免词典序造成时间倒退或丢点。"""
    import heapq
    from contextlib import closing
    from dataclasses import replace
    from ..coordination import operation
    from ..utils.mysqlite.connection_pool import reserve_process_connections, release_process_connections

    with operation(table):
        column_types = table.spec().columns
        projections = ','.join(
            f'CAST({table._quote_identifier(field)} AS INTEGER) AS {table._quote_identifier(field)}'
            if field == x_col else table._quote_identifier(field) for field in fields)
        order_sql = ','.join(table._quote_identifier(field) + ' ASC' for field, _ in order)
        specs = []
        for ref, query in table._select_queries_for_where(fields, table._build_where(comps or None), column_types):
            table._ensure_ref_file_present(ref)
            sql = f'SELECT {projections} FROM ({query.sql.rstrip(";")}) ORDER BY {order_sql}'
            specs.append((ref.path, replace(query, sql=sql)))
        if not specs:
            return
        if len(specs) > table._max_ordered_read_connections:
            raise BadArgument('数字文本图表超出有序读取连接上限，请缩小查询范围')
        streams = [table._part_ordered_rows(path, query, 1000) for path, query in specs]
        reservation = reserve_process_connections(len(streams), table._max_process_connections)
        try:
            with closing(heapq.merge(*streams, key=table._row_sort_key(table._parse_order_by(order)))) as merged:
                batch = []
                for row in merged:
                    batch.append(dict(row))
                    if len(batch) >= EXPORT_BATCH_SIZE:
                        yield batch
                        batch = []
                if batch:
                    yield batch
        finally:
            for stream in streams:
                stream.close()
            release_process_connections(reservation)


def _chart_raw_stream(table: Any, x_col: str, value_cols: tuple[str, ...],
                      symbol_col: str, group_by_symbol: bool,
                      comps: list[dict[str, Any]],
                      time_col: Optional[str], kind: str
                      ) -> Iterator[str]:
    fields = list(dict.fromkeys(
        ([symbol_col] if group_by_symbol else []) + [x_col] + list(value_cols)
    ))
    header: dict[str, Any] = {
        "type": "meta", "kind": kind, "x_col": x_col,
        "time_col": time_col,
        "value_cols": list(value_cols),
        "group_by_symbol": group_by_symbol,
    }
    yield _chart_event(header)

    pending: dict[Optional[str], list[list[Any]]] = {}
    counts: dict[Optional[str], int] = {}
    last_x: dict[Optional[str], int | float] = {}
    total_rows = 0
    # 明确按 symbol 和 x 排序，避免不同物理文件/写入批次顺序影响折线走势。
    order_by = [(symbol_col, "asc"), (x_col, "asc")] if group_by_symbol else [(x_col, "asc")]

    try:
        x_type = str(table.spec().columns.get(x_col, '')).lower()
        batches = (_numeric_text_chart_batches(table, fields, x_col, order_by, comps)
                   if any(kind in x_type for kind in ('text', 'char')) else table.iter_select_batches(
                       fields, where=comps or None, order_by=order_by,
                       to_dict=True, batch_size=EXPORT_BATCH_SIZE))
        for batch in batches:
            for row in batch:
                raw_symbol = row.get(symbol_col) if group_by_symbol else None
                label = None if raw_symbol is None else str(raw_symbol)
                if label not in counts and len(counts) >= MAX_CHART_SYMBOLS:
                    raise _bad_request(
                        f"查询结果超过 {MAX_CHART_SYMBOLS} 个 symbol",
                        hint=f"请指定不超过 {MAX_CHART_SYMBOLS} 个 symbol 后绘图",
                    )
                x_value = _chart_stream_x(row.get(x_col), x_col)
                previous = last_x.get(label)
                if previous is not None and x_value < previous:
                    raise _bad_request(
                        f"symbol `{label or ''}` 的 x 值不是递增顺序",
                        hint="请检查时间列或排序契约；为避免误导，本次绘图已中止",
                    )
                last_x[label] = x_value
                points = pending.setdefault(label, [])
                point = [x_value]
                point.extend(_chart_stream_y(row.get(col), col) for col in value_cols)
                points.append(point)
                counts[label] = counts.get(label, 0) + 1
                total_rows += 1
                if len(points) >= CHART_STREAM_BATCH_ROWS:
                    yield _chart_event({
                        "type": "data", "symbol": label, "points": points,
                    })
                    pending[label] = []

        for label, points in pending.items():
            if points:
                yield _chart_event({
                    "type": "data", "symbol": label, "points": points,
                })
        yield _chart_event({
            "type": "done", "rows": total_rows,
            "series": [
                {"symbol": label, "rows": count}
                for label, count in counts.items()
            ],
        })
    except Exception as exc:
        if isinstance(exc, AppError):
            detail, hint = exc.detail, exc.hint
        elif isinstance(exc, BadArgument):
            detail, hint = str(exc), "请检查筛选条件、时间列和图表数据类型"
        else:
            logger.exception("chart raw stream failed")
            detail, hint = "图表数据读取失败", "请检查服务端日志和表数据后重试"
        yield _chart_event({"type": "error", "detail": detail, "hint": hint})


def _chart_line_stream(table: Any, x_col: str, col: str, symbol_col: str,
                       group_by_symbol: bool, comps: list[dict[str, Any]],
                       time_col: Optional[str]
                       ) -> StreamingResponse:
    return _chart_stream_response(_chart_raw_stream(
        table, x_col, (col,), symbol_col, group_by_symbol, comps,
        time_col, "line",
    ))


def _chart_candle_stream(table: Any, x_col: str,
                         ohlc: tuple[str, str, str, str], symbol_col: str,
                         group_by_symbol: bool, comps: list[dict[str, Any]],
                         time_col: Optional[str]
                         ) -> StreamingResponse:
    return _chart_stream_response(_chart_raw_stream(
        table, x_col, ohlc, symbol_col, group_by_symbol, comps,
        time_col, "candle",
    ))


# ---------------------------------------------------------------- export 实现


def _content_disposition(name: str, fmt: str) -> str:
    """构造 Content-Disposition：ASCII 降级名 + RFC 5987 UTF-8 文件名。

    表名可来自用户数据（可能含中文/引号/换行）；直接拼 header 会抛
    latin-1 编码错或注入畸形 header，这里统一降级与转义。
    """
    filename = f"{name}.{fmt}"
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    ascii_name = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in '\\"' else "_"
        for ch in ascii_name
    )
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


# CSV 公式注入危险前缀（Excel/LibreOffice 会把它们当公式执行）
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    """CSV 公式注入防护：危险前缀的文本前加单引号，强制按文本解析。"""
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def _export_endpoint(name: str, request: Request, format: str,
                     where: Optional[str], order_by: Optional[str],
                     limit: Optional[int]) -> StreamingResponse:
    if format not in ("csv", "jsonl"):
        raise _bad_request(
            f"非法导出格式 `{format}`",
            hint="format 仅支持 csv 或 jsonl",
        )
    table = get_table(request.app.state.db, name)
    spec = table.spec()
    comps: Any = None
    if where:
        try:
            parsed = json.loads(where)
        except json.JSONDecodeError as exc:
            raise _bad_request(
                "where 参数不是合法 JSON",
                hint="请对条件数组做 URL 编码后传入，"
                     '示例: where=[{"field":"symbol","op":"==","value":"AAPL"}',
            ) from exc
        if isinstance(parsed, dict):
            parsed = [parsed]
        comps = build_case(normalize_where(parsed))

    media_types = {"csv": "text/csv", "jsonl": "application/x-ndjson"}
    headers = {
        "Content-Disposition": _content_disposition(name, format),
    }
    batch_iter = table.iter_select_batches(
        "*", where=comps, limit=limit, order_by=order_by,
        to_dict=True, batch_size=EXPORT_BATCH_SIZE,
    )
    # 预取首个批次：字段级错误（未知列名等）在响应头发出的 200 之前暴露，
    # 否则客户端只会拿到截断文件而看不到 {detail, hint}
    first_batch = next(batch_iter, None)
    csv_columns = table_columns(table, spec)
    generator = _export_stream(batch_iter, first_batch, format, csv_columns)
    return StreamingResponse(
        generator,
        media_type=media_types[format],
        headers=headers,
    )


def _export_stream(batch_iter: Iterator[list[dict[str, Any]]],
                   first_batch: Optional[list[dict[str, Any]]],
                   format: str,
                   csv_columns: list[str]) -> Iterator[str]:
    """批处理流式产出文件内容。

    csv 列集在流式开始前由契约/抽样预计算（evolve 扩列后契约 columns 是
    schema 上确界），避免以首行为准静默丢弃后续批次的新增列。
    """
    batches: Iterator[list[dict[str, Any]]] = (
        chain([first_batch], batch_iter) if first_batch is not None else batch_iter
    )
    header_written = False
    for batch in batches:
        buffer = StringIO()
        if format == "csv":
            writer = csv.writer(buffer, lineterminator="\n")
            if not header_written and csv_columns:
                writer.writerow(csv_columns)
                header_written = True
            for row in batch:
                writer.writerow([_csv_safe(row.get(c)) for c in csv_columns])
        else:
            for row in batch:
                buffer.write(json.dumps(
                    to_jsonable(row), ensure_ascii=False, allow_nan=False,
                ))
                buffer.write("\n")
        yield buffer.getvalue()
    # 空结果集也必须输出表头（零批次时此前无表头，导出文件不可直接用）
    if format == "csv" and not header_written and csv_columns:
        buffer = StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(csv_columns)
        yield buffer.getvalue()


# ---------------------------------------------------------------- 写端点（仅 write 模式注册）


def _register_write_routes(app: FastAPI) -> None:
    dependencies = [Depends(require_write_mode)]

    @app.post("/api/table/{name}/write", dependencies=dependencies)
    def post_write(name: str, body: WriteBody, request: Request) -> dict:
        db = request.app.state.db
        table = get_table(db, name)
        if not isinstance(body.rows, list) or not body.rows:
            raise _bad_request(
                "rows 必须是非空列表",
                hint='示例: {"rows": [{"symbol":"AAPL","ts":1735689600000,"price":187.4}]}',
            )
        if any(not isinstance(row, dict) for row in body.rows):
            raise _bad_request(
                "rows 中每个元素必须是对象",
                hint='示例: {"rows": [{"symbol":"AAPL","ts":1735689600000,"price":187.4}]}',
            )
        if body.mode is not None and body.mode not in WRITE_MODES:
            raise _bad_request(
                f"非法写入模式 `{body.mode}`",
                hint=f"mode 仅支持: {', '.join(WRITE_MODES)}；不传则使用表契约 conflict",
            )
        result = table.write(body.rows, mode=body.mode if body.mode else None)
        invalidate_row_count(db.uri, name)
        return {"result": serialize_result(result)}

    @app.post("/api/table/{name}/delete", dependencies=dependencies)
    def post_delete(name: str, body: DeleteBody, request: Request) -> dict:
        db = request.app.state.db
        table = get_table(db, name)
        if not body.where:
            raise AppError(
                detail="where 必填",
                hint="条件删除必须提供过滤条件；如需清空全表请使用 truncate 并输入表名确认",
                status=422,
            )
        comps = build_case(normalize_where(body.where))
        deleted = table.delete(comps)
        invalidate_row_count(db.uri, name)
        return {"deleted": deleted}

    @app.post("/api/table/{name}/truncate", dependencies=dependencies)
    def post_truncate(name: str, body: TruncateBody, request: Request) -> dict:
        db = request.app.state.db
        table = get_table(db, name)
        if body.confirm != name:
            raise _bad_request(
                "确认文本不匹配",
                hint=f"清空表不可恢复；请在 confirm 字段中输入表名 `{name}` 以确认",
            )
        result = table.truncate(confirm=True)
        invalidate_row_count(db.uri, name)
        return {"result": serialize_result(result)}

    @app.post("/api/table/{name}/create_index", dependencies=dependencies)
    def post_create_index(name: str, body: CreateIndexBody,
                          request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        if body.order not in ("asc", "desc"):
            raise _bad_request(
                f"非法索引方向 `{body.order}`",
                hint="order 仅支持 asc / desc",
            )
        table.create_index(body.column, order=body.order)
        # iosql create_index 返回 None（探针确认），回显输入作为执行凭据
        return {"result": {"column": body.column, "order": body.order}}

    @app.post("/api/table/{name}/compact", dependencies=dependencies)
    def post_compact(name: str, body: CompactBody, request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        result = table.compact(partition=body.partition, target_rows=body.target_rows)
        return {"result": serialize_result(result)}

    @app.post("/api/table/{name}/cleanup_obsolete", dependencies=dependencies)
    def post_cleanup_obsolete(name: str, body: CleanupBody,
                              request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        # 保留 iosql 自带语义：实删前默认 validate(deep=True)，失败抛 UnsafeOperation
        result = table.cleanup_obsolete(
            dry_run=body.dry_run, partition=body.partition
        )
        return {"result": serialize_result(result)}

    @app.post("/api/table/{name}/repartition", dependencies=dependencies)
    def post_repartition(name: str, body: RepartitionBody,
                         request: Request) -> dict:
        table = get_table(request.app.state.db, name)
        target_uri = str(body.target_uri or "").strip()
        if not target_uri:
            raise _bad_request(
                "target_uri 必填",
                hint="重分区永远写入新库（源库不变），示例: sqlite://db/market-hourly.iosql",
            )
        try:
            new_partition = parse_partition_spec(body.partition_str)
        except PartitionSpecError as exc:
            raise _bad_request(
                f"非法 partition 表达式: {exc}",
                hint=_PARTITION_HINT,
            ) from exc
        spec = table.spec()
        if spec is None:
            raise AppError(
                detail="该表没有持久化契约，无法重分区",
                hint="repartition_to 需要基于现有契约生成新布局",
                status=409,
            )
        # 探针确认：with_partition 同时接受字符串与 PartitionSpec 对象，传解析后的对象
        new_spec = spec.with_partition(partition=new_partition)
        result = table.repartition_to(target_uri, spec=new_spec)
        return {"result": serialize_result(result)}


# ---------------------------------------------------------------- 工厂


class _IPv6HostNormalizer:
    """把 ``[::1]:port`` 形式的 Host 归一为 ``localhost:port``。

    Starlette 的 TrustedHostMiddleware 以 ``host.split(":")[0]`` 解析
    Host，IPv6 字面量会得到 ``"["``，导致 ``[::1]`` 无法命中白名单。
    本中间件只做这一处归一，校验仍交给 TrustedHostMiddleware。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            new_headers = []
            changed = False
            for key, value in scope.get("headers") or []:
                if key == b"host" and value.startswith(b"[::1]"):
                    rest = value[len(b"[::1]"):]
                    value = b"localhost" + rest
                    changed = True
                new_headers.append((key, value))
            if changed:
                scope = dict(scope, headers=new_headers)
        await self.app(scope, receive, send)


def _register_api_fallback(app: FastAPI) -> None:
    """API 兜底路由：吞掉所有未匹配的 /api/* 请求，统一返回 404。

    必须在全部 API 路由（含写路由）之后注册。没有它时，readonly 进程里
    未注册的写路径会落到静态挂载上返回 405 Method Not Allowed——这既泄露
    "该路径形态存在"的信息，也违反"写端点不存在（404）"的安全模型。
    """

    @app.api_route("/api/{rest:path}", methods=[
        "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
    ], include_in_schema=False)
    def api_fallback(rest: str) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": "端点不存在", "hint": "请对照设计文档第 7 节的 API 契约"},
        )


def create_app(db_uri: str, mode: str = "readonly", allowed_hosts: Optional[list[str]] = None) -> FastAPI:
    """创建 iosql Web 的 FastAPI 应用。

    - mode="readonly"（默认）：只注册读路由，写端点不存在（404）。
    - mode="write"：额外注册写路由（均挂 require_write_mode 守卫）。
    - Database 在 lifespan 启动时创建（进程单例），shutdown 时 close()；
      TestClient 需用 ``with TestClient(app)`` 才能触发完整生命周期。
    """
    if mode not in MODES:
        raise BadArgument(
            f"非法 mode `{mode}`，仅支持: {', '.join(MODES)}"
        )

    app = FastAPI(title="iosql Web", lifespan=_lifespan)
    app.state.db_uri = db_uri
    app.state.mode = mode
    app.state.db = None

    # Host 头安全：默认只信任本机回环地址；非本机绑定需显式添加访问主机名。
    trusted_hosts = ["127.0.0.1", "localhost", "[::1]", "::1"]
    for host in allowed_hosts or []:
        if not host or any(char in host for char in ('*', '/', '\\', ':')):
            raise BadArgument(f"非法 allowed_host: {host!r}；请输入具体主机名或 IPv4 地址")
        if host not in trusted_hosts:
            trusted_hosts.append(host)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=trusted_hosts,
    )
    app.add_middleware(_IPv6HostNormalizer)

    _register_error_handlers(app)
    _register_read_routes(app)
    if mode == "write":
        # 结构性隔离：readonly 进程里写路由从未注册，而非注册后被拒
        _register_write_routes(app)
    # 兜底 404 必须在所有 API 路由之后、静态挂载之前
    _register_api_fallback(app)

    static_dir = Path(__file__).parent / "static"
    # API 路由之后挂载静态目录，SPA 入口兜底其余路径
    app.mount(
        "/",
        StaticFiles(directory=str(static_dir), html=True, check_dir=False),
        name="static",
    )
    return app
