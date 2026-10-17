"""
LoopAgent Grok 启动脚本 (支持 Context 开关)

运行：
    python run_agent_grok.py

"""
from pathlib import Path
import sys
import configparser

# ============================================================
# 项目路径
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ============================================================
# Core / Adapter
# ============================================================

from loop_agent_core import LoopAgent
from loop_agent_grok import GrokSession
from loop_agent_context import build_context_from_file

# ============================================================
# Task
# ============================================================

TASK_DESC = """
将当前 Seed 改造为一个能够真正从目标网页获取并保存有意义网站信息的程序。

要求：
保留 Seed 当前已经验证可用的功能。
browser.py、database.py、extractor.py 为外置模块，不允许修改。
可以使用 Seed 已生成的 DOM Skeleton、运行结果及其他 current/ 数据辅助分析页面结构。
应根据真实页面结构进行改造，而不是仅输出演示性质或静态示例代码。
最终程序必须能够实际运行，并产生可用的爬取结果。
"""

# ============================================================
# Main
# ============================================================

def main():
    # ============================================================
    # 1. 创建 Context (带开关保护)
    # ============================================================
    context_config_path = PROJECT_ROOT / "context_config.txt"
    initial_context_batches = None
    
    if not context_config_path.exists():
        print("[CONTEXT] context_config.txt 不存在，将使用原有文件上传模式")
    else:
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

            if context_result.batches:
                initial_context_batches = context_result.batches
                print(f"[CONTEXT] 已启用，准备 {len(initial_context_batches)} 个 Context batch")
                for idx, batch in enumerate(initial_context_batches, start=1):
                    batch_text = getattr(batch, "text", batch)
                    print(f"[CONTEXT] Batch {idx}: {len(batch_text.encode('utf-8'))} bytes")
            else:
                print("[CONTEXT] 没有 Context batches，使用原有文件上传模式")

    # ============================================================
    # 2. 启动 Session 与 Agent
    # ============================================================
    print("[RUNNER] 初始化 Grok Session...")
    session = GrokSession()

    print("[RUNNER] 初始化 LoopAgent...")
    agent = LoopAgent(
        session=session,
        runtime_workspace=(PROJECT_ROOT / "agent_idle"),
        initial_context_batches=initial_context_batches,
    )

    agent.start()

    try:
        result = agent.self_improve_loop_enhanced(
            task_description=TASK_DESC,
            max_iter=5,
        )
        print("[RUN] 状态:", result.get("status"))
        print("[RUN] 迭代:", result.get("iterations"))
    finally:
        agent.close()

if __name__ == "__main__":
    main()
