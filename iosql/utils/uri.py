"""本地库位置解析、显式类型校验和已有库类型识别。"""

import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import url2pathname

from ..exception import BadArgument, UnsafeOperation
from .schema import parse_db_schema
from .storage_identity import _check_engine, detect_storage_engine


def normalize_uri(uri):
    try:
        value = os.fspath(uri)
    except TypeError as exc:
        raise BadArgument('数据库位置必须是 URI 或本地路径') from exc
    if not isinstance(value, str) or not value.strip():
        raise BadArgument('数据库位置不能为空')
    if '://' in value:
        value = value.strip()
    if value.startswith('iosql+'):
        value = value[len('iosql+'):]
    return value


def _local_location(value):
    """返回 (显式引擎或 None, 本地路径或 None)；远程 URI 路径为 None。"""
    if value.startswith('iosql://'):
        parsed = urlsplit(value)
        if parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith('/'):
            raise BadArgument('iosql:// 仅接受无主机名的绝对本地路径，如 iosql:///data/db.iosql')
        return None, url2pathname(parsed.path)
    if '://' not in value:
        return None, value
    try:
        schema, options = parse_db_schema(value)
    except ValueError as exc:
        raise BadArgument('数据库 URI 无效，请检查连接格式') from exc
    if schema == 'postgresql':
        return schema, None
    if schema not in ('sqlite', 'sqlite3', 'parquet'):
        raise BadArgument('不支持的数据库 URI 协议')
    if options.get('memory'):
        return schema, ':memory:'
    path = os.path.join(options.get('path') or '', options.get('db') or '')
    if not path:
        raise BadArgument(f'数据库 URI 缺少路径: {value}')
    return schema, path


def _engine_name(engine):
    if engine is None:
        return None
    if engine not in ('sqlite', 'sqlite3', 'parquet'):
        raise BadArgument('本地数据库 engine 仅支持 sqlite 或 parquet')
    return 'sqlite' if engine == 'sqlite3' else engine


def _typed_local_uri(engine, path):
    # 历史本地 URI 使用 ?version=... 并裁剪首尾空白；编码可保留
    # 普通路径中的问号、边缘空白和字面量百分号。
    if '?' in path or path != path.strip():
        encoded = quote(path, safe='/\\:')
        return f'{engine}://{encoded}?iosql_path_encoding=percent'
    return f'{engine}://{path}'


def resolve_database_uri(uri, *, engine=None, allow_missing=False, inspect_existing=True,
                         recover=True, lock_deadline=None):
    """解析后端 URI；lock_deadline 为识别所需锁的单调时钟绝对时间。"""
    value = normalize_uri(uri)
    declared, path = _local_location(value)
    if declared is None and path is not None:
        # 普通相对路径在打开时固定，后续切换工作目录不会让对象指向另一库。
        path = os.path.abspath(path)
    requested = _engine_name(engine)
    if path is None:
        if requested is not None:
            raise BadArgument('PostgreSQL 连接不能指定本地数据库 engine')
        return value
    explicit = _engine_name(declared)
    if requested is not None and explicit is not None and requested != explicit:
        raise BadArgument(f'URI 与 engine 类型冲突: {explicit} / {requested}')
    expected = requested or explicit
    if path == ':memory:':
        if expected != 'sqlite':
            raise BadArgument('仅 SQLite 支持内存数据库')
        return value
    actual = None
    if inspect_existing and Path(path).exists():
        from ..coordination import lease, physical_path
        from .storage_identity import storage_engine_lock
        # 自动识别可能遇到待恢复的 Parquet 平坦发布，须先经过恢复屏障。
        probe = explicit or requested or 'parquet'
        probe_uri = _typed_local_uri(probe, path)
        timeout = 0.0 if lock_deadline is None else max(0.0, lock_deadline - time.monotonic())
        with lease(probe_uri, recover=recover, timeout=timeout):
            with storage_engine_lock(probe_uri, exclusive=True, deadline=lock_deadline):
                actual = detect_storage_engine(physical_path(path))
        if expected is not None:
            _check_engine(actual, expected, path)
    if expected is None:
        if not inspect_existing:
            raise BadArgument('不检查目标的预览需要显式指定目标存储类型')
        if not Path(path).exists():
            raise BadArgument(f'数据库不存在: {path}；新库请指定 sqlite 或 parquet')
        if actual is None:
            raise UnsafeOperation(f'无法识别数据库存储类型: {path}；新库请指定 sqlite 或 parquet')
        expected = actual
    if actual is None and not allow_missing and not Path(path).exists():
        raise BadArgument(f'数据库不存在: {path}')
    if declared is not None:
        return value
    return _typed_local_uri(expected, path)
