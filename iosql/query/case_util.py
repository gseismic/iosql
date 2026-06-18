"""Compatibility wrapper for :mod:`iosql.core.query.case_util`."""

from ..core.query.case_util import (
    flatten_expression,
    get_case_range,
    sorted_cases,
    unique_or_and_cases,
)

__all__ = [
    'flatten_expression',
    'get_case_range',
    'sorted_cases',
    'unique_or_and_cases',
]
