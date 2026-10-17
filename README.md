# simple_agent_v0.2.0

LoopAgent 🔄 v0.2
LoopAgent 是一个由大语言模型（LLM）驱动的网页自动化与 RPA（机器人流程自动化）框架。

简单来说：它可以根据任务要求自动编写、执行 Python 代码来完成一些基础任务（如数据清洗，网页爬取）。 无论你是需要写爬虫，还是想让 AI 帮你自动填表，LoopAgent 都能为你提供一个闭环的自我进化环境。

✨ 核心特性
🛡️ 风控应对 (Anti-Bot)：底层基于 Patchright 驱动，可绕过基础的自动化检测机制（直接用playwight可能导致的网站卡死）。

⚡ 超大文本“秒”注入：大模型需要读取海量的网页源码？我们弃用了传统的模拟输入，利用跨平台系统剪贴板（Windows / Linux Wayland）与底层 JS 劫持，实现几十万字上下文的瞬间载入，缓解前端卡顿问题。

🐧 跨平台：兼容 Windows 和 Linux 桌面环境。

🔒 安全审查：内置基础的静态代码审查机制，防止高危系统命令意外执行。

⚙️ 工作原理 (Pipeline)
LoopAgent 的运行就像一个自动流水线：

Seed 阶段：预先设计好所需的代码，无需从零开始。

LLM 交互：将当前网页结构和任务目标发送给大模型（如 DeepSeek）。

生成与执行：大模型生成 Python 代码，LoopAgent 在相对安全的临时工作区（agent_idle/）内自动运行。

自我进化 (Loop)：如果代码报错或未达到预期，Agent 会自动将错误日志反馈给大模型进行“返厂维修”，直到任务成功。

🚀 快速开始
环境准备
操作系统：Windows 10/11 或 Linux (支持 Wayland/X11)

Python 版本：Python 3.10+

所需库:requirements.txt

浏览器：系统中已安装Microsoft Edge

⚠️ 安全与极客模式 (Low Security Mode)
大模型生成的代码具有不可控性。LoopAgent 默认开启最高级别的安全审查，拦截任何涉及系统级操作、跨目录文件读写等高危代码。

如果你是进阶开发者，希望大模型拥有完全的系统控制权以进行深度实验，可以在项目根目录创建一个 safety_check.txt 文件：
当内容变为：
[safety]
strict_mode = false

警告：关闭安全审查后，Agent 将无条件执行大模型生成的任何代码。请务必仅在虚拟机 (VM) 或隔离的 Docker 沙盒环境中使用此模式！ 损坏宿主机系统后果自负。

📁 目录结构说明
loop_agent_core.py：核心流程控制器（管理 Prompt、自我进化循环）。

loop_agent_support.py：底层支撑组件（代码执行器、安全沙盒、本地文件数据库）。

agent_idle/：Agent 运行生成的代码和临时文件的隔离沙盒。

虚拟机与内部相关配置
Ubuntu 26.04.1 LTS

Python 3.14.x

Chrome for Testing 113.x

VirtualBox 7.2.16

📄 开源协议
本项目采用 MIT License 开源协议。
