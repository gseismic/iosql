"""Parquet 物理集合的安全清单解析和字面路径扫描。"""

import json
import os

from ..exception import UnsafeOperation


def fragment_directory(path):
    path = os.fspath(path)
    return os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.parts')


def parquet_files(path):
    """只返回已发布的可见文件；拒绝缺片、越界引用及符号链接。"""
    path = os.fspath(path)
    if not os.path.isfile(path) or os.path.islink(path):
        raise UnsafeOperation(f'Parquet 数据文件缺失或为符号链接: {path}')
    manifest = path + '.manifest.json'
    if not os.path.lexists(manifest):
        return [path]
    if os.path.islink(manifest):
        raise UnsafeOperation(f'Parquet 清单不能是符号链接: {manifest}')
    try:
        with open(manifest, encoding='utf-8') as handle:
            payload = json.load(handle)
        files = payload['files']
        if payload.get('format') != 'iosql-parquet-parts-v1' or not isinstance(files, list) or not files:
            raise ValueError('数据片清单格式无效')
        directory = fragment_directory(path)
        if os.path.islink(directory):
            raise ValueError('数据片目录不能是符号链接')
        resolved = []
        for name in files:
            if name == '@base':
                candidate = path
            elif (isinstance(name, str) and len(name) == 40 and name.endswith('.parquet')
                  and all(char in '0123456789abcdef' for char in name[:-8])):
                candidate = os.path.join(directory, name)
            else:
                raise ValueError('数据片名称无效')
            if not os.path.isfile(candidate) or os.path.islink(candidate):
                raise ValueError('数据片缺失或不是安全文件')
            resolved.append(candidate)
        if len(resolved) != len(set(resolved)):
            raise ValueError('数据片重复引用')
        return resolved
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UnsafeOperation(f'Parquet 数据清单损坏: {manifest}') from exc


def read_parquet(conn, path):
    """DuckDB 对列表中的路径也会展开 glob；逐字符转义保留字面路径。"""
    escapes = {'[': '[[]', ']': '[]]', '*': '[*]', '?': '[?]'}
    paths = [
        ''.join(escapes.get(char, char) for char in os.path.abspath(file))
        for file in parquet_files(path)
    ]
    return conn.read_parquet(paths, union_by_name=True)
