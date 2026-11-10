# mini_nanobot 开发约定

本项目已裁剪为单一中文编程助手。主要学习路径是 cli.py → server.py / agent.py → runner.py → tools/；任务图协议在 task_graph.py，调度在 subagents.py，模型在 provider.py，会话在 session.py，上下文在 context.py。

- 界面、提示词、文档和主要注释使用中文；Python/TypeScript 标识符与标准协议字段使用常见英文。
- 执行、权限、模型调用和持久化属于 Python 后端，前端只负责交互与事件展示。
- 计划须整批校验后再启动；等待依赖不能占并发名额。交接的文件和命令证据来自工具，角色的完成声明不能冒充机器验收。
- 文件工具必须经过 security.resolve_path，检查解析后的符号链接边界。探索与审查角色只提供只读工具。
- Shell 的目录检查和命令过滤属于应用层限制。配置 seatbelt/bwrap 后必须成功启动隔离，不得失败后无隔离执行。
- Shell 子进程不得继承模型密钥。停止与超时必须清理进程组。
- 工具按 base.Tool 实现，并在 tools.registry.build_tools 中显式注册；不要重新引入自动插件扫描、渠道或自更新系统。
- 配置和旧版数据不得删除；精简版使用独立 mini_sessions.sqlite3。API、网页和日志不得回显密钥。
- 会话写入必须通过 SessionStore 的 SQLite 事务。恢复遇到未配对工具记录时先检查实际结果，不能自动重放未知操作。
- 新测试应保护真实用户路径或重要权限边界；不要测试实现细节。不要运行 ruff format，不做无关机械改动。
- 修改后按需运行 pytest、ruff check、scripts/check_boundaries.py 和前端 npm run build / npm test。
