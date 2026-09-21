"""
柳如烟智能体 - FastAPI 后端入口（多智能体版）
"""
from fastapi import FastAPI, HTTPException, Body, Request, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Any, Optional, List, Dict
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
import time
import uuid
import sys
from pathlib import Path
from datetime import datetime
import re

# 应用入口统一日志配置：root=INFO（uvicorn 默认不给 root 配 handler，否则
# 启动期 info 横幅会被 lastResort 只输出 WARNING+ 吞掉）。basicConfig 幂等，
# 重复 import / 多次调用是 no-op。三方 client 压到 WARNING 避免噪音。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for _noisy_logger in ("httpx", "httpcore", "openai", "anthropic"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# 添加项目根到路径，支持 `python backend/main.py` 直接运行
sys.path.insert(0, str(Path(__file__).parent.parent))

# ★ 关键：把 backend/ 本身从 sys.path 里剔除（必须在所有 import 之前！）。
# Python 启动脚本时会把脚本所在目录（backend/）自动放进 sys.path[0]，导致
# 任何 `from mcp import X` 命中我们的 `backend/mcp/` 子包而非第三方 mcp SDK。
# 见：notion server 启动时 `StdioServerParameters` ImportError。
_backend_dir = Path(__file__).parent.resolve().as_posix()
sys.path[:] = [p for p in sys.path if Path(p).resolve().as_posix() != _backend_dir]

# 防御：如果有更早的 import 已经把我们的 backend/mcp/ 错误地缓存为 'mcp'，清掉。
# 触发条件：某个 .pth 文件或 site-packages 钩子在 main.py 之前 import 过 mcp。
_stale_mcp = sys.modules.get("mcp")
if _stale_mcp is not None:
    _mcp_file = getattr(_stale_mcp, "__file__", "") or ""
    if _backend_dir in Path(_mcp_file).resolve().as_posix() if _mcp_file else False:
        # 移除 mcp 及其所有子模块缓存，强制重新 import
        for _k in [k for k in sys.modules if k == "mcp" or k.startswith("mcp.")]:
            sys.modules.pop(_k, None)

from backend.agent import get_agent, clear_agent_cache
from backend.config import get_config_loader
from backend.memory_manager import get_memory_manager
from backend.agent_config import get_agent_manager
from backend.emotion_updater import get_emotion_updater
from backend.mask_system import get_mask_system
from backend.session import get_session_manager, DEFAULT_EMOTION, DEFAULT_SESSION_BASE_DIR
from backend.orchestrator import (
    get_orchestrator, VALID_SCENE_TYPES, SCENE_TYPE_LABELS,
    BOND_MILESTONES, EXCLUSIVE_EVENTS, WORK_ACHIEVEMENTS,
    END_RESOLVED, END_CANCELLED, END_TTL_EXPIRED, END_USER_INTERRUPT,
)
from backend.runtime_protocol import (
    build_runtime_notice_payload,
    build_runtime_transport_event as _build_runtime_transport_event,
    iter_runtime_transport_events_from_payload as _iter_runtime_transport_events_from_payload,
)
from backend.runtime_route_history import (
    list_session_route_history,
    summarize_route_history_outcomes,
)
from backend.runtime_model_config import (
    _configure_runtime_model,
    _get_current_model,
    _resolve_runtime_model_id,
)
from backend.llm_call_config import (
    resolve_call_model_config,
    ModelResolutionError,
)
from backend.llm_client import use_call_config
from backend.runtime_segments import (
    append_stream_event_segment as _append_stream_event_segment,
    build_session_payload_from_segments as _build_session_payload_from_segments,
    collect_runtime_event as _collect_runtime_event,
    extract_assistant_text as _extract_assistant_text,
    encode_sse as _encode_sse,
    normalize_session_segments as _normalize_session_segments,
)
from backend.transcript import TranscriptReader
from backend.provider_utils import validate_provider, get_provider_info, get_provider_display_name
from backend.sse_manager import get_sse_manager, encode_sse_event, parse_last_event_id
from backend import paths
# hello from 柳如烟



# 全局 SSE 管理器
sse_manager = get_sse_manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan：启动时恢复所有会话"""
    session_mgr = get_session_manager()
    await session_mgr.recover_all_sessions()

    # 初始化 Guardian 权限守卫
    from .guardian import get_guardian
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    guardian = get_guardian()
    guardian.set_project_root(project_root)
    logger.info("[GUARDIAN] initialized with project_root=%s", project_root)

    # 搜索引擎配置：把 UI 配的 Tavily key 灌进 os.environ（柳如烟全局搜索 + 内容模块共用）
    # 仅 UI 配了 key 时覆盖；为空则保留 .env / 外部 export 的值
    try:
        from backend.search_config import apply_search_config_to_env
        apply_search_config_to_env()
    except Exception as e:  # noqa: BLE001
        logger.warning("[SEARCH] apply search config failed (non-fatal): %s", e)

    # Fast Context (代码探索) 配置：把 UI 配的环境变量灌进 os.environ
    try:
        from backend.fast_context_config import apply_fast_context_config_to_env
        apply_fast_context_config_to_env()
    except Exception as e:  # noqa: BLE001
        logger.warning("[FAST_CONTEXT] apply fast context config failed (non-fatal): %s", e)

    # 清理 7 天前的写前备份快照 + jsonl 审计超阈值滚动
    try:
        from backend.tools.defense.backup import cleanup_old_backups, rotate_audit_log_if_needed
        removed = cleanup_old_backups(retention_days=7)
        if removed:
            logger.info("[BACKUP] cleaned %s stale backup snapshot(s)", removed)
        archived = rotate_audit_log_if_needed(max_bytes=10 * 1024 * 1024)
        if archived:
            logger.info("[BACKUP] rotated audit log to %s", archived.name)
    except Exception as e:
        logger.warning("[BACKUP] cleanup failed (non-fatal): %s", e)

    # 启动时跑一次记忆温度衰减：把超过 TEMP_DECAY_DAYS 天未更新的 warm 降到 cold
    # 失败不影响启动（best-effort）
    try:
        from backend.agent_config import get_agent_manager
        from backend.memory_manager import MemoryManager
        agent_mgr = get_agent_manager()
        total_changed = 0
        for agent_info in agent_mgr.list_agents():
            agent_id = agent_info.get("id") if isinstance(agent_info, dict) else None
            if not agent_id:
                continue
            try:
                mm = MemoryManager(agent_id=agent_id)
                # force=True：启动钩子必跑，不受运行时节流闸约束
                # （MemoryManager 是新建实例，_last_decay_ts=0，本来也不会被拦，
                # 显式传 True 是为了语义清晰——"启动时强制扫一次"是合同的一部分）
                changed = mm.apply_temp_decay(force=True)
                if changed:
                    total_changed += changed
                    logger.info("[TEMP_DECAY] %s: %s line(s) warm→cold", agent_id, changed)
            except Exception as e:  # noqa: BLE001
                logger.warning("[TEMP_DECAY] %s failed (non-fatal): %s", agent_id, e)
        if total_changed:
            logger.info("[TEMP_DECAY] startup total: %s line(s) decayed", total_changed)
    except Exception as e:  # noqa: BLE001
        logger.warning("[TEMP_DECAY] startup pass failed (non-fatal): %s", e)

    # Phase 1B-1: AgentActor 心跳骨架（默认 OFF；LIURUYAN_AGENT_TICK=1 启用）
    from backend.core.runtime_bootstrap import (
        bootstrap_agent_actors,
        shutdown_agent_actors,
    )
    await bootstrap_agent_actors()

    # Cron scheduler（默认 OFF；FEATURE_CRON_TOOL=1 启用）
    # 工具不注册时 scheduler 也不起，已存在的 cron_jobs.json 保留不删
    _cron_env_raw = os.getenv("FEATURE_CRON_TOOL", "")
    cron_enabled = (_cron_env_raw or "").strip().lower() in ("1", "true", "yes", "on")
    logger.info("[CRON] FEATURE_CRON_TOOL=%r enabled=%s", _cron_env_raw, cron_enabled)
    if cron_enabled:
        from backend.cron_scheduler import get_cron_scheduler
        sched = get_cron_scheduler()
        await sched.start()
        logger.info("[CRON] scheduler started, %s jobs in memory", len(sched._jobs))

    # v2 C-3: 装配 EventBus + MessageBus + TraceJournal（process-wide singleton）
    # EventBus 给 SSE / IM / TraceJournal / Metrics 等订阅；MessageBus 当前
    # 无消费者（等 Phase E IM 接入）。失败 non-fatal（v2 还没强依赖）。
    _event_bus = None
    _message_bus = None
    _trace_journal = None
    try:
        from liuruyan.buses import EventBus, MessageBus, set_event_bus, set_message_bus
        from liuruyan.tracing import TraceJournal
        from backend.paths import data_dir as _data_dir

        _event_bus = EventBus()
        _message_bus = MessageBus()
        set_event_bus(_event_bus)
        set_message_bus(_message_bus)

        _trace_journal = TraceJournal(base_dir=_data_dir())
        _trace_journal.attach_to(_event_bus)

        # mw 14 TitleGen 订阅者：title_gen_requested → LLM → session.title → SSE
        from backend.title_gen_subscriber import get_title_gen_subscriber
        _title_gen_sub = get_title_gen_subscriber()
        _title_gen_sub.attach_to(_event_bus)

        logger.info(
            "[v2:buses] EventBus + MessageBus + TraceJournal + TitleGenSubscriber "
            "initialized (data_dir=%s)",
            _data_dir(),
        )
    except Exception as e:
        logger.warning("[v2:buses] init failed (non-fatal): %s", e)
        _title_gen_sub = None

    # v2 E-3.5: Phase E channels 系统装配（默认 OFF；LIURUYAN_V2_CHANNELS=1 启用）
    # ChannelStore + ChannelManager + InboundRouter 三件套；adapter 实现层
    # （dingtalk / feishu / web 等）的 factory 注册留给 E-4 子刀。失败 non-fatal。
    _v2_channels = None
    _v2_flag_raw = os.getenv("LIURUYAN_V2_CHANNELS", "")
    logger.info("[v2:channels] LIURUYAN_V2_CHANNELS env = %r", _v2_flag_raw)
    try:
        from backend.v2_bootstrap import setup_v2_channels, is_enabled as _v2_enabled
        if not _v2_enabled():
            logger.info("[v2:channels] flag OFF, skipping setup")
        elif _message_bus is None:
            logger.warning("[v2:channels] _message_bus is None, skipping setup")
        else:
            logger.info("[v2:channels] flag ON, calling setup_v2_channels...")
            _v2_channels = await setup_v2_channels(_message_bus)
            if _v2_channels is None:
                logger.warning("[v2:channels] setup returned None (see logger errors)")
            else:
                _statuses = _v2_channels.manager.list_status()
                _kinds = _v2_channels.manager.registered_channels()
                logger.info(
                    "[v2:channels] setup complete: kinds=%s channels=%s web=%s",
                    _kinds, len(_statuses), _v2_channels.web_channel is not None,
                )
                for _info in _statuses:
                    logger.info(
                        "[v2:channels]   - %s channel=%s status=%s err=%s",
                        _info.config_key, _info.channel, _info.status.value,
                        _info.last_error[:120] if _info.last_error else '',
                    )
    except Exception as e:
        logger.error("[v2:channels] bootstrap failed (non-fatal): %s", e, exc_info=True)

    logger.info("[v2:subagent] legacy scheduler bootstrap skipped")

    # MCP 子系统装配。设计文档：_docs/MCP_DESIGN.md
    # 默认 ON（M1+M2+M3+M4 全部稳定，61 测试绿）；想关 → LIURUYAN_MCP=0
    # bootstrap 失败 non-fatal——单 server 启动失败时它自己标 DEGRADED，不影响主进程
    _mcp_flag = (os.getenv("LIURUYAN_MCP", "1") or "1").strip().lower()
    _mcp_enabled = _mcp_flag not in ("0", "false", "no", "off")
    logger.info("[MCP] LIURUYAN_MCP=%s", _mcp_enabled)
    if _mcp_enabled:
        try:
            from backend.mcp.controller import get_controller as _get_mcp_controller
            from backend.mcp.tool_bridge import attach_to_controller as _attach_mcp_bridge
            from backend.mcp.sse_bridge import attach_to_controller as _attach_mcp_sse
            from backend.mcp.watcher import get_watcher as _get_mcp_watcher
            # 注意装配顺序：SSE 先 attach（拿初始 status 推送），再 tool_bridge（挂工具），
            # 最后 bootstrap（启动 server → 触发状态事件）
            _attach_mcp_sse()
            _attach_mcp_bridge()
            await _get_mcp_controller().bootstrap()
            # watcher 启 bootstrap 之后——避免 bootstrap 自己触发一次 reconcile
            await _get_mcp_watcher().start()
            logger.info("[MCP] bootstrap done (+ sse bridge + config watcher)")
        except Exception as e:  # noqa: BLE001
            logger.warning("[MCP] bootstrap failed (non-fatal): %s", e, exc_info=True)

    # ACP delegate tool 注册（方向 B：LLM 调外部 ACP agent 如 Claude Code）
    # 默认 ON；LIURUYAN_ACP_DELEGATE=0 关闭。失败 non-fatal（不影响主对话）
    _acp_delegate_flag = (os.getenv("LIURUYAN_ACP_DELEGATE", "1") or "1").strip().lower()
    if _acp_delegate_flag not in ("0", "false", "no", "off"):
        try:
            from backend.acp.tools import register_delegate_acp_agent_tool
            register_delegate_acp_agent_tool()
            logger.info("[ACP] delegate_acp_agent tool registered")
        except Exception as e:  # noqa: BLE001
            logger.warning("[ACP] delegate tool registration failed (non-fatal): %s", e)

    # Computer Use 策略持久化恢复（M4b）：从 ~/.liuruyan/computer_use_policy.json 加载
    # 用户上次设的开关 + 限流。文件不存在时保持默认（enabled=False，必须显式开）。
    try:
        from backend.computer_use import safety as _cu_safety
        if _cu_safety.load_from_disk():
            logger.info("[computer_use] policy restored from disk")
    except Exception as e:  # noqa: BLE001 — 加载失败不影响主路径
        logger.warning("[computer_use] policy load skipped: %s", e)

    # 量化操盘台本地定时调度（纸面 run_once；与 FEATURE_CRON_TOOL 隔离）
    try:
        from backend.quant.scheduler import start_scheduler as _start_quant_sched
        await _start_quant_sched()
        logger.info("[QUANT-SCHED] desk scheduler started")
    except Exception as e:  # noqa: BLE001
        logger.warning("[QUANT-SCHED] start failed (non-fatal): %s", e)

    # 不再在 lifespan 里打 ready 信号：lifespan startup 在 uvicorn bind 之前执行,
    # bind 失败时这条 print 会误报 ready。Tauri main.rs 改为监听 uvicorn 自己的
    # "Uvicorn running on" 日志作为真信号（bind 成功后才打）。

    try:
        yield
    finally:
        # 量化 desk 定时调度收尾
        try:
            from backend.quant.scheduler import stop_scheduler as _stop_quant_sched
            await _stop_quant_sched()
        except Exception:
            pass
        # MCP 关停：必须在 buses / pools 之前——StatefulMCPClient 内部跑独立 task，
        # cross-task cancellation safeguard
        if _mcp_enabled:
            try:
                from backend.mcp.controller import get_controller as _get_mcp_controller_sd
                from backend.mcp.watcher import get_watcher as _get_mcp_watcher_sd
                await _get_mcp_watcher_sd().stop()
                await _get_mcp_controller_sd().shutdown()
                logger.info("[MCP] shutdown done")
            except Exception as e:  # noqa: BLE001
                logger.warning("[MCP] shutdown failed (non-fatal): %s", e)
        if cron_enabled:
            from backend.cron_scheduler import get_cron_scheduler
            await get_cron_scheduler().stop()
        await shutdown_agent_actors()

        # v2 E-3.5: 拆 channels 系统（必须在拆 MessageBus 之前——router 内部
        # 还在消费 bus；按 02_runtime_core § 11 优雅关闭顺序"先停 channels 停
        # 接收 → drain MessageBus"）
        try:
            if _v2_channels is not None:
                from backend.v2_bootstrap import teardown_v2_channels
                await teardown_v2_channels(_v2_channels)
        except Exception as e:
            logger.warning("[v2:channels] teardown failed (non-fatal): %s", e)

        # v2 C-3: 拆 buses（在 pools 之前，因 TraceJournal 落盘走 io_pool）
        try:
            if _trace_journal is not None:
                _trace_journal.detach()
            try:
                from backend.title_gen_subscriber import get_title_gen_subscriber
                get_title_gen_subscriber().detach()
            except Exception:
                pass
            if _event_bus is not None:
                await _event_bus.flush(timeout=5.0)
                _event_bus.close()
            if _message_bus is not None:
                _message_bus.close()
            from liuruyan.buses import set_event_bus, set_message_bus
            set_event_bus(None)
            set_message_bus(None)
        except Exception as e:
            logger.warning("[v2:buses] shutdown failed (non-fatal): %s", e)

        # v2 A8: 关 harness 池（必须在 buses 之后）
        # 顺序：业务层 (cron/agent_actor) → buses → harness pools
        try:
            from liuruyan.runtime import pools as _liuruyan_pools
            _liuruyan_pools.shutdown_pools(timeout=10.0)
        except Exception as e:
            logger.warning("[v2:pools] shutdown failed (non-fatal): %s", e)


# 创建 FastAPI 应用
app = FastAPI(
    title="柳如烟智能体",
    description="多智能体独立成长架构，每个智能体拥有独立的记忆、情感和人格",
    version="3.3.0",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ====== 请求模型 ======
class ChatRequest(BaseModel):
    message: str
    stream: bool = True
    session_id: Optional[str] = None
    model: Optional[str] = None
    # provider id（前端 picker 切的 provider，与 model 配对的强 hint）。
    # 同一个 model id 可能存在于多个 provider 下（典型：多家 Anthropic 反代都有
    # claude-opus-4-7），仅靠 model id 反查会永远命中第一家。这里把 provider 也
    # 透传给 resolve_call_model_config 走严格匹配路径，避免静默走错家。
    provider: Optional[str] = None
    agent_id: Optional[str] = None  # 新增：智能体 ID
    execution_mode: str = "auto"
    route_max_steps: int = 6
    # 前端为本轮 user / assistant 各生成的 UUID，端到端追踪用于 dedup（治根 segments 漂移问题）
    client_id: Optional[str] = None        # 本轮 assistant message 的 client_id
    user_client_id: Optional[str] = None   # 本轮 user message 的 client_id
    # 用户从输入框 📎 按钮上传的文件绝对路径数组（后端 POST /chat/upload 返回的 path）。
    # chat() 顶部会拼一个 "[附件]" footer 到 message 末尾，LLM 看到后自动调
    # read_file 读取（已支持 PDF / Office / .ipynb / image）。
    attachments: Optional[List[str]] = None


class SessionCreate(BaseModel):
    title: Optional[str] = None
    agent_id: Optional[str] = None  # 新增：绑定智能体


# MemoryRequest / MemoryUpdateRequest 已随 /memory 路由迁至 routes/memory_routes.py


# SkillToggle/SkillContentUpdate/EquipSkill/SetSkills/SkillPromotionReview 请求模型
# 已随 skills 路由迁至 routes/skills_routes.py


class ConfigResponse(BaseModel):
    identity: Dict[str, str]
    user: Dict[str, str]


# Agent CRUD 请求模型
# AgentCreateRequest / AgentUpdateRequest / SaveAsTemplateRequest
# 已随 /agents 核心路由迁至 routes/agents_routes.py


# ====== 会话管理（委托给 SessionManager）======
# 仅保留旧格式读取能力，用于历史会话迁移
_sessions_dir = DEFAULT_SESSION_BASE_DIR


# 旧格式兼容函数（仅用于迁移历史会话）
def _load_session(session_id: str) -> Optional[Dict]:
    """加载会话（旧格式兼容）"""
    path = _sessions_dir / f"{session_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
    return None


def _is_recall_request(message: str) -> bool:
    """是否为显式回忆请求。"""
    text = (message or "").strip()
    if not text:
        return False
    patterns = [
        r"想起",
        r"回忆",
        r"还记得",
        r"记不记得",
        r"记得.*吗",
        r"上次",
        r"之前",
        r"当时",
        r"那次",
        r"我们.*聊过",
        r"你.*忘了",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def _extract_recall_keywords(message: str) -> List[str]:
    """从回忆请求里提取关键词。"""
    text = (message or "").strip()
    if not text:
        return []

    keywords: List[str] = []
    quoted = re.findall(r'["“](.+?)["”]', text)
    for item in quoted:
        item = item.strip()
        if len(item) >= 2 and item not in keywords:
            keywords.append(item)

    cleaned = re.sub(r"[，。！？、,.!?：:；;（）()\[\]{}<>《》'\"“”]", " ", text)
    stop_words = {
        "我们", "你们", "你", "我", "当时", "之前", "上次", "那次", "这个", "那个",
        "什么", "怎么", "是不是", "已经", "还有", "就是", "一下", "然后", "因为",
        "所以", "如果", "可以", "不会", "不是", "没有", "一下子", "一下儿", "记得",
        "回忆", "想起", "聊过", "说过", "提过", "忘了", "记不记得", "还记得",
    }
    for token in cleaned.split():
        token = token.strip()
        if len(token) < 2 or token in stop_words:
            continue
        if token not in keywords:
            keywords.append(token)

    return keywords[:8]


_RECALL_CONFIG = {
    "top_k": 3,
    "batch_sizes": (3, 5),
    "strong_score": 6,
    "support_score": 4,
    "support_strong_score": 7,
    "direct_hits_single": 1,
    "direct_hits_multi": 2,
    "min_support_snippets": 2,
    "snippet_fact_chars": 40,
    "summary_min_candidates": 2,
    "summary_min_total_chars": 280,
    "summary_source_limit": 4,
    "summary_char_budget": 1800,
    "summary_raw_top_k": 2,
}

# 自动回忆的语义层（P4，与 session_search 共用 transcript 向量索引）。
# 仅在 embedding 就绪时叠加；未配置则自动回忆维持纯关键词，行为与以前一致。
# 余弦相似度下限：低于此不算"语义相关"，不进候选（防无关项污染回忆门控）。
_RECALL_SEMANTIC_MIN_SIM = 0.6
# 每个 session 向量检索取回多少条候选。
_RECALL_SEMANTIC_TOP_K = 5


def _parse_recall_time(raw: Optional[str]) -> float:
    """将 ISO 时间转成可排序时间戳；失败时返回 0。"""
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _build_session_source_label(session_obj, *, is_current: bool = False) -> str:
    """构建回忆片段的来源标签。"""
    title = (getattr(session_obj, "title", None) or "未命名会话").strip()
    updated_at = getattr(session_obj, "updated_at", "") or ""
    date_text = ""
    if updated_at:
        try:
            date_text = datetime.fromisoformat(updated_at).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            date_text = updated_at
    prefix = "当前会话" if is_current else f"其他会话「{title}」"
    if date_text:
        return f"{prefix} · {date_text}"
    return prefix


def _collect_recall_candidates(
    session_obj,
    *,
    keywords: List[str],
    preferred_span: Optional[List[int]] = None,
    is_current: bool = False,
) -> List[Dict]:
    """从单个 transcript 收集候选回忆片段。"""
    transcript_path = getattr(session_obj, "transcript_path", None)
    if not transcript_path or not transcript_path.exists():
        return []

    reader = TranscriptReader(transcript_path)
    snippets = reader.recall_snippets(
        keywords=keywords,
        preferred_span=preferred_span,
        max_snippets=_RECALL_CONFIG["top_k"],
        window=1,
    )
    if not snippets:
        return []

    source_label = _build_session_source_label(session_obj, is_current=is_current)
    recency = _parse_recall_time(getattr(session_obj, "updated_at", ""))
    candidates: List[Dict] = []
    lowered_keywords = [kw.lower() for kw in keywords if kw]

    for snippet in snippets:
        text = (snippet.get("text") or "").strip()
        if not text:
            continue
        lowered_text = text.lower()
        keyword_hits = sum(1 for kw in lowered_keywords if kw in lowered_text)
        span = snippet.get("span") if isinstance(snippet.get("span"), list) else None
        candidates.append({
            "session_id": getattr(session_obj, "id", ""),
            "source_label": source_label,
            "score": int(snippet.get("score", 0) or 0),
            "keyword_hits": keyword_hits,
            "span": span,
            "text": text,
            "recency": recency,
            "is_current": is_current,
            "has_facts": len(text) >= _RECALL_CONFIG["snippet_fact_chars"] or "\n" in text,
        })

    return candidates


async def _collect_recall_candidates_semantic(
    session_obj,
    *,
    query_message: str,
    is_current: bool,
    embed_budget: int,
) -> tuple[List[Dict], int]:
    """从单个 transcript 用向量语义检索收集候选回忆片段（P4）。

    与 ``_collect_recall_candidates``（关键词）互补：捕捉"换义/转述"——用户用词与
    当年原话不同也能想起来。与 session_search 共用同一 per-session 向量索引
    （``transcript_embedding.build_indexable_messages`` 保证序号口径一致）。

    懒索引：本 session 第一次被回忆时才 embed 入库（受 ``embed_budget`` 上限保护，
    一次回忆不会把所有会话 embed 爆掉）。embedding 未就绪 / query 无法 embed →
    返回空候选，调用方退回纯关键词回忆。

    Returns:
        (candidates, remaining_embed_budget)
    """
    try:
        from backend.transcript_embedding import (
            MAX_INDEX_MESSAGES_PER_SESSION,
            build_indexable_messages,
            embedding_ready,
            ensure_indexed,
            get_embedding_client,
            get_transcript_index,
            search_index,
        )
    except Exception:  # noqa: BLE001
        return [], embed_budget

    if not embedding_ready():
        return [], embed_budget

    transcript_path = getattr(session_obj, "transcript_path", None)
    if not transcript_path or not transcript_path.exists():
        return [], embed_budget

    client = await get_embedding_client()
    if client is None:
        return [], embed_budget
    try:
        query_vec = await client.embed(query_message)
    except Exception:  # noqa: BLE001
        return [], embed_budget
    if query_vec is None:
        return [], embed_budget

    indexable = build_indexable_messages(transcript_path)
    if not indexable:
        return [], embed_budget

    session_id = getattr(session_obj, "id", "")
    index = get_transcript_index(transcript_path.parent, session_id)
    if index is None:
        return [], embed_budget

    items = [(str(d["ordinal"]), d["text"]) for d in indexable[:MAX_INDEX_MESSAGES_PER_SESSION]]
    used = await ensure_indexed(index, items, client, max(embed_budget, 0))
    embed_budget -= used

    source_label = _build_session_source_label(session_obj, is_current=is_current)
    recency = _parse_recall_time(getattr(session_obj, "updated_at", ""))
    candidates: List[Dict] = []
    for ordinal, sim in search_index(index, query_vec, _RECALL_SEMANTIC_TOP_K):
        if sim < _RECALL_SEMANTIC_MIN_SIM:
            continue
        if ordinal < 0 or ordinal >= len(indexable):
            continue
        text = (indexable[ordinal]["text"] or "").strip()
        if not text:
            continue
        candidates.append({
            "session_id": session_id,
            "source_label": source_label,
            # 把相似度映射进与关键词分同尺度（strong_score=6 → sim≈0.6）：
            # 强语义匹配能像强关键词命中一样满足回忆充分性门控。
            "score": int(round(sim * 10)),
            "keyword_hits": 0,  # 语义召回不靠词法
            "span": None,
            "text": text,
            "recency": recency,
            "is_current": is_current,
            "has_facts": len(text) >= _RECALL_CONFIG["snippet_fact_chars"] or "\n" in text,
            "match_reason": "vector",
        })
    return candidates, embed_budget


def _sort_recall_candidates(candidates: List[Dict]) -> List[Dict]:
    """按相关性优先、时间次优先排序。"""
    return sorted(
        candidates,
        key=lambda item: (
            item.get("score", 0),
            item.get("keyword_hits", 0),
            item.get("recency", 0.0),
        ),
        reverse=True,
    )


def _is_recall_sufficient(candidates: List[Dict], keyword_count: int) -> bool:
    """判断当前候选是否足以回答回忆问题。"""
    ranked = _sort_recall_candidates(candidates)
    if not ranked:
        return False

    top = ranked[0]
    direct_threshold = (
        _RECALL_CONFIG["direct_hits_single"]
        if keyword_count <= 1
        else _RECALL_CONFIG["direct_hits_multi"]
    )
    direct_ok = (
        top.get("keyword_hits", 0) >= direct_threshold
        or top.get("score", 0) >= _RECALL_CONFIG["strong_score"]
    )
    factual_ok = bool(top.get("has_facts"))
    support_ok = (
        top.get("score", 0) >= _RECALL_CONFIG["support_strong_score"]
        or sum(
            1
            for item in ranked[:2]
            if item.get("score", 0) >= _RECALL_CONFIG["support_score"]
        ) >= _RECALL_CONFIG["min_support_snippets"]
    )
    return direct_ok and factual_ok and support_ok


def _select_top_recall_candidates(candidates: List[Dict], limit: Optional[int] = None) -> List[Dict]:
    """去重后选出最终注入本轮上下文的少量片段。"""
    max_items = limit or _RECALL_CONFIG["top_k"]
    selected: List[Dict] = []
    used = set()
    for candidate in _sort_recall_candidates(candidates):
        key = (candidate.get("session_id"), tuple(candidate.get("span") or []), candidate.get("text"))
        if key in used:
            continue
        used.add(key)
        selected.append(candidate)
        if len(selected) >= max_items:
            break
    return selected


def _select_recall_summary_candidates(candidates: List[Dict]) -> List[Dict]:
    """选出适合做回忆聚焦摘要的候选片段。"""
    selected: List[Dict] = []
    total_chars = 0
    used = set()

    for candidate in _sort_recall_candidates(candidates):
        text = (candidate.get("text") or "").strip()
        if not text:
            continue

        key = (candidate.get("session_id"), tuple(candidate.get("span") or []), text)
        if key in used:
            continue

        if selected and total_chars + len(text) > _RECALL_CONFIG["summary_char_budget"]:
            break

        used.add(key)
        selected.append(candidate)
        total_chars += len(text)

        if len(selected) >= _RECALL_CONFIG["summary_source_limit"]:
            break

    return selected


def _should_summarize_recall(candidates: List[Dict]) -> bool:
    """判断是否值得先做一层回忆摘要，再注入少量原片段。"""
    summary_candidates = _select_recall_summary_candidates(candidates)
    if len(summary_candidates) < _RECALL_CONFIG["summary_min_candidates"]:
        return False

    total_chars = sum(len(item.get("text") or "") for item in summary_candidates)
    session_count = len({
        item.get("session_id")
        for item in summary_candidates
        if item.get("session_id")
    })
    return (
        total_chars >= _RECALL_CONFIG["summary_min_total_chars"]
        or session_count > 1
    )


async def _run_runtime_prompt_text(
    agent,
    *,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Run a neutral system task through the shared runtime without injecting role prompt context."""
    result = await agent.run_messages_collected(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        use_tools=False,
        collect_events=False,
    )
    return (result.get("text") or "").strip()


async def _build_recall_summary(message: str, candidates: List[Dict], agent) -> Optional[str]:
    """基于检索到的候选片段生成面向当前问题的短回忆摘要。"""
    if not agent or not _should_summarize_recall(candidates):
        return None

    summary_candidates = _select_recall_summary_candidates(candidates)
    if not summary_candidates:
        return None

    source_blocks = []
    for idx, candidate in enumerate(summary_candidates, 1):
        span = candidate.get("span") or []
        span_text = f"（消息 {span[0]}-{span[1]}）" if len(span) == 2 else ""
        source_blocks.append(
            f"片段{idx} · {candidate.get('source_label', '历史片段')}{span_text}\n{candidate.get('text', '')}"
        )

    summary = await _run_runtime_prompt_text(
        agent,
        system_prompt=(
            "你是回忆检索摘要器。你的任务不是直接回复用户，而是把检索到的历史片段压成一段短摘要，"
            "供另一个模型回答当前问题时参考。请只保留已经出现过的事实、约定、决定、关系状态和时间线，"
            "不要补充猜测，不要改写成聊天口吻，不要输出列表。"
        ),
        user_prompt=(
            f"当前用户问题：{message.strip()}\n\n"
            "请基于下面这些历史片段，生成 2-4 句中文短摘要。\n"
            "要求：\n"
            "1. 只总结与当前问题最相关的历史信息。\n"
            "2. 信息不足时明确说“现有回忆不足以确定”。\n"
            "3. 不要引用“片段1/片段2”这种编号，不要逐句复述原文。\n\n"
            f"历史片段：\n\n{chr(10).join([''] + source_blocks).strip()}"
        ),
    )
    if not summary:
        return None

    summary = re.sub(r"^\s*回忆摘要[:：]?\s*", "", summary)
    summary = re.sub(r"\n{3,}", "\n\n", summary).strip()
    return summary or None


def _format_recall_context(
    candidates: List[Dict],
    recall_summary: Optional[str] = None,
) -> tuple[Optional[str], Optional[List[int]]]:
    """把候选片段格式化为本轮临时回忆上下文。"""
    chosen_limit = (
        _RECALL_CONFIG["summary_raw_top_k"]
        if recall_summary
        else _RECALL_CONFIG["top_k"]
    )
    chosen = _select_top_recall_candidates(candidates, limit=chosen_limit)
    if not chosen:
        return None, None

    blocks = []
    current_spans = []
    for idx, candidate in enumerate(chosen, 1):
        span = candidate.get("span") or []
        span_text = f"（消息 {span[0]}-{span[1]}）" if len(span) == 2 else ""
        blocks.append(
            f"片段{idx} · {candidate.get('source_label', '历史片段')}{span_text}\n{candidate.get('text', '')}"
        )
        if candidate.get("is_current") and len(span) == 2:
            current_spans.append(span)

    message_span = None
    if current_spans:
        message_span = [
            min(span[0] for span in current_spans),
            max(span[1] for span in current_spans),
        ]

    if recall_summary:
        context = (
            f"## 回忆摘要\n{recall_summary.strip()}\n\n"
            f"## 关键原始片段\n\n{chr(10).join([''] + blocks).strip()}"
        ).strip()
        return context, message_span

    return "\n\n".join(blocks).strip(), message_span


async def _build_recall_context(session, message: str, agent) -> tuple[Optional[str], Optional[List[int]]]:
    """按需构建本轮临时回忆上下文。

    策略：
    1. 先检索当前会话
    2. 当前命中不足时，再按近到远渐进补查同角色其他会话
    3. 最终只注入少量相关片段，且仅对本轮生效
    """
    if not session or not _is_recall_request(message):
        return None, None

    keywords = _extract_recall_keywords(message)
    if not keywords:
        return None, None

    # 语义召回的 embed 预算（整次回忆共享，懒索引摊销，避免一次回忆把所有会话 embed 爆掉）。
    try:
        from backend.transcript_embedding import MAX_EMBED_PER_CALL as _embed_budget
    except Exception:  # noqa: BLE001
        _embed_budget = 0

    combined_candidates = _collect_recall_candidates(
        session,
        keywords=keywords,
        is_current=True,
    )
    sem_candidates, _embed_budget = await _collect_recall_candidates_semantic(
        session,
        query_message=message,
        is_current=True,
        embed_budget=_embed_budget,
    )
    combined_candidates.extend(sem_candidates)
    if _is_recall_sufficient(combined_candidates, len(keywords)):
        recall_summary = await _build_recall_summary(message, combined_candidates, agent)
        return _format_recall_context(combined_candidates, recall_summary=recall_summary)

    session_mgr = get_session_manager()
    session_summaries = await session_mgr.list_sessions(agent_id=session.agent_id)
    other_ids = [
        item.get("id")
        for item in session_summaries
        if item.get("id") and item.get("id") != session.id
    ]

    checked = 0
    for batch_size in _RECALL_CONFIG["batch_sizes"]:
        batch_ids = other_ids[checked:checked + batch_size]
        if not batch_ids:
            break

        for other_id in batch_ids:
            other_session = await session_mgr.get_session(other_id)
            if not other_session:
                continue
            combined_candidates.extend(
                _collect_recall_candidates(
                    other_session,
                    keywords=keywords,
                    is_current=False,
                )
            )
            sem_other, _embed_budget = await _collect_recall_candidates_semantic(
                other_session,
                query_message=message,
                is_current=False,
                embed_budget=_embed_budget,
            )
            combined_candidates.extend(sem_other)

        checked += len(batch_ids)
        if _is_recall_sufficient(combined_candidates, len(keywords)):
            break

    recall_summary = await _build_recall_summary(message, combined_candidates, agent)
    return _format_recall_context(combined_candidates, recall_summary=recall_summary)


# affinity helper 已抽到 backend/social_service.py。main.py 仅保留 chat 收尾用的
# _apply_user_relation_update；collab 专用的 classify/apply_pair/flush 已随 collab 路由迁出。
from backend.social_service import (
    apply_user_relation_update as _apply_user_relation_update,
)


async def root():
    """根路径"""
    return {
        "name": "柳如烟智能体",
        "status": "在线",
        "version": "3.0.0",
        "message": "多智能体独立成长架构",
        "agents_count": len(get_agent_manager().list_agents())
    }


@app.get("/config", response_model=ConfigResponse)
async def get_config(agent_id: Optional[str] = None):
    """获取配置信息"""
    config_loader = get_config_loader()
    return ConfigResponse(
        identity=config_loader.get_identity(agent_id),
        user=config_loader.get_user_info()
    )


# ====== 智能体核心 API：/agents/*（模板/头像/CRUD）→ 已拆分到 backend/routes/agents_routes.py ======
# （路径与行为不变；见上方 include_router(_agents_router)。情感/面具/文件/同步仍在下方）


# ====== 情感/面具/文件/模板同步 API → 已拆分到 backend/routes/agent_state_routes.py ======
# （路径与行为不变；见上方 include_router(_agent_state_router)）


# ----- 导入/导出 API：/export/* · /import/archive → 已拆分到 backend/routes/export_import_routes.py -----
# （路径与行为不变；见上方 include_router(_export_import_router)）


# ----- 聊天附件上传 -----
_UPLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50 MB。超过这个上传趋于不理智；LLM 读这么大的文件本身也费 token
_UPLOAD_FILENAME_UNSAFE_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")  # 保留中文/字母/数字/./_/-，其余 → _


def _sanitize_upload_filename(name: str) -> str:
    """上传文件名安全化：防 path traversal / 保留扩展名 / 干掉特殊字符。"""
    name = os.path.basename((name or "").strip())
    if not name:
        return "upload.bin"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = _UPLOAD_FILENAME_UNSAFE_RE.sub("_", stem).strip("._-") or "upload"
    if len(stem) > 80:
        stem = stem[:80]
    if ext:
        ext = _UPLOAD_FILENAME_UNSAFE_RE.sub("", ext)[:10]
        return f"{stem}.{ext}" if ext else stem
    return stem


@app.post("/chat/upload")
async def chat_upload(file: UploadFile = File(...)):
    """前端聊天输入框 📎 上传的文件 → 落到 data/uploads/，返绝对路径。

    LLM 后续通过 read_file 工具读取这些路径（PDF/Office/.ipynb/image 都支持），
    本 endpoint 只负责落盘 + 回路径。限制：50 MB size cap，文件名 sanitize。
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # 流式读 、边读边检 size，避免 一次性 read() 爆内存
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _UPLOAD_MAX_SIZE:
            await file.close()
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File too large: {total / 1024 / 1024:.1f} MB exceeds "
                    f"{_UPLOAD_MAX_SIZE / 1024 / 1024:.0f} MB limit"
                ),
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    safe_name = _sanitize_upload_filename(file.filename)
    timestamp = int(time.time() * 1000)
    dest = paths.uploads_dir() / f"{timestamp}_{safe_name}"
    dest.write_bytes(content)

    logger.info("[CHAT-UPLOAD] saved: %s (%s bytes, original=%r)", dest, total, file.filename)
    return {
        "path": str(dest.resolve()),
        "original_name": file.filename,
        "size": total,
        "mime": file.content_type or "application/octet-stream",
    }


@app.get("/chat/upload/file")
async def chat_upload_get(path: str):
    """以 HTTP 形式回吐之前 POST /chat/upload 落盘的文件（前端用户消息气泡显示已发图片用）。

    安全契约：请求路径 resolve 后必须落在 paths.uploads_dir() 之内。
    任何 traversal / 绝对路径越界 / 软链接逃逸都返 403。
    本地桌面应用，不做更多权限层；同源请求即可。
    """
    from fastapi.responses import FileResponse
    import mimetypes

    try:
        requested = Path(path).resolve()
        root = paths.uploads_dir().resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid path")
    try:
        requested.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path outside uploads dir")
    if not requested.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    mime, _ = mimetypes.guess_type(str(requested))
    return FileResponse(requested, media_type=mime or "application/octet-stream")


# ----- IDE 反向调结果回填（C-2）-----
# 前端 SSE consumer 收到 backend 推的 'ide_tool_request' 事件 → 调 runtime.ide.<x> →
# POST 回填结果到这里。backend ide_tool_bridge 等待槽（asyncio.Future）被 set_result，
# 阻塞中的 tool handler 拿到结果继续执行。详见 backend/ide_tool_bridge.py。
@app.post("/v2/ide_tool_result")
async def ide_tool_result(payload: Dict[str, Any]):
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise HTTPException(status_code=400, detail="call_id (string) required")
    from backend.ide_tool_bridge import get_ide_tool_bridge
    delivered = get_ide_tool_bridge().receive_result(
        call_id,
        ok=bool(payload.get("ok")),
        result=payload.get("result"),
        error=payload.get("error"),
    )
    if not delivered:
        # call_id 不存在 / future 已 done（超时 / 重复回填）。返 404 让前端可观测，
        # 但不向用户报错——这是良性 race condition（前端断网恢复后又回填，已超时）。
        raise HTTPException(
            status_code=404,
            detail=f"unknown or expired call_id: {call_id}",
        )
    return {"ok": True, "call_id": call_id}


# ----- 聊天 -----
@app.post("/chat")
async def chat(request: ChatRequest, http_request: Request = None):
    """聊天接口（支持多智能体 + 多会话 + 动态模型）
    
    Phase 0 改造：使用 SessionManager 管理会话上下文
    - 会话恢复：优先从 SessionManager 获取，fallback 到旧格式
    - 情感/面具更新：写入 Session 级别
    """
    agent_id = request.agent_id or "default"
    session_id = request.session_id or "default"

    # 新一轮用户消息开始：清零 spawn_subagent 同轮调用计数
    try:
        from backend.subagent.engine import reset_spawn_attempts
        reset_spawn_attempts(session_id)
    except Exception:
        pass

    # 检查 agent_id 是否有效
    agent_manager = get_agent_manager()
    if agent_id != "default" and not (agent_manager.agents_dir / agent_id).exists():
        logger.error("[CHAT] Unknown agent_id=%s, directory not found.", agent_id)
        raise HTTPException(status_code=400, detail=f"智能体不存在: {agent_id}，请刷新页面重试")
    
    # 使用 SessionManager 获取/创建会话
    session_mgr = get_session_manager()

    # 尝试从 SessionManager 获取会话
    session = await session_mgr.get_session(session_id)

    if session:
        # SessionManager 有此会话时，以持久化 owner 为准，不再静默改绑
        if session.agent_id != agent_id:
            logger.warning("[CHAT] Session agent_id=%s != request agent_id=%s, keeping session owner", session.agent_id, agent_id)
            agent_id = session.agent_id or agent_id
    else:
        invalid_session = session_mgr.get_invalid_session(session_id)
        if invalid_session:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "会话归属异常，已从正常列表隐藏，请删除或手动处理",
                    "session_id": session_id,
                    "reason": invalid_session.get("reason", "invalid session owner"),
                },
            )

        if session_id != "default":
            session = await session_mgr.create_session(
                agent_id=agent_id,
                title="新对话"
            )
            session_id = session.id
        else:
            session = await session_mgr.get_session("default")
            if not session:
                session = await session_mgr.create_session(
                    agent_id=agent_id,
                    title="默认对话",
                    session_id="default",
                )
            session_id = "default"

    if not session:
        raise HTTPException(status_code=409, detail="会话初始化失败，请删除该会话后重试")


    logger.debug("[CHAT] resolved agent_id=%s, session_id=%s, session_found=%s", agent_id, session_id, session is not None)
    # 传 agent_id 让 _resolve_runtime_model_id 走完整优先级链：
    # request.model > session.model > agent.active_model > global current_model
    effective_model = _resolve_runtime_model_id(request.model, session, agent_id=agent_id)
    # ⏱️ session.model / session.provider 的语义是"用户在该会话主动切过 picker"
    # ——仅当 request.model 显式给了才应该锁进去。以前写的是 effective_model，
    # 会把 agent / global 兜底值也锁进 session → 后续改 agent.active_model
    # 不生效 / 第一次发消息就被当时 global 默认"焕"上会话。
    #
    # provider 必须跟 model 同时写：仅靠 model id 同名多 provider 时会锁不唯一
    # 的“走哪家”（事故 2026-05-12 雷洛 / getrouter / llmapi 都有 claude-opus-4-7）。
    # 详见 _docs/handover_model_provider_pairing_full_audit_2026-05-12.md
    if session and request.model and (
        session.model != request.model
        or session.provider != (request.provider or None)
    ):
        session.model = request.model
        session.provider = request.provider or None
        session.save_meta()

    # 附件：拼 footer 到 user message 末尾。根据模型 vision 能力决定提示内容。
    # mutate request.message 是有意为之：下游 8 处 request.message 使用点（route、
    # session.append、agent.chat、_finalize_chat_turn、recall、corridor）都需看到拼后
    # 版本 —— 不 mutate 就得在 8 处 each 里传 effective 变量，反而容易漏一处。
    _attachments = request.attachments or []
    if _attachments:
        # 检测当前模型是否有 vision 能力
        from backend.providers.capabilities import infer_capabilities
        model_caps = infer_capabilities(effective_model)
        has_vision = "vision" in model_caps
        
        if has_vision:
            # 有 vision：提示用 read_file 读取
            _footer = "\n\n[附件 / 用户上传，请用 read_file 工具读取]\n" + "\n".join(
                f"- {p}" for p in _attachments
            )
        else:
            # 无 vision：只显示路径，提示可用技能处理（引导 LLM 去找 skill）
            _footer = "\n\n[附件 / 用户上传]\n" + "\n".join(
                f"- {p}" for p in _attachments
            ) + "\n（当前模型无 vision 能力，无法识别图片内容。如需生成或编辑图片，可使用已装载的技能）"
        
        request.message = request.message + _footer
        logger.debug("[CHAT-UPLOAD] attached %s file(s), has_vision=%s, model=%s", len(_attachments), has_vision, effective_model)

    route: Dict[str, Any] = {}

    prefilled_user_message = bool(route.get("prefilled_user_message"))
    prefilled_user_message_index = route.get("prefilled_user_message_index")

    # 方案 2：inline 路径在 LLM 调用前先将 user 持久化。
    # 以前是 LLM 流跑完之后才在 _finalize_chat_turn 里 append user，造成两个问题：
    # 1) 并发乱序：worker 回流 / cron / 走廊消息在本轮 LLM 还没跑完时写 history，
    #    本轮拿的 session_history 快照与最终落盘顺序不一致
    # 2) 失忆：同 session 后续请求取快照时看不到本轮未落盘的 user
    # 提前 append 后，prefilled_user_message=True 会让下游 _finalize_chat_turn 跳过
    # 重复 append，agent.chat 也传 user_message_already_in_history=True 让 build_messages 跳过
    # 末尾重复追加。
    if session and not prefilled_user_message:
        await session_mgr.append_message(
            session.id,
            "user",
            request.message,
            client_id=request.user_client_id,
        )
        prefilled_user_message = True
        prefilled_user_message_index = session.message_count

    agent = get_agent(agent_id)

    # per-agent model routing 阶段 3 Commit 3.2：动态切换模型（如果请求指定了模型）
    # 老路径 _configure_runtime_model 改 LLMClient 单例字段，并发 chat 会互踩；
    # 新路径 resolve 纯函数拿 cfg + 下面 agent.chat 外层 with use_call_config 注入
    # ContextVar，per-task 隔离。resolve 失败时 cfg=None → with 是 no-op，走
    # LLMClient self 字段 fallback（老行为）。
    call_config = None
    try:
        # 仅当前端显式给了 (model, provider) 二元组时才启动严格匹配 ——
        # request.model 为空的 fallback 路径上 provider 不应该被硬套。
        override_provider = request.provider if request.model else None
        # ⚠️ override 只传 request.model，不传 effective_model——让
        # resolve_call_model_config 内部按 override > session > agent > global 链路
        # 自洽走，并选对应层的 provider_hint。之前传 effective_model
        # 会让 fallback 兑底值被误当 override 路径，错选 hint。
        call_config = resolve_call_model_config(
            agent_id=agent_id,
            override=request.model or None,
            session=session,
            override_provider=override_provider,
        )
    except ModelResolutionError as exc:
        # 保留异常分支诊断日志：picker 给了不存在的 (model, provider) 组合时
        # 这条会立刻告诉我们是个哪组合出错。
        logger.warning(
            "[CHAT-RESOLVE-FAIL] agent=%s req_model=%r req_provider=%r err=%s",
            agent_id, request.model, request.provider, exc,
        )

    # 从 Session 恢复对话历史（不再覆盖 Agent 的 self.conversation_history）
    session_history = None
    if session:
        session_history = session.conversation_history.copy()

    # 传递 session_id 给 Agent（用于跨会话感知注入）
    effective_session_id = session.id if session else session_id
    recall_context, recall_message_span = await _build_recall_context(
        session,
        request.message,
        agent,
    )

    # 召回上一轮 plan 合并交付（_deliver_plan_final_result 砍了之后 main agent 失忆补救）。
    # 找到未消化的 worker_plan_completed entry → 拼成 system_extra 注入。
    # 详见 _consume_pending_plan_results docstring 和 .lry/rules/06-worker-channel-separation.md
    pending_plan_hint = None

    if request.stream:
        async def generate():
            full_response = ""
            collected_segments: List[Dict[str, Any]] = []
            persistence_done = False

            async def persist_if_needed(extra_note: Optional[str] = None) -> None:
                """幂等持久化：正常完成 / 异常 / cancel 三条路径都调这里。

                根因纪律（2026-05-16）：以前 _finalize_chat_turn 只在 [DONE] 出口和
                AGENT_ERROR 分支调，CancelledError（用户点停止 / 浏览器关页）会让
                generator 直接 unwind 到外层 except，整段 assistant 内容不写盘 →
                重开会话只剩 user 一条消息。这个 helper 收口三条出口，
                generator 任意路径退出前都至少写一次。
                """
                nonlocal persistence_done
                if persistence_done:
                    return
                persistence_done = True
                try:
                    save_obj = _build_session_payload_from_segments(
                        collected_segments,
                        full_response,
                        agent_id=agent_id,
                    )
                    segments_list = list(save_obj.get("segments") or [])
                    save_text = full_response or ""
                    if extra_note:
                        segments_list.append({
                            "id": len(segments_list),
                            "type": "text",
                            "content": f"\n\n⚠️ {extra_note}",
                        })
                        save_text = save_text + f"\n\n⚠️ {extra_note}"
                    # 空兜底：没有任何 segment 也没文本 → 写一条人话占位
                    if not segments_list and not save_text.strip():
                        fallback_text = "本轮回复中断了，可能是网络异常 / 模型空返回 / 工具被拦。请重试。"
                        segments_list = [{"id": 0, "type": "text", "content": fallback_text}]
                        save_text = fallback_text
                    save_obj["segments"] = segments_list
                    save_content = json.dumps(save_obj, ensure_ascii=False)
                    # 用 shield 防 finalize 过程中被二次 cancel 切断（shutil/await 写盘可被打断）
                    await asyncio.shield(_finalize_chat_turn(
                        session_mgr=session_mgr,
                        session=session,
                        agent=agent,
                        agent_id=agent_id,
                        user_message=request.message,
                        assistant_text=save_text,
                        assistant_payload_json=save_content,
                        assistant_segments=segments_list,
                        prefilled_user_message=prefilled_user_message,
                        prefilled_user_message_index=prefilled_user_message_index,
                        recall_message_span=recall_message_span,
                        user_client_id=request.user_client_id,
                        assistant_client_id=request.client_id,
                    ))
                except Exception as save_exc:
                    logger.error("[CHAT-PERSIST] finalize failed: %s", save_exc)

            try:
                # ── 双生产者合流：agent 主事件流 + sse_manager 旁路事件（子代理） ──
                merged_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
                AGENT_SENTINEL = "agent_done"
                AGENT_ERROR = "agent_error"

                async def pump_agent():
                    """消费 agent.chat() 的事件流"""
                    try:
                        # use_call_config 注入 per-request cfg；call_config=None
                        # 时 no-op → LLMClient fallback 到 self 字段（老行为）。
                        with use_call_config(call_config):
                            async for event in agent.chat(
                                request.message,
                                stream=True,
                                session_id=effective_session_id,
                                conversation_history=session_history,
                                system_extra=pending_plan_hint,
                                recall_context=recall_context,
                                defer_refine=True,
                                user_message_already_in_history=prefilled_user_message,
                            ):
                                await merged_queue.put(("agent", event))
                    except Exception as e:
                        await merged_queue.put((AGENT_ERROR, e))
                    finally:
                        await merged_queue.put((AGENT_SENTINEL, None))

                async def pump_sidecar():
                    """消费 sse_manager 旁路事件（engine.spawn 在工具 await 内推送时，此协程不阻塞）。

                    职责切分（2026-05-16 修）：
                    - chat SSE = main agent 自己的事件（thinking / text / tool_use / system_notice）
                    - sse_manager 独立长连接（useDispatchEventConsumer）= 跨请求 session 事件

                    chat 路径过滤掉带 `worker_id` / `route_id` 的历史旁路事件，仅放行真正属于
                    主对话的 system_notice（engine.spawn 推的子代理事件等）。
                    """
                    try:
                        last_event_id = None
                        client_last_event_id = (
                            http_request.headers.get("last-event-id")
                            if http_request is not None else None
                        )
                        if client_last_event_id:
                            try:
                                last_event_id = int(client_last_event_id)
                            except ValueError:
                                pass
                        async for event in sse_manager.subscribe(effective_session_id, last_event_id=last_event_id):
                            # 过滤 worker / dispatch 事件：它们归独立长连接消费，不再混入 chat SSE
                            if isinstance(event, dict) and (event.get("worker_id") or event.get("route_id")):
                                continue
                            await merged_queue.put(("sidecar", event))
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error("[SSE-SIDECAR] error: %s", e)

                agent_task = asyncio.create_task(pump_agent())
                sidecar_task = asyncio.create_task(pump_sidecar())

                agent_error: Optional[Exception] = None
                try:
                    agent_finished = False
                    # main agent 结束就退出循环立刻 finalize。
                    # 之前 `while ... or route_sidecars:` 会让 main 本轮 finalize 被旁路事件拖住，
                    # transcript 里子任务交付可能排在 main spawn 决策前面，历史看着像
                    # "AI 先长篇分析、再说要派子任务"，倒因为果。
                    while not agent_finished:
                        kind, item = await merged_queue.get()
                        if kind == AGENT_SENTINEL:
                            agent_finished = True
                            continue
                        if kind == AGENT_ERROR:
                            agent_error = item
                            agent_finished = True
                            # 关键：之前这条路径只把异常存到 agent_error 等 finalize 写盘，
                            # **不向 SSE 流 yield 任何用户可见事件**。结果就是 OpenRouter 这种
                            # "模型转付费"等 provider 业务错误从 _chat_one_round 外层 raise 出来
                            # 时，前端啥也看不到（必须刷新会话才看到 partial_obj 里那条 ⚠️）。
                            # 现在主动 yield 一条 system_notice + 一条 content，跟 _chat_with_tools
                            # 内层 except 路径（llm_client.py:2085-2104）拉齐展示效果。
                            try:
                                from backend.llm_client import _classify_llm_exception
                                _err_type = type(item).__name__
                                _err_str = str(item) if item is not None else ""
                                _kind, _friendly = _classify_llm_exception(_err_type, _err_str)
                                yield _encode_sse({
                                    "type": "system_notice",
                                    "kind": _kind,
                                    "content": _friendly,
                                    "payload": {
                                        "error_type": _err_type,
                                        "error_message": _err_str,
                                    },
                                })
                                # provider_error 已经把 message 当 friendly 了，不再重复贴原始错误，
                                # 避免重复显示同一段；其他 kind（generic）才追加原始错误辅助排查。
                                if _kind != "llm_provider_error":
                                    yield _encode_sse({
                                        "type": "content",
                                        "text": (
                                            f"\n\n⚠️ {_friendly}\n\n"
                                            f"**原始错误**：`{_err_type}`\n\n"
                                            f"```\n{_err_str}\n```"
                                        ),
                                    })
                                else:
                                    # provider_error：直接把 friendly（=provider 给的人话）
                                    # 作为 content 发出来，让前端聊天气泡内显式渲染
                                    yield _encode_sse({
                                        "type": "content",
                                        "text": f"\n\n⚠️ {_friendly}",
                                    })
                            except Exception as _emit_exc:
                                logger.warning("[CHAT] AGENT_ERROR 路径 yield 用户可见事件失败（不致命）: %s", _emit_exc)
                            continue
                        if kind == "agent":
                            payload = _build_runtime_transport_event(item, agent_id=agent_id)
                            if payload is None:
                                continue
                            full_response += _collect_runtime_event(collected_segments, item, agent_id=agent_id)
                            yield _encode_sse(payload)
                        elif kind == "sidecar":
                            # engine 推送的 system_notice 已是 transport 格式，直接透传
                            # 不进 collected_segments：子代理过程事件不落持久化
                            # 用带 id: 行的编码，支持浏览器 Last-Event-ID 续传
                            yield encode_sse_event(item)
                finally:
                    sidecar_task.cancel()
                    try:
                        await sidecar_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    # agent_task 已通过 sentinel 自然结束，wait 防止协程悬挂
                    if not agent_task.done():
                        try:
                            await asyncio.wait_for(agent_task, timeout=1.0)
                        except (asyncio.TimeoutError, Exception):
                            agent_task.cancel()

                # 异常恢复纪律（2026-04-29 修）：agent_task 抛异常时也要 finalize 已经流出的内容。
                # 之前是直接 raise agent_error，导致 _finalize_chat_turn 整段被跳过 →
                # 用户聊了半天的对话不写盘 → session message_count=0、transcript.jsonl 不存在 →
                # 重启或刷新后整个对话凭空消失。典型场景：
                # - anthropic SDK 在某些反代错误下抛 RuntimeError("Event loop is closed")
                # - 网络抖动让 stream 中途断
                # - 工具循环里的某个 await 异常
                # 解法：异常时先尽量保存已经收到的 segments，再向前端 / 上层抛错。
                # 用户至少不会"东西没了"。
                if agent_error is not None:
                    await persist_if_needed(
                        extra_note=f"本轮中途异常：{type(agent_error).__name__}: {agent_error}"
                    )
                    raise agent_error

                # 防御：LLM 流未产生任何有效 segment 时给前端发 system_notice + content
                # 让用户在 UI 上明确看到提示。落盘的兜底文本由 persist_if_needed 内部处理。
                if not collected_segments and not (full_response or "").strip():
                    fallback_text = (
                        "本轮回复中断了，可能是网络异常 / 模型空返回 / 工具被拦。请重试。"
                    )
                    yield _encode_sse({
                        "type": "system_notice",
                        "kind": "empty_assistant_fallback",
                        "content": fallback_text,
                    })
                    yield _encode_sse({"type": "content", "text": fallback_text})

                # 正常完成：持久化（幂等，后续 finally 不会再写一次）
                await persist_if_needed()

                yield "data: [DONE]\n\n"

                # 走廊回应检测（后台处理，不阻塞用户）
                try:
                    from backend.social_bus import get_message_bus
                    bus = get_message_bus()
                    corridor_replies = await bus.process_corridor_response(agent_id, full_response, request.message)
                    if corridor_replies:
                        logger.info("[CORRIDOR] %s 回应了 %s 条走廊消息: %s", agent_id, len(corridor_replies), corridor_replies)
                except Exception as e:
                    logger.warning("[CORRIDOR] 走廊回应检测失败: %s", e)

            except asyncio.CancelledError:
                # 用户点停止 / 浏览器关页 / 网络断 → SSE generator 被 cancel
                # 持久化已收到的 segments，再让 cancel 正常 propagate（uvicorn 期望）
                try:
                    await persist_if_needed(extra_note="本轮被中断（用户停止 / 连接断开）")
                except Exception:
                    pass
                raise
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error("[ERROR] Chat exception: %s", tb)
                # 2026-04-29：识别"主流程已完成、只是 SDK/httpx cleanup 阶段抛的延迟异常"。
                # 典型：anthropic SDK 在 stream 退出时清理 httpx async client，碰上
                # uvicorn 已取消的 SSE generator 协程，loop 关闭后还想 await close →
                # RuntimeError("Event loop is closed")。这种情况 transcript 已经在
                # _finalize_chat_turn 写盘成功，前端不应该看到 error 帧。
                # 我们把它降级成 [DONE]，让前端走正常完成路径。
                err_text = str(e) or ""
                is_benign_cleanup = (
                    isinstance(e, RuntimeError)
                    and "event loop is closed" in err_text.lower()
                )
                if is_benign_cleanup:
                    logger.warning("[ERROR] 识别为 cleanup 阶段延迟异常（数据已写盘），降级为 [DONE]")
                    yield "data: [DONE]\n\n"
                else:
                    # 兜底持久化（agent_error 分支已经调过会因 persistence_done 跳过）
                    try:
                        await persist_if_needed(
                            extra_note=f"本轮异常：{type(e).__name__}: {e}"
                        )
                    except Exception:
                        pass
                    yield f"data: {json.dumps({'error': str(e), 'traceback': tb})}\n\n"
            finally:
                # 终极兜底：上面所有路径都漏了（理论上不该发生）
                if not persistence_done:
                    try:
                        await persist_if_needed(extra_note="本轮异常退出（未知原因）")
                    except Exception:
                        pass

        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        response_text = ""
        collected_segments: List[Dict[str, Any]] = []
        # use_call_config 注入 per-request cfg；call_config=None 时 no-op。
        with use_call_config(call_config):
            async for event in agent.chat(
                request.message,
                stream=False,
                session_id=effective_session_id,
                conversation_history=session_history,
                system_extra=pending_plan_hint,
                recall_context=recall_context,
                defer_refine=True,
                user_message_already_in_history=prefilled_user_message,
            ):
                response_text += _collect_runtime_event(collected_segments, event, agent_id=agent_id)

        save_obj = _build_session_payload_from_segments(
            collected_segments,
            response_text,
            agent_id=agent_id,
        )
        # 同 stream 路径的防御：空 segment 时写 fallback，避免 transcript 鬼条目。
        if not save_obj.get("segments") and not (response_text or "").strip():
            fallback_text = (
                "本轮回复中断了，可能是网络异常 / 模型空返回 / 工具被拦。请重试。"
            )
            response_text = fallback_text
            save_obj = {
                "segments": [
                    {"id": 0, "type": "text", "content": fallback_text}
                ],
            }
        save_content = json.dumps(save_obj, ensure_ascii=False)

        await _finalize_chat_turn(
            session_mgr=session_mgr,
            session=session,
            agent=agent,
            agent_id=agent_id,
            user_message=request.message,
            assistant_text=response_text,
            assistant_payload_json=save_content,
            assistant_segments=save_obj.get("segments"),
            prefilled_user_message=prefilled_user_message,
            prefilled_user_message_index=prefilled_user_message_index,
            recall_message_span=recall_message_span,
            user_client_id=request.user_client_id,
            assistant_client_id=request.client_id,
        )

        return {"content": response_text, "segments": save_obj["segments"]}



# ----- 多会话管理 -----
@app.get("/sessions")
async def list_sessions(agent_id: Optional[str] = None):
    """获取所有会话列表（支持按 agent_id 过滤）"""
    session_mgr = get_session_manager()
    sessions = await session_mgr.list_sessions(agent_id=agent_id)
    return {
        "sessions": sessions,
        "session_warnings": session_mgr.get_invalid_session_summary(),
    }


@app.post("/sessions")
async def create_session(request: SessionCreate = None):
    """创建新会话"""
    session_mgr = get_session_manager()
    resolved_agent_id = request.agent_id if request else None
    title = request.title if request and request.title else "新对话"

    try:
        session = await session_mgr.create_session(
            agent_id=resolved_agent_id or "default",
            title=title
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 返回格式兼容旧版
    return {"session": {
        "id": session.id,
        "title": session.title,
        "agent_id": session.agent_id,
        "status": session.status,
        "messageCount": session.message_count,
        "historyRevision": session.history_revision,
        "nextMessageSeq": session.next_message_seq,
        "nextEventSeq": session.next_event_seq,
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "messages": [],  # 新创建的会话没有消息
    }}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """获取指定会话详情"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 返回格式兼容旧版。client_id 仅当 transcript 持久化了的才带，老消息没有此字段。
    #
    # 新写盘格式（v3 Anthropic content blocks）下，每轮产出最多 2 条 entry：
    #   - 1 条 role=assistant，content 是 list[ContentBlock]（thinking/text/tool_use 交错保序）
    #   - 1 条 role=user，content 是 list[tool_result block]（多条 tool_result 合一条）
    # 前端契约："一轮 = 一条 assistant message 含 segments 数组"——所以：
    # - assistant entry 的 content blocks → 拆成 segments（含 tool_call segments）
    # - 后跟的 user(tool_result) entry → 把 tool_result blocks 配对回前面的 tool_call segments
    # - v2/v1 老 entry 走老路径
    messages: List[Dict] = []
    history = list(session.conversation_history)
    i = 0
    while i < len(history):
        msg = history[i]
        role = msg.get("role")
        content = msg.get("content", "")

        # ── v3: assistant content 是 list[ContentBlock] ─────────────────
        if role == "assistant" and isinstance(content, list):
            agent_id_for_msg = msg.get("agent_id")
            segments = _anthropic_blocks_to_segments(content, agent_id=agent_id_for_msg)
            # 看下一条是不是 v3 user(tool_result) —— 是的话把 tool_result 配对回 segments
            if i + 1 < len(history):
                nxt = history[i + 1]
                if (
                    nxt.get("role") == "user"
                    and isinstance(nxt.get("content"), list)
                    and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in nxt["content"]
                    )
                ):
                    # 把 user.content 里的 tool_result blocks 追加到 segments 末尾
                    # （前端 ChatRow 按 segments 顺序渲染，tool_call/tool_result 按 tool_use_id 配对）
                    tr_segments = _anthropic_blocks_to_segments(
                        nxt["content"], agent_id=agent_id_for_msg
                    )
                    # 重新分配 id（保持递增）
                    for ts in tr_segments:
                        if ts.get("type") == "tool_result":
                            ts["id"] = len(segments)
                            segments.append(ts)
                    i += 1  # 把这条 user(tool_result) 折叠掉
            message_dict = {
                "role": "assistant",
                "content": "",  # v3 前端不再读 content 字段；segments 才是渲染依据
                "timestamp": msg.get("timestamp", session.updated_at),
                "segments": segments,
            }
            client_id = msg.get("client_id")
            if client_id:
                message_dict["client_id"] = client_id
            message_seq = msg.get("message_seq")
            if isinstance(message_seq, int) and message_seq > 0:
                message_dict["message_seq"] = message_seq
            history_revision = msg.get("history_revision")
            if isinstance(history_revision, int) and history_revision > 0:
                message_dict["history_revision"] = history_revision
            messages.append(message_dict)
            i += 1
            continue

        # ── v3: user content 是 list[tool_result block]，但前面没 assistant ──
        # 这种情况是孤儿 tool_result（不该出现，但容错跳过）
        if role == "user" and isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            i += 1
            continue

        # ── v1 兼容: content 是 segments JSON string，或纯 user/system 消息 ─
        message_dict = {
            "role": role,
            "content": content if isinstance(content, str) else "",
            "timestamp": msg.get("timestamp", session.updated_at),
            "segments": _extract_message_segments(role, content if isinstance(content, str) else ""),
        }
        client_id = msg.get("client_id")
        if client_id:
            message_dict["client_id"] = client_id
        message_seq = msg.get("message_seq")
        if isinstance(message_seq, int) and message_seq > 0:
            message_dict["message_seq"] = message_seq
        history_revision = msg.get("history_revision")
        if isinstance(history_revision, int) and history_revision > 0:
            message_dict["history_revision"] = history_revision
        messages.append(message_dict)
        i += 1

    return {"session": {
        "id": session.id,
        "title": session.title,
        "agent_id": session.agent_id,
        "status": session.status,
        "messageCount": session.message_count,
        "historyRevision": session.history_revision,
        "nextMessageSeq": session.next_message_seq,
        "nextEventSeq": session.next_event_seq,
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "messages": messages,
        "metadata": session.metadata or {},
        # per-session model override，见 session.list_sessions 同字段注释
        "model": session.model,
        # 与 model 配对的 provider id（2026-05-12 引入）。前端 hydrate picker
        # 时必须读这个字段而非按 model id 反查，避免同名 model 跨多 provider 走错家。
        "provider": session.provider,
    }}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除指定会话（连带清理本会话的任务）"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if session and session.agent_id:
        get_memory_manager(agent_id=session.agent_id).on_session_end(
            session_id=session.id,
            metadata=session.metadata or {},
        )
    if await session_mgr.delete_session(session_id):
        try:
            from backend.tools.builtin.task_tools import get_task_manager
            removed = get_task_manager().clear_all(session_id=session_id)
            if removed:
                logger.info("[SESSION] deleted %s tasks for session %s", removed, session_id)
        except Exception as exc:
            logger.warning("[SESSION] failed to cascade-clear tasks for %s: %s", session_id, exc)
        return {"status": "success", "message": "会话已删除"}
    raise HTTPException(status_code=404, detail="会话不存在")


@app.put("/sessions/{session_id}/title")
async def update_session_title(session_id: str, body: dict):
    """更新会话标题"""
    session_mgr = get_session_manager()
    session = await session_mgr.update_title(session_id, body.get('title', ''))
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session": {
        "id": session.id,
        "title": session.title,
        "agent_id": session.agent_id,
        "updatedAt": session.updated_at,
    }}


@app.post("/sessions/{session_id}/truncate")
async def truncate_session(session_id: str, body: dict):
    """截断会话消息：保留前 keep 条用户视角 message，之后全部删除。

    Body: ``{"keep": int}``。

    语义见 ``Session.truncate_messages``：跟随某 assistant turn 的 tool_result
    / image attachment 行**不计入 keep 但跟随保留**，避免下次 LLM 调用 tool_use
    没有 tool_result 配对而 400。

    用途：前端"编辑用户消息"（截断到该消息之前 + 用户改完发送）和"回滚到此处"
    （截断到该消息之前）共用本端点。前端只需算出"该消息在 messages 数组中的
    index"作为 ``keep`` 即可。
    """
    session_mgr = get_session_manager()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
    raw = body.get("keep")
    try:
        keep = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="keep 必须是整数")
    if keep < 0:
        raise HTTPException(status_code=400, detail="keep 不能为负数")

    result = await session_mgr.truncate_messages(session_id, keep)
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "status": "success",
        "session_id": session_id,
        "removed_lines": result.get("removed_lines", 0),
        "kept_visible": result.get("kept_visible", 0),
        "message_count": result.get("message_count", 0),
    }


@app.patch("/sessions/{session_id}/messages/{visible_index}")
async def edit_session_message(session_id: str, visible_index: int, body: dict):
    """编辑指定"用户视角"消息的文本内容（v4 2026-05-22 引入，L2 聊天编辑）。

    Body 两种合法形态（与 ``Session.edit_message`` payload 对齐）：

    1. user 消息（原 content 是字符串）::

        {"content": "新文本"}

    2. assistant 消息（原 content 是 list[ContentBlock]）::

        {"block_patches": [
            {"block_index": 0, "type": "text", "content": "新可见文字"},
            {"block_index": 2, "type": "thinking", "content": "新思考"},
        ]}

    - ``visible_index`` 是 messages 数组 0-indexed 索引（跟 truncate 的 keep 口径一致）。
    - tool_use / tool_result 块**不可编辑**，要改请走 truncate + 重发。
    - assistant patch 的 ``type`` 必须与原块类型匹配（防错位），否则 400。

    Returns:
        ``{"status": "success", "edited": bool, ...}``
    """
    session_mgr = get_session_manager()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
    if visible_index < 0:
        raise HTTPException(status_code=400, detail="visible_index 不能为负数")

    try:
        result = await session_mgr.edit_message(session_id, visible_index, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    if not result.get("edited"):
        # 业务上不算错（比如内容相同 / 越界），但前端应能区分
        return {
            "status": "noop",
            "session_id": session_id,
            "visible_index": visible_index,
            "reason": result.get("reason", "未发生编辑"),
        }

    return {
        "status": "success",
        "session_id": session_id,
        "visible_index": visible_index,
        "edited": True,
        "role": result.get("role"),
        "fields_changed": result.get("fields_changed", 0),
    }


@app.delete("/sessions/{session_id}/messages/{visible_index}")
async def delete_session_message(session_id: str, visible_index: int):
    """删除指定"用户视角"消息（v5 2026-05-26 单条删除，对齐 Cherry "选哪条删哪条"）。

    - ``visible_index`` 是 messages 数组 0-indexed 索引（与 truncate / edit 口径一致）。
    - 删 user 普通消息：仅删该行。
    - 删 assistant turn：删该行 + 紧随其后的 tool_result / image artifact 行
      （永远不留孤儿 tool_use/tool_result 配对，下次 LLM 调用零 400 风险）。
    - emotion/mask/worker 等非 message 行**保留**（会话级状态）。

    Returns:
        ``{status: success|noop, session_id, visible_index, deleted, role, removed_lines}``
    """
    session_mgr = get_session_manager()
    if visible_index < 0:
        raise HTTPException(status_code=400, detail="visible_index 不能为负数")

    result = await session_mgr.delete_message(session_id, visible_index)
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    if not result.get("deleted"):
        return {
            "status": "noop",
            "session_id": session_id,
            "visible_index": visible_index,
            "reason": result.get("reason", "未发生删除"),
        }

    return {
        "status": "success",
        "session_id": session_id,
        "visible_index": visible_index,
        "deleted": True,
        "role": result.get("role"),
        "removed_lines": result.get("removed_lines", 0),
        "message_count": result.get("message_count"),
    }


# Pinned context 上下文图钉注入与指导原则机制

@app.get("/sessions/{session_id}/pinned")
async def get_session_pinned(session_id: str):
    """读取会话的 pinned guideline + scope items。"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "session_id": session.id,
        "guideline": session.pinned_guideline,
        "scope": list(session.pinned_scope),
    }


@app.put("/sessions/{session_id}/pinned/guideline")
async def update_session_pinned_guideline(session_id: str, body: dict):
    """设置/清空会话级 guideline。body: {guideline: str | null}。"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    raw = body.get("guideline") if isinstance(body, dict) else None
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="guideline 必须是字符串或 null")
    session.set_pinned_guideline(raw)
    return {
        "session_id": session.id,
        "guideline": session.pinned_guideline,
    }


@app.put("/sessions/{session_id}/pinned/scope")
async def update_session_pinned_scope(session_id: str, body: dict):
    """覆盖式设置会话 pinned scope。body: {scope: [ScopeItem...]}。非法条目静默丢弃。"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    raw_scope = body.get("scope") if isinstance(body, dict) else None
    if raw_scope is not None and not isinstance(raw_scope, list):
        raise HTTPException(status_code=400, detail="scope 必须是数组")
    saved = session.set_pinned_scope(raw_scope or [])
    return {
        "session_id": session.id,
        "scope": saved,
    }


# ====== Pinned context 检索 API（@ mention 候选）======

@app.post("/context/scope/match")
async def match_context_scope(body: dict):
    """模糊匹配 ScopeItem 候选（前端 @ 下拉数据源）。

    body: {query: str, types?: ['file', 'directory', 'rule', 'code_map', 'code_context'], limit?: int}
    返回: {items: ScopeItem[]}
    """
    from . import paths as paths_mod
    from .context_scope import DEFAULT_LIMIT, match_scope_items
    from .scope_item import dump_scope_items

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    query = body.get("query", "")
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query 必须是字符串")
    types_raw = body.get("types")
    types = None
    if types_raw is not None:
        if not isinstance(types_raw, list):
            raise HTTPException(status_code=400, detail="types 必须是数组")
        types = [str(t) for t in types_raw if isinstance(t, str)]
    limit_raw = body.get("limit")
    try:
        limit = int(limit_raw) if limit_raw is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit 必须是整数")
    limit = max(1, min(limit, 100))

    project_root = paths_mod.legacy_project_root()
    items = match_scope_items(
        query,
        project_root=project_root,
        types=types,
        limit=limit,
    )
    return {"items": dump_scope_items(items)}


# ====== 跨会话感知 API ======

@app.get("/sessions/{session_id}/awareness")
async def get_session_awareness(session_id: str):
    """获取同角色其他会话的感知信息（不含对话内容，隐私边界）"""
    session_mgr = get_session_manager()
    awareness = await session_mgr.get_peer_awareness(session_id)
    return {"session_id": session_id, "peers": awareness}


@app.post("/sessions/{session_id}/reactivate")
async def reactivate_session(session_id: str):
    """重新激活空闲会话"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status == "terminated":
        raise HTTPException(status_code=400, detail="已终止的会话无法重新激活")
    session.status = "active"
    session.updated_at = datetime.now().isoformat()
    session.last_active_at = session.updated_at
    session.save_meta()
    return {"session": {
        "id": session.id,
        "status": session.status,
        "updatedAt": session.updated_at,
    }}


# ----- 记忆系统 API：/memory/* → 已拆分到 backend/routes/memory_routes.py -----
# （路径与行为不变；见上方 include_router(_memory_router)）


# ----- 模型管理 API：/models/* ----
# 从 main.py 拆出（路由瘦身），路径与行为不变。handler 均函数内惰性 import。
from backend.routes import models_router as _models_router
app.include_router(_models_router)


# ----- 智能体核心 API：/agents/*（模板 / 头像 / CRUD）----
# 从 main.py 拆出（路由瘦身），路径与行为不变。
from backend.routes import agents_router as _agents_router
app.include_router(_agents_router)


# ----- 智能体状态 API：/agents/{id}/（情感 / 面具 / 文件 / 模板同步）----
# 从 main.py 拆出（路由瘦身），路径与行为不变。
from backend.routes import agent_state_router as _agent_state_router
app.include_router(_agent_state_router)


# ----- 技能管理 API：/skills/* · /skill-packs/* · /agents/{id}/skills* ----
# 从 main.py 拆出（路由瘦身），路径与行为不变。
from backend.routes import skills_router as _skills_router
app.include_router(_skills_router)


# ----- 导入/导出 API：/export/* · /import/archive ----
# 从 main.py 拆出（路由瘦身），路径与行为不变。
from backend.routes import export_import_router as _export_import_router
app.include_router(_export_import_router)


# ----- 运行预算 API：/runtime/budget ----
from backend.routes import runtime_budget_router as _runtime_budget_router
app.include_router(_runtime_budget_router)


# ----- Cron jobs API：/cron/jobs* ----
from backend.routes import cron_router as _cron_router
app.include_router(_cron_router)


# ----- 跨角色消息 API：/social/messages* ----
from backend.routes import social_messages_router as _social_messages_router
app.include_router(_social_messages_router)


# ----- 社交网络图 API：/social/graph/* ----
from backend.routes import social_graph_router as _social_graph_router
app.include_router(_social_graph_router)


# ----- 用户档案 API：/user/profile ----
from backend.routes import user_profile_router as _user_profile_router
app.include_router(_user_profile_router)


# ----- 叙事编排引擎 API：/social/orchestrator/* ----
from backend.routes import social_orchestrator_router as _social_orchestrator_router
app.include_router(_social_orchestrator_router)


# ----- 场景编排 API：/social/scenes* ----
from backend.routes import social_scenes_router as _social_scenes_router
app.include_router(_social_scenes_router)


# ----- 协作社交 API：/social/consult + /social/collab-chat ----
from backend.routes import social_collab_router as _social_collab_router
app.include_router(_social_collab_router)


# ----- 工作流引擎 API：/workflow/list + /workflow/run（子进程调 ai-quant 引擎）----
from backend.routes import workflow_router as _workflow_router
app.include_router(_workflow_router)

# ----- 发布能力 API：/publish/wechat/draft（内容流水线终稿 → 公众号草稿箱）----
from backend.routes import publish_router as _publish_router
app.include_router(_publish_router)

# ----- 量化模块 API：/quant/*（可整体剥离的自包含子系统，子进程调 ai-quant）----
try:
    from backend.quant import quant_router as _quant_router
    app.include_router(_quant_router)
except ImportError:
    pass

# ----- 内容多平台发布 API：/content/*（可整体剥离的自包含子系统）----
try:
    from backend.content import content_router as _content_router
    app.include_router(_content_router)
except ImportError:
    pass

# ----- 分步创作工作台 API：/studio/*（可整体剥离的自包含子系统，子进程调 ai-quant studio_json）----
try:
    from backend.studio import studio_router as _studio_router
    app.include_router(_studio_router)
except ImportError:
    pass


# ----- 技能管理 API：/skills/* · /skill-packs/* · /agents/{id}/skills* → 已拆分到 backend/routes/skills_routes.py -----
# （路径与行为不变；见上方 include_router(_skills_router)）


# ====== 模型管理 API：/models/* → 已拆分到 backend/routes/models_routes.py ======
# （路径与行为不变；见上方 include_router(_models_router)）


# ----- 运行预算 API：/runtime/budget → 已拆分到 backend/routes/runtime_budget_routes.py -----
# （路径与行为不变；见上方 include_router(_runtime_budget_router)）


# ----- Guardian 权限配置（路由抖到 backend/guardian_routes.py 进 Guardian 红区，避免角色自进化改 main.py 偷换权限）-----
from backend.guardian_routes import router as _guardian_router
app.include_router(_guardian_router)


# ----- v2 channels web routes（E-4'b：POST inbound + GET outbound SSE）-----
# 默认 OFF；LIURUYAN_V2_CHANNELS=1 + LIURUYAN_V2_CHANNELS_WEB=1 时端点才有
# 真行为，否则返 503（见 routes/channels_web.py）。即使 flag OFF 也安装路由
# 让 endpoint 存在 + 返 503——这跟 lifespan 装配解耦，flag 切换不需要重启。
from backend.routes import channels_web_router as _channels_web_router
app.include_router(_channels_web_router)


# ----- v2 模型配置 P1：providers CRUD（GET/POST/PUT/DELETE /providers）-----
from backend.routes import providers_router as _providers_router
app.include_router(_providers_router)


# ----- 语音转写：POST /voice/transcribe + GET/PUT /voice/config ----------
from backend.routes import voice_router as _voice_router
app.include_router(_voice_router)


# ----- 搜索引擎设置：GET/PUT /settings/search（Tavily key，柳如烟+内容模块共用）----
from backend.routes import search_router as _search_router
app.include_router(_search_router)


# ----- Fast Context 设置：GET/PUT /settings/fast-context（模型模式、自定义模型、Devin凭据）----
from backend.routes import fast_context_router as _fast_context_router
app.include_router(_fast_context_router)


# ----- TTS 语音输出：POST /tts/synthesize + GET/PUT /tts/config + POST /tts/test ----
# 零污染装配：删除 TTS 时此处 2 行 + backend/tts/ + backend/routes/tts_routes.py +
# backend/utils/should_speak.py + 前端 TTSPane.tsx + VoiceSettings.tsx 子标签即可
# 完全撤除。设计文档：_docs/handover_tts_design_2026-05-29.md
from backend.routes import tts_router as _tts_router
app.include_router(_tts_router)


# ----- 通用 LLM 工具：POST /utility/translate（思考块翻译按钮等）----------
# 不走 chat 会话流，独立 oneshot LLM 调用。沿用当前 session/agent 的 model
# 配置，不引第二个 model 选择器。设计文档：backend/routes/utility_routes.py
from backend.routes import utility_router as _utility_router
app.include_router(_utility_router)


# ----- Embedding 长期记忆配置：GET/PUT /embedding/config + /test + /stats + /reindex ----
# 默认禁用（./backend\embedding_config.py），用户在
# 设置面板配 provider/model 后启用，recall_memory 自动从 BM25 升级到向量混合检索。
from backend.routes import embedding_router as _embedding_router
app.include_router(_embedding_router)


# ----- 前端遥测收集：POST /debug/frontend_events （路径 A）---------------------
# 前端 main.tsx boot 启用 devTelemetry 后，console.error/warn + fetch/XHR + 全局错误
# 都批量上报到这里 → ~/.liuruyan/frontend_events.jsonl。AI 用 read_frontend_events
# 工具读取，让前端 bug 排查不再需要用户手动复制粘贴。设计文档：debug_routes.py 模块 docstring。
from backend.routes import debug_router as _debug_router
app.include_router(_debug_router)


# ----- .lry/ 工作区只读 API：GET /api/lry/* 给前端面板用 ----------
from backend.routes import lry_router as _lry_router
app.include_router(_lry_router)


# ----- 游戏化打磨 API：/game/*（attention / jealousy / achievements / notifications）----
# 从 main.py 拆出（路由瘦身），路径与行为不变。handler 仅依赖 game_mechanics 单例。
from backend.routes import game_router as _game_router
app.include_router(_game_router)


# ----- 监控/诊断 API：/tools/stats* · /stats/prompt_cache · /debug/flags · /system/health ----
# 从 main.py 拆出（路由瘦身），路径与行为不变。handler 均函数内惰性 import，零模块级依赖。
from backend.routes import monitoring_router as _monitoring_router
app.include_router(_monitoring_router)


# ----- 任务管理 API：/tasks/* ----
# 从 main.py 拆出（路由瘦身），路径与行为不变。handler 惰性 import task_tools。
from backend.routes import task_router as _task_router
app.include_router(_task_router)


# ----- 记忆系统 API：/memory/* ----
# 从 main.py 拆出（路由瘦身），路径与行为不变。依赖 get_memory_manager / get_agent。
from backend.routes import memory_router as _memory_router
app.include_router(_memory_router)


# ----- v2 channels admin routes（E-5：CRUD + 启停，配 SettingsView/ChannelsSettings）-----
# 同 web routes 装配模式：LIURUYAN_V2_CHANNELS=0 时端点存在但返 503。
from backend.v2_channels_routes import router as _v2_channels_admin_router
app.include_router(_v2_channels_admin_router)


# ----- MCP 子系统 REST API（默认装配；运行行为由 LIURUYAN_MCP flag + bootstrap 决定）-----
# 即使 flag OFF 也安装路由——同 v2 channels 风格：端点存在不会 404，
# controller 没 bootstrap 时 list_servers 返空、add 操作仍持久化配置。
# 设计文档：_docs/MCP_DESIGN.md
from backend.mcp_routes import router as _mcp_router
app.include_router(_mcp_router)


# ----- 工具检视台 + 白名单浅编辑（Tier 1 + Tier 2）-----
# 单一事实源是 backend.tools.builtin.clean_registry.registry。
# UI 只读 description / schema；只允许改 agent meta.json["tools"] 白名单。
# 设计契约见 backend/tools_inventory_routes.py 模块 docstring。
from backend.tools_inventory_routes import router as _tools_inventory_router
app.include_router(_tools_inventory_router)


# ----- Computer Use 控制面（M4b）-----
# 总开关 / 黑白名单 / 限流 / 立即停止 / 审计读取。
# 默认 policy.enabled=False；用户必须在 Settings → Computer Use 里显式开。
from backend.computer_use.routes import router as _computer_use_router
app.include_router(_computer_use_router)


# ----- ACP 外部 agent 状态（设置 → ACP 面板）-----
# GET /acp/external_agents 返回每个 ACP agent 当前是否可用 + 从哪借的 key + 缺什么。
# 设计文档：_docs/acp_integration_design.md 修订 7（Phase 5.5c provider 借 key）
try:
    from backend.acp.routes import router as _acp_router
    app.include_router(_acp_router)
except Exception as _acp_router_exc:  # noqa: BLE001
    logger.warning("[ACP] routes registration failed (non-fatal): %s", _acp_router_exc)


# ----- LLM Gateway（对外网关；外部客户端通过本地 endpoint + token 用柳如烟 provider 池）-----
# 默认 enabled=false（~/.liuruyan/gateway_config.json）；用户在 UI 显式打开后才真生效。
# 完整设计：_docs/GATEWAY.md ；隔离红线：.windsurf/rules/gateway-isolation.md
# 砍除流程：见隔离契约第三节"砍除清单"，5 步走人。
try:
    from backend.gateway import router as _gateway_router
    app.include_router(_gateway_router)
except Exception as _gateway_router_exc:  # noqa: BLE001
    logger.warning("[Gateway] routes registration failed (non-fatal): %s", _gateway_router_exc)


# ----- Remote Access 注册位置说明 -----
# M8a 的注册块（include_router + maybe_mount_frontend）已挪到 main.py 末尾、
# `if __name__ == "__main__":` 之前——StaticFiles 挂在 / 会拦截所有未注册路径，
# 必须晚于本文件中所有 `@app.get/post/...` 装饰器，否则 /health 等 API 会被 StaticFiles
# 抢先匹配返回 404（实际遇到过：line 3871 时期，line 3872+ 的 3000 多个 `@app.xxx`
# 路由全被静态资源 mount 抢走 → 手机访问黑屏 + 后端日志疯刷 GET /health 404）。
# 详见文件末尾 "Remote Access M8a 注册块" 段。


# ----- 对话历史 -----
@app.delete("/history")
async def clear_history(session_id: Optional[str] = None):
    """清空当前会话历史"""
    sid = session_id or "default"
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(sid)
    if session:
        if session.agent_id:
            get_memory_manager(agent_id=session.agent_id).on_session_end(
                session_id=session.id,
                metadata=session.metadata or {},
            )
        await session.close()
        session.conversation_history = []
        session.message_count = 0
        session.updated_at = datetime.now().isoformat()
        session.last_active_at = session.updated_at
        session.invalidate_context_checkpoint()
        session.save_meta()
        if session.transcript_path.exists():
            session.transcript_path.write_text("", encoding='utf-8')

    return {"status": "success", "message": "对话历史已清空"}


# 全局事件 SSE — 跨 session 订阅 EventBus 上"用户跨 session 关心"的事件
# 白名单：cron_fired/cron_jobs_changed（toast/CronJobsPanel）；tasks_changed
# （InlineTasksPanel 免轮询刷新）。新增类型必须同步加，否则不订阅就到不了浏览器。
_GLOBAL_SSE_ALLOWED_TYPES = ("cron_fired", "cron_completed", "cron_failed", "cron_jobs_changed", "tasks_changed")


@app.get("/events/global")
async def global_events_stream(request: Request):
    """跨 session 全局事件 SSE 长连接。

    与 /sessions/{sid}/events 平行：那个走 sse_manager（per-session ring
    buffer），这个走 v2 EventBus（process-wide fan-out）。

    白名单 _GLOBAL_SSE_ALLOWED_TYPES：仅 cron_fired 等真正"用户跨 session
    关心"的事件，避免把所有 LLM/工具事件灌给前端 toast。

    断线即清理订阅，不做 backlog 重放（错过就错过；transcript 已有持久副本）。
    多 tab 同开会同时收到事件，前端按 event_id 自行 dedup。
    """
    from liuruyan.buses import AgentEvent, get_event_bus

    bus = get_event_bus()
    if bus is None:
        raise HTTPException(status_code=503, detail="EventBus not initialized")

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    async def _on_event(event: AgentEvent) -> None:
        # bus.publish 出错 swallow（队列满 → 慢消费者丢事件，不阻塞 publisher）
        try:
            queue.put_nowait({
                "event_id": event.event_id,
                "type": event.type,
                "trace_id": event.trace_id,
                "session_id": event.session_id,
                "agent_id": event.agent_id,
                "payload": event.payload,
                "ts": event.ts,
            })
        except asyncio.QueueFull:
            pass

    unsubs = []
    for ev_type in _GLOBAL_SSE_ALLOWED_TYPES:
        unsubs.append(bus.subscribe(ev_type, _on_event))

    async def generate():
        # 立刻吐一个 hello 事件让前端 EventSource onopen 触发，方便调试
        yield encode_sse_event({"event_id": 0, "type": "hello", "ts": time.time()})
        try:
            while True:
                # 心跳：每 30s 没事件就吐一个 comment line 让代理 / 反向代理不掉连
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield encode_sse_event(event)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[SSE-global] error: %s", exc)
        finally:
            for un in unsubs:
                try:
                    un()
                except Exception:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ----- Cron jobs API：/cron/jobs* → 已拆分到 backend/routes/cron_routes.py -----
# （路径与行为不变；见上方 include_router(_cron_router)）


@app.get("/sessions/{session_id}/events")
async def session_events_stream(session_id: str, request: Request):
    """Phase 1B-4：per-session SSE 长连接 endpoint。

    前端独立 EventSource('/sessions/{sid}/events') 订阅本 session 的
    sse_manager channel，拿到 worker_* / task_routed / subagent_* / 心跳
    等所有事件，实时驱动派发卡 UI（不再依赖 /chat 流 + polling 重建）。

    断线续传：header `Last-Event-ID` 或 query `last_event_id` 任一指定即从该
    event_id 之后回放 ring buffer。前端首次连接显式传 `?last_event_id=0` 拿
    全量 backlog，重建 dispatch state。
    """
    last_event_id = parse_last_event_id(
        request.headers.get("last-event-id"),
        request.query_params.get("last_event_id"),
    )

    sse_manager_local = get_sse_manager()

    async def generate():
        try:
            async for event in sse_manager_local.subscribe(
                session_id,
                last_event_id=last_event_id,
                include_replay_control=True,
            ):
                yield encode_sse_event(event)
        except asyncio.CancelledError:
            # 客户端断线：fastapi 会取消该 task；正常退出
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[SSE-events] sid=%s error: %s", session_id, exc)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/sessions/{session_id}/route-history")
async def get_session_route_history(session_id: str, limit: int = 50):
    """获取会话级调度路由历史 ledger。"""
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.session_type != "main":
        raise HTTPException(status_code=400, detail="只能在主会话上查看路由历史")

    bounded_limit = max(1, min(limit, 200))
    routes = list_session_route_history(session, limit=bounded_limit, newest_first=True)
    summary = summarize_route_history_outcomes(session, limit=bounded_limit)
    return {
        "routes": routes,
        "summary": summary,
    }


# ====== 跨角色消息通道 API (Phase 3.1) ======

from backend.social_bus import (
    get_message_bus, CrossAgentMessage, PrivacyFilter,
    MSG_INFORM, MSG_REQUEST, MSG_REACT, MSG_GOSSIP,
    MSG_TASK, MSG_REPORT,
    PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH, PRIORITY_URGENT,
)


# CrossMessageSend 模型 + /social/messages* 路由
# → 已拆分到 backend/routes/social_messages_routes.py
# （路径与行为不变；见上方 include_router(_social_messages_router)）


# ConsultRequest 模型 → 已拆分到 backend/routes/social_collab_routes.py

def _extract_message_segments(role: str, content: str) -> List[Dict]:
    if role != "assistant":
        return _normalize_session_segments(None, content)

    if not content:
        return []

    try:
        parsed = json.loads(content)
        depth = 0
        while (
            depth < 3
            and isinstance(parsed, dict)
            and isinstance(parsed.get("content"), str)
            and parsed["content"].strip().startswith("{")
        ):
            try:
                inner = json.loads(parsed["content"])
            except Exception:
                break
            if isinstance(inner, dict):
                parsed = inner
                depth += 1
            else:
                break

        if isinstance(parsed, dict):
            agent_id = None
            raw_segments = parsed.get("segments")
            if isinstance(raw_segments, list):
                for segment in raw_segments:
                    if isinstance(segment, dict) and segment.get("agent_id"):
                        agent_id = segment.get("agent_id")
                        break
                return _normalize_session_segments(raw_segments, parsed.get("content", "") or "", agent_id)

            if isinstance(parsed.get("content"), str):
                return _normalize_session_segments(None, parsed["content"])
    except Exception:
        pass

    return _normalize_session_segments(None, content)

def _segments_to_anthropic_blocks(segments: List[Dict]) -> Dict[str, Any]:
    """从 segments 数组转成 Anthropic Messages API 格式的 content blocks（保留交错顺序）。

    这是 v3 写盘格式的核心 helper（参考 cline ClineStorageMessage 设计）：
    逐个把 segments 映射成 Anthropic content block，保留 text/tool_use/thinking
    的交错顺序。

    Anthropic Messages API content blocks（按 spec）：
        - {"type": "thinking", "thinking": "..."}
        - {"type": "text", "text": "..."}
        - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
        - {"type": "tool_result", "tool_use_id": "...", "content": "..."}  ← 走 user 消息

    本函数只输出 assistant 消息那部分（thinking + text + tool_use blocks），
    tool_result 单独拆出来交给调用方拼成 user 消息。

    Returns:
        {
            "assistant_blocks": list of thinking/text/tool_use blocks（按 segments 原顺序）,
            "tool_results": list of {tool_use_id, content}（按 segments 原顺序），
                            调用方拼成 user 消息：role=user, content=[tool_result, ...]
            "image_user_messages": list of {content_blocks, tool_use_ids, tool_names}
                            （cline 流派 2：本轮工具产 image 拼成的独立 user message，
                            session 写盘时单独成一条 transcript user entry，紧跟
                            tool_results user message 之后）
        }

    设计取舍：
    - tool_use.input 在 Anthropic 协议里是 dict（不是字符串）—— 跟 OpenAI 协议刚好相反，
      OpenAI 那边 arguments 是 JSON 字符串。我们盘上存 dict（更可读、解析成本低），
      调 OpenAI / 讯飞时 to_openai_messages adapter 会 json.dumps 拍成字符串。
    - thinking block 的 signature 字段我们暂不存（讯飞 anthropic 兼容协议不返；真 Anthropic
      原生返但我们目前也没把它从 LLM stream 提到 segments 里）。如果未来要支持 prompt
      caching 完整签名验证，runtime_segments 那侧需要先补 signature 透传链。
    """
    assistant_blocks: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    image_user_messages: List[Dict[str, Any]] = []

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        if stype == "thinking":
            thinking_text = seg.get("content", "") or ""
            if thinking_text:
                assistant_blocks.append({
                    "type": "thinking",
                    "thinking": thinking_text,
                })
        elif stype == "text":
            text = seg.get("content", "") or ""
            if text:
                assistant_blocks.append({
                    "type": "text",
                    "text": text,
                })
        elif stype == "tool_call":
            # runtime_segments 双写 input/arguments，优先 input
            args = seg.get("input")
            if args is None:
                args = seg.get("arguments")
            # Anthropic input 字段必须是 dict，字符串要解
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
            elif not isinstance(args, dict):
                args = {}
            assistant_blocks.append({
                "type": "tool_use",
                "id": seg.get("tool_use_id") or seg.get("id") or "",
                "name": seg.get("tool_name") or seg.get("name") or "",
                "input": args,
            })
        elif stype == "tool_result":
            # cline 流派 2 切换后：tool_result.content 永远是字符串
            # （image 已被 llm_client 拆走当 deferred user message）。
            # 兼容老 transcript：seg["content_blocks"] 历史上可能存 list[block]
            # （Stage 1b 写盘格式），照旧透传以免破坏老对话回放。
            cb = seg.get("content_blocks")
            if isinstance(cb, list) and cb:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": seg.get("tool_use_id") or "",
                    "content": cb,
                })
            else:
                result = seg.get("result")
                if result is None:
                    result = seg.get("content", "") or ""
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": seg.get("tool_use_id") or "",
                    "content": result,
                })
        elif stype == "tool_image_attachment":
            # cline 流派 2：本轮工具产出的 image 集中拼成一条独立 user message，
            # 让模型在合法位置（user message.content 里）看到真图，避免反代
            # 在转 OpenAI 协议时把 tool_result.content 内的 image 剥光。
            cb = seg.get("content_blocks")
            if isinstance(cb, list) and cb:
                image_user_messages.append({
                    "content_blocks": cb,
                    "tool_use_ids": list(seg.get("tool_use_ids") or []),
                    "tool_names": list(seg.get("tool_names") or []),
                })
        # system_notice / 其它柳如烟自有类型：不映射到 LLM 协议层
        # 仍走前端 SSE / segments 链路，跟旧逻辑一致

    return {
        "assistant_blocks": assistant_blocks,
        "tool_results": tool_results,
        "image_user_messages": image_user_messages,
    }


def _anthropic_blocks_to_segments(
    content: Any,
    agent_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """从 Anthropic content blocks 数组反向重建 segments（前端展示用）。

    与 _segments_to_anthropic_blocks 是逆向操作。读盘后 /sessions/{id} 端点用
    本函数把 content blocks 拍成前端期待的 segments 数组（顺序天然保留）。

    Args:
        content: 可能是 str（极简 assistant 只有文本）或 list[ContentBlock]
        agent_id: 协作场景下标识 segment 来源 agent

    Returns:
        前端期待的 segments 数组（thinking/text/tool_call/tool_result 各类型）
    """
    if isinstance(content, str):
        # str content（无交错时也合法，比如纯文本回复）
        if not content:
            return []
        # v4 (2026-05-22) L2 edit：纯字符串 content 没有"block 索引"概念，
        # 不设 block_index；前端编辑这类 assistant 消息要走 user 消息的纯文本编辑路径
        # （实际上后端 transcript.edit_message 的 assistant 分支只接受 list content，
        #  纯字符串 assistant content 走不到 block_patches，需要不同编辑策略）。
        return [{"id": 0, "type": "text", "content": content, "agent_id": agent_id}]

    if not isinstance(content, list):
        return []

    out: List[Dict[str, Any]] = []
    for src_idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        # v4 (2026-05-22) L2 edit：记录原始 block 在 content[] 中的下标，
        # 供前端 editSessionMessage 构造 block_patches[].block_index 用。
        # 即使内容为空被跳过的块（empty thinking/text）也占一个 src_idx，
        # 所以 src_idx ≠ len(out)，必须用 enumerate 提供的下标。
        if btype == "thinking":
            text = block.get("thinking", "") or ""
            if text:
                out.append({
                    "id": len(out),
                    "type": "thinking",
                    "content": text,
                    "agent_id": agent_id,
                    "block_index": src_idx,
                })
        elif btype == "text":
            text = block.get("text", "") or ""
            if text:
                out.append({
                    "id": len(out),
                    "type": "text",
                    "content": text,
                    "agent_id": agent_id,
                    "block_index": src_idx,
                })
        elif btype == "tool_use":
            tc_input = block.get("input", {}) or {}
            name = block.get("name", "") or ""
            tc_id = block.get("id", "") or ""
            out.append({
                "id": len(out),
                "type": "tool_call",
                "tool_name": name,
                "name": name,
                "tool_use_id": tc_id,
                "input": tc_input,
                "arguments": tc_input,
                "agent_id": agent_id,
                "block_index": src_idx,
            })
        elif btype == "tool_result":
            # tool_result block 通常出现在 user 消息里；assistant content 里出现属异常，
            # 仍尽力还原成前端 segments 让用户看到。
            # 注意：当本函数被 get_session 第二次调用合并 user(tool_result) 时，
            # 这里的 block_index 指向的是 user message 的 content[]，**不是**
            # assistant message 的——前端不应用此 block_index 编辑 assistant。
            # 编辑器只允许 text/thinking 段被选中，所以无关。
            tcid = block.get("tool_use_id", "") or ""
            tr_content = block.get("content", "") or ""
            if isinstance(tr_content, list):
                # Anthropic 协议允许 tool_result.content 是 list of text/image blocks
                tr_content = "\n".join(
                    b.get("text", "") for b in tr_content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            out.append({
                "id": len(out),
                "type": "tool_result",
                "tool_use_id": tcid,
                "content": tr_content,
                "result": tr_content,
                "agent_id": agent_id,
                "block_index": src_idx,
            })
        # 其他 block 类型（image / document）暂不映射 segments，前端目前也不消费

    return out



async def _finalize_chat_turn(
    *,
    session_mgr,
    session,
    agent,
    agent_id: str,
    user_message: str,
    assistant_text: str,
    assistant_payload_json: str,
    assistant_segments: Optional[List[Dict]] = None,
    prefilled_user_message: bool,
    prefilled_user_message_index: Optional[int],
    recall_message_span: Optional[List[int]],
    user_client_id: Optional[str] = None,
    assistant_client_id: Optional[str] = None,
) -> None:
    """Persist one completed chat turn and run all session-level post-processing.

    Phase B/C/D 后续：写盘格式从自创 segments JSON string 切到 OpenAI ChatCompletion
    标准——assistant content 是纯文本，tool_calls 顶层数组，tool_result 单独成行。

    Args:
        assistant_payload_json: 旧格式 json.dumps({content, segments})，仍保留
            因为 trigger_refine_for_turn 等下游消费它（segments 解析后供记忆提炼）
        assistant_segments: 结构化 segments 列表（runtime_segments 输出）。
            提供时走新写盘路径（OpenAI std），不提供时降级写旧字符串格式。
            两种格式 rebuild_conversation_history 都能解。

    user_client_id / assistant_client_id 来自前端 ChatRequest，端到端追踪 dedup 用。
    """
    if not session:
        return

    before_count = session.message_count
    if not prefilled_user_message:
        await session_mgr.append_message(session.id, "user", user_message, client_id=user_client_id)

    if assistant_segments is not None:
        # 新格式写盘：OpenAI ChatCompletion 标准
        await session_mgr.append_assistant_structured(
            session.id,
            segments=assistant_segments,
            text=assistant_text,
            client_id=assistant_client_id,
            agent_id=agent_id,
        )
    else:
        # 兼容降级：调用方没传 segments → 走旧字符串路径
        await session_mgr.append_message(
            session.id, "assistant", assistant_payload_json, client_id=assistant_client_id
        )

    if prefilled_user_message and prefilled_user_message_index:
        current_span = [int(prefilled_user_message_index), session.message_count]
    else:
        current_span = [before_count + 1, before_count + 2]
    if recall_message_span:
        current_span = [min(recall_message_span[0], current_span[0]), current_span[1]]

    agent.trigger_refine_for_turn(
        user_message,
        assistant_payload_json,
        session_id=session.id,
        message_span=current_span,
    )

    emotion_updater = get_emotion_updater()
    current_emotion = session.emotion_state or session.load_emotion()
    emotion_result = emotion_updater.update_emotion(
        agent_id,
        user_message,
        assistant_text,
        current_emotion=current_emotion,
    )
    if emotion_result:
        await session_mgr.update_emotion(session.id, emotion_result)
        session.emotion_state = emotion_result

    mask_sys = get_mask_system()
    mask_result = mask_sys.detect_and_update(
        agent_id,
        user_message,
        assistant_text,
        emotion_data=emotion_result or current_emotion,
    )
    if mask_result:
        await session_mgr.update_emotion(session.id, mask_result)
        session.emotion_state = mask_result

    _apply_user_relation_update(agent_id, user_message, assistant_text)


# 协作流式引擎 _stream_collab_agent_turn + consult/collab 持久化 helper
# → 已拆分到 backend/routes/social_collab_routes.py
# /social/consult → 已拆分到 backend/routes/social_collab_routes.py（见 include_router）
# /social/corridor → 已并入 backend/routes/social_messages_routes.py
# /social/bus/stats → 已并入 backend/routes/social_messages_routes.py
# /user/profile → 已拆分到 backend/routes/user_profile_routes.py
# （路径与行为不变；见上方 include_router）


# ====== 社交网络 API (Phase 3.2) ======

# social_graph 常量 import 与 ensure_social_graph 别名已随 collab 路由迁出（main 无剩余使用者）。


# ----- 社交网络图 API：/social/graph/* → 已拆分到 backend/routes/social_graph_routes.py -----
# （路径与行为不变；见上方 include_router(_social_graph_router)）


# ====== 场景编排 API (Phase 3.3) ======

# SceneCreateRequest / SceneAdvanceRequest 已随 scenes 路由迁至 routes/social_scenes_routes.py


# CollabChatRequest 模型 → 已拆分到 backend/routes/social_collab_routes.py
# ----- 场景编排 API：/social/scenes* → 已拆分到 backend/routes/social_scenes_routes.py -----
# （路径与行为不变；见上方 include_router(_social_scenes_router)。collab-chat 仍在 main.py）


# /social/collab-chat → 已拆分到 backend/routes/social_collab_routes.py
# /social/collab-chat/{scene_id} (DELETE) → 已拆分到 backend/routes/social_collab_routes.py
# /social/orchestrator/* → 已拆分到 backend/routes/social_orchestrator_routes.py
# （路径与行为不变；见上方 include_router(_social_orchestrator_router)）


@app.post("/agents/{agent_id}/open-folder")
async def open_agent_folder(agent_id: str):
    """在文件管理器中打开智能体工作目录"""
    import subprocess
    manager = get_agent_manager()
    agent_dir = manager.agents_dir / agent_id
    if not agent_dir.exists():
        return {"success": False, "error": f"目录不存在: {agent_dir}"}
    try:
        subprocess.Popen(["explorer", str(agent_dir)])
        return {"success": True, "path": str(agent_dir)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/workspace/open-folder")
async def open_workspace_folder(body: dict):
    """在文件管理器中打开指定工作目录"""
    import subprocess
    target_path = body.get("path") if isinstance(body, dict) else None
    if not target_path:
        try:
            from .lry_workspace import resolve_workspace_root
            root = resolve_workspace_root()
            target_path = str(root) if root else None
        except Exception:
            target_path = None
    if not target_path or not os.path.exists(target_path):
        return {"success": False, "error": f"目录不存在: {target_path}"}
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(target_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target_path)])
        else:
            subprocess.Popen(["xdg-open", str(target_path)])
        return {"success": True, "path": str(target_path)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/health")
async def health_check():
    """健康检查（v1 兼容路径，保留不动）"""
    manager = get_agent_manager()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "agents_count": len(manager.list_agents())
    }


# ── v2 健康检查端点（Kubernetes 风格 liveness / readiness）────────────────
# 设计：_docs/architecture_v2/02_runtime_core.md § 10
# /healthz 是 liveness：FastAPI 起来即 200，不查任何下游
# /readyz 是 readiness：依赖（数据目录 / 池 / LLM）就绪才 200，否则 503

@app.get("/healthz")
async def healthz():
    """v2 liveness probe — FastAPI 起来即 200。

    附带 v2 channels 装配状态（运维可观测）：
    - ``v2_channels``: enabled / disabled / not_configured
    - ``v2_channels_web``: 是否启用 ephemeral web channel
    - ``registered_kinds``: registry 已注册的 subagent kinds（F-1' placeholder）

    failure-mode：任何 v2 状态查询失败 → 字段填 "unknown"，liveness 仍 200
    （/healthz 设计就是"FastAPI 起来即 200"，不应该因 v2 挂掉拒绝 traffic）。
    """
    response: dict[str, object] = {"status": "ok"}

    # v2 channels 状态（best-effort）
    try:
        from backend.v2_bootstrap import get_v2_channels_handle, is_enabled, is_web_channel_enabled
        handle = get_v2_channels_handle()
        if not is_enabled():
            response["v2_channels"] = "not_configured"
        elif handle is None:
            response["v2_channels"] = "setup_failed"
        else:
            response["v2_channels"] = "running"
            response["v2_channels_web"] = handle.web_channel is not None
            try:
                response["v2_channels_count"] = len(handle.manager.list_status())
            except Exception:
                pass
    except Exception:
        response["v2_channels"] = "unknown"

    # v2 subagent registry 状态（best-effort）
    try:
        from liuruyan.subagents import get_default_registry
        kinds = get_default_registry().list_kinds()
        response["v2_subagent_kinds"] = kinds
    except Exception:
        response["v2_subagent_kinds"] = "unknown"

    # v2 F-4 ExecutorPool snapshot（best-effort）
    try:
        from liuruyan.subagents import get_default_executor_pool
        pool = get_default_executor_pool()
        if pool is None:
            response["v2_executor_pool"] = "not_configured"
        else:
            response["v2_executor_pool"] = pool.snapshot().to_dict()
    except Exception:
        response["v2_executor_pool"] = "unknown"

    return response


@app.get("/readyz")
async def readyz():
    """v2 readiness probe — 依赖就绪才 200，否则 503。

    检查项：
    - 数据目录可写
    - 至少一个 LLM 配置（models.json 至少一条）

    池（liuruyan.runtime.pools）走懒启动，不在 readyz 强制初始化；
    Phase A 阶段不要求所有池都已构造（gateway warmup 由 lifespan 决定）。
    """
    from fastapi.responses import JSONResponse

    checks: dict[str, bool] = {}

    # 1. 数据目录可写
    try:
        from backend.paths import get_paths
        paths = get_paths()
        # 尝试在 base_dir 下原子写一个探针文件
        from liuruyan.persistence.atomic import atomic_write_text
        probe = paths.base_dir / ".readyz_probe"
        atomic_write_text(probe, "ok")
        probe.unlink(missing_ok=True)
        checks["data_dir_writable"] = True
    except Exception:
        checks["data_dir_writable"] = False

    # 2. 至少一个 LLM 配置可用
    try:
        manager = get_agent_manager()
        # agents 至少 1 个 + models.json 至少 1 条
        checks["at_least_one_agent"] = len(manager.list_agents()) > 0
    except Exception:
        checks["at_least_one_agent"] = False

    if all(checks.values()):
        return {"status": "ready", "checks": checks}
    return JSONResponse(
        {"status": "not_ready", "checks": checks},
        status_code=503,
    )


# ======================================================================
# Phase 3.4 游戏化打磨 API → 已拆分到 backend/routes/game_routes.py
# （路径 /game/* 与行为不变；见上方 include_router(_game_router)）
# ======================================================================


# ====== 工具统计 / Prompt Cache / debug / system health API ======
#   → 已拆分到 backend/routes/monitoring_routes.py
# ====== 任务管理 API /tasks/* ======
#   → 已拆分到 backend/routes/task_routes.py
# （路径与行为不变；见上方 include_router(_monitoring_router / _task_router)）


# ======================================================================
# ====== 自进化系统地基 Phase 1A（WS 多路复用 + EventBus）==============
# ======================================================================
# 与现有 SSE 路径并行存在；下窗口完成 Chat.tsx 切换后合并 sse_manager。
# 详见 backend/core/__init__.py 与 _docs/handover_self_evolving_foundation_2026_04_25.md
from fastapi import WebSocket as _WebSocket
from backend.core.adapters.ws import serve_websocket as _serve_ws
from backend.core.event_bus import get_default_bus as _get_default_bus
from backend.core.inbox_message import BusEvent as _BusEvent


@app.websocket("/ws")
async def ws_endpoint(ws: _WebSocket):
    """新架构 WebSocket 多路复用入口

    协议（前后端约定，详见 backend/core/adapters/ws.py 文件头）：
      C→S: subscribe / unsubscribe / ping / pong
      S→C: subscribed / unsubscribed / event / ping / pong / error
    """
    await ws.accept()
    await _serve_ws(ws)


@app.post("/_dev/publish")
async def _dev_publish(payload: Dict[str, Any] = Body(...)):
    """[DEV] 往 EventBus 推一条事件 — 仅供前端 demo 页验证用

    Body：{"channel": "session:demo", "type": "message", "payload": {"text": "hi"}}
    """
    channel = payload.get("channel")
    if not isinstance(channel, str) or not channel:
        raise HTTPException(status_code=400, detail="channel required")
    event_type = str(payload.get("type") or "message")
    event_payload = payload.get("payload") or {}
    if not isinstance(event_payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object")
    bus = _get_default_bus()
    eid = await bus.publish(
        channel,
        _BusEvent(type=event_type, payload=event_payload),
    )
    return {"ok": True, "channel": channel, "event_id": eid}


@app.get("/_dev/bus_stats")
async def _dev_bus_stats():
    """[DEV] EventBus 状态统计 — 排障用"""
    return _get_default_bus().stats()


# 启动入口
def _resolve_port(requested: int) -> int:
    """port=0 时向 OS 预借一个可用端口，随后立刻释放并交给 uvicorn 绑定。
    Windows 本地回环下竞态窗口极小，够用。"""
    if requested > 0:
        return requested
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server() -> None:
    """启动 uvicorn。被 `python backend/main.py` 和打包后的 _entry.py 两处调用。"""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="柳如烟后端")
    # M8a：--host 默认值从 ~/.liuruyan/remote.json 读取（启用远程访问 → 0.0.0.0；否则 127.0.0.1）。
    # LIURUYAN_HOST 环境变量 > remote.json，让 ops/CI 显式覆盖始终生效。
    from backend.remote import _remote_host_default
    parser.add_argument("--host", default=os.environ.get("LIURUYAN_HOST") or _remote_host_default())
    parser.add_argument("--port", type=int, default=int(os.environ.get("LIURUYAN_PORT", "8001")))
    args = parser.parse_args()

    actual_port = _resolve_port(args.port)

    is_frozen = getattr(sys, "frozen", False) or "onefile_" in os.path.abspath(__file__).lower()
    prod_mode = is_frozen or os.environ.get("LIURUYAN_PROD") == "1"

    paths.seed_models_json_if_missing()
    paths.seed_templates_if_missing()
    paths.seed_skills_if_missing()
    os.environ["LIURUYAN_ACTUAL_PORT"] = str(actual_port)

    # Prod（PyInstaller 打包）传 app 对象；dev 永远走字符串形式让 uvicorn
    # 把它作为 backend.main package 导入，相对 import (`from .guardian import …`)
    # 才能正确 resolve。dev reload 默认 OFF。
    #
    # 历史教训：dev 默认开 uvicorn reload 时 watchdog 会监听 backend/ 下所有 .py。
    # 微澜（AI 角色）的工作内容就是改 backend/* 代码，每次她改完文件 watchdog 立刻
    # 重启进程 → 当前 streaming response 被截断 → 这一轮 assistant 落盘"半成品" →
    # 下一轮她读历史看不到自己干过什么，看起来像"失忆"。日志特征：
    #   "[Agent:xxx] 记忆提炼器已初始化" 反复出现（每次进程重启 lazy init 一次）
    # 设计稿 _docs/handover_history_format_align_with_industry_2026_04_28.md §7.2
    # 把这条标了"已知不在范围"，但用户体感最大痛点。
    #
    # 改完代码要生效：Ctrl+C 杀掉这个 backend 窗口，重新 `python main.py`
    # 调试 hot reload 临时开：set LIURUYAN_DEV_RELOAD=1 && python main.py
    dev_reload = os.environ.get("LIURUYAN_DEV_RELOAD") == "1" and not prod_mode
    if prod_mode:
        # PyInstaller bundled：app 对象已经在当前进程构造好，直接传
        uvicorn.run(app, host=args.host, port=actual_port, reload=False)
    elif dev_reload:
        logger.info("[uvicorn] dev reload ENABLED (LIURUYAN_DEV_RELOAD=1)；改 backend/*.py 会触发进程重启，可能打断 streaming")
        uvicorn.run(
            "backend.main:app",
            host=args.host,
            port=actual_port,
            reload=True,
        )
    else:
        logger.info("[uvicorn] dev reload DISABLED；改 backend/*.py 后请 Ctrl+C 重启此窗口（LIURUYAN_DEV_RELOAD=1 可临时启用）")
        # 注意：这里**也**用字符串形式，不能传 app 对象——因为 main.py 在 dev 模式
        # 是被启动.bat 用 `python main.py` 直接执行（顶层脚本，__package__ 为空），
        # 此时已经构造好的 app 对象 lifespan 里的 `from .guardian import ...` 相对
        # import 会在 startup 时炸 ImportError。让 uvicorn 用字符串重新 import，
        # 它会把 backend 当 package 处理，相对 import 才 resolve。
        uvicorn.run(
            "backend.main:app",
            host=args.host,
            port=actual_port,
            reload=False,
        )


# ============================================================================
# Remote Access M8a 注册块（必须放在文件末尾——所有 @app.get/post/... 装饰器之后）
# ============================================================================
#
# 为什么放这里而不是 line 3863 那种"include_router 旁边"的位置：
#
# StaticFiles 用 `app.mount("/", ..., html=True)` 挂在根路径下，意思是"任何未匹配
# 到的 / 开头请求都 fallback 到前端 dist（再找不到就返 index.html）"。FastAPI/Starlette
# 路由匹配机制是"按 app.routes 列表顺序，first match wins"——所以 mount 必须晚于本文件
# 里所有 `@app.get(...)` / `@app.post(...)` 等装饰器，否则 mount 会被插在前面，把所有
# 后续注册的具体 API 路由全部抢先匹配掉。
#
# 历史教训：M8a 落地时这段代码原本写在 line 3863 附近，但 main.py 在它之后还有 3000+
# 行的 @app.xxx 装饰器，结果所有这些路由（含 /health /sessions /providers 等核心 API）
# 在请求时全部走 StaticFiles → 磁盘上没对应文件 → 全部 404。表现：
#   - 手机从 Tailscale 访问 → 黑窗体（前端 boot 调 /providers /sessions 全 404 卡死）
#   - 后端日志疯刷 `GET /health 404 Not Found`（前端 waitForBackend 在轮询）
#
# include_router(_remote_router) 单独挪过来也合理——保持"M8a 一切代码集中在一处"
# 的可读性。完整设计 _docs/REMOTE_M8A.md §5.4；隔离契约 .windsurf/rules/remote-isolation.md
try:
    from backend.remote import router as _remote_router, maybe_mount_frontend
    app.include_router(_remote_router)
    maybe_mount_frontend(app)  # 条件挂载——找不到 dist 自动 skip（dev 模式正常路径）
except Exception as _remote_router_exc:  # noqa: BLE001
    logger.warning("[Remote] routes registration failed (non-fatal): %s", _remote_router_exc)


if __name__ == "__main__":
    run_server()
