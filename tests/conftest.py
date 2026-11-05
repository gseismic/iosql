"""旧 SQL 生成测试显式替换协调层；真实锁和事务由安全迁移集成测试覆盖。"""

import shutil

import pytest


@pytest.fixture
def invoke_separate():
    """分别捕获标准输出与标准错误，兼容不同 Click 版本。"""
    from typer.testing import CliRunner
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:
        runner = CliRunner()

    def invoke(app, *args):
        return runner.invoke(app, [str(arg) for arg in args], terminal_width=80)

    return invoke


@pytest.fixture
def fake_pg_coordination(monkeypatch):
    import iosql.coordination as coordination
    original = coordination._coordinator

    class FakeCoordinator:
        def __init__(self, item):
            self.resource = item

        def acquire(self, exclusive, deadline):
            pass

        def release(self):
            pass

        def state(self):
            return {'generation': None, 'transaction': None}

    monkeypatch.setattr(coordination, '_coordinator', lambda item: (
        FakeCoordinator(item) if item.backend == 'postgresql' else original(item)))


@pytest.fixture
def fake_migration_publisher(monkeypatch, fake_pg_coordination):
    """仅隔离旧批次/SQL 单元测试，不能作为生产发布后备实现。"""
    import iosql.migration as migration
    import iosql.migration_postgresql as pg

    class FakePublisher:
        def __init__(self, uri, coordinator, workspace, factory, timeout):
            self.uri, self.workspace, self.factory = uri, workspace, factory
            self.db = None

        def preflight(self, plans, verify):
            pass

        def prepare(self, plans):
            self.db = self.factory(self.uri)
            for plan in plans:
                self.db.table(plan.target_name, spec=plan.target_spec)

        def table(self, plan):
            return self.db.tables[plan.target_name]

        def finish_stage(self, plans):
            for plan in plans:
                plan.result.target_rows = self.table(plan).count_rows()
                if plan.result.verify:
                    migration._verify(plan, plan.result.target_rows)

        def publish(self, operation_id):
            pass

        def close(self):
            if self.db:
                self.db.close()

        def cleanup(self, published):
            shutil.rmtree(self.workspace)

    class FakeSnapshot:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def count(self, plan):
            return plan.source.count_rows()

        def batches(self, plan, batch_size):
            return plan.source.iter_batches(batch_size=batch_size)

    monkeypatch.setattr(pg, 'PostgreSQLPublisher', FakePublisher)
    monkeypatch.setattr(pg, 'SourceSnapshot', FakeSnapshot)
