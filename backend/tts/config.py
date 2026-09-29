"""TTSConfig schema + 独立持久化（``~/.liuruyan/tts.json``）。

独立 JSON 文件，**不进 models.json**——保留零污染纪律。撤除 TTS 时删此文件，
对 models.json / providers / 任何核心配置都零影响。

schema
======
- enabled: 总开关（前端 chat 流是否消费 say segment 由这控制；后端 /tts/synthesize
  在 enabled=False 时仍然能跑，方便用户在配置面板里点试听调参）
- mode: 三档 off / key / all（详见 ``backend/utils/should_speak.py`` docstring）
- provider_type: openai_compat（M1 唯一）/ gptsovits_compat（M3 加，已预留字段）

openai_compat 字段对齐 ``POST /v1/audio/speech`` OpenAI 协议：
    body = {model, voice, input, response_format?, speed?}

gptsovits_compat 字段（M3 加）对齐 GPT-SoVITS api_v2 风格：
    body = {refer_wav_path, prompt_text, text, text_lang, ...}

设计文档：``_docs/handover_tts_design_2026-05-29.md``。
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .. import paths

ProviderType = Literal["openai_compat", "gptsovits_compat", "tencent_compat", "baidu_compat", "aliyun_compat"]
SpeakMode = Literal["off", "key", "all"]
ResponseFormat = Literal["mp3", "opus", "wav", "pcm"]


def tts_config_path():
    """``~/.liuruyan/tts.json`` 绝对路径。

    单独维护此 helper（不进 paths.py）保留零污染——撤除 TTS 时连这个 path 都不
    残留在共享基础设施里。
    """
    return paths.data_dir() / "tts.json"


class TTSConfig(BaseModel):
    """TTS 全量配置。所有字段都给默认值，缺字段 = 默认 = disabled。"""

    # ── 全局 ──────────────────────────────────────────────────
    enabled: bool = False
    """总开关。False 时前端不订阅 say segment，但配置面板仍可用（试听）。"""

    mode: SpeakMode = "off"
    """三档朗读策略，详见 ``backend.utils.should_speak`` docstring。"""

    provider_type: ProviderType = "openai_compat"
    """当前激活的 TTS 后端。"""

    streaming_enabled: bool = True
    """聊天流中是否按句切片提前合成。False 时回退到整段完成后合成。"""

    streaming_min_segment_chars: int = Field(default=12, ge=1, le=500)
    """流式切片的最小字符数，避免过短碎片频繁打 TTS。"""

    streaming_sentence_enders: str = ".!?。！？\n"
    """流式切片使用的句末符集合。"""

    streaming_protect_say_tags: bool = True
    """流式切片是否保护 <say>...</say> 标签，不在标签内部切开。"""

    streaming_flush_tail_on_complete: bool = True
    """流结束时是否合成最后一段未遇到句末符的尾巴。"""

    # ── 复用 providers store（推荐路径，2026-05-29 改造） ──────
    provider_id: str = ""
    """指向 ``models.json`` 里的 OpenAI 兼容 provider id（如 'openai' / 'siliconflow'）。

    设计动机：避免用户在两个地方（模型服务 + TTS）重复填同一份 api_key / base_url。
    跟 voice_routes 复用 providers store 的策略完全对齐。空字符串表示"未选"
    （engine 会 fallback 到下面的旧 base_url / api_key 字段，保留 M1 早期老数据兼容）。
    """

    # ── 旧字段（兼容 M1 早期保存的 tts.json；不再在新 UI 暴露）──
    base_url: str = ""
    """[兼容] 旧版 M1.0 直填的 ``/v1/audio/speech`` base URL。
    设了 provider_id 时此字段被忽略；保留只为不让老用户配置丢。
    """

    api_key: str = ""
    """[兼容] 旧版 M1.0 直填的 api_key。同上，设了 provider_id 时忽略。"""

    model: str = "tts-1"
    """模型名。OpenAI 用 ``tts-1`` / ``tts-1-hd`` / ``gpt-4o-mini-tts``；
    SiliconFlow 用 ``FunAudioLLM/CosyVoice2-0.5B``；
    edge-tts wrapper 一般填 ``tts-1`` 即可。"""

    voice: str = "alloy"
    """音色 id。OpenAI: alloy / echo / fable / onyx / nova / shimmer / ...
    edge-tts wrapper 接 voice 名（如 ``zh-CN-XiaoxiaoNeural``）也常见。"""

    response_format: ResponseFormat = "mp3"
    """音频编码。mp3 跨平台兼容性最好；wav 体积大但解码 0 损耗；opus 最小。"""

    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    """语速倍数，OpenAI 协议 0.25-4.0 范围。"""

    # ── gptsovits_compat 字段（M3 加，先预留 schema） ──────────
    gptsovits_base_url: str = "http://127.0.0.1:9880"
    """本地 GPT-SoVITS / Qwen3TTS server 地址。M3 才会用到。"""

    gptsovits_refer_wav_path: str = ""
    """参考音频绝对路径，GPT-SoVITS 风格 zero-shot 必填。"""

    gptsovits_prompt_text: str = ""
    """参考音频对应的文字稿。"""

    gptsovits_text_lang: str = "zh"
    """合成文本语言：zh / ja / en。"""

    # ── tencent_compat 字段 ─────────────────────────────────────
    tencent_secret_id: str = ""
    """腾讯云 API 密钥 ID（访问管理 → API 密钥）。和 api_key 一样存 tts.json，不写盘。"""

    tencent_secret_key: str = ""
    """腾讯云 API 密钥 Key。public_view 中脱敏为 has_tencent_secret_key 布尔。"""

    tencent_voice_type: int = 101001
    """音色 ID。推荐：101001 情感女声（温柔）/ 101002 知性 / 101003 甜美 / 101008 怀念。"""

    tencent_region: str = "ap-guangzhou"
    """腾讯云地域。默认广州（ap-guangzhou）。可选北京（ap-beijing）、上海（ap-shanghai）等。"""

    # ── baidu_compat 字段 ───────────────────────────────────────
    baidu_api_key: str = ""
    """百度云应用 API Key。public_view 中保留明文展示，便于确认当前应用。"""

    baidu_secret_key: str = ""
    """百度云应用 Secret Key。public_view 中脱敏为 has_baidu_secret_key 布尔。"""

    baidu_voice: int = 0
    """百度云发音人 per。0 女声 / 1 男声 / 3 情感合成-度逍遥 / 4 情感合成-度丫丫。"""

    baidu_speed: int = Field(default=5, ge=0, le=15)
    """百度云语速 spd，0-15，默认 5。"""

    baidu_pitch: int = Field(default=5, ge=0, le=15)
    """百度云音调 pit，0-15，默认 5。"""

    baidu_volume: int = Field(default=5, ge=0, le=15)
    """百度云音量 vol，0-15，默认 5。"""

    baidu_cuid: str = "liuruyan"
    """百度云客户端标识 cuid，任意可区分设备/应用的字符串。"""

    # ── aliyun_compat 字段 ──────────────────────────────────────
    aliyun_access_key_id: str = ""
    """阿里云 AccessKey ID。"""

    aliyun_access_key_secret: str = ""
    """阿里云 AccessKey Secret。public_view 中脱敏为 has_aliyun_access_key_secret 布尔。"""

    aliyun_appkey: str = ""
    """阿里云智能语音交互项目 AppKey。"""

    aliyun_voice: str = "xiaoyun"
    """阿里云音色名，如 xiaoyun / xiaogang / aixia / aicheng 等。"""

    aliyun_emotion: str = ""
    """阿里云多情感音色的情感参数。空串表示不指定，使用音色默认情感。"""

    aliyun_region: str = "cn-shanghai"
    """阿里云 NLS 服务地域，默认 cn-shanghai。"""

    aliyun_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    """阿里云采样率。常用 16000 / 24000。"""

    aliyun_volume: int = Field(default=50, ge=0, le=100)
    """阿里云音量，0-100。"""

    aliyun_speech_rate: int = Field(default=0, ge=-500, le=500)
    """阿里云语速，-500 到 500。"""

    aliyun_pitch_rate: int = Field(default=0, ge=-500, le=500)
    """阿里云语调，-500 到 500。"""

    # ── 网络 ──────────────────────────────────────────────────
    timeout_sec: int = 60
    """单次合成请求超时（短文本 ~3-10s，长 ~30s，60s 给足余量）。"""



def load_tts_config() -> TTSConfig:
    """从 ``~/.liuruyan/tts.json`` 读配置。文件缺失或损坏 → 返默认。"""
    p = tts_config_path()
    if not p.exists():
        return TTSConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return TTSConfig()
    try:
        return TTSConfig.model_validate(raw)
    except Exception:
        # schema 升级 / 字段不兼容 → 退默认，不阻断
        return TTSConfig()


def save_tts_config(cfg: TTSConfig) -> None:
    """整体覆盖写回。无需 merge——独立文件，没有"其他字段"要保护。"""
    p = tts_config_path()
    p.write_text(
        json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_tts_config(patch: dict) -> TTSConfig:
    """partial-update 入口：合并 patch 进当前配置后写回，返新配置。"""
    current = load_tts_config().model_dump()
    current.update({k: v for k, v in patch.items() if v is not None})
    new_cfg = TTSConfig.model_validate(current)
    save_tts_config(new_cfg)
    return new_cfg


def public_view(cfg: Optional[TTSConfig] = None) -> dict:
    """返"前端可见的"配置 dict——api_key 字段脱敏（只返是否有 key）。

    用于 GET /tts/config——api_key 永远不应回传到前端，避免 dev tools 偷看。
    """
    if cfg is None:
        cfg = load_tts_config()
    raw = cfg.model_dump()
    raw["has_api_key"] = bool(raw.pop("api_key", ""))
    raw["has_tencent_secret_key"] = bool(raw.pop("tencent_secret_key", ""))
    raw["has_baidu_secret_key"] = bool(raw.pop("baidu_secret_key", ""))
    raw["has_aliyun_access_key_secret"] = bool(raw.pop("aliyun_access_key_secret", ""))
    return raw
