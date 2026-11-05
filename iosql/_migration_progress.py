"""迁移过程内部事件；未订阅时不改变库接口或输出。"""

from contextlib import contextmanager
from contextvars import ContextVar


_observer = ContextVar('iosql_migration_observer', default=None)
_count_scope = ContextVar('iosql_migration_count_scope', default=None)


@contextmanager
def observe(callback):
    """在当前上下文订阅事件，退出时恢复此前的订阅。"""
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def _notify(callback, event, **details):
    try:
        callback(event, **details)
    except Exception:
        # 观察者不能影响迁移或其发布结果。
        pass


def emit(event, **details):
    callback = _observer.get()
    if callback is not None:
        _notify(callback, event, **details)


@contextmanager
def counting(phase, table):
    token = _count_scope.set((phase, table))
    try:
        yield
    finally:
        _count_scope.reset(token)


def count_reporter():
    """返回当前计数上下文中的文件进度回调，未订阅时返回 None。"""
    callback, scope = _observer.get(), _count_scope.get()
    if callback is None or scope is None:
        return None
    phase, table = scope

    def report(done, total, rows):
        _notify(callback, 'count_progress', phase=phase, table=table,
                done=done, total=total, rows=rows)

    return report
