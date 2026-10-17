
"""LoopAgent 浏览器平台分发层

职责：
1. 根据当前运行平台加载对应的浏览器实现。
2. 向上层提供统一的浏览器组件入口。
3. 不包含具体的浏览器控制逻辑。
4. 不包含网站、LLM 或业务逻辑。

平台实现：
- Windows -> loop_agent_browser_windows.py
- Linux   -> loop_agent_browser_linux.py

上层模块原则上只应该从本模块导入浏览器组件，
而不应该直接依赖具体的平台实现。
"""

from __future__ import annotations

import sys


def _load_platform_components():
    """加载当前平台对应的浏览器组件。"""

    if sys.platform.startswith("win"):
        from loop_agent_browser_windows import (
            EdgeBrowser,
            ClipboardUploader,
        )

        return EdgeBrowser, ClipboardUploader

    if sys.platform.startswith("linux"):
        from loop_agent_browser_linux import (
            EdgeBrowser,
            ClipboardUploader,
        )

        return EdgeBrowser, ClipboardUploader

    raise RuntimeError(
        f"当前平台暂不支持 LoopAgent 浏览器组件: {sys.platform}"
    )


# ---------------------------------------------------------
# 平台组件
# ---------------------------------------------------------

EdgeBrowser, ClipboardUploader = _load_platform_components()


__all__ = [
    "EdgeBrowser",
    "ClipboardUploader",
]

