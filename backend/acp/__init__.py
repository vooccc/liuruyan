"""柳如烟 ACP (Agent Client Protocol) 集成模块。

设计文档：@_docs/acp_integration_design.md

子模块：
- server/  方向 A：把柳如烟暴露成 ACP server，被 Zed 等编辑器宿主调用
- client/  方向 B：柳如烟当 ACP client，召唤外部 agent（Claude Code 等）当角色
- tools/   方向 B 的 LLM 入口工具 delegate_acp_agent

红线（实施时务必遵守）：
1. stdout 只能输出 ACP JSON-RPC 帧，任何 print/log 必须走 stderr（serve.py 启动第一步重定向）
2. 不允许给 LLMClient / SessionManager / WorkerScheduler / SSEManager 等单例加实例变量，
   需要"当前 ACP session"用 ContextVar，参考 backend/llm_client.py:_current_session_id_var
3. 跟 backend/mcp/ 的成熟范式对齐：插件式独立子包，核心代码零修改
"""

__all__ = []
