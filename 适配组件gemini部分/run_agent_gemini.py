"""
LoopAgent Gemini 启动器

用法：
    python run_agent_gemini.py
"""

from pathlib import Path
import sys
import configparser

# 确保能导入同级目录的模块
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loop_agent_core import LoopAgent
from loop_agent_gemini import GeminiSession
from loop_agent_context import build_context_from_file

# ============================================================
# 任务描述
# ============================================================
TASK_DESC = """
将当前 Seed 改造为一个能够真正从目标网页获取并保存有意义网站信息的程序。

要求：
保留 Seed 当前已经验证可用的功能。
browser.py、database.py、extractor.py 为外置模块，不允许修改。
可以使用 Seed 已生成的 DOM Skeleton、运行结果及其他 current/ 数据辅助分析页面结构。
最终程序必须能够实际运行，并产生可用的结果。
"""

def main():
    # ============================================================
    # 1. 创建 Context
    # ============================================================
    context_config_path = PROJECT_ROOT / "context_config.txt"
    initial_context_batches = None
    
    if not context_config_path.exists():
        print("[CONTEXT] context_config.txt 不存在，将使用原有文件上传模式")
    else:
        # 正确读取 enabled 参数
        parser = configparser.ConfigParser()
        try:
            parser.read(context_config_path, encoding="utf-8")
            is_enabled = parser.getboolean("context", "enabled", fallback=False)
        except Exception as exc:
            print(f"[CONTEXT] 读取配置文件失败 ({exc})，默认禁用 Context 模式")
            is_enabled = False

        if not is_enabled:
            print("[CONTEXT] 配置文件中 enabled=false，禁用 Context 模式，使用原有物理文件上传")
        else:
            context_result = build_context_from_file(context_config_path)

            if not context_result.success:
                raise RuntimeError(f"Context 构建失败：{context_result.error}")

            # Context 构建成功且存在 batches 时，启用 Context
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
                print("[CONTEXT] 没有 Context batches，使用原有文件上传模式")

    # ============================================================
    # 2. 创建 Gemini Session
    # ============================================================
    print("[RUNNER] 初始化 Gemini Session...")
    session = GeminiSession()

    # ============================================================
    # 3. 创建 LoopAgent
    # ============================================================
    print("[RUNNER] 初始化 LoopAgent...")
    agent = LoopAgent(
        session=session,
        runtime_workspace=PROJECT_ROOT / "agent_idle",
        initial_context_batches=initial_context_batches,
    )

    # 启动浏览器并连接 Gemini
    agent.start()

    # ============================================================
    # 4. 启动自我改进循环
    # ============================================================
    print("[RUNNER] 启动自我改进循环...")
    try:
        agent.self_improve_loop_enhanced(
            task_description=TASK_DESC,
            max_iter=5,  # 可根据需要调整最大迭代次数
        )
    finally:
        # 无论成功或异常，最后安全关闭浏览器
        agent.close()

if __name__ == "__main__":
    main()
