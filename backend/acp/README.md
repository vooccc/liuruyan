# backend/acp/

柳如烟 ACP（Agent Client Protocol）集成。

> **设计文档**：[`docs/acp_integration_design.md`](../../docs/acp_integration_design.md)
> **协议参考**：`references/acp-spec`（Zed 官方）
> **实现参考**：`references/llm-adapters`、`references/acp-specs`

## 当前状态

| Phase | 内容 | 状态 |
|---|---|---|
| 0 | 调研 + 设计文档 | ✅ |
| 1 | 骨架 + initialize/authenticate/new_session（echo prompt） | 🚧 进行中 |
| 2 | 文本流式 prompt（接 LLMClient） | ⏳ |
| 3 | 工具循环 + event_translator | ⏳ |
| 4 | 反向能力 + permission | ⏳ |
| 5 | 方向 B 客户端 + delegate_acp_agent | ⏳ |
| 6 | 稳定化 + slash commands + session/load + 配置 UI | ⏳ |

## 启动方式

**Server 模式**（被 Zed 等调用）：

```bash
python -m backend.acp.server
```

**Client 模式**（在柳如烟内部召唤外部 agent，方向 B）：
不直接 CLI 调，由 LLM 通过 `delegate_acp_agent` 工具触发（Phase 5+）。

## 在 Zed 里配置（Phase 1 完成后可试）

`~/.config/zed/settings.json`：

```jsonc
{
  "agent_servers": {
    "liuruyan": {
      "command": "<path-to-python>",
      "args": ["-m", "backend.acp.server"],
      "cwd": "<path-to-柳如烟>",
      "env": {
        "OPENAI_API_KEY": "...",
        "ANTHROPIC_API_KEY": "..."
      }
    }
  }
}
```

## 开发约定

- 调试：所有日志走 `logger.info/debug`，**禁止** print 到 stdout（污染 JSON-RPC 帧）
- 单元测试：`backend/tests_core/test_acp_*.py`
- 字段名：本仓库内部一律 snake_case；只在 schema model 序列化层做 camelCase 转换（pydantic alias 自动处理）

## 红线汇总

详见设计文档 §8。三条最关键的：

1. **stdout 隔离**：`serve.py` 启动第一件事就是把 logging 改到 stderr
2. **单例不加实例变量**：用 ContextVar
3. **subprocess 用 AsyncExitStack 兜清理**：方向 B 的连接管理必须能 graceful 关闭
