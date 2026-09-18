from sqlite3 import OperationalError


class NoSuchTable(OperationalError):
    pass


class NoSuchColumn(OperationalError):
    pass


class DuplicateColumn(OperationalError):
    pass


class TableAlreadyExists(OperationalError):
    pass


class PrimaryKeyNotInColumn(ValueError):
    pass


class BadArgument(ValueError):
    """参数非法（如连接池不支持 :memory:）。"""
    pass
