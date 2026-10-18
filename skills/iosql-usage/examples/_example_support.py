"""示例专用清理：只对脚本自己拥有的演示目录调用。"""

import shutil
import uuid
from pathlib import Path

from iosql.coordination import lease


def reset_database(path):
    path = Path(path).resolve()
    # 演示之间没有打开的表对象；同时重置外置控制记录，允许重复运行。
    with lease(f'sqlite://{path}', exclusive=True) as held:
        previous_state = held.coordinator.state()
        shutil.rmtree(path, ignore_errors=True)
        held.coordinator.write_state(uuid.uuid4().hex, physical_root=None, previous_state=previous_state)
