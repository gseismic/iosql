class IosqlError(Exception):
    pass


class BadArgument(IosqlError):
    pass


class TableSpecConflict(IosqlError):
    pass


class MissingPrimaryKey(IosqlError):
    pass


class MissingTimeColumn(IosqlError):
    pass


class PartitionSpecError(BadArgument):
    """分区/表契约参数错误。

    归入 BadArgument 族，保证调用方用 BadArgument 也能统一捕获，
    同时保留更精确的类型供分区层单独处理。
    """
    pass


class UnsafeOperation(IosqlError):
    pass


class SchemaConflict(IosqlError):
    pass


class QueryFieldError(IosqlError):
    pass


class ChunkVerificationError(IosqlError):
    pass


class EngineNotExist(IosqlError):
    pass


class InvalidUriSchema(IosqlError):
    pass
