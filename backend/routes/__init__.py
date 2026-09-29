"""backend/routes — v2 gateway routes 模块。

按需独立 router，main.py 通过 ``app.include_router`` 接入。
v1 routes 仍直接在 main.py 上 ``@app.post/@app.get``。

落地：
- E-4'b-2: channels_web — POST /v2/channels/web/inbound + GET /v2/channels/web/outbound/{chat_id}
"""

from .agent_state_routes import router as agent_state_router
from .agents_routes import router as agents_router
from .channels_web import router as channels_web_router
from .cron_routes import router as cron_router
from .debug_routes import router as debug_router
from .embedding_routes import router as embedding_router
from .export_import_routes import router as export_import_router
from .game_routes import router as game_router
from .lry_routes import router as lry_router
from .memory_routes import router as memory_router
from .models_routes import router as models_router
from .monitoring_routes import router as monitoring_router
from .providers_routes import router as providers_router
from .publish_routes import router as publish_router
from .runtime_budget_routes import router as runtime_budget_router
from .fast_context_routes import router as fast_context_router
from .search_routes import router as search_router
from .skills_routes import router as skills_router
from .social_collab_routes import router as social_collab_router
from .social_graph_routes import router as social_graph_router
from .social_messages_routes import router as social_messages_router
from .social_orchestrator_routes import router as social_orchestrator_router
from .social_scenes_routes import router as social_scenes_router
from .workflow_routes import router as workflow_router
from .task_routes import router as task_router
from .user_profile_routes import router as user_profile_router
from .tts_routes import router as tts_router
from .utility_routes import router as utility_router
from .voice_routes import router as voice_router

__all__ = [
    "agent_state_router",
    "agents_router",
    "channels_web_router",
    "cron_router",
    "debug_router",
    "embedding_router",
    "export_import_router",
    "game_router",
    "lry_router",
    "memory_router",
    "models_router",
    "monitoring_router",
    "providers_router",
    "publish_router",
    "runtime_budget_router",
    "fast_context_router",
    "search_router",
    "skills_router",
    "social_collab_router",
    "social_graph_router",
    "social_messages_router",
    "social_orchestrator_router",
    "social_scenes_router",
    "workflow_router",
    "task_router",
    "tts_router",
    "user_profile_router",
    "utility_router",
    "voice_router",
]
