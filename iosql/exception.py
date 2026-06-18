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


class PartitionSpecError(IosqlError):
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
