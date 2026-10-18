"""本地 Parquet 存储后端；DuckDB 随 iosql 默认安装，打开后端时按需导入。"""

from .table import Table

__all__ = ['Table']
