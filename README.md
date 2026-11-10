# mini_nanobot：中文 AI 编程助手

这是在 nanobot 基础上重新整理的精简学习版，聚焦代码阅读、文件修改、命令验证、多 agent、会话和项目记忆。界面、提示词、主要注释和学习文档使用中文；标准协议字段和函数名保留英文。

## 在 VS Code 中运行

本机已安装依赖并配置好 DeepSeek。打开本目录，在“运行和调试”中选择 **mini_nanobot：完整前后端**，按 **F5**。

也可以在 VS Code 新建终端，执行：

```bash
.venv/bin/python -m mini_nanobot web
```

使用终端输出的本机登录链接。默认页面地址为 `http://127.0.0.1:8765`；链接中的访问码只用于本机页面登录，不是模型密钥。保持终端开启，Ctrl+C 会停止服务、子任务和正在执行的命令。

前端热更新：

```bash
.venv/bin/python -m mini_nanobot web --dev
```

终端编程：

```bash
.venv/bin/python -m mini_nanobot code -w /你的代码目录
.venv/bin/python -m mini_nanobot code -w /你的代码目录 -m '探索项目，修复一个明确的问题并验证'
```

终端会输出会话 ID。使用 `code --session 会话ID` 续接；交互模式输入“退出”结束。同一数据目录同时运行一个网页服务或 CLI，避免两个进程互相误判任务中断。需要独立运行时可指定 `--home /另一个数据目录`。

## 第一次安装

需要 Python 3.11+、Node.js 18+ 和 npm。推荐使用 uv：

```bash
uv sync --extra dev
cd webui
npm ci
npm run build
cd ..
cp .env.example .env
# 编辑 .env 后执行：
.venv/bin/python -m mini_nanobot init --if-missing
.venv/bin/python -m mini_nanobot web
```

`.env` 的核心配置如下，密钥不要提交到 Git：

```dotenv
MINI_NANOBOT_MODEL=deepseek-flash
MINI_NANOBOT_API_BASE=https://api.deepseek.com
MINI_NANOBOT_API_KEY=你的密钥
```

已有环境变量优先于 `.env`。默认继续读取 `~/.mini_nanobot/config.json` 中的核心配置，旧版渠道等字段不再使用。修改 `.env` 后需要重启；界面会提示模型是否已配置。

## 现在只有这条主线

```text
网页 / 终端
    ↓
agent.py：管理会话和任务
    ↓
runner.py：模型 → 工具 → 结果反馈 → 下一轮
    ├── provider.py：DeepSeek / OpenAI 兼容接口
    ├── tools/：文件、搜索、命令、记忆
    └── subagents.py：探索 / 实现 / 审查
```

| 位置 | 内容 |
| --- | --- |
| `mini_nanobot/cli.py` | 初始化、网页和终端启动入口 |
| `mini_nanobot/server.py` | 本地 HTTP API、认证和事件流 |
| `mini_nanobot/agent.py` | 主任务协调、停止和检查点 |
| `mini_nanobot/runner.py` | ReAct 执行循环 |
| `mini_nanobot/provider.py` | 模型请求、流式响应和重试 |
| `mini_nanobot/context.py` | 中文提示词、预算估算和摘要压缩 |
| `mini_nanobot/session.py` | SQLite 会话、任务事件和项目记忆 |
| `mini_nanobot/subagents.py` | 任务依赖调度、生命周期和并发控制 |
| `mini_nanobot/task_graph.py` | 任务计划校验与结构化交接协议 |
| `mini_nanobot/tools/` | 工具接口、显式注册、文件操作和 Shell |
| `mini_nanobot/security.py`、`sandbox.py` | 目录边界和可选系统沙箱 |
| `webui/src/` | 单页中文前端与事件流客户端 |
| `tests/` | 核心行为和权限测试 |

第一次学习从 [架构导读](docs/架构导读.md) 开始，再读 [接口与配置](docs/接口与配置.md)。

## 多 agent 与工具

主 agent 可创建 explorer（探索）、coder（实现）、reviewer（审查）三个角色。探索和审查只能读取、列目录、搜索和读取记忆；实现角色可编辑文件和执行命令。子任务不能继续创建子任务。主 agent 保留直接编程能力，简单任务无需委派。

所有角色共享代码目录和模型配置，默认最多 4 个子任务同时执行。主 agent 必须明确文件归属，避免相互覆盖。`wait=true` 等待当前结果；`wait=false` 允许主任务继续独立工作，在最终答复前会等待并汇总子任务。`send`、`check`、`cancel` 的任务 ID 作用域是当前这次请求。

第一阶段已加入 **依赖任务图与结构化交接**。主 agent 通过 `subagent(action="plan")` 一次提交任务、前置 ID 和验收要求；后端拒绝循环、重复 ID 和不存在的前置任务。无依赖的任务可并发，等待依赖不占执行名额。前置失败、取消或标记待处理时，下游进入“前置阻断”，不调用模型。

在网页中可以直接发送：

> 请使用任务计划，先让探索 agent 定位问题；完成后由实现 agent 修复并运行针对性验证；最后让审查 agent 读取修改后的代码和前置交接，独立复核。明确每项的验收要求，最终核对实际命令记录和未解决问题。

右侧任务卡片会显示前置任务、验收要求、角色汇报、文件工具修改记录、实际命令退出码与未解决问题。交接自动传给下游。角色自报“完成”与实际执行记录分别保存，不等于机器自动验收；命令产生的文件变化不包含在文件工具修改记录中。具体协议与示例见 [接口与配置](docs/接口与配置.md#多-agent-任务计划与交接)。

保留的文件工具为 `read_file`、`list_dir`、`search`、`write_file`、`edit_file`；还有 `exec`、`read_memory`、`remember` 和 `subagent`。全部在 `tools/registry.py` 显式注册，没有插件扫描。项目技能可写成 `.mini_nanobot/skills/*.md`，按需把简短说明加入上下文。

## 数据与执行边界

精简版使用独立的 `~/.mini_nanobot/mini_sessions.sqlite3`，旧版配置、聊天数据库和工作区数据保持原样，不自动迁移或删除。新页面只显示精简版会话。会话检查点和项目记忆通过 SQLite 事务保存；子任务保留状态、依赖和交接事件，不提供完整子任务历史恢复。

上下文按文本长度估算预算，超过预算时通过模型生成摘要，保留当前任务和完整的工具调用组。估算值不是模型的实际计费 Token。中断后会提示先核实工具是否生效，不承诺“恰好执行一次”，不自动重放未知操作。

文件工具会解析符号链接并拒绝工作目录越界，也会拒绝 `.env` 与 Git 内部文件。Shell 保留基本命令过滤、目录检查、超时和进程组取消，并且不向子进程传递模型密钥。应用层检查不能隔离任意 Python/脚本代码；需要更强隔离时配置 macOS Seatbelt 或 Linux bwrap。沙箱配置后失败即停止，不会自动降级。该项目面向本机学习和开发，只绑定回环地址。

## 开发验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check mini_nanobot tests scripts
.venv/bin/python scripts/check_boundaries.py
cd webui
npm test
npm run build
```

## 来源与裁剪范围

来源为 HKUDS/nanobot，基线提交 `3e4068411275fac02fbc109926fffea855a94961`。当前已将编程主线重新组织到 `mini_nanobot/`，保留并适配了上游系统沙箱实现。移除了通用渠道、自动化、远程连接、自更新、多模型 OAuth、文档转换、原生 TUI、MCP、应用商店和复杂前端路由。精简版 API 与旧 WebUI 协议不兼容，网页和后端应一起更新。

源码遵循上游 MIT [LICENSE](LICENSE)，保留 [第三方来源声明](THIRD_PARTY_NOTICES.md)。
