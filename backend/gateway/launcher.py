"""
Gateway Launcher Snippets（M5）。

给主流外部 CLI 生成"指向本 Gateway"的启动命令片段。

设计取舍：**只生成命令，不写用户配置文件**。
- 优点：零侵入（不改 ~/.claude/settings.json / ~/.codex/config.toml 等）；token 旋转后用户重新复制即可。
- 缺点：每次开终端需要带前缀 env；用户嫌烦时可以改成把 env 写到 shell rc 里。

未来想加"一键写入配置文件"模式，写个 launcher_writer.py + 备份/还原 API；不要直接膨胀本模块。

支持的 CLI（现阶段）：
- claude-code: Anthropic Messages 协议 → 用 /gateway 路径
- codex: OpenAI Chat 协议 → 用 /gateway/v1
- generic_openai: 给任意 OpenAI 兼容 CLI 看的环境变量样例
- generic_anthropic: 给任意 Anthropic 兼容 CLI 看的环境变量样例

参考：
- cc-switch `apply_claude_takeover_fields`（_research/cc-switch-main 里 Claude env 字段名）
- Codex `~/.codex/config.toml` + `auth.json` schema（同上）
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List


@dataclass
class LauncherSnippet:
    id: str  # 稳定 id，前端拿来 key
    name: str  # 给用户看的名字
    description: str  # 一行简短说明
    protocol: str  # "anthropic_messages" | "openai_chat"
    # 启动命令（POSIX shell 风格；Windows 用户复制后基本能跑，cmd 需要改 SET 才行——前端额外提供 powershell 一份）
    posix_command: str
    # PowerShell 风格命令（Windows 用户友好）
    powershell_command: str
    # 用户需要补的字段：模型名（model_id）等
    placeholders: List[str]
    # 备注：哪些 transport / 模型可用 / 配置 GUI 路径等
    notes: List[str]
    # 是否可直接在新终端窗口里跑（True = CLI 命令；False = 仅 GUI 配置说明，启动按钮隐藏）
    # 默认 True；GUI 类 launcher（generic_*_gui）显式置 False
    runnable: bool = True


def _quote_posix(value: str) -> str:
    """简易 shell 引号；token 不会含 ' 但稳妥起见。"""
    if "'" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return f"'{value}'"


def _quote_powershell(value: str) -> str:
    """PowerShell 单引号字符串内不解析变量；token 不含 ' 但 escape 一下。"""
    return "'" + value.replace("'", "''") + "'"


def build_launchers(*, openai_base_url: str, anthropic_base_url: str, token: str) -> List[LauncherSnippet]:
    """构造所有 launcher snippet。所有调用方应通过此函数获取，避免散落的 hardcoded URL。

    入参：
      openai_base_url    形如 http://127.0.0.1:8001/gateway/v1
      anthropic_base_url 形如 http://127.0.0.1:8001/gateway        （注意不带 /v1）
      token              gateway token，会按字面量嵌入命令

    返回：固定顺序的 LauncherSnippet 列表（前端按顺序展示）
    """
    tok_p = _quote_posix(token)
    tok_ps = _quote_powershell(token)
    oai_p = _quote_posix(openai_base_url)
    oai_ps = _quote_powershell(openai_base_url)
    ant_p = _quote_posix(anthropic_base_url)
    ant_ps = _quote_powershell(anthropic_base_url)

    out: List[LauncherSnippet] = []

    # ─── Claude Code ───
    # Anthropic 官方 CLI 看 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY）
    # 我们用 ANTHROPIC_AUTH_TOKEN（更准确：这是 Anthropic 客户端 SDK 优先级最高的环境变量）
    out.append(LauncherSnippet(
        id="claude_code",
        name="Claude Code",
        description="Anthropic 官方编程 CLI；走 Anthropic Messages 协议。",
        protocol="anthropic_messages",
        posix_command=(
            f"ANTHROPIC_BASE_URL={ant_p} "
            f"ANTHROPIC_AUTH_TOKEN={tok_p} "
            f"claude"
        ),
        powershell_command=(
            f"$env:ANTHROPIC_BASE_URL={ant_ps}; "
            f"$env:ANTHROPIC_AUTH_TOKEN={tok_ps}; "
            f"claude"
        ),
        placeholders=[],
        notes=[
            "需要先 `npm i -g @anthropic-ai/claude-code` 装好 claude CLI。",
            "Claude Code 不需要在命令里指定 model；它有自己的模型选择 UI。M2-B 后跨协议互转开启，任何 transport 的 provider 都能用（openai_chat 会被自动翻译为 Anthropic Messages 协议）。",
            "如果你想固定走某个 provider，model 字段填 `<provider_id>:<vendor_model>` 格式（在柳如烟内 Gateway 面板「模型清单」里查）。",
        ],
    ))

    # ─── Codex ───
    # OpenAI Codex CLI 走 OPENAI_BASE_URL + OPENAI_API_KEY，
    # 但模型仍由 ~/.codex/config.toml 里的 model = "..." 决定。
    # 我们让用户用 OPENAI_BASE_URL 这条 env 接管 base，model 自己在 config.toml 里写好。
    out.append(LauncherSnippet(
        id="codex",
        name="OpenAI Codex CLI",
        description="OpenAI 官方编程 CLI（codex 0.30+）。柳如烟 gateway 已实现 Responses↔Chat 翻译，可用任意 OpenAI/Anthropic 上游。",
        protocol="openai_chat",
        posix_command=(
            f"OPENAI_BASE_URL={oai_p} "
            f"OPENAI_API_KEY={tok_p} "
            f"codex"
        ),
        powershell_command=(
            f"$env:OPENAI_BASE_URL={oai_ps}; "
            f"$env:OPENAI_API_KEY={tok_ps}; "
            f"codex"
        ),
        placeholders=[],
        notes=[
            "Codex 0.30+ 强制 Responses API。柳如烟 gateway 内部翻译层把 /v1/responses 转成 chat completions，上游不受影响。",
            "model 字段填 alias 名或 `<provider_id>:<vendor_model>`，跨协议互转可路由 Anthropic 上游。",
            "已知失真：Responses 内置工具（web_search/file_search/code_interpreter）会被拒为 400；普通对话与 function calling 完整支持。",
        ],
        runnable=True,
    ))

    # ─── 通用 OpenAI 风格客户端（GUI 类，比如 Cline / Cursor / Continue.dev） ───
    out.append(LauncherSnippet(
        id="generic_openai_gui",
        name="Cline / Cursor / Continue.dev（GUI 配置）",
        description="VSCode 类编程助手；在它们的设置 GUI 里填两个字段。",
        protocol="openai_chat",
        posix_command=(
            f"# 不需要终端命令——在 VSCode 扩展设置里填：\n"
            f"#   OpenAI API Base / Base URL = {openai_base_url}\n"
            f"#   OpenAI API Key             = {token}\n"
            f"#   Model                      = <provider_id>:<vendor_model>"
        ),
        powershell_command=(
            f"# 不需要终端命令——在 VSCode 扩展设置里填：\n"
            f"#   OpenAI API Base / Base URL = {openai_base_url}\n"
            f"#   OpenAI API Key             = {token}\n"
            f"#   Model                      = <provider_id>:<vendor_model>"
        ),
        placeholders=["<provider_id>:<vendor_model>"],
        notes=[
            "Cline：在 VSCode 设置 → Cline → 选 \"OpenAI Compatible\" → 填上述三项。",
            "Cursor：Settings → Models → 添加自定义模型 → OpenAI 兼容 → 填上述。",
            "model 字段必须用 `<provider_id>:<vendor_model>` 命名空间格式。",
            "M2-B 后跨协议互转开启，可路由到任何 transport 的 provider（包括 Anthropic Claude）。",
        ],
        runnable=False,  # GUI 配置型，启动按钮不显示
    ))

    # ─── 通用 Anthropic 风格客户端 ───
    out.append(LauncherSnippet(
        id="generic_anthropic_gui",
        name="Cherry Studio / 任意 Anthropic 客户端（GUI 配置）",
        description="自带 Anthropic 模式的桌面客户端；在它们的设置 GUI 里填两个字段。",
        protocol="anthropic_messages",
        posix_command=(
            f"# 不需要终端命令——在客户端的 Anthropic provider 设置里填：\n"
            f"#   Base URL / API Host = {anthropic_base_url}\n"
            f"#   API Key             = {token}\n"
            f"#   Model               = <provider_id>:<vendor_model>"
        ),
        powershell_command=(
            f"# 不需要终端命令——在客户端的 Anthropic provider 设置里填：\n"
            f"#   Base URL / API Host = {anthropic_base_url}\n"
            f"#   API Key             = {token}\n"
            f"#   Model               = <provider_id>:<vendor_model>"
        ),
        placeholders=["<provider_id>:<vendor_model>"],
        notes=[
            "Cherry Studio：设置 → 模型服务 → 添加 → Anthropic → API Host 填上述 Base URL。",
            "客户端会自己拼 /v1/messages，所以 Base URL 不要带 /v1 后缀。",
            "M2-B 后跨协议互转已上线：openai_chat provider 也可以用，柳如烟会自动从 Anthropic Messages 翻译为 OpenAI Chat。",
        ],
        runnable=False,  # GUI 配置型，启动按钮不显示
    ))

    return out


def serialize_launchers(snippets: List[LauncherSnippet]) -> List[dict]:
    return [asdict(s) for s in snippets]


__all__ = ["LauncherSnippet", "build_launchers", "serialize_launchers"]
