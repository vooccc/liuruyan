"""模型能力标签推断（id 模式匹配）。

返回 tag 列表，顺序 = 注册顺序。每个 tag 是字符串，前端按色板渲染：
- free        免费（pricing=0 或 :free 后缀）
- reasoning   推理模型（o1 / o3 / r1 / reasoner / thinking）
- vision      多模态（vl / vision / 4o / claude-3 / gemini-pro / 1.5-pro）
- websearch   能联网搜索（perplexity / sonar / search）
- function_calling 默认 true，黑名单排除
- embedding   向量
- rerank      重排
- audio       音频（whisper / tts）
- image_gen   图像生成（dall-e / sd / midjourney / image / flux）

设计参考：
- references/provider-models src/renderer/src/config/models/utils.ts
- references/provider-models src/renderer/src/config/models/reasoning.ts
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set


# 黑名单：embedding / rerank 不参与 function_calling
_FC_BLACKLIST = re.compile(r"(embed|rerank|whisper|tts|dall-e|stable-diffusion|sd-|flux|midjourney|moderation|agnes-image|wanx|cogview|kolors|image-gen|image-\d)", re.IGNORECASE)

_REASONING_PATTERNS = [
    re.compile(r"\bo[1-9]\b", re.IGNORECASE),         # o1 / o3 / o5
    re.compile(r"\br1\b", re.IGNORECASE),             # deepseek-r1
    re.compile(r"deepseek[-_/]?r\d", re.IGNORECASE),  # deepseek-r1, deepseek/r1
    re.compile(r"reasoner", re.IGNORECASE),
    re.compile(r"thinking", re.IGNORECASE),
    re.compile(r"qwq", re.IGNORECASE),                # qwen QwQ
    re.compile(r"claude.*opus.*4", re.IGNORECASE),    # opus 4 thinking
    re.compile(r"gemini.*2\.0.*flash.*thinking", re.IGNORECASE),
    # cherry 同款补充（references/provider-models reasoning.ts REASONING_REGEX）
    re.compile(r"gpt-oss", re.IGNORECASE),            # OpenAI 开源推理模型
    re.compile(r"grok-(?:3-mini|4|4-fast)\b", re.IGNORECASE),  # xAI 推理模型
    re.compile(r"\bhunyuan-t1\b", re.IGNORECASE),     # 腾讯 hunyuan-t1
    re.compile(r"\bglm-zero-preview\b", re.IGNORECASE),  # 智谱 GLM-Zero
]

_VISION_PATTERNS = [
    re.compile(r"\bvl\b", re.IGNORECASE),
    re.compile(r"vision", re.IGNORECASE),
    re.compile(r"gpt-4o", re.IGNORECASE),
    re.compile(r"gpt-5", re.IGNORECASE),
    re.compile(r"gpt-?6", re.IGNORECASE),
    re.compile(r"claude-3", re.IGNORECASE),
    re.compile(r"claude-4", re.IGNORECASE),
    re.compile(r"claude-(?:fable|mythos)", re.IGNORECASE),
    re.compile(r"claude.*opus", re.IGNORECASE),
    re.compile(r"claude.*sonnet", re.IGNORECASE),
    re.compile(r"claude.*haiku", re.IGNORECASE),
    re.compile(r"gemini.*pro", re.IGNORECASE),
    re.compile(r"gemini.*flash", re.IGNORECASE),
    re.compile(r"gemini-1\.5", re.IGNORECASE),
    re.compile(r"gemini-2", re.IGNORECASE),
    re.compile(r"gemini-3", re.IGNORECASE),
    re.compile(r"gemini-4", re.IGNORECASE),
    re.compile(r"gemini.*hydrogen", re.IGNORECASE),
    re.compile(r"glm-4v", re.IGNORECASE),
    re.compile(r"qwen.*vl", re.IGNORECASE),
    re.compile(r"qwen2.*vl", re.IGNORECASE),
    re.compile(r"llava", re.IGNORECASE),
    re.compile(r"pixtral", re.IGNORECASE),
    re.compile(r"intern.*vl", re.IGNORECASE),
]

_WEBSEARCH_PATTERNS = [
    re.compile(r"perplexity", re.IGNORECASE),
    re.compile(r"sonar", re.IGNORECASE),
    re.compile(r":online", re.IGNORECASE),  # OpenRouter 后缀
    re.compile(r"search-preview", re.IGNORECASE),
]

_EMBEDDING_PATTERNS = [
    re.compile(r"embed", re.IGNORECASE),
    re.compile(r"text-embedding", re.IGNORECASE),
    re.compile(r"\bbge\b", re.IGNORECASE),
    re.compile(r"\bm3e\b", re.IGNORECASE),
    re.compile(r"sentence-transformer", re.IGNORECASE),
]

_RERANK_PATTERNS = [
    re.compile(r"rerank", re.IGNORECASE),
    re.compile(r"\bbge.*rerank", re.IGNORECASE),
]

_AUDIO_PATTERNS = [
    re.compile(r"whisper", re.IGNORECASE),
    re.compile(r"\btts\b", re.IGNORECASE),
    re.compile(r"voice", re.IGNORECASE),
    re.compile(r"audio", re.IGNORECASE),
]

_IMAGE_GEN_PATTERNS = [
    re.compile(r"dall-?e", re.IGNORECASE),
    re.compile(r"\bsd-", re.IGNORECASE),
    re.compile(r"stable-diffusion", re.IGNORECASE),
    re.compile(r"flux", re.IGNORECASE),
    re.compile(r"midjourney", re.IGNORECASE),
    re.compile(r"image-?gen", re.IGNORECASE),
    re.compile(r"image-?\d", re.IGNORECASE),  # gpt-image-1
    re.compile(r"agnes-image", re.IGNORECASE),  # agnes-image-2.1-flash 等
    re.compile(r"wanx", re.IGNORECASE),          # 阿里通义万象
    re.compile(r"cogview", re.IGNORECASE),        # 智谱 CogView
    re.compile(r"kolors", re.IGNORECASE),         # 快手可图
]


def _matches_any(text: str, patterns: List[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def infer_capabilities(model_id: str, name: Optional[str] = None) -> List[str]:
    """从 model_id（+ 可选 name）推断能力标签列表。

    Args:
        model_id: 厂商模型 id，如 "deepseek/deepseek-r1:free"
        name: 显示名（可选，加入匹配文本）

    Returns:
        tag 列表（无重复，注册顺序）。
    """
    if not model_id:
        return []
    text = f"{model_id} {name or ''}"
    tags: Set[str] = set()
    out: List[str] = []

    def add(tag: str) -> None:
        if tag not in tags:
            tags.add(tag)
            out.append(tag)

    # rerank 必须先于 embedding 检测：bge-reranker 也含 "bge"
    if _matches_any(text, _RERANK_PATTERNS):
        add("rerank")
        return out
    if _matches_any(text, _EMBEDDING_PATTERNS):
        add("embedding")
        return out  # embedding 模型只标 embedding，不再标其他
    if _matches_any(text, _AUDIO_PATTERNS):
        add("audio")
    if _matches_any(text, _IMAGE_GEN_PATTERNS):
        add("image_gen")
        return out

    if _matches_any(text, _REASONING_PATTERNS):
        add("reasoning")
    # GPT-5 家族：cherry 同款规则——包含 gpt-5 且不含 chat 都算 reasoning
    # 覆盖 gpt-5 / gpt-5.1 / gpt-5.2-codex / gpt-5.4-mini 等所有变体；排除 gpt-5-chat / gpt-5.1-chat
    elif re.search(r"gpt-5", text, re.IGNORECASE) and not re.search(r"\bchat\b", text, re.IGNORECASE):
        add("reasoning")
    if _matches_any(text, _VISION_PATTERNS):
        add("vision")
    if _matches_any(text, _WEBSEARCH_PATTERNS):
        add("websearch")

    # function_calling: 默认 true，黑名单排除
    if not _FC_BLACKLIST.search(text):
        add("function_calling")

    return out


def infer_context_length(model_id: str) -> Optional[int]:
    """从模型 ID 推断上下文长度（token 数）。命中失败返 ``None``。

    这里只维护公开、稳定的模型族能力；第三方别名和反代不猜测，交给
    ``ModelEntry.context_length`` 显式覆盖。上下文长度是模型元数据，不等同于
    给请求追加某个 header。
    """
    if not model_id:
        return None
    text = model_id.lower()
    # 显式数字
    m = re.search(r"(\d+)k(?:-context)?\b", text)
    if m:
        n = int(m.group(1))
        if 4 <= n <= 4000:
            return n * 1000
    if "1m" in text or "1000k" in text:
        return 1_000_000

    # xAI：Grok 4.5 官方模型页标注 500K；200K 只是长上下文计价分界线。
    model_slug = text.rsplit("/", 1)[-1]
    if model_slug in {"grok-4.5", "grok-4.5-latest", "grok-build-latest"}:
        return 500_000

    # 当前主流官方模型。精确型号必须先于下方厂商级兜底规则，避免 Kimi/DeepSeek
    # 被旧模型家族的 200K/128K 默认值提前命中。
    if model_slug == "glm-5.2":
        return 1_000_000
    if model_slug in {"kimi-k2.7-code", "kimi-k2.7-code-highspeed"}:
        return 262_144
    if model_slug == "minimax-m3":
        return 1_000_000
    if model_slug in {"deepseek-v4-pro", "deepseek-v4-flash"}:
        return 1_000_000

    # Anthropic：当前 1M 已是这些模型的原生窗口，不需要 beta header。
    if re.search(r"claude-opus-4-(?:6|7|8)(?:\b|-)", text):
        return 1_000_000
    if re.search(r"claude-sonnet-(?:4-6|5(?:-\d+)?)(?:\b|-)", text):
        return 1_000_000
    if re.search(r"claude-(?:fable-5|mythos-5|mythos-preview)(?:\b|-)", text):
        return 1_000_000
    if "claude-3" in text or "claude-opus" in text or "claude-sonnet" in text or "claude-haiku" in text:
        return 200_000

    # OpenAI：窗口属于模型本身。chat-latest 等聊天别名保持各自较小窗口。
    if model_slug in {
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6-cyber",
        "gpt-6.1-sol", "gpt-6.1-terra",
        "gpt-6.2-astra", "gpt-6.2-sol",
    }:
        return 1_050_000
    if "chat-latest" in text:
        return 128_000
    if re.search(r"gpt-5\.(?:4|5|6)(?:\b|-)", text):
        return 1_050_000
    if re.search(r"gpt-5\.1(?:\b|-)", text):
        return 400_000
    if re.search(r"gpt-4\.1(?:\b|-)", text):
        return 1_047_576
    if "gpt-4o" in text or "gpt-4-turbo" in text:
        return 128_000
    if "deepseek" in text:
        return 128_000
    if "moonshot" in text or "kimi" in text:
        return 200_000
    if "gemini" in text:
        return 1_000_000 if "1.5" in text or "2." in text else 32_768
    return None


def resolve_model_capabilities(
    model_id: str,
    provider_id: Optional[str] = None,
    *,
    explicit_tags: Optional[Sequence[str]] = None,
    name: Optional[str] = None,
) -> List[str]:
    """解析模型的有效能力标签（以已识别/配置的权威标识为主，缺失时才正则兜底）。

    优先级：
    1. 显式传入的非空 ``explicit_tags``
    2. 算力账号虚拟通道中该模型的权威 ``tags`` / ``vision`` 声明
    3. ``models.json`` 中对应 ``ModelEntry.tags``（非空即权威，不再被正则复筛覆盖）
    4. 兜底：``infer_capabilities(model_id, name)``
    """
    if explicit_tags:
        return list(explicit_tags)
    if not model_id:
        return []

    # 1. 算力账号虚拟通道（如 Google Antigravity）
    try:
        from backend.compute_accounts.models_provider import get_pro_providers, is_pro_provider

        pro_providers = get_pro_providers()
        if provider_id and is_pro_provider(provider_id):
            pid_l = provider_id.lower()
            target_vps = [
                vp for vp in pro_providers
                if (vp.get("id") or "").lower() == pid_l
                or (pid_l == "google" and vp.get("id") == "google-antigravity")
            ] or pro_providers
        elif not provider_id:
            target_vps = pro_providers
        else:
            target_vps = []

        for vp in target_vps:
            for pm in vp.get("models", []):
                if pm.get("id") == model_id:
                    pm_tags = pm.get("tags")
                    if pm_tags:
                        return list(pm_tags)
                    if "vision" in pm or "tools" in pm:
                        out_tags: List[str] = []
                        if pm.get("vision"):
                            out_tags.append("vision")
                        if pm.get("tools", True):
                            out_tags.append("function_calling")
                        return out_tags or ["chat"]
    except Exception:
        pass

    # 2. providers store（models.json 中的 ModelEntry.tags）
    entry = None
    try:
        from .store import load_providers_config_cached

        cfg = load_providers_config_cached()
        providers = cfg.get("providers") or {}
        if provider_id:
            pc = providers.get(provider_id)
            if pc is not None:
                entry = next((m for m in pc.models if m.id == model_id), None)
        else:
            for pc in providers.values():
                entry = next((m for m in pc.models if m.id == model_id), None)
                if entry is not None:
                    break
    except Exception:
        entry = None

    if entry is not None:
        if entry.tags:
            return list(entry.tags)
        if not name and entry.name:
            name = entry.name

    # 3. 缺失权威标签时，回落到模型 ID / 名称正则推断
    return infer_capabilities(model_id, name)


def model_supports_vision(
    model_id: str,
    provider_id: Optional[str] = None,
    *,
    explicit_tags: Optional[Sequence[str]] = None,
    name: Optional[str] = None,
) -> bool:
    """便捷判定：模型是否支持视觉能力（优先权威 tags，缺失才正则兜底）。"""
    return "vision" in resolve_model_capabilities(
        model_id,
        provider_id=provider_id,
        explicit_tags=explicit_tags,
        name=name,
    )


__all__ = [
    "infer_capabilities",
    "infer_context_length",
    "resolve_model_capabilities",
    "model_supports_vision",
]
