"""LoopAgent Core

职责：
1. 管理项目级资源：seed/、input/、prompt_set/
2. 管理 agent_idle/ 运行空间
3. 检测并上传 seed / input 文件
4. 接收 LLM Session 生成的代码
5. 执行生成代码
6. 检测 file_database/current/ 的反馈
7. 根据运行状态选择下一阶段 Prompt
8. 对安全拒绝执行“返厂维修”，而不是直接终止循环
9. 提供预留的 Evaluator 接口

项目结构：

LoopAgent/
├── loop_agent_core.py
├── loop_agent_support.py
├── seed/
│   ├── browser.py
│   ├── database.py
│   ├── extractor.py
│   └── ...
├── input/
│   ├── ...
├── prompt_set/
│   ├── core_goal.txt
│   ├── first.txt
│   ├── success_and_next.txt
│   ├── error.txt
│   ├── no_output.txt
│   └── rejected.txt
└── agent_idle/
    └── file_database/
        ├── current/
        └── data_history/

说明：
- seed/ 与 input/ 是项目级资源，不属于 agent_idle/。
- seed/ 中支持多个 Python 文件。
- input/ 中的文件会在任务开始时上传给 Web LLM，但不会自动复制到 agent_idle/。
- agent_idle/ 仅作为生成程序的运行空间。
- rejected 表示“本轮代码未获准执行”，不是任务终止状态。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from loop_agent_support import (
    CodeRunner,
    FileDatabase,
    RuntimeWorkspace,
    SafetyChecker,
)


# ============================================================
# Global Configuration
# ============================================================

MAX_ITERATIONS = 10


# ============================================================
# Project Resources
# ============================================================


class ProjectResources:
    """管理项目级资源目录。"""

    def __init__(self, root: str | Path | None = None):
        if root is None:
            self.root = Path(__file__).resolve().parent
        else:
            root_path = Path(root)
            if root_path.is_absolute():
                self.root = root_path.resolve()
            else:
                self.root = (
                    Path(__file__).resolve().parent / root_path
                ).resolve()

        self.seed = self.root / "seed"
        self.input = self.root / "input"
        self.prompt_set = self.root / "prompt_set"

    def ensure(self) -> None:
        """创建项目级资源目录。目录为空是允许的。"""
        self.seed.mkdir(parents=True, exist_ok=True)
        self.input.mkdir(parents=True, exist_ok=True)
        self.prompt_set.mkdir(parents=True, exist_ok=True)

    def get_seed_files(self) -> list[Path]:
        """返回 seed/ 下全部 Python 文件。"""
        if not self.seed.exists():
            return []

        return sorted(
            (
                path
                for path in self.seed.glob("*.py")
                if path.is_file() and not path.name.startswith(".")
            ),
            key=lambda p: p.name.lower(),
        )

    def get_input_files(self) -> list[Path]:
        """返回 input/ 下全部非隐藏文件。"""
        if not self.input.exists():
            return []

        return sorted(
            (
                path
                for path in self.input.rglob("*")
                if path.is_file()
                and not path.name.startswith(".")
            ),
            key=lambda p: str(p.relative_to(self.input)).lower(),
        )


# ============================================================
# Prompt Set
# ============================================================


class PromptSet:
    """管理外置 Prompt。"""

    REQUIRED_FILES = (
        "core_goal.txt",
        "first.txt",
        "success_and_next.txt",
        "error.txt",
        "no_output.txt",
        "rejected.txt",
    )

    PLACEHOLDER_PATTERN = re.compile(
        r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}"
    )

    def __init__(self, root: str | Path | None = None):
        if root is None:
            self.root = (
                Path(__file__).resolve().parent / "prompt_set"
            ).resolve()
        else:
            root_path = Path(root)
            if root_path.is_absolute():
                self.root = root_path.resolve()
            else:
                self.root = (
                    Path(__file__).resolve().parent / root_path
                ).resolve()

        self._cache: dict[str, str] = {}

    def _get_path(self, filename: str) -> Path:
        path = (self.root / filename).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"非法 Prompt 路径: {filename}") from exc
        return path

    def load(self, filename: str) -> str:
        if filename in self._cache:
            return self._cache[filename]
        path = self._get_path(filename)
        if not path.is_file():
            raise FileNotFoundError(f"Prompt 文件不存在: {path}")
        content = path.read_text(encoding="utf-8")
        self._cache[filename] = content
        return content

    def reload(self) -> None:
        self._cache.clear()

    def validate(self) -> None:
        missing = []
        for filename in self.REQUIRED_FILES:
            if not self._get_path(filename).is_file():
                missing.append(filename)
        if missing:
            raise FileNotFoundError(
                "Prompt Set 不完整，缺少:\n"
                + "\n".join(f"  - {filename}" for filename in missing)
            )

    def render(self, filename: str, **variables: Any) -> str:
        template = self.load(filename)
        placeholders = set(self.PLACEHOLDER_PATTERN.findall(template))
        missing = sorted(key for key in placeholders if key not in variables)
        if missing:
            raise ValueError(f"{filename} 存在未提供的模板变量: " + ", ".join(missing))

        rendered = template
        for key in placeholders:
            rendered = rendered.replace("{{" + key + "}}", str(variables[key]))
        return rendered.strip()

    def get_core_goal(self) -> str:
        return self.load("core_goal.txt").strip()


# ============================================================
# Evaluator
# ============================================================


class Evaluator:
    def evaluate(self, task_description: str, result: dict, context: dict | None = None) -> dict:
        raise NotImplementedError


class NullEvaluator(Evaluator):
    def evaluate(self, task_description: str, result: dict, context: dict | None = None) -> dict:
        return {
            "status": "unused",
            "score": None,
            "feedback": "",
            "next_action": None,
        }


# ============================================================
# LoopAgent
# ============================================================


class LoopAgent:
    def __init__(
        self,
        session,
        core_goal: str | None = None,
        runtime_workspace: str | Path | None = None,
        prompt_set: str | Path | None = None,
        evaluator: Evaluator | None = None,
        project_root: str | Path | None = None,
        initial_context_batches=None,
    ):
        self.project = ProjectResources(project_root)
        self.project.ensure()
        self.runtime = RuntimeWorkspace(runtime_workspace)

        print(f"[CORE] 文件: {Path(__file__).resolve()}")
        print(f"[CORE] 项目根目录: {self.project.root}")
        print(f"[WORKSPACE] runtime: {self.runtime.root}")
        print(f"[WORKSPACE] current: {self.runtime.current}")

        self.prompt_set = PromptSet(prompt_set)
        self.prompt_set.validate()

        print(f"[PROMPT] Prompt Set: {self.prompt_set.root}")

        self.runner = CodeRunner(workspace=self.runtime.root, timeout=60)
        self.file_db = FileDatabase(self.runtime.file_database)
        self.safety = SafetyChecker(workspace=self.runtime.root, project_root=self.project.root)

        self.session = session
        self.core_goal = (
            core_goal.strip() if core_goal is not None else self.prompt_set.get_core_goal()
        )
        self.session.core_goal = self.core_goal
        self.evaluator = evaluator if evaluator is not None else NullEvaluator()
        
        self.initial_context_batches = (
            None if initial_context_batches is None else list(initial_context_batches)
        )
        self._last_generated_file: Path | None = None

    def start(self):
        self.session.start()

    def close(self):
        self.session.close()

    def detect_seed(self) -> list[Path]:
        files = self.project.get_seed_files()
        if files:
            print(f"[SEED] 检测到 {len(files)} 个 seed 文件:")
            for path in files:
                print(f"[SEED]   - {path}")
        else:
            print("[SEED] seed/ 为空，本轮从零开始")
        return files

    def upload_seed(self) -> bool:
        files = self.detect_seed()
        if not files: return True
        print(f"[SEED] 上传 {len(files)} 个代码种子...")
        paths = [str(path.resolve()) for path in files]
        success = self.session.upload_files(paths)
        print(f"[SEED] 上传结果: {'成功' if success else '失败'}")
        return success

    def detect_input_files(self) -> list[Path]:
        files = self.project.get_input_files()
        if not files:
            print("[INPUT] input/ 为空，无初始输入文件")
            return []
        print(f"[INPUT] 检测到 {len(files)} 个输入文件:")
        for path in files:
            print(f"[INPUT]   - {path}")
        return files

    def upload_initial_inputs(self) -> bool:
        files = self.detect_input_files()
        if not files: return True
        print(f"[INPUT] 上传 {len(files)} 个初始输入文件...")
        return self.upload_data_files(files)

    def upload_data_files(self, file_paths: list[Path]) -> bool:
        paths = [str(Path(path).resolve()) for path in file_paths]
        if not paths: return True
        print(f"[DATA] 上传 {len(paths)} 个文件")
        return self.session.upload_files(paths)

    def _build_feedback_context(self, file_paths: list[Path]) -> str:
        """
        核心新增方法：将二次生成的反馈文件读取为 Context 文本块
        """
        parts = []
        for path in file_paths:
            try:
                content = path.read_text(encoding="utf-8")
                size = path.stat().st_size
                rel_path = path.relative_to(self.runtime.root).as_posix()
                parts.append(
                    f"[FILE]\nPATH: {rel_path}\nSIZE_BYTES: {size}\n\n{content}\n\n[/FILE]"
                )
            except Exception as exc:
                print(f"[DATA] 读取反馈文件失败 {path.name}: {exc}")
        return "\n\n".join(parts)

    def generate_and_save(
        self,
        prompt: str,
        task_idx: int = 0,
        extra_context: str | None = None,
    ) -> Path | None:
        """
        新增了 extra_context 参数。
        在第一阶段（task_idx=0）处理完整的 initial_context_batches；
        在后续阶段，如果提供 extra_context（如前一轮文件反馈文本），则作为纯文本一并注入。
        """
        if task_idx == 0 and self.initial_context_batches is not None:
            print(f"[CONTEXT] 第一阶段 Context 已启用，共 {len(self.initial_context_batches)} batches")
            for index, batch in enumerate(self.initial_context_batches, start=1):
                text = getattr(batch, "text", batch) if hasattr(batch, "text") else batch
                print(f"[CONTEXT] 追加 batch {index}/{len(self.initial_context_batches)} ({len(text.encode('utf-8'))} bytes)")
                if not self.session.append_input_text(text):
                    print(f"[CONTEXT] batch {index} 追加失败")
                    return None
            print("[CONTEXT] 追加最终 Prompt")
            if not self.session.append_input_text(prompt):
                print("[CONTEXT] 最终 Prompt 追加失败")
                return None
            saved = self.session.generate_prepared(prompt, task_idx, workspace=str(self.runtime.root))
            
        elif extra_context:
            print(f"[CONTEXT] 追加二次反馈 Context ({len(extra_context.encode('utf-8'))} bytes)")
            if not self.session.append_input_text(extra_context):
                return None
            print("[CONTEXT] 追加最终 Prompt")
            if not self.session.append_input_text(prompt):
                return None
            saved = self.session.generate_prepared(prompt, task_idx, workspace=str(self.runtime.root))
            
        else:
            saved = self.session.generate(prompt, task_idx, workspace=str(self.runtime.root))

        if not saved:
            return None

        path = Path(saved).resolve()
        path = self.runtime.path_in_root(path)
        self._last_generated_file = path
        return path

    def _run_generated(self, code_path: Path) -> dict:
        try:
            code = code_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "status": "error", "error": f"无法读取生成代码: {exc}",
                "file": code_path.name, "data_files": [],
            }

        safe, reason = self.safety.check_code(code)
        if not safe:
            print(f"[SAFETY] {reason}")
            return {
                "status": "rejected", "error": reason,
                "file": code_path.name, "data_files": [], "executed": False,
            }

        result = self.runner.run(code_path.name)
        changed = self.file_db.detect_changes()
        result["data_files"] = [str(path.relative_to(self.runtime.root)) for path in changed]
        result["file"] = code_path.name
        result["executed"] = True

        if result.get("success"):
            result["status"] = "ok"
        else:
            result["status"] = result.get("ended_by", "error")

        return result

    def _evaluate(self, task_description: str, result: dict, iteration: int, history: list[dict]) -> dict:
        context = {
            "iteration": iteration,
            "history": history,
            "runtime_workspace": str(self.runtime.root),
            "current": str(self.runtime.current),
            "data_files": result.get("data_files", []),
        }
        try:
            evaluation = self.evaluator.evaluate(task_description, result, context)
        except Exception as exc:
            return {"status": "error", "score": None, "feedback": "", "next_action": None, "error": f"Evaluator 执行失败: {exc}"}
        
        if not isinstance(evaluation, dict):
            return {"status": "invalid", "score": None, "feedback": "", "next_action": None, "error": "Evaluator 必须返回 dict"}
        return evaluation

    def _build_first_prompt(self, task_description: str) -> str:
        return self.prompt_set.render("first.txt", CORE_GOAL=self.core_goal, TASK_DESCRIPTION=task_description, OUTPUT="", DATA_FILES="", ERROR_TEXT="")

    def _build_success_prompt(self, task_description: str, result: dict) -> str:
        output = (result.get("output") or "").strip()
        changed_files = (result.get("data_files") or [])
        data_files = "\n".join(changed_files) if changed_files else "本轮没有检测到 current 数据变化。"
        return self.prompt_set.render("success_and_next.txt", CORE_GOAL=self.core_goal, TASK_DESCRIPTION=task_description, OUTPUT=output, DATA_FILES=data_files, ERROR_TEXT="")

    def _build_error_prompt(self, task_description: str, result: dict) -> str:
        stderr = (result.get("stderr") or "").strip()
        output = (result.get("output") or "").strip()
        error_text = stderr if stderr else output
        return self.prompt_set.render("error.txt", CORE_GOAL=self.core_goal, TASK_DESCRIPTION=task_description, OUTPUT=output, DATA_FILES="", ERROR_TEXT=error_text[:4000])

    def _build_no_output_prompt(self, task_description: str, result: dict) -> str:
        output = (result.get("output") or "").strip()
        return self.prompt_set.render("no_output.txt", CORE_GOAL=self.core_goal, TASK_DESCRIPTION=task_description, OUTPUT=output, DATA_FILES="", ERROR_TEXT="")

    def _build_rejected_prompt(self, task_description: str, result: dict) -> str:
        error_text = (result.get("error") or "代码未通过安全检查。").strip()
        return self.prompt_set.render("rejected.txt", CORE_GOAL=self.core_goal, TASK_DESCRIPTION=task_description, OUTPUT="", DATA_FILES="", ERROR_TEXT=error_text[:4000])

    def self_improve_loop(self, task_description: str, max_iter: int = MAX_ITERATIONS) -> dict:
        return self.self_improve_loop_enhanced(task_description=task_description, max_iter=max_iter)

    def self_improve_loop_enhanced(self, task_description: str, max_iter: int = MAX_ITERATIONS) -> dict:
        print("\n" + "#" * 60)
        print("# LoopAgent 自我完善循环")
        print(f"# TASK_DESC: {task_description}")
        print(f"# MAX_ITER: {max_iter}")
        print("#" * 60)

        if max_iter <= 0:
            return {"status": "invalid_max_iter", "iterations": 0, "history": [], "data_mode": False, "seed_used": False, "inputs_used": False, "runtime_workspace": str(self.runtime.root), "data_current": str(self.runtime.current), "project_root": str(self.project.root)}

        try:
            self.prompt_set.validate()
        except Exception as exc:
            print(f"[PROMPT] 初始化失败: {exc}")
            return {"status": "prompt_error", "iterations": 0, "history": [], "data_mode": False, "seed_used": False, "inputs_used": False, "error": str(exc), "runtime_workspace": str(self.runtime.root), "data_current": str(self.runtime.current), "project_root": str(self.project.root)}

        seed_files = self.detect_seed()
        seed_used = False
        if seed_files:
            if self.initial_context_batches is None:
                seed_used = self.upload_seed()
            else:
                print("[SEED] Context 第一阶段已启用，跳过 seed 物理文件上传")
                seed_used = True
            if not seed_used:
                print("[SEED] 上传失败，继续执行，但不会把 seed 视为已上传")

        input_files = self.detect_input_files()
        inputs_used = True
        if input_files:
            if self.initial_context_batches is None:
                inputs_used = self.upload_initial_inputs()
            else:
                print("[INPUT] Context 第一阶段已启用，跳过 input 物理文件上传")
                inputs_used = True
            if not inputs_used:
                print("[INPUT] 输入文件上传失败，继续执行")

        current_prompt = self._build_first_prompt(task_description)
        history: list[dict] = []
        data_mode = False
        pending_feedback_context: str | None = None

        for iteration in range(max_iter):
            iteration_number = iteration + 1
            print("\n" + "=" * 50)
            print(f"[LOOP] 迭代 {iteration_number}/{max_iter}")
            print("=" * 50)

            saved = self.generate_and_save(
                current_prompt,
                task_idx=iteration,
                extra_context=pending_feedback_context,
            )
            
            # 用完之后清空，防止被重复追加到后续迭代
            pending_feedback_context = None

            if saved is None:
                result = {"status": "gen_failed", "error": "模型没有生成可执行代码", "data_files": []}
                evaluation = self._evaluate(task_description, result, iteration_number, history)
                result["evaluation"] = evaluation
                history.append({"iteration": iteration_number, "task": current_prompt, "result": result})
                break

            print(f"[GENERATE] 生成代码: {saved}")

            result = self._run_generated(saved)
            status = result.get("status")

            if status == "rejected":
                history.append({"iteration": iteration_number, "task": current_prompt, "result": result})
                print("[LOOP] rejected\n[LOOP] 代码未执行，进入返厂维修。")
                if iteration >= max_iter - 1:
                    print("[LOOP] 达到最大迭代次数，结束")
                    break
                current_prompt = self._build_rejected_prompt(task_description, result)
                continue

            evaluation = self._evaluate(task_description, result, iteration_number, history)
            result["evaluation"] = evaluation
            history.append({"iteration": iteration_number, "task": current_prompt, "result": result})

            changed_files = result.get("data_files") or []
            has_next_iteration = (iteration < max_iter - 1)

            if changed_files:
                data_mode = True
                changed_paths = [self.runtime.root / path for path in changed_files]
                print(f"[DATA] 检测到 {len(changed_paths)} 个反馈文件:")
                for path in changed_paths:
                    print(f"[DATA]   - {path}")

                if has_next_iteration:
                    # 核心新增：Context 模式下不再调用上传，而是转换成文本缓存下来
                    if self.initial_context_batches is not None:
                        print("[DATA] Context 模式启用，将二次反馈文件读取为文本...")
                        pending_feedback_context = self._build_feedback_context(changed_paths)
                    else:
                        upload_success = self.upload_data_files(changed_paths)
                        print(f"[DATA] 反馈文件物理上传: {'成功' if upload_success else '失败'}")
                else:
                    print("[DATA] 最后一轮执行，跳过反馈处理")

            if status == "ok":
                print("[LOOP] 本轮执行成功")
                if not has_next_iteration:
                    print("[LOOP] 达到最大迭代次数，结束")
                    break
                current_prompt = self._build_success_prompt(task_description, result)
                continue

            if status in ("timeout", "missing_file", "gen_failed"):
                print(f"[LOOP] {status}，结束")
                break

            if iteration >= max_iter - 1:
                print("[LOOP] 达到最大迭代次数，结束")
                break

            if status == "no_output":
                current_prompt = self._build_no_output_prompt(task_description, result)
                continue

            current_prompt = self._build_error_prompt(task_description, result)

        if history:
            final_status = history[-1]["result"].get("status", "unknown")
        else:
            final_status = "no_history"

        return {
            "status": final_status,
            "iterations": len(history),
            "history": history,
            "data_mode": data_mode,
            "seed_used": seed_used,
            "inputs_used": inputs_used,
            "runtime_workspace": str(self.runtime.root),
            "data_current": str(self.runtime.current),
            "project_root": str(self.project.root),
            "prompt_set": str(self.prompt_set.root),
        }


__all__ = [
    "ProjectResources",
    "PromptSet",
    "Evaluator",
    "NullEvaluator",
    "LoopAgent",
]
