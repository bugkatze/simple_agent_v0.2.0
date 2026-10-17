"""LoopAgent DeepSeek 网页适配层。

职责：
1. 使用 loop_agent_browser 提供的 Edge / Playwright 浏览器能力
2. 打开并等待 DeepSeek 聊天页面就绪
3. 接收 LoopAgent 已经渲染完成的最终 Prompt
4. 提交 Prompt
5. 监测 DeepSeek 生成状态
6. 提取最终 Python 代码
7. 保存生成代码
8. 上传文件接口保持与 LoopAgent Core 兼容
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from loop_agent_browser import EdgeBrowser


# ============================================================
# DeepSeek configuration
# ============================================================

DEEPSEEK_URL = (
    "https://chat.deepseek.com/"
)

STARTUP_WAIT = 5
READY_TIMEOUT = 60
MAX_GENERATE_WAIT = 300
SEND_RETRY_INTERVAL = 1.0
SEND_RETRY_TIMEOUT = 30
STOP_APPEAR_TIMEOUT = 20
STOP_GONE_STABLE_POLLS = 2
STABLE_POLLS_REQUIRED = 3
POLL_INTERVAL = 1.0


# DeepSeek 私有 selector
INPUT_SELECTOR = (
    'textarea[name="search"]'
)


# ============================================================
# DeepSeek Session
# ============================================================


class DeepSeekSession:
    """
    DeepSeek 网站适配层。
    """

    def __init__(self):
        self.browser_session = EdgeBrowser(
            startup_wait=STARTUP_WAIT,
        )
        self.page = None
        self._connected = False

    # ========================================================
    # Browser lifecycle
    # ========================================================

    def start(self):
        if self._connected:
            print("[DEEPSEEK] 会话已经启动")
            return

        print("[DEEPSEEK] 启动 Edge / 连接浏览器...")
        self.page = self.browser_session.start(DEEPSEEK_URL)
        print(f"[DEEPSEEK] DOMContentLoaded: {self.page.url}")

        if not self._wait_for_ready():
            self._save_debug_screenshot("deepseek_debug_not_ready.png")
            self.browser_session.close()
            self.page = None
            raise RuntimeError("DeepSeek 页面在等待时间内未出现聊天输入框。")

        self._connected = True
        print(f"[DEEPSEEK] 会话就绪: {self.page.url}")

    def _wait_for_ready(self, timeout: float = READY_TIMEOUT) -> bool:
        deadline = time.time() + timeout
        announced = False

        while time.time() < deadline:
            try:
                url = (self.page.url or "").lower()
                if "/login" in url or "/signin" in url or "/sign-in" in url:
                    print("[DEEPSEEK] 检测到登录页面，请完成登录...")

                editor = self.page.locator(INPUT_SELECTOR).first
                if editor.count() > 0 and editor.is_visible():
                    return True
            except Exception:
                pass

            if not announced:
                print(f"[DEEPSEEK] 等待聊天输入框加载 (最多 {timeout:.0f}s)...")
                announced = True

            time.sleep(0.5)
        return False

    def _ensure_connected(self):
        if not self._connected or self.page is None:
            raise RuntimeError("DeepSeekSession 未启动，请先调用 start()")

    # ========================================================
    # DOM state
    # ========================================================

    def get_message_count(self) -> int:
        self._ensure_connected()
        return int(
            self.page.evaluate(
                "() => document.querySelectorAll('.ds-message').length"
            )
        )

    def get_assistant_count(self) -> int:
        self._ensure_connected()
        return int(
            self.page.evaluate(
                "() => document.querySelectorAll('.ds-assistant-message-main-content').length"
            )
        )

    def get_code_block_count(self) -> int:
        self._ensure_connected()
        return int(
            self.page.evaluate(
                "() => document.querySelectorAll('.md-code-block').length"
            )
        )

    def get_generating_state(self) -> bool:
        self._ensure_connected()
        return bool(
            self.page.evaluate(
                """
                () => {
                    const buttons = [...document.querySelectorAll('[role="button"].ds-button')];
                    return buttons.some(btn => {
                        const cls = String(btn.className || '');
                        const aria = btn.getAttribute('aria-label') || '';
                        const text = btn.innerText || '';
                        return (
                            (cls.includes('ds-button--primary') && !cls.includes('ds-button--disabled'))
                            ||
                            /stop|停止|cancel|取消/i.test(`${aria} ${text}`)
                        );
                    });
                }
                """
            )
        )

    def _get_last_assistant_signature(self) -> dict:
        self._ensure_connected()
        return self.page.evaluate(
            """
            () => {
                const messages = document.querySelectorAll('.ds-assistant-message-main-content');
                if (!messages.length) {
                    return { exists: false, messageLength: 0, codeLength: 0, codeBlockCount: 0 };
                }
                const message = messages[messages.length - 1];
                const codeBlocks = message.querySelectorAll('.md-code-block pre');
                const text = message.innerText || message.textContent || '';
                let codeText = '';
                if (codeBlocks.length) {
                    const last = codeBlocks[codeBlocks.length - 1];
                    codeText = last.innerText || last.textContent || '';
                }
                return {
                    exists: true,
                    messageLength: text.length,
                    codeLength: codeText.length,
                    codeBlockCount: codeBlocks.length
                };
            }
            """
        )

    def _get_last_python_code(self) -> str:
        self._ensure_connected()
        return self.page.evaluate(
            """
            () => {
                const messages = document.querySelectorAll('.ds-assistant-message-main-content');
                if (!messages.length) return '';
                const message = messages[messages.length - 1];
                const blocks = message.querySelectorAll('.md-code-block');
                if (!blocks.length) return '';
                for (let i = blocks.length - 1; i >= 0; i--) {
                    const block = blocks[i];
                    const banner = block.querySelector('.md-code-block-banner');
                    const langText = banner ? (banner.innerText || banner.textContent || '') : '';
                    const pre = block.querySelector('pre');
                    if (pre && /python/i.test(langText)) {
                        return pre.innerText || pre.textContent || '';
                    }
                }
                const lastPre = blocks[blocks.length - 1].querySelector('pre');
                return lastPre ? (lastPre.innerText || lastPre.textContent || '') : '';
            }
            """
        ) or ""

    # ========================================================
    # Sending
    # ========================================================

    def _get_input_box(self):
        self._ensure_connected()
        input_box = self.page.locator(INPUT_SELECTOR).first
        input_box.wait_for(state="visible", timeout=READY_TIMEOUT * 1000)
        return input_box

    def append_input_text(self, text: str) -> bool:
        """
        向当前 DeepSeek 输入框追加文本，但不发送。
        """
        self._ensure_connected()
        if not isinstance(text, str):
            raise TypeError(f"text 必须是 str，实际类型: {type(text).__name__}")

        try:
            input_box = self._get_input_box()
        except Exception as exc:
            print(f"[DEEPSEEK] 输入框不存在: {exc}")
            return False

        try:
            input_box.click()
            input_box.evaluate(
                """
                (el, txt) => {
                    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                    if (nativeSetter) {
                        nativeSetter.call(el, el.value + txt);
                    } else {
                        el.value = el.value + txt;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
                """,
                text
            )
            return True
        except Exception as exc:
            print(f"[DEEPSEEK] 追加输入失败: {exc}")
            return False

    def send(self) -> bool:
        self._ensure_connected()
        try:
            input_box = self._get_input_box()
        except Exception as exc:
            print(f"[DEEPSEEK] 输入框不存在: {exc}")
            return False
        return self._send_prompt(input_box)

    def _send_prompt(self, input_box) -> bool:
        start_time = time.time()
        print("[DEEPSEEK] 等待并尝试提交...")
        while time.time() - start_time < SEND_RETRY_TIMEOUT:
            time.sleep(SEND_RETRY_INTERVAL)
            try:
                input_box.press("Enter")
            except Exception as exc:
                print(f"[DEEPSEEK] 发送失败: {exc}")
                continue

            try:
                if self.get_generating_state():
                    print("[DEEPSEEK] 检测到生成状态")
                    return True
            except Exception:
                pass
        print("[DEEPSEEK] 未检测到生成状态")
        return False

    # ========================================================
    # Generation
    # ========================================================

    def generate(self, prompt: str, task_idx: int = 0, workspace: str | None = None):
        self._ensure_connected()
        if not isinstance(prompt, str):
            raise TypeError(f"prompt 必须是 str，实际类型: {type(prompt).__name__}")
        if workspace is None:
            workspace = os.environ.get("LOOP_AGENT_DATA_CURRENT", "agent_idle/file_database/current")

        print(f"[DEEPSEEK] 发送任务 idx={task_idx}")
        try:
            input_box = self._get_input_box()
            input_box.wait_for(state="visible", timeout=READY_TIMEOUT * 1000)
        except Exception as exc:
            print(f"[DEEPSEEK] 输入框不存在: {exc}")
            return None

        input_box.click()
        input_box.evaluate(
            """
            (el, txt) => {
                const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                if (nativeSetter) {
                    nativeSetter.call(el, txt);
                } else {
                    el.value = txt;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
            """,
            prompt
        )

        if not self._send_prompt(input_box):
            self._save_debug_screenshot("deepseek_debug_send_failed.png")
            return None

        return self._wait_and_extract_generation(prompt, task_idx, workspace)

    def generate_prepared(self, task_description: str, task_idx: int = 0, workspace: str | None = None):
        self._ensure_connected()
        if not isinstance(task_description, str):
            raise TypeError(f"task_description 必须是 str，实际类型: {type(task_description).__name__}")
        if workspace is None:
            workspace = os.environ.get("LOOP_AGENT_DATA_CURRENT", "agent_idle/file_database/current")

        print(f"[DEEPSEEK] 发送已准备输入 idx={task_idx}")
        if not self.send():
            self._save_debug_screenshot("deepseek_debug_send_failed.png")
            return None

        return self._wait_and_extract_generation(task_description, task_idx, workspace)

    def _wait_and_extract_generation(self, task_description: str, task_idx: int, workspace: str):
        start_time = time.time()
        print("[DEEPSEEK] 等待生成结束...")
        last_signature = None
        stable_count = 0
        primary_gone_count = 0

        while time.time() - start_time < MAX_GENERATE_WAIT:
            try:
                generating = self.get_generating_state()
                signature = self._get_last_assistant_signature()
                compact = (
                    signature.get("codeBlockCount", 0),
                    signature.get("codeLength", 0),
                    signature.get("messageLength", 0),
                )

                if compact == last_signature and signature.get("messageLength", 0) > 0:
                    stable_count += 1
                else:
                    stable_count = 0
                    last_signature = compact

                if generating:
                    primary_gone_count = 0
                else:
                    primary_gone_count += 1

                if (not generating and primary_gone_count >= STOP_GONE_STABLE_POLLS and stable_count >= STABLE_POLLS_REQUIRED):
                    print("[DEEPSEEK] 生成完成")
                    break
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
        else:
            print("[DEEPSEEK] 达到最大等待时间")

        time.sleep(1)
        code = self._get_last_python_code().replace("\u00a0", " ")
        print(f"[DEEPSEEK] 代码长度: {len(code)}")

        # 核心最小修改：代码为空时不阻断流程，仅打印提示，继续往下正常保存
        if not code.strip():
            print("[DEEPSEEK] 提取的代码为空（这通常是因为处于上下文准备阶段）")

        return self._save_code(task_description, code, task_idx, workspace)

    # ========================================================
    # Save generated code
    # ========================================================

    def _save_code(self, task_description: str, code: str, task_idx: int, workspace: str):
        workspace_path = Path(workspace)
        workspace_path.mkdir(parents=True, exist_ok=True)
        filename = f"{time.strftime('%Y%m%d')}_{task_idx:03d}.py"
        filepath = workspace_path / filename

        # 仅在实际提取到代码时检查 END 标记
        has_marker = "END_AND_END" in code
        if code.strip() and not has_marker:
            print("[DEEPSEEK] 警告：代码缺少 END_AND_END 标记")

        safe_desc = task_description.replace("\n", " | ").replace("\r", "")
        header = f"""# {filename}
# TASK_IDX: {task_idx}
# SOURCE_DESC: {safe_desc}
# GENERATED_BY: deepseek_web
# TIMESTAMP: {time.strftime('%Y-%m-%d %H:%M:%S')}
# HAS_END_MARKER: {has_marker}

"""
        filepath.write_text(header + code, encoding="utf-8")
        print(f"[DEEPSEEK] 已保存: {filepath}")
        return filepath

    # ========================================================
    # Upload compatibility API
    # ========================================================

    def upload_file_via_clipboard(self, file_path: str) -> bool:
        if not self._connected: return False
        return self.browser_session.upload_file(file_path, input_selector=INPUT_SELECTOR)

    def upload_files_via_clipboard(self, file_paths: list[str]) -> bool:
        if not self._connected: return False
        return self.browser_session.upload_files(file_paths, input_selector=INPUT_SELECTOR)

    def upload_file(self, file_path: str) -> bool:
        return self.upload_file_via_clipboard(file_path)

    def upload_files(self, file_paths: list[str]) -> bool:
        return self.upload_files_via_clipboard(file_paths)

    def _save_debug_screenshot(self, filename: str) -> None:
        try:
            if self.page:
                self.page.screenshot(path=filename, full_page=True)
                print(f"[DEEPSEEK] 调试截图: {filename}")
        except Exception as exc:
            print(f"[DEEPSEEK] 保存截图失败: {exc}")

    def close(self):
        try:
            self.browser_session.close()
        except Exception as exc:
            print(f"[DEEPSEEK] 关闭浏览器失败: {exc}")
        self.page = None
        self._connected = False
        print("[DEEPSEEK] 会话关闭")


__all__ = [
    "DeepSeekSession",
    "DEEPSEEK_URL",
]
