"""兼容 `python -m cli` 的薄包装。

真实 CLI 入口位于 `iosql.cli`，避免项目之间共享顶层 `cli` 包时发生导入冲突。
"""

from iosql.cli import app


if __name__ == "__main__":
    app()
