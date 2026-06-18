"""Compatibility wrapper for :mod:`iosql.engines.sqlite.const`."""

from ..engines.sqlite import const as _const

globals().update({
    name: getattr(_const, name)
    for name in dir(_const)
    if not name.startswith('__')
})
