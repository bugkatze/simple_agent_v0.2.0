"""
LoopAgent Support

职责：

1. 管理 agent_idle/ 工作空间
2. 管理 file_database/
3. 执行生成的 Python 程序
4. 检查生成代码的基础安全条件

本文件不负责：

- Prompt
- Loop 生命周期
- Agent 状态转移
- LLM Session
- Evaluator

这些逻辑由 loop_agent_core.py 负责。
"""

import configparser
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional


# ============================================================
# Safety configuration
# ============================================================

DANGEROUS_OPS = (
    "eval(",
    "exec(",
    "__import__(",
)

CURRENT_BROWSER_EXE = "msedge.exe"
CURRENT_DEBUG_PORT = "9222"
CURRENT_USER_DATA = os.path.expanduser(
    r"~\AppData\Local\Microsoft\Edge\User Data"
)


# ============================================================
# RuntimeWorkspace
# ============================================================

class RuntimeWorkspace:
    """
    Runtime workspace used by the agent.

    Directory structure:

        agent_idle/
        └── file_database/
            ├── current/
            └── data_history/

    This class only manages runtime-related paths.

    Project-level resources such as:
        seed/
        input/
        prompt_set/

    are intentionally NOT managed here.
    """

    def __init__(self, root: Optional[str] = None):
        if root is None:
            root_path = Path(__file__).resolve().parent / "agent_idle"
        else:
            root_path = Path(root)
            if not root_path.is_absolute():
                root_path = Path(__file__).resolve().parent / root_path

        self.root = root_path.resolve()

        self.file_database = self.root / "file_database"
        self.current = self.file_database / "current"
        self.history = self.file_database / "data_history"

    def ensure(self) -> None:
        """Create the runtime directory structure if it does not exist."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.file_database.mkdir(parents=True, exist_ok=True)
        self.current.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)

    def path_in_root(self, path: str) -> Path:
        """
        Resolve a path and ensure that it remains inside the runtime root.

        Raises:
            ValueError: if the resolved path escapes the runtime workspace.
        """
        candidate = Path(path)

        if not candidate.is_absolute():
            candidate = self.root / candidate

        candidate = candidate.resolve()

        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise ValueError(
                f"Path escapes runtime workspace: {candidate}"
            )

        return candidate


# ============================================================
# FileDatabase
# ============================================================

class FileDatabase:
    """
    Tracks files generated or modified during a runtime round.

    Directory structure:

        file_database/
        ├── current/
        └── data_history/

    current:
        Files produced/modified by the current execution.

    data_history:
        Archived snapshots from previous executions.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.current = self.root / "current"
        self.history = self.root / "data_history"

        self.current.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)

        self._snapshot = self._scan()

    # --------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------

    @staticmethod
    def _file_signature(path: Path):
        """
        Return a stable file signature:

            (size, mtime_ns, sha256)

        Hashing is used to avoid relying only on timestamp/size.
        """
        stat = path.stat()

        sha256 = hashlib.sha256()

        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)

        return (
            stat.st_size,
            stat.st_mtime_ns,
            sha256.hexdigest(),
        )

    def _scan(self) -> Dict[str, tuple]:
        """Return a snapshot of all files currently under current/."""
        snapshot = {}

        if not self.current.exists():
            return snapshot

        for path in self.current.rglob("*"):
            if not path.is_file():
                continue

            rel = path.relative_to(self.current).as_posix()
            snapshot[rel] = self._file_signature(path)

        return snapshot

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    def detect_changes(self) -> List[Path]:
        """
        Detect new or modified files in current/.

        Returns:
            A list of absolute paths of newly created or changed files.
        """
        latest = self._scan()

        changed = []

        for rel_path, signature in latest.items():
            if self._snapshot.get(rel_path) != signature:
                changed.append(self.current / rel_path)

        self._snapshot = latest

        return sorted(changed)

    def get_all_files(self) -> List[Path]:
        """Return all files currently stored in current/."""
        if not self.current.exists():
            return []

        return sorted(
            path
            for path in self.current.rglob("*")
            if path.is_file()
        )

    def archive_current(self) -> List[Path]:
        """
        Move all files in current/ into data_history/.

        A timestamp-based directory is created for each archive.
        Name collisions are resolved with a numeric suffix.

        Returns:
            List of archived file paths.
        """
        files = self.get_all_files()

        if not files:
            self._snapshot = self._scan()
            return []

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        archive_dir = self.history / timestamp

        counter = 1
        while archive_dir.exists():
            archive_dir = self.history / f"{timestamp}_{counter}"
            counter += 1

        archive_dir.mkdir(parents=True, exist_ok=True)

        archived = []

        for source in files:
            relative = source.relative_to(self.current)
            destination = archive_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)

            shutil.move(str(source), str(destination))
            archived.append(destination)

        # Remove empty directories left inside current/
        for directory in sorted(
            (
                p
                for p in self.current.rglob("*")
                if p.is_dir()
            ),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

        self._snapshot = self._scan()

        return archived


# ============================================================
# CodeRunner
# ============================================================

class CodeRunner:
    """
    Execute generated Python code inside the runtime workspace.

    The working directory is agent_idle/.

    Environment variables exposed to generated code:

        LOOP_AGENT_WORKSPACE
        LOOP_AGENT_DATA_CURRENT
        LOOP_AGENT_DATA_HISTORY
    """

    def __init__(
        self,
        workspace: str | Path,
        timeout: int = 60,
    ):
        self.runtime = RuntimeWorkspace(workspace)
        self.runtime.ensure()
        self.timeout = timeout

    def run(self, script: str | Path) -> dict:
        """
        Execute a Python script.

        Args:
            script:
                Script path. Relative paths are resolved from runtime root.

        Returns:
            Execution result dictionary.
        """
        script_path = self.runtime.path_in_root(str(script))

        if not script_path.exists():
            return {
                "success": False,
                "output": "",
                "stderr": f"Script not found: {script_path}",
                "runtime": str(self.runtime.root),
                "ended_by": "missing_file",
                "returncode": None,
            }

        env = os.environ.copy()

        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "LOOP_AGENT_WORKSPACE": str(self.runtime.root),
                "LOOP_AGENT_DATA_CURRENT": str(self.runtime.current),
                "LOOP_AGENT_DATA_HISTORY": str(self.runtime.history),
            }
        )

        command = [
            sys.executable,
            str(script_path),
        ]

        try:
            process = subprocess.run(
                command,
                cwd=str(self.runtime.root),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            return {
                "success": process.returncode == 0,
                "output": process.stdout,
                "stderr": process.stderr,
                "runtime": str(self.runtime.root),
                "ended_by": (
                    "success"
                    if process.returncode == 0
                    else "error"
                ),
                "returncode": process.returncode,
            }

        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "output": exc.stdout or "",
                "stderr": exc.stderr or "",
                "runtime": str(self.runtime.root),
                "ended_by": "timeout",
                "returncode": None,
            }

        except Exception as exc:
            return {
                "success": False,
                "output": "",
                "stderr": str(exc),
                "runtime": str(self.runtime.root),
                "ended_by": "runner_error",
                "returncode": None,
            }


# ============================================================
# SafetyChecker
# ============================================================


class SafetyChecker:
    """
    Lightweight static safety checker for generated code.

    Public interface is kept compatible with the existing LoopAgent Core:

        check_code(code) -> (safe, reason)
        check_task(code) -> (safe, reason)
    """

    def __init__(self, workspace: str | Path, project_root: str | Path | None = None):
        self.workspace = Path(workspace).resolve()
        
        if project_root is not None:
            self.project_root = Path(project_root).resolve()
        else:
            # 默认假设 workspace (agent_idle) 的上一级是项目根目录
            self.project_root = self.workspace.parent
            
        self.strict_mode = self._load_safety_config()

    def _load_safety_config(self) -> bool:
        """加载安全配置"""
        config_path = self.project_root / "safety_check.txt"
        strict_mode = True  # 默认死守底线
        
        if config_path.exists():
            parser = configparser.ConfigParser()
            try:
                parser.read(config_path, encoding="utf-8")
                if "safety" in parser:
                    strict_mode = parser["safety"].getboolean("strict_mode", fallback=True)
            except Exception as exc:
                print(f"[SAFETY] 读取配置文件失败，默认强制开启严格模式。错误: {exc}")

        # 如果用户主动设为了 false，打印醒目的警告
        if not strict_mode:
            print("\n" + "="*60)
            print("[WARNING] ⚠️ 极客/低审查模式已激活！")
            print("[WARNING] 危险代码拦截已被关闭，Agent 具有较高的系统执行权限。")
            print("[WARNING] 为了防止大模型幻觉损坏系统，强烈建议仅在【虚拟机 (VM)】或【隔离沙盒】中运行此模式！")
            print("="*60 + "\n")
        else:
            print("[SAFETY] 严格安全审查已启用 (默认模式)。")
            
        return strict_mode

    def check_code(self, code: str):
        """
        Check generated code.

        Returns:
            (True, "") when allowed.
            (False, reason) when rejected.
        """
        
        # 核心改动：如果处于低审查模式，直接放行一切代码
        if not self.strict_mode:
            return True, ""

        code_lower = code.lower()

        # ----------------------------------------------------
        # Dangerous Python operations
        # ----------------------------------------------------

        for operation in DANGEROUS_OPS:
            if operation in code_lower:
                return (
                    False,
                    f"Dangerous operation detected: {operation}",
                )

        # ----------------------------------------------------
        # Current browser / debugging connection
        # ----------------------------------------------------

        if (
            CURRENT_BROWSER_EXE.lower() in code_lower
            and CURRENT_DEBUG_PORT in code
        ):
            return (
                False,
                "Direct connection to the current Edge debugging "
                "session is not allowed.",
            )

        # ----------------------------------------------------
        # Current Edge user-data directory
        # ----------------------------------------------------

        user_data_lower = CURRENT_USER_DATA.lower()

        if user_data_lower in code_lower:
            return (
                False,
                "Direct access to the current Edge user-data "
                "directory is not allowed.",
            )

        # ----------------------------------------------------
        # Hardcoded runtime workspace path
        # ----------------------------------------------------

        workspace_text = str(self.workspace)

        if workspace_text and workspace_text.lower() in code_lower:
            return (
                False,
                "Hardcoded runtime workspace path detected.",
            )

        return True, ""
