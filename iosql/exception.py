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


class DatabaseBusy(IosqlError):
    """库正在迁移或恢复，当前操作未进入业务读写。"""


class MigrationError(IosqlError):
    """迁移失败；部分进度不代表正式目标已经发布。"""

    def __init__(self, message, *, operation_id=None, partial_result=None,
                 stage=None, source_table=None, target_table=None, batch=None, recovery_path=None):
        super().__init__(message)
        self.operation_id = operation_id
        self.partial_result = partial_result
        self.stage = stage
        self.source_table = source_table
        self.target_table = target_table
        self.batch = batch
        self.recovery_path = recovery_path


class MigrationVerificationError(MigrationError, BadArgument):
    """迁移校验未通过，保留 BadArgument 捕获兼容性。"""


class MigrationLockTimeout(MigrationError):
    """获取全部库锁超过调用方给定的等待期限。"""


class MigrationRecoveryRequired(MigrationError):
    """存在未完成发布，需要先恢复，不能继续普通读写。"""
