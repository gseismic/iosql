"""Compatibility wrapper for :mod:`iosql.engines.sqlite.query_util`."""

from ..engines.sqlite import query_util as _query_util

globals().update({
    name: getattr(_query_util, name)
    for name in dir(_query_util)
    if not name.startswith('__')
})
