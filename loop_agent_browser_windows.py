"""LoopAgent Windows 浏览器基础适配层 (Patchright 隐身版)

职责：
1. 启动并连接 Edge
2. 管理 Patchright / Browser / Page 生命周期
3. 提供通用页面导航与截图能力
4. 提供基础文件上传能力

本模块不包含任何具体网站逻辑。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

try:
    import win32clipboard
    import win32con
    _CLIPBOARD_AVAILABLE = True
except Exception as exc:
    _CLIPBOARD_AVAILABLE = False
    print("[CLIPBOARD] 导入 Windows Clipboard 组件失败:")
    print(f"[CLIPBOARD] {type(exc).__name__}: {exc}")


# ============================================================
# Browser configuration
# ============================================================

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "LoopAgent",
    "Edge",
    "User Data",
)

CURRENT_DEBUG_PORT = "9224"
STARTUP_WAIT = 5
CDP_READY_TIMEOUT = 30.0
CDP_POLL_INTERVAL = 0.25


# ============================================================
# Clipboard uploader
# ============================================================

class ClipboardUploader:
    def __init__(self, page, ready_timeout: float = 60):
        self.page = page
        self.ready_timeout = ready_timeout
        self._last_clipboard: str | None = None

    def _backup_clipboard(self) -> None:
        if not _CLIPBOARD_AVAILABLE: return
        try:
            win32clipboard.OpenClipboard()
            try:
                self._last_clipboard = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:
                self._last_clipboard = None
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            print(f"[CLIPBOARD] 备份失败: {exc}")
            self._last_clipboard = None

    def _restore_clipboard(self) -> None:
        if not _CLIPBOARD_AVAILABLE or self._last_clipboard is None: return
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, self._last_clipboard)
            finally:
                win32clipboard.CloseClipboard()
            print("[CLIPBOARD] 剪贴板已恢复")
        except Exception as exc:
            print(f"[CLIPBOARD] 恢复失败: {exc}")

    def _copy_files_to_clipboard(self, file_paths: list[str]) -> bool:
        if not _CLIPBOARD_AVAILABLE:
            print("[CLIPBOARD] pywin32 不可用")
            return False

        valid_paths: list[str] = []
        for file_path in file_paths:
            abs_path = os.path.abspath(file_path).replace("/", "\\")
            if os.path.isfile(abs_path):
                valid_paths.append(abs_path)
            else:
                print(f"[CLIPBOARD] 文件不存在: {file_path}")

        if not valid_paths:
            print("[CLIPBOARD] 没有有效文件")
            return False

        class DROPFILES(ctypes.Structure):
            _fields_ = [
                ("pFiles", wintypes.DWORD),
                ("pt", wintypes.POINT),
                ("fNC", wintypes.BOOL),
                ("fWide", wintypes.BOOL),
            ]

        try:
            offset = ctypes.sizeof(DROPFILES)
            files_data = ("\0".join(valid_paths) + "\0\0").encode("utf-16-le")
            dropfiles = DROPFILES()
            dropfiles.pFiles = offset
            dropfiles.pt.x = 0
            dropfiles.pt.y = 0
            dropfiles.fNC = False
            dropfiles.fWide = True
            data = bytes(dropfiles) + files_data

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
            finally:
                win32clipboard.CloseClipboard()

            print(f"[CLIPBOARD] {len(valid_paths)} 个文件写入")
            return True
        except Exception as exc:
            print(f"[CLIPBOARD] 写入失败: {exc}")
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
            return False

    def _copy_file_to_clipboard(self, file_path: str) -> bool:
        return self._copy_files_to_clipboard([file_path])

    def upload_file(self, file_path: str, input_selector: str) -> bool:
        self._backup_clipboard()
        try:
            if not self._copy_file_to_clipboard(file_path):
                return False
            input_box = self.page.locator(input_selector).first
            input_box.wait_for(state="visible", timeout=int(self.ready_timeout * 1000))
            input_box.click()
            time.sleep(0.3)
            self.page.keyboard.press("Control+v")
            time.sleep(1)
            print("[CLIPBOARD] 粘贴完成")
            return True
        except Exception as exc:
            print(f"[CLIPBOARD] 上传失败: {exc}")
            return False
        finally:
            self._restore_clipboard()

    def upload_files(self, file_paths: list[str], input_selector: str) -> bool:
        success_count = 0
        for file_path in file_paths:
            if self.upload_file(file_path, input_selector):
                success_count += 1
                time.sleep(0.5)
        print(f"[CLIPBOARD] 批量上传完成 {success_count}/{len(file_paths)}")
        return success_count > 0


# ============================================================
# Edge browser session
# ============================================================

class EdgeBrowser:
    def __init__(
        self,
        edge_path: str | Path = EDGE_PATH,
        user_data: str | Path = USER_DATA,
        debug_port: str = CURRENT_DEBUG_PORT,
        startup_wait: float = STARTUP_WAIT,
    ):
        self.edge_path = str(Path(edge_path).expanduser())
        self.user_data = str(Path(user_data).expanduser())
        self.debug_port = str(debug_port)
        self.startup_wait = startup_wait

        self.edge_process: subprocess.Popen | None = None
        self.browser = None
        self.context = None
        self.page = None
        
        # 将原有的 Playwright 对象替换为 Patchright
        self._patchright = None
        self._connected = False

    def _cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"

    def _cdp_version_url(self) -> str:
        return f"{self._cdp_url()}/json/version"

    def _wait_for_cdp(self, timeout: float = CDP_READY_TIMEOUT) -> None:
        url = self._cdp_version_url()
        print(f"[BROWSER] 等待 CDP: {url}")
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        print("[BROWSER] CDP 已就绪")
                        return
            except Exception as exc:
                last_error = exc
            time.sleep(CDP_POLL_INTERVAL)

        raise RuntimeError(f"Edge CDP 启动超时: {url}; last_error={last_error!r}")

    def start(self, url: str | None = None, wait_until: str = "domcontentloaded", timeout: float = 60_000):
        if self._connected:
            print("[BROWSER] 浏览器会话已经启动")
            if url:
                self.goto(url, wait_until=wait_until, timeout=timeout)
            return self.page

        print("[BROWSER] 启动 Edge...")
        user_data_path = Path(self.user_data)
        user_data_path.mkdir(parents=True, exist_ok=True)

        print(f"[BROWSER] User Data: {user_data_path}")
        print(f"[BROWSER] CDP Port: {self.debug_port}")

        try:
            self.edge_process = subprocess.Popen([
                self.edge_path,
                f"--user-data-dir={self.user_data}",
                f"--remote-debugging-port={self.debug_port}",
                "--no-first-run",
                "--no-default-browser-check",
            ])
        except Exception:
            self.edge_process = None
            raise

        try:
            self._wait_for_cdp()
        except Exception:
            self._terminate_edge_process()
            raise

        # ----------------------------------------------------
        # 核心改动：这里从 patchright 导入而不是 playwright
        # ----------------------------------------------------
        from patchright.sync_api import sync_playwright

        try:
            self._patchright = sync_playwright().start()
            self.browser = self._patchright.chromium.connect_over_cdp(self._cdp_url())
            self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self._connected = True
        except Exception:
            try:
                if self._patchright:
                    self._patchright.stop()
            except Exception:
                pass

            self._patchright = None
            self.browser = None
            self.context = None
            self.page = None
            self._terminate_edge_process()
            raise

        print(f"[BROWSER] Edge 已连接: {self.page.url}")
        if url:
            self.goto(url, wait_until=wait_until, timeout=timeout)
        return self.page

    def _ensure_connected(self) -> None:
        if not self._connected or self.page is None:
            raise RuntimeError("EdgeBrowser 未启动，请先调用 start()")

    def _terminate_edge_process(self) -> None:
        process = self.edge_process
        if process is None: return
        self.edge_process = None
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print("[BROWSER] Edge 未在正常时间内退出，强制结束自己的 Edge 进程")
                    try:
                        process.kill()
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[BROWSER] 结束 Edge 进程失败: {exc}")

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: float = 60_000) -> None:
        self._ensure_connected()
        self.page.goto(url, wait_until=wait_until, timeout=timeout)
        print(f"[BROWSER] 页面: {self.page.url}")

    def screenshot(self, filename: str | Path, full_page: bool = True) -> None:
        self._ensure_connected()
        self.page.screenshot(path=str(filename), full_page=full_page)
        print(f"[BROWSER] 截图保存: {filename}")

    def upload_file(self, file_path: str, input_selector: str, ready_timeout: float = 60) -> bool:
        self._ensure_connected()
        uploader = ClipboardUploader(self.page, ready_timeout=ready_timeout)
        return uploader.upload_file(file_path, input_selector)

    def upload_files(self, file_paths: list[str], input_selector: str, ready_timeout: float = 60) -> bool:
        self._ensure_connected()
        uploader = ClipboardUploader(self.page, ready_timeout=ready_timeout)
        return uploader.upload_files(file_paths, input_selector)

    def close(self) -> None:
        try:
            if self.browser:
                self.browser.close()
        except Exception as exc:
            print(f"[BROWSER] 关闭 browser 失败: {exc}")

        try:
            if self._patchright:
                self._patchright.stop()
        except Exception as exc:
            print(f"[BROWSER] 停止 Patchright 失败: {exc}")

        self._patchright = None
        self.browser = None
        self.context = None
        self.page = None
        self._connected = False
        self._terminate_edge_process()
        print("[BROWSER] 浏览器会话关闭")

__all__ = [
    "EdgeBrowser",
    "ClipboardUploader",
    "EDGE_PATH",
    "USER_DATA",
    "CURRENT_DEBUG_PORT",
    "STARTUP_WAIT",
]
