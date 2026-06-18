"""Compatibility exports for query primitives.

Canonical implementation lives in :mod:`iosql.core.query`.
"""

from ..core.query import Case, CaseExpression, Where, get_case_range

__all__ = ['Case', 'CaseExpression', 'Where', 'get_case_range']
