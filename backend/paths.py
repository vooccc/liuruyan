"""统一数据目录入口。

所有用户数据、配置、凭证、运行时产物的落地路径都从这里取。

- 打包后（`sys.frozen` 为真）：`~/.liuruyan/`，由 PyInstaller exe 首启时创建
- 开发模式（源码直接运行）：项目根，避免和 msi 已安装版本共享同一份数据互相覆盖
- 任何环境都可用 `LIURUYAN_DATA_DIR` 环境变量覆盖

目录名集中在 APP_DIR_NAME —— 以后想改名只改这一处。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

APP_DIR_NAME = ".liuruyan"


def _is_frozen() -> bool:
    """检测是否在打包环境（PyInstaller sys.frozen 或 Nuitka onefile）中运行。"""
    if getattr(sys, "frozen", False):
        return True
    # Nuitka onefile 解压到 Temp\onefile_XXX 目录
    try:
        if "onefile_" in os.path.abspath(__file__).lower():
            return True
    except Exception:
        pass
    return False


def _resolve_data_dir() -> Path:
    env_override = os.environ.get("LIURUYAN_DATA_DIR")
    if env_override:
        return Path(_strip_trailing_whitespace(env_override)).expanduser()
    if not _is_frozen():
        # 开发模式（源码直接运行）：存放在独立的 ~/.liuruyan-dev/ 用户目录，
        # 与正式安装版 (~/.liuruyan/) 物理隔离，且彻底避免向 Git 源码根目录写入任何用户凭据与动态数据。
        return Path.home() / f"{APP_DIR_NAME}-dev"
    return Path.home() / APP_DIR_NAME


def _strip_trailing_whitespace(path_str: str) -> str:
    """逐段清理路径分隔符之间的尾部空格。

    例: 'D:\\\\AI_Project\\\\柳如烟 \\\\backend' -> 'D:\\\\AI_Project\\\\柳如烟\\\\backend'
    """
    sep = os.sep
    parts = path_str.split(sep)
    cleaned = [seg.rstrip() for seg in parts]
    return sep.join(cleaned)


def data_dir() -> Path:
    """用户数据根目录。首次调用时自动创建。"""
    path = _resolve_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def memory_dir() -> Path:
    path = data_dir() / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir() -> Path:
    path = memory_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def agents_dir() -> Path:
    path = data_dir() / "agents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent_sessions_dir(agent_id: str) -> Path:
    path = agents_dir() / agent_id / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent_session_dir(agent_id: str, session_id: str) -> Path:
    path = agent_sessions_dir(agent_id) / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def agent_attachments_dir(agent_id: str) -> Path:
    """图片等多模态附件存储目录（per-agent，sha256 内容寻址）。

    用途：read_file 读图片后 transcript 写盘前，把 base64 像素抽出来按
    sha256 命名持久化到本目录，transcript 里只存 image_ref 引用块。
    决策见 docs/architecture_spec.md D5/D6。
    """
    path = agents_dir() / agent_id / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def council_dir() -> Path:
    path = data_dir() / "council"
    path.mkdir(parents=True, exist_ok=True)
    return path


def council_rooms_dir() -> Path:
    path = council_dir() / "rooms"
    path.mkdir(parents=True, exist_ok=True)
    return path


def council_room_dir(room_id: str) -> Path:
    path = council_rooms_dir() / room_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def council_room_session_dir(room_id: str, session_id: str) -> Path:
    path = council_room_dir(room_id) / "sessions" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def orchestrator_data_dir() -> Path:
    path = council_dir() / "orchestrator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def skills_dir() -> Path:
    path = data_dir() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mcp_dir() -> Path:
    """MCP 子系统数据根（OAuth token、审计、缓存等）。"""
    path = data_dir() / "mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mcp_config_path() -> Path:
    """MCP servers 配置文件（兼容 Claude Desktop 同名格式）。"""
    return data_dir() / "mcp_servers.json"


def user_profile_path() -> Path:
    return data_dir() / "user_profile.json"


def acp_external_agents_config_path() -> Path:
    """ACP 外部 agent 的用户偏好（每个 agent 选哪个 provider 借 key）。

    schema: {"providers": {"claude-code": "custom-llmapi-pro", ...}}
    """
    return data_dir() / "acp_external_agents.json"


def models_json_path() -> Path:
    return data_dir() / "models.json"


def models_secrets_path() -> Path:
    return data_dir() / "models.secrets.json"


def models_example_path() -> Path:
    """打包资源中的 models.json.example。优先 PyInstaller _MEIPASS，其次开发源码。"""
    bundled = _bundle_resource("models.json.example")
    if bundled is not None and bundled.exists():
        return bundled
    return Path(__file__).parent.parent / "models.json.example"


def tasks_json_path() -> Path:
    return data_dir() / "tasks.json"


def runtime_budget_path() -> Path:
    return data_dir() / "runtime_budget.json"


def guardian_config_path() -> Path:
    return data_dir() / "guardian_config.json"


def backend_backups_dir() -> Path:
    """写前备份根目录。每次 write_file/patch/notebook_edit 在此下建 <UTC时间戳>/<相对路径> 副本。"""
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    """用户聊天输入框附件上传目录。

    前端 📎 按钮选完文件 POST 到 /chat/upload，后端落到这里返绝对路径，
    LLM 通过 read_file 工具读取（PDF/Office/.ipynb/image 都已支持）。
    单人桌面应用，不做 per-session 隔离；积累过多由用户/cron 手动清理。
    """
    path = data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backend_writes_jsonl_path() -> Path:
    """JSONL 审计日志：每次 backend 文件被角色写入的记录。"""
    return data_dir() / "backend_writes.jsonl"


def tool_stats_jsonl_path() -> Path:
    return data_dir() / "tool_stats.jsonl"


def frontend_events_jsonl_path() -> Path:
    """前端遥测落盘文件：dev console / fetch / 全局错误 / unhandledrejection。

    设计参考：backend/routes/debug_routes.py
    单文件 JSONL，超过 ~50MB 由 route 自动 rotate 一次（保留 .1 历史）。
    AI 通过 read_frontend_events 工具读取——绝大多数前端 bug 排查（"console 报啥"
    "哪个 fetch 4xx" "组件崩了没"）的入口数据全在这。
    """
    return data_dir() / "frontend_events.jsonl"


def cron_jobs_path() -> Path:
    return data_dir() / "cron_jobs.json"


def embedding_config_path() -> Path:
    """Embedding 长期记忆配置文件（向量化 recall_memory 的 provider/model 设定）。"""
    return data_dir() / "embedding_config.json"


def search_config_path() -> Path:
    """搜索引擎配置（设置→辅助功能→搜索引擎）。目前存 Tavily API key。

    含密钥，已 gitignore，不入库。柳如烟全局 web_search 与内容模块「联网搜资料」
    共用——二者都 os.getenv("TAVILY_API_KEY")，所以 UI 配的 key 会在保存/启动时
    灌进 os.environ。砍除时只删本文件 + search_config.py + 路由注册即可。
    """
    return data_dir() / "search_config.json"


def fast_context_config_path() -> Path:
    """Fast Context (代码探索) 配置（设置→辅助功能→代码探索）。

    支持当前会话模型跟随、自选模型服务商模型、Devin sessionToken 凭据。
    含密钥，已 gitignore，不入库。
    """
    return data_dir() / "fast_context_config.json"


def wechat_mp_config_path() -> Path:
    """微信公众号草稿箱集成配置（appid/secret/默认封面）。

    环境变量（WECHAT_APPID 等）未配时的兜底来源。含密钥，已 gitignore，不入库。
    示例见项目根 wechat_mp.example.json。
    """
    return data_dir() / "wechat_mp.json"


def computer_use_policy_path() -> Path:
    """Computer Use 策略持久化（总开关 + 限流 + 黑白名单）。"""
    return data_dir() / "computer_use_policy.json"


def computer_use_audit_path() -> Path:
    """Computer Use 审计日志（每个 action 的 forensic trail）。"""
    return data_dir() / "computer_use_audit.jsonl"


# ────────────────────── LLM Gateway 子系统（可拆插件） ──────────────────────
# Gateway 是柳如烟的"对外 LLM 网关"：外部客户端（Cursor/Claude Code/Codex
# 等）通过本地 HTTP endpoint + token 调用柳如烟里所有 provider。
# 设计规范：docs/GATEWAY.md；隔离规范：网关端口隔离与安全沙箱规范
# 子系统数据文件全部聚集在 ~/.liuruyan/gateway_*，砍除时只删这三个文件 + backend/gateway/。

def gateway_config_path() -> Path:
    """Gateway 配置：{enabled, host, port, cors, ...}。默认 enabled=false。"""
    return data_dir() / "gateway_config.json"


def gateway_tokens_path() -> Path:
    """Gateway 令牌仓库（多 token + 配额限流）。

    schema 见 ``backend/gateway/tokens.py`` 顶部 docstring。明文存储 + POSIX 0600
    权限——本地软件场景下 hash 防护是安全表演，详见 ``docs/GATEWAY.md``。
    首次启动若该文件不存在，``tokens.bootstrap_if_empty()`` 会自动建一条"默认令牌"。
    """
    return data_dir() / "gateway_tokens.json"


def gateway_aliases_path() -> Path:
    """Gateway 别名映射 + fallback 链（M3）。

    格式见 docs/GATEWAY.md §2。文件不存在视为 {"aliases": {}}。
    """
    return data_dir() / "gateway_aliases.json"


def gateway_audit_path() -> Path:
    """Gateway 调用审计 JSONL：每条 request 一行（token_id / model / provider / 耗时 / status）。"""
    return data_dir() / "gateway_audit.jsonl"


def gateway_installer_state_path() -> Path:
    """Gateway M5 一键 CLI 接入状态：记录我们写过哪些文件 + hash + 备份路径，方便 uninstall。

    格式：{"installers": {"<installer_id>": {target_path, last_install_at, written_hash, backup_paths: []}}}
    文件不存在视为空状态。详见 docs/GATEWAY.md。
    """
    return data_dir() / "gateway_installers.json"


# ────────────────────── Remote Access 子系统（M8a 可拆插件） ──────────────────────
# 远程访问 + 单端口 + PWA：让用户在外部设备（手机/出差笔记本）通过 Tailscale 等私网
# 访问完整柳如烟 Web UI；详见 docs/REMOTE_SPEC.md ；隔离红线：远程安全隔离规范
# 砍除时只删 backend/remote/ + 此 helper + ~/.liuruyan/remote.json + 前端配套文件即可。

def remote_config_path() -> Path:
    """Remote Access 配置：{enabled, host, pwa_enabled, ...}。默认 enabled=false。

    schema 见 ``backend/remote/config.py``。文件不存在视为默认（disabled, 127.0.0.1）。
    """
    return data_dir() / "remote.json"


def agent_embedding_dir(agent_id: str) -> Path:
    """每个 agent 的 embedding 索引根目录（向量 + ids + meta）。

    与 jsonl 同级——jsonl 是真相源，embedding 是衍生缓存，丢了能 rebuild。
    """
    path = agents_dir() / agent_id / "memory" / "embeddings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def shared_memory_dir() -> Path:
    """跨角色共享记忆根目录（save_memory(scope='shared') 落在这里）。

    设计动机：用户跟柳如烟说的"项目秘密"应该角色私有；但"这个项目用 React 18"
    这种公共事实所有角色共享。Cascade 的 corpus 单维度做不到这点，柳如烟有
    10 角色所以可以双轨。
    """
    path = memory_dir() / "shared"
    path.mkdir(parents=True, exist_ok=True)
    return path


def shared_embedding_dir() -> Path:
    """跨角色共享记忆的 embedding 索引目录。"""
    path = shared_memory_dir() / "embeddings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_project_root() -> Path:
    """项目根（仅用于老数据迁移场景）。打包后返回 exe 所在目录。"""
    if _is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def _bundle_resource(relative: str) -> Optional[Path]:
    """访问打包时附带的资源文件（PyInstaller 或 Nuitka）。未冻结时返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        return Path(base) / relative
    if _is_frozen():
        # Nuitka onefile：不设 _MEIPASS，__file__ 在解压目录里，
        # 父目录的父目录就是解压根（_entry.py → backend/ → 解压根）
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = Path(base) / relative
        if candidate.exists():
            return candidate
    return None


def seed_models_json_if_missing() -> None:
    """首次启动：若 ~/.liuruyan/models.json 不存在，从打包模板拷贝一份。"""
    target = models_json_path()
    if target.exists():
        return
    source = models_example_path()
    if not source.exists():
        return
    try:
        shutil.copyfile(source, target)
    except OSError:
        pass


def _templates_source() -> Optional[Path]:
    """打包资源中的 agents/templates 目录。优先 PyInstaller _MEIPASS，其次开发源码。"""
    bundled = _bundle_resource("agents/templates")
    if bundled is not None and bundled.exists():
        return bundled
    dev = Path(__file__).parent.parent / "agents" / "templates"
    if dev.exists():
        return dev
    return None


def seed_templates_if_missing() -> None:
    """首次启动：若 ~/.liuruyan/agents/templates/ 为空，从打包资源整套拷贝。
    若已存在，则将官方模板中新增的工具（如 fast_context_search）安全增量同步到既有模板 meta.json。
    """
    target = agents_dir() / "templates"
    src = _templates_source()
    if src is None:
        return
    if not target.exists() or not any(target.iterdir()):
        _seed_dir_if_empty(target, src)
        return

    # 已有模板时的平滑增量升级：补齐官方模板新增的工具声明
    try:
        import json
        for item in src.iterdir():
            if not item.is_dir():
                continue
            dst_meta_file = target / item.name / "meta.json"
            src_meta_file = item / "meta.json"
            if dst_meta_file.exists() and src_meta_file.exists():
                try:
                    dst_meta = json.loads(dst_meta_file.read_text(encoding="utf-8-sig"))
                    src_meta = json.loads(src_meta_file.read_text(encoding="utf-8-sig"))
                    src_tools = src_meta.get("tools")
                    dst_tools = dst_meta.get("tools")
                    if isinstance(src_tools, list) and isinstance(dst_tools, list):
                        changed = False
                        for t in src_tools:
                            if t not in dst_tools:
                                dst_tools.append(t)
                                changed = True
                        if changed:
                            dst_meta["tools"] = dst_tools
                            dst_meta_file.write_text(
                                json.dumps(dst_meta, ensure_ascii=False, indent=2),
                                encoding="utf-8"
                            )
                except Exception:
                    pass
    except Exception:
        pass


def _skills_source() -> Optional[Path]:
    """打包资源中的 skills 目录。优先 PyInstaller _MEIPASS，其次开发源码。"""
    bundled = _bundle_resource("skills")
    if bundled is not None and bundled.exists():
        return bundled
    dev = Path(__file__).parent.parent / "skills"
    if dev.exists():
        return dev
    return None


def seed_skills_if_missing() -> None:
    """首次启动：若 ~/.liuruyan/skills/ 为空，从打包资源整套拷贝。"""
    _seed_dir_if_empty(skills_dir(), _skills_source())


def _seed_dir_if_empty(target: Path, source: Optional[Path]) -> None:
    if source is None:
        return
    if target.exists() and any(target.iterdir()):
        return
    target.mkdir(parents=True, exist_ok=True)
    try:
        for item in source.iterdir():
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copyfile(item, dest)
    except OSError:
        pass
