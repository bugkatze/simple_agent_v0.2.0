"""
LoopAgent Grok 网站适配层 (v0.2 跨平台 & Context 注入兼容版)

职责：
1. 提供 Grok 网页交互 Session
2. 管理 Grok 页面状态检测
3. 提供生成状态判断
4. 提取 Grok 响应内容
5. 支持多轮大文本 Context 注入

依赖：
    loop_agent_browser.EdgeBrowser

本模块不负责：
    - Edge 启动与跨平台路径解析
    - Playwright 生命周期
    - 浏览器进程管理
    - 剪贴板底层实现

这些由 loop_agent_browser 提供。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from loop_agent_browser import EdgeBrowser

# ============================================================
# Grok configuration
# ============================================================

GROK_URL = "https://grok.com/"

# ============================================================
# Runtime parameters
# ============================================================

STARTUP_WAIT = 5
READY_TIMEOUT = 60
MAX_GENERATE_WAIT = 300
ASSISTANT_APPEAR_TIMEOUT = 30
STOP_APPEAR_TIMEOUT = 20
STOP_GONE_STABLE_POLLS = 2
STABLE_POLLS_REQUIRED = 3
POLL_INTERVAL = 1.0

# ============================================================
# Grok Session
# ============================================================

class GrokSession:
    """
    LoopAgent Grok 网页适配。

    与 GeminiSession / DeepSeekSession 架构完全一致。
    """

    def __init__(self):
        # 移除硬编码，使用统一的跨平台 Browser Adapter 配置
        self.browser_session = EdgeBrowser(
            startup_wait=STARTUP_WAIT,
        )
        self.page = None
        self._connected = False
        self.core_goal = None

    # ========================================================
    # Browser lifecycle
    # ========================================================

    def start(self):
        if self._connected:
            print("[GROK] 会话已经启动")
            return

        print("[GROK] 启动 Edge / 连接浏览器...")
        self.page = self.browser_session.start(GROK_URL)
        print(f"[GROK] DOMContentLoaded: {self.page.url}")

        if not self._wait_for_chat_or_login():
            self._save_debug_screenshot("grok_debug_not_ready.png")
            self.browser_session.close()
            self.page = None
            raise RuntimeError("Grok 页面未进入可用聊天状态")

        self._connected = True
        print(f"[GROK] 会话就绪: {self.page.url}")

    def _has_login_page(self) -> bool:
        if not self.page:
            return False
        url = (self.page.url or "").lower()
        return "/login" in url or "/signin" in url or "/sign-in" in url

    def _wait_for_chat_or_login(self, timeout: float = READY_TIMEOUT) -> bool:
        if not self.page:
            return False
        deadline = time.time() + timeout
        announced = False

        while time.time() < deadline:
            try:
                if self._has_login_page():
                    print("[GROK] 检测到登录页面，请在浏览器中完成登录...")
                    time.sleep(1)
                    continue

                editor = self.page.locator(
                    '[data-testid="chat-input"] [contenteditable="true"], '
                    'div[aria-label="Ask Grok anything"][contenteditable="true"], '
                    '[contenteditable="true"]'
                ).first

                if editor.count() > 0 and editor.is_visible():
                    print("[GROK] 聊天输入框已就绪")
                    return True
            except Exception:
                pass

            if not announced:
                print(f"[GROK] 等待聊天界面加载 （最多 {timeout:.0f}s）...")
                announced = True

            time.sleep(0.5)

        return False

    def _ensure_connected(self) -> None:
        if not self._connected or self.page is None:
            raise RuntimeError("GrokSession 未启动，请先调用 start()")

    # ========================================================
    # DOM helpers
    # ========================================================

    def get_assistant_count(self) -> int:
        self._ensure_connected()
        return int(
            self.page.evaluate(
                "() => document.querySelectorAll('[data-testid=\"assistant-message\"]').length"
            )
        )

    def get_code_block_count(self) -> int:
        self._ensure_connected()
        return int(
            self.page.evaluate(
                "() => document.querySelectorAll('[data-testid=\"code-block\"]').length"
            )
        )

    def get_stop_button_count(self) -> int:
        self._ensure_connected()
        return int(
            self.page.evaluate(
                "() => document.querySelectorAll('button[aria-label=\"Stop model response\"]').length"
            )
        )

    def get_generating_state(self) -> bool:
        return self.get_stop_button_count() > 0

    def _get_last_assistant_signature(self) -> dict:
        self._ensure_connected()
        return self.page.evaluate(
            """
            () => {
                const messages = document.querySelectorAll('[data-testid="assistant-message"]');
                if (!messages.length) {
                    return { exists: false, messageLength: 0, codeLength: 0, codeBlockCount: 0, text: '' };
                }

                const message = messages[messages.length - 1];
                const codeBlocks = message.querySelectorAll('[data-testid="code-block"] pre code');
                let codeText = '';
                
                if (codeBlocks.length) {
                    const lastCode = codeBlocks[codeBlocks.length - 1];
                    codeText = lastCode.innerText || lastCode.textContent || '';
                }

                const text = message.innerText || message.textContent || '';
                return {
                    exists: true,
                    messageLength: text.length,
                    codeLength: codeText.length,
                    codeBlockCount: codeBlocks.length,
                    text: text
                };
            }
            """
        )

    def _get_input_box(self, timeout: float = READY_TIMEOUT):
        self._ensure_connected()
        selector = (
            '[data-testid="chat-input"] [contenteditable="true"], '
            'div[aria-label="Ask Grok anything"][contenteditable="true"], '
            '[contenteditable="true"]'
        )
        try:
            locator = self.page.locator(selector).first
            locator.wait_for(state="visible", timeout=int(timeout * 1000))
            return locator
        except Exception as exc:
            print(f"[GROK] 输入框未就绪: {exc}")
            return None

    def _has_new_response(self, initial_assistant: int, initial_code: int) -> bool:
        try:
            return (
                self.get_assistant_count() > initial_assistant or
                self.get_code_block_count() > initial_code
            )
        except Exception:
            return False

    # ========================================================
    # Sending & Input (Context Support)
    # ========================================================

    def append_input_text(self, text: str) -> bool:
        """支持 Context 批处理追加输入"""
        self._ensure_connected()
        if not isinstance(text, str):
            raise TypeError(f"text 必须是 str，实际类型: {type(text).__name__}")

        try:
            input_box = self._get_input_box()
            # Grok 的 ProseMirror 同样最适合 execCommand 文本注入
            input_box.evaluate(
                """
                (el, t) => {
                    el.focus();
                    document.execCommand('insertText', false, t);
                }
                """,
                text,
            )
            return True
        except Exception as exc:
            print(f"[GROK] 追加输入失败: {exc}")
            return False

    def send(self) -> bool:
        """提交当前输入框"""
        self._ensure_connected()
        try:
            input_box = self._get_input_box()
            input_box.press("Enter")
            return True
        except Exception as exc:
            print(f"[GROK] 提交发送失败: {exc}")
            return False

    def _wait_for_generation(self, initial_assistant: int, initial_codes: int) -> bool:
        """封装的 Grok 专属等待生成逻辑"""
        start_time = time.time()
        
        # 阶段1：确认生成开始
        print("[GROK] 阶段1: 等待生成状态...")
        stop_seen = False
        deadline = start_time + STOP_APPEAR_TIMEOUT
        
        while time.time() < deadline:
            try:
                if self.get_generating_state():
                    stop_seen = True
                    print("[GROK] 检测到 Stop model response")
                    break
                if self._has_new_response(initial_assistant, initial_codes):
                    print("[GROK] 检测到新响应")
                    break
            except Exception:
                pass
            time.sleep(0.25)
            
        if not stop_seen:
            print("[GROK] 未检测到明显生成状态，继续等待响应稳定")

        # 阶段2：等待生成结束
        print("[GROK] 阶段2: 等待生成完成...")
        last_signature = None
        stable_count = 0
        stop_gone_count = 0
        
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
                    stop_gone_count = 0
                else:
                    stop_gone_count += 1

                if not generating and stop_gone_count >= STOP_GONE_STABLE_POLLS and stable_count >= STABLE_POLLS_REQUIRED:
                    print(
                        f"[GROK] 生成完成: message={signature.get('messageLength',0)}, "
                        f"code={signature.get('codeLength',0)}, "
                        f"blocks={signature.get('codeBlockCount',0)}"
                    )
                    return True
            except Exception:
                pass
            
            elapsed = time.time() - start_time
            if int(elapsed) > 0 and int(elapsed) % 15 == 0:
                print(f"[GROK] 仍在生成... {int(elapsed)}s")
                
            time.sleep(POLL_INTERVAL)
            
        print("[GROK] 达到最大等待时间，强制提取当前结果")
        return True

    # ========================================================
    # Generation
    # ========================================================

    def generate(self, prompt: str, task_idx: int = 0, workspace: str | None = None):
        self._ensure_connected()
        if workspace is None:
            workspace = os.environ.get("LOOP_AGENT_DATA_CURRENT", "agent_idle/file_database/current")

        print(f"[GROK] 发送任务 (idx={task_idx})...")
        initial_assistant = self.get_assistant_count()
        initial_codes = self.get_code_block_count()
        
        try:
            input_box = self._get_input_box()
            input_box.fill("")
            self.append_input_text(prompt)
            self.send()
        except Exception as exc:
            print(f"[GROK] 输入/发送失败: {exc}")
            return None

        # 等待与提取
        if not self._wait_for_generation(initial_assistant, initial_codes):
            return None

        return self._extract_and_save(prompt, task_idx, workspace)

    def generate_prepared(self, task_description: str, task_idx: int = 0, workspace: str | None = None):
        """发送已经 prepare 好的 Context 批处理文本"""
        self._ensure_connected()
        if workspace is None:
            workspace = os.environ.get("LOOP_AGENT_DATA_CURRENT", "agent_idle/file_database/current")

        print(f"[GROK] 发送已准备好的 Context 任务 (idx={task_idx})...")
        initial_assistant = self.get_assistant_count()
        initial_codes = self.get_code_block_count()
        
        if not self.send():
            print("[GROK] Context 发送失败")
            return None

        if not self._wait_for_generation(initial_assistant, initial_codes):
            return None

        return self._extract_and_save(task_description, task_idx, workspace)

    def _extract_and_save(self, description: str, task_idx: int, workspace: str):
        time.sleep(1)
        print("[GROK] 阶段3: 提取 Python 代码...")
        code = self._get_last_python_code().replace("\u00a0", " ")
        print(f"[GROK] 代码长度: {len(code)}")

        if not code.strip():
            self._save_debug_screenshot("grok_debug_empty_code.png")
            print("[GROK] 未提取到代码")
            return None

        return self._save_code(description, code, task_idx, workspace)

    # ========================================================
    # Code extraction
    # ========================================================

    def _get_last_python_code(self) -> str:
        self._ensure_connected()
        return self.page.evaluate(
            """
            () => {
                const messages = document.querySelectorAll('[data-testid="assistant-message"]');
                if (!messages.length) return '';

                const message = messages[messages.length - 1];
                const blocks = message.querySelectorAll('[data-testid="code-block"]');
                if (!blocks.length) return '';

                // 优先寻找 Python 标记
                for (let i = blocks.length - 1; i >= 0; i--) {
                    const block = blocks[i];
                    const text = block.innerText || block.textContent || '';
                    const code = block.querySelector('pre code');
                    if (!code) continue;

                    if (/\\bpython\\b/i.test(text)) {
                        return code.innerText || code.textContent || '';
                    }
                }

                // fallback: 使用最后一个代码块
                const last = blocks[blocks.length - 1];
                const pre = last.querySelector('pre code');
                return pre ? (pre.innerText || pre.textContent || '') : '';
            }
            """
        ) or ""

    # ========================================================
    # Save generated code
    # ========================================================

    def _save_code(self, task_description: str, code: str, task_idx: int, workspace: str):
        workspace_path = Path(workspace)
        workspace_path.mkdir(parents=True, exist_ok=True)

        filename = f"{time.strftime('%Y%m%d')}_{task_idx:03d}.py"
        filepath = workspace_path / filename

        has_marker = "END_AND_END" in code
        if not has_marker:
            print("[GROK] 警告: 代码缺少 END_AND_END 标记")

        safe_desc = task_description.replace("\n", " | ").replace("\r", "")

        header = f"""# {filename}
# TASK_IDX: {task_idx}
# SOURCE_DESC: {safe_desc}
# GENERATED_BY: grok_web
# TIMESTAMP: {time.strftime('%Y-%m-%d %H:%M:%S')}
# HAS_END_MARKER: {has_marker}

"""
        filepath.write_text(header + code, encoding="utf-8")
        print(f"[GROK] 已保存: {filepath} (标记检测: {has_marker})")
        return filepath

    # ========================================================
    # Upload compatibility API
    # ========================================================

    def upload_file(self, file_path: str) -> bool:
        if not self._connected:
            return False
        return self.browser_session.upload_file(file_path, input_selector='[contenteditable="true"]')

    def upload_files(self, file_paths: list[str]) -> bool:
        if not self._connected:
            return False
        return self.browser_session.upload_files(file_paths, input_selector='[contenteditable="true"]')

    def upload_file_via_clipboard(self, file_path: str) -> bool:
        return self.upload_file(file_path)

    def upload_files_via_clipboard(self, file_paths: list[str]) -> bool:
        return self.upload_files(file_paths)

    # ========================================================
    # Diagnostics & Cleanup
    # ========================================================

    def _save_debug_screenshot(self, filename: str) -> None:
        try:
            if self.page:
                self.page.screenshot(path=filename, full_page=True)
                print(f"[GROK] 调试截图: {filename}")
        except Exception as exc:
            print(f"[GROK] 保存截图失败: {exc}")

    def close(self):
        try:
            self.browser_session.close()
        except Exception as exc:
            print(f"[GROK] 浏览器关闭失败: {exc}")
        self.page = None
        self._connected = False
        print("[GROK] 会话关闭")

__all__ = ["GrokSession", "GROK_URL"]
