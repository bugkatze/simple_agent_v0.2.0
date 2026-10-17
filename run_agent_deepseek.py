from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loop_agent_core import LoopAgent
from loop_agent_deepseek import DeepSeekSession
from loop_agent_context import build_context_from_file


TASK_DESC = """

将当前 Seed 改造为一个能清洗出有价值的评论的程序

要求：

保留 Seed 当前已经验证可用的功能。
清理点赞数量小于3的评论。
最终程序必须能够实际运行，并产生可用的结果。

"""


def main():
    # ============================================================
    # 1. 创建 Context
    # ============================================================
    context_config_path = PROJECT_ROOT / "context_config.txt"
    context_result = build_context_from_file(context_config_path)

    if not context_result.success:
        raise RuntimeError(
            f"Context 构建失败：{context_result.error}"
        )

    # Context 构建成功且存在 batches 时，启用 Context
    # 若配置禁用时 Builder 返回空 batches，则走原有上传逻辑
    if context_result.batches:
        initial_context_batches = context_result.batches

        print(
            f"[CONTEXT] 已启用，准备 "
            f"{len(initial_context_batches)} 个 Context batch"
        )

        for idx, batch in enumerate(initial_context_batches, start=1):
            batch_text = getattr(batch, "text", batch)
            print(
                f"[CONTEXT] Batch {idx}: "
                f"{len(batch_text.encode('utf-8'))} bytes"
            )
    else:
        initial_context_batches = None
        print("[CONTEXT] 没有 Context batches，使用原有文件上传模式")

    # ============================================================
    # 2. 创建 DeepSeek Session
    # ============================================================
    session = DeepSeekSession()

    # ============================================================
    # 3. 创建 LoopAgent
    # ============================================================
    agent = LoopAgent(
        session=session,
        runtime_workspace=PROJECT_ROOT / "agent_idle",
        initial_context_batches=initial_context_batches,
    )

    agent.start()

    # ============================================================
    # 4. 启动原有自我改进循环
    # ============================================================
    agent.self_improve_loop_enhanced(
        task_description=TASK_DESC,
        max_iter=5,
    )


if __name__ == "__main__":
    main()
