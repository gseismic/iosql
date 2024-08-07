from sqlite3 import OperationalError


class NoSuchTable(OperationalError):
    pass


class NoSuchColumn(OperationalError):
    pass


class DuplicateColumn(OperationalError):
    pass


class TableAlreadyExists(OperationalError):
    pass


class UnKnownOperationalError(OperationalError):
    pass
