"""
LoopAgent Linux Browser Adapter (Patchright 隐身版)

Backend:
    Microsoft Edge for Linux
    Patchright
    Chrome DevTools Protocol (CDP)

Design:
    - Dedicated persistent Edge profile
    - Dedicated CDP port
    - Never kills unrelated browser processes
    - Linux upload uses GNOME/Wayland file clipboard
    - Public API kept compatible with the existing Windows adapter
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Sequence

# 核心修改：从 patchright 导入而不是 playwright
from patchright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)


EDGE_CANDIDATES = (
    "microsoft-edge-stable",
    "microsoft-edge",
    "msedge",
)

DEFAULT_EDGE_PATH = None

USER_DATA = os.path.expanduser(
    "~/.config/LoopAgent/Edge/User Data"
)

CURRENT_DEBUG_PORT = "9224"

STARTUP_WAIT = 5

CDP_READY_TIMEOUT = 30.0
CDP_POLL_INTERVAL = 0.25

CLIPBOARD_TIMEOUT = 10.0
CLIPBOARD_POLL_INTERVAL = 0.2

INPUT_SELECTOR = 'textarea[name="search"]'


class ClipboardUploader:
    """
    Linux GNOME/Wayland clipboard file uploader.
    """

    MIME_TYPE = "x-special/gnome-copied-files"

    @staticmethod
    def _check_command(name: str) -> str:
        path = shutil.which(name)

        if not path:
            raise RuntimeError(
                f"Required command not found: {name}\n"
                "\n"
                "This Linux upload backend requires "
                "wl-copy/wl-paste from wl-clipboard.\n"
                "\n"
                "Install with:\n"
                "  sudo apt install wl-clipboard"
            )

        return path

    @classmethod
    def _build_gnome_file_list(
        cls,
        file_paths: Sequence[
            str | os.PathLike
        ],
    ) -> str:
        lines = ["copy"]

        for file_path in file_paths:
            path = (
                Path(file_path)
                .expanduser()
                .resolve()
            )

            if not path.is_file():
                raise FileNotFoundError(
                    f"Upload file not found: {path}"
                )

            lines.append(
                path.as_uri()
            )

        return "\n".join(lines) + "\n"

    @classmethod
    def _set_file_clipboard(
        cls,
        file_paths: Sequence[
            str | os.PathLike
        ],
    ) -> subprocess.Popen:
        wl_copy = cls._check_command(
            "wl-copy"
        )

        content = cls._build_gnome_file_list(
            file_paths
        )

        process = subprocess.Popen(
            [
                wl_copy,
                "--type",
                cls.MIME_TYPE,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        try:
            if process.stdin is None:
                raise RuntimeError(
                    "Failed to open wl-copy stdin."
                )

            process.stdin.write(
                content
            )
            process.stdin.close()

        except Exception:
            try:
                process.terminate()
            except Exception:
                pass

            try:
                process.wait(
                    timeout=2
                )
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

            raise

        deadline = (
            time.monotonic()
            + CLIPBOARD_TIMEOUT
        )

        while time.monotonic() < deadline:
            return_code = process.poll()

            if return_code is not None:
                stderr = ""

                try:
                    stderr = (
                        process.stderr.read()
                        if process.stderr is not None
                        else ""
                    )
                except Exception:
                    pass

                raise RuntimeError(
                    "wl-copy exited unexpectedly "
                    "while setting the file clipboard.\n"
                    f"Exit code: {return_code}\n"
                    f"stderr: {stderr.strip()}"
                )

            try:
                types = cls._read_clipboard_types()

                if cls.MIME_TYPE in types:
                    return process

            except Exception:
                pass

            time.sleep(
                CLIPBOARD_POLL_INTERVAL
            )

        try:
            process.terminate()
        except Exception:
            pass

        try:
            process.wait(
                timeout=2
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

        raise TimeoutError(
            "wl-copy did not successfully "
            "publish the GNOME file clipboard "
            f"within {CLIPBOARD_TIMEOUT:.1f}s."
        )

    @classmethod
    def _read_clipboard_types(cls) -> list[str]:
        wl_paste = cls._check_command(
            "wl-paste"
        )

        process = subprocess.run(
            [
                wl_paste,
                "--list-types",
            ],
            text=True,
            capture_output=True,
            timeout=CLIPBOARD_TIMEOUT,
        )

        if process.returncode != 0:
            return []

        return [
            line.strip()
            for line in process.stdout.splitlines()
            if line.strip()
        ]

    @classmethod
    def _wait_for_clipboard(
        cls,
        timeout: float = CLIPBOARD_TIMEOUT,
    ) -> None:
        deadline = (
            time.monotonic()
            + timeout
        )

        while time.monotonic() < deadline:
            try:
                types = cls._read_clipboard_types()

                if cls.MIME_TYPE in types:
                    return

            except Exception:
                pass

            time.sleep(
                CLIPBOARD_POLL_INTERVAL
            )

        raise TimeoutError(
            "GNOME/Wayland file clipboard "
            "did not become available.\n"
            "\n"
            f"Expected MIME type:\n"
            f"  {cls.MIME_TYPE}\n"
            "\n"
            "Available clipboard types were:\n"
            + "\n".join(
                f"  {item}"
                for item in cls._read_clipboard_types()
            )
        )

    @staticmethod
    def _stop_clipboard_provider(
        process: subprocess.Popen | None,
    ) -> None:
        if process is None:
            return

        if process.poll() is not None:
            return

        try:
            os.killpg(
                process.pid,
                signal.SIGTERM,
            )
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
            except Exception:
                return

        try:
            process.wait(
                timeout=2
            )
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

        try:
            os.killpg(
                process.pid,
                signal.SIGKILL,
            )
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.kill()
            except Exception:
                return

        try:
            process.wait(
                timeout=2
            )
        except Exception:
            pass

    @classmethod
    def _paste(
        cls,
        locator,
    ) -> None:
        locator.click()
        locator.press("Control+V")

    @classmethod
    def upload_file(
        cls,
        locator,
        file_path: str | os.PathLike,
    ) -> None:
        cls.upload_files(
            locator,
            [file_path],
        )

    @classmethod
    def upload_files(
        cls,
        locator,
        file_paths: Sequence[
            str | os.PathLike
        ],
    ) -> None:
        paths = [
            Path(path)
            .expanduser()
            .resolve()
            for path in file_paths
        ]

        missing = [
            str(path)
            for path in paths
            if not path.is_file()
        ]

        if missing:
            raise FileNotFoundError(
                "Upload file(s) not found: "
                + ", ".join(missing)
            )

        clipboard_process = None

        try:
            clipboard_process = (
                cls._set_file_clipboard(
                    paths
                )
            )

            cls._wait_for_clipboard()
            cls._paste(locator)
            time.sleep(0.5)

        finally:
            cls._stop_clipboard_provider(
                clipboard_process
            )


class EdgeBrowser:
    def __init__(
        self,
        edge_path: str | None = None,
        user_data: str | None = None,
        debug_port: str | int | None = None,
        startup_wait: float = STARTUP_WAIT,
    ):
        self.edge_path = (
            edge_path
            or DEFAULT_EDGE_PATH
        )

        self.user_data = (
            os.path.expanduser(user_data)
            if user_data
            else USER_DATA
        )

        self.debug_port = str(
            debug_port
            if debug_port is not None
            else CURRENT_DEBUG_PORT
        )

        self.startup_wait = startup_wait
        self.edge_process = None

        # 将 playwright 对象替换为 patchright
        self.patchright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

        self._connected = False

    def _resolve_edge_executable(self) -> str:
        candidates: list[str] = []

        if self.edge_path:
            candidates.append(
                os.path.expanduser(
                    self.edge_path
                )
            )

        env_path = os.environ.get(
            "LOOPAGENT_BROWSER_EXE"
        )

        if env_path:
            candidates.append(
                os.path.expanduser(
                    env_path
                )
            )

        candidates.extend(
            EDGE_CANDIDATES
        )

        for candidate in candidates:
            if os.path.sep in candidate:
                path = (
                    Path(candidate)
                    .expanduser()
                )

                if (
                    path.is_file()
                    and os.access(
                        path,
                        os.X_OK,
                    )
                ):
                    return str(
                        path.resolve()
                    )

                continue

            resolved = shutil.which(
                candidate
            )

            if resolved:
                return resolved

        raise FileNotFoundError(
            "Microsoft Edge for Linux was not found.\n"
            "\n"
            "Checked:\n"
            f"  explicit path: {self.edge_path!r}\n"
            "  LOOPAGENT_BROWSER_EXE\n"
            + "\n".join(
                f"  {candidate}"
                for candidate in EDGE_CANDIDATES
            )
            + "\n\n"
            "Install Microsoft Edge or set:\n"
            "  export LOOPAGENT_BROWSER_EXE="
            "/path/to/microsoft-edge"
        )

    def _cdp_url(self) -> str:
        return (
            f"http://127.0.0.1:"
            f"{self.debug_port}"
        )

    def _cdp_version_url(self) -> str:
        return (
            f"{self._cdp_url()}/json/version"
        )

    def _check_cdp(self) -> bool:
        try:
            request = urllib.request.Request(
                self._cdp_version_url(),
                headers={
                    "Accept": "application/json"
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=0.5,
            ) as response:
                return response.status == 200

        except Exception:
            return False

    def _wait_for_cdp(self) -> None:
        deadline = (
            time.monotonic()
            + CDP_READY_TIMEOUT
        )

        last_error = None

        while time.monotonic() < deadline:
            if (
                self.edge_process is not None
                and self.edge_process.poll()
                is not None
            ):
                raise RuntimeError(
                    "Edge exited before CDP became ready. "
                    f"Exit code: "
                    f"{self.edge_process.returncode}"
                )

            try:
                request = urllib.request.Request(
                    self._cdp_version_url(),
                    headers={
                        "Accept": "application/json"
                    },
                )

                with urllib.request.urlopen(
                    request,
                    timeout=1.0,
                ) as response:
                    if response.status == 200:
                        print(
                            "[BROWSER] CDP 已就绪"
                        )
                        return

            except Exception as exc:
                last_error = exc

            time.sleep(
                CDP_POLL_INTERVAL
            )

        raise TimeoutError(
            "Edge CDP did not become ready "
            f"within {CDP_READY_TIMEOUT:.1f}s.\n"
            f"URL: {self._cdp_version_url()}\n"
            f"Last error: {last_error}"
        )

    def start(
        self,
        url: str | None = None,
    ) -> Page:
        if self._connected:
            if url:
                self.goto(url)

            if self.page is None:
                raise RuntimeError(
                    "Browser is marked connected, "
                    "but no active page exists."
                )

            return self.page

        executable = (
            self._resolve_edge_executable()
        )

        profile = (
            Path(self.user_data)
            .expanduser()
            .resolve()
        )

        profile.mkdir(
            parents=True,
            exist_ok=True,
        )

        print(
            f"[BROWSER] Executable: "
            f"{executable}"
        )

        print(
            f"[BROWSER] User Data: "
            f"{profile}"
        )

        print(
            f"[BROWSER] CDP Port: "
            f"{self.debug_port}"
        )

        if self._check_cdp():
            raise RuntimeError(
                f"CDP port {self.debug_port} "
                "is already occupied by a "
                "responding browser.\n"
                "\n"
                "LoopAgent will NOT attach to "
                "an existing browser process."
            )

        command = [
            executable,
            f"--user-data-dir={profile}",
            (
                "--remote-debugging-port="
                f"{self.debug_port}"
            ),
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        try:
            self.edge_process = (
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            )

            if self.startup_wait > 0:
                time.sleep(
                    min(
                        self.startup_wait,
                        1.0,
                    )
                )

            self._wait_for_cdp()

            # 使用 patchright 启动
            self.patchright = (
                sync_playwright().start()
            )

            self.browser = (
                self.patchright
                .chromium
                .connect_over_cdp(
                    self._cdp_url(),
                    timeout=int(
                        CDP_READY_TIMEOUT
                        * 1000
                    ),
                )
            )

            contexts = (
                self.browser.contexts
            )

            if contexts:
                self.context = (
                    contexts[0]
                )
            else:
                self.context = (
                    self.browser.new_context()
                )

            pages = (
                self.context.pages
            )

            if pages:
                self.page = pages[0]
            else:
                self.page = (
                    self.context.new_page()
                )

            if self.page is None:
                raise RuntimeError(
                    "Patchright connected to Edge "
                    "but no Page could be obtained."
                )

            self._connected = True

            print(
                "[BROWSER] Edge 已连接: "
                f"{self.page.url}"
            )

            if url:
                self.goto(url)

            return self.page

        except Exception:
            self._cleanup()
            raise

    def goto(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout: int = 30000,
    ):
        if self.page is None:
            raise RuntimeError(
                "Browser is not started. "
                "Call start() first."
            )

        if not url.startswith(
            (
                "http://",
                "https://",
            )
        ):
            raise ValueError(
                "Only HTTP(S) URLs are supported: "
                f"{url}"
            )

        return self.page.goto(
            url,
            wait_until=wait_until,
            timeout=timeout,
        )

    def screenshot(
        self,
        path: str | os.PathLike | None = None,
        full_page: bool = False,
    ):
        if self.page is None:
            raise RuntimeError(
                "Browser is not started. "
                "Call start() first."
            )

        if path is not None:
            target = (
                Path(path)
                .expanduser()
                .resolve()
            )

            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            path = str(target)

        return self.page.screenshot(
            path=path,
            full_page=full_page,
        )

    def upload_file(
        self,
        file_path: str | os.PathLike,
        selector: str = INPUT_SELECTOR,
        input_selector: str | None = None,
    ) -> None:
        if self.page is None:
            raise RuntimeError(
                "Browser is not started. "
                "Call start() first."
            )

        if input_selector is not None:
            selector = input_selector

        locator = (
            self.page
            .locator(selector)
            .first
        )

        ClipboardUploader.upload_file(
            locator,
            file_path,
        )

    def upload_files(
        self,
        file_paths: Sequence[
            str | os.PathLike
        ],
        selector: str = INPUT_SELECTOR,
        input_selector: str | None = None,
    ) -> None:
        if self.page is None:
            raise RuntimeError(
                "Browser is not started. "
                "Call start() first."
            )

        if input_selector is not None:
            selector = input_selector

        locator = (
            self.page
            .locator(selector)
            .first
        )

        ClipboardUploader.upload_files(
            locator,
            file_paths,
        )

    def _terminate_edge_process(self) -> None:
        process = self.edge_process

        if process is None:
            return

        try:
            if process.poll() is None:
                process.terminate()

                try:
                    process.wait(
                        timeout=5
                    )

                except subprocess.TimeoutExpired:
                    process.kill()

                    try:
                        process.wait(
                            timeout=5
                        )

                    except Exception:
                        pass

        finally:
            self.edge_process = None

    def _cleanup(self) -> None:
        self.page = None
        self.context = None
        self.browser = None
        self._connected = False

        if self.patchright is not None:
            try:
                self.patchright.stop()

            except Exception:
                pass

            finally:
                self.patchright = None

        self._terminate_edge_process()

    def close(self) -> None:
        self._cleanup()
        print("[BROWSER] 浏览器会话关闭")

    def __enter__(self):
        self.start()
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        self.close()


__all__ = [
    "EdgeBrowser",
    "ClipboardUploader",
    "CURRENT_DEBUG_PORT",
    "USER_DATA",
    "STARTUP_WAIT",
]