"""TTS REST 路由。

端点
====

- ``POST /tts/synthesize``：把文本合成为音频，返 binary。
  body: ``{"text": "...", "mode": "off|key|all" (可选)}``。
  会先过 ``should_speak`` 过滤——mode='off' 或被兜底规则拒绝时返 204 No Content。

- ``GET /tts/config``：返当前配置（api_key 脱敏为 has_api_key 布尔）+ 当前提示词
  片段（用户复制粘贴到角色 system prompt）。

- ``PUT /tts/config``：partial-update 配置；返新配置（同样脱敏）。

- ``POST /tts/test``：试听端点。body: ``{"text": "...", "override": {...部分配置}}``。
  override 是临时不写盘的配置覆盖（用户在面板里改但还没保存时点试听）。
  **不走 should_speak**——用户既然手动点了试听，就字面合成。

零污染契约
==========
本路由只新增 1 行 ``include_router`` 到 main.py，不引用任何 LLM / agent 核心模块。

设计文档：``docs/architecture_spec.md``。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Response, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.tts import (
    TTSConfig,
    load_tts_config,
    synthesize,
)
from backend.tts.agent_override import (
    TTSAgentOverride,
    _is_relative_asset_path,
    delete_agent_override,
    expand_refer_path,
    load_agent_override,
    merge_into_config,
    update_agent_override,
)
from backend.tts.config import public_view, update_tts_config
from backend.tts.providers import TTSError
from backend.utils.should_speak import SpeakMode, should_speak

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts", tags=["tts"])


# ── system prompt 片段（用户复制粘贴用） ─────────────────────────
# 不自动注入到 agent.py（零污染）；只在 GET /tts/config 返出来，让用户手动放进角色 prompt。
SYSTEM_PROMPT_SNIPPET = """你的回复会同时显示文字 + TTS 朗读。规则：
- 默认所有内容只显示，不念
- 用 <say>...</say> 标记你想"用嘴说"的关键句（结论 / 提醒 / 确认 / 打招呼）
- 一次回复通常 0-2 个 <say>，每个 ≤ 30 字
- 长分析、代码、表格、文件路径绝不放进 <say>"""


# ── provider 推荐配置（前端默认填，可改） ──────────────────────────
# 给每个 OpenAI 兼容 provider 配一份"推荐 TTS 模型 + voice"，方便用户切 provider 时一键填好。
# 留空表示该 provider 主要做 chat，TTS 用法不主流（如 anthropic / deepseek）。

_RECOMMENDED_MODELS: Dict[str, str] = {
    "openai": "tts-1",
    "siliconflow": "FunAudioLLM/CosyVoice2-0.5B",
    "groq": "playai-tts",
    "openrouter": "",  # OpenRouter 不代理 TTS，留空让前端给警告
}

_RECOMMENDED_VOICES: Dict[str, List[str]] = {
    "openai": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
    # SiliconFlow 的 voice 必须用 "<model>:<voice>" 完整格式，
    # 单写 "alex" 会返 20047 "Invalid voice."
    # 默认列 CosyVoice2-0.5B 的 8 个预置音色（alex/anna/bella/benjamin/charles/claire/david/diana）
    "siliconflow": [
        "FunAudioLLM/CosyVoice2-0.5B:alex",
        "FunAudioLLM/CosyVoice2-0.5B:anna",
        "FunAudioLLM/CosyVoice2-0.5B:bella",
        "FunAudioLLM/CosyVoice2-0.5B:benjamin",
        "FunAudioLLM/CosyVoice2-0.5B:charles",
        "FunAudioLLM/CosyVoice2-0.5B:claire",
        "FunAudioLLM/CosyVoice2-0.5B:david",
        "FunAudioLLM/CosyVoice2-0.5B:diana",
    ],
    "groq": ["Fritz-PlayAI", "Briggs-PlayAI", "Ahsoka-PlayAI"],
    # 本地 edge-tts wrapper 接微软 Edge TTS 中文音色
    "edge-tts": [
        "zh-CN-XiaoxiaoNeural",
        "zh-CN-YunxiNeural",
        "zh-CN-XiaoyiNeural",
        "zh-CN-YunyangNeural",
    ],
}

_BAIDU_VOICES: List[Dict[str, Any]] = [
    {"per": 0, "name": "度小美", "style": "基础女声", "tags": ["女声", "通用"]},
    {"per": 1, "name": "度小宇", "style": "基础男声", "tags": ["男声", "通用"]},
    {"per": 3, "name": "度逍遥", "style": "情感男声", "tags": ["男声", "情感"]},
    {"per": 4, "name": "度丫丫", "style": "童声/情感女声", "tags": ["女声", "情感"]},
    {"per": 5, "name": "度小娇", "style": "成熟女声", "tags": ["女声"]},
    {"per": 103, "name": "度米朵", "style": "可爱女声", "tags": ["女声", "可爱"]},
    {"per": 106, "name": "度博文", "style": "情感男声", "tags": ["男声", "情感"]},
    {"per": 110, "name": "度小童", "style": "童声", "tags": ["童声"]},
    {"per": 111, "name": "度小萌", "style": "软萌女声", "tags": ["女声", "可爱"]},
    {"per": 5003, "name": "度逍遥-精品", "style": "精品男声", "tags": ["男声", "精品"]},
    {"per": 5118, "name": "度小鹿", "style": "甜美女声", "tags": ["女声", "甜美"]},
    {"per": 10600, "name": "度博文-精品", "style": "精品男声", "tags": ["男声", "精品"]},
]

_ALIYUN_VOICES: List[Dict[str, Any]] = [
    {"voice": "abin", "name": "阿斌", "style": "广东普通话", "tags": ["对话", "数字人", "中文"]},
    {"voice": "zhixiaobai", "name": "知小白", "style": "普通话女声", "tags": ["对话", "数字人", "中文"]},
    {"voice": "zhixiaoxia", "name": "知小夏", "style": "普通话女声", "tags": ["对话", "数字人", "中文"]},
    {"voice": "zhixiaomei", "name": "知小妹", "style": "普通话女声", "tags": ["直播", "数字人", "中文"]},
    {"voice": "zhigui", "name": "知柜", "style": "普通话女声", "tags": ["直播", "中文"]},
    {"voice": "zhishuo", "name": "知硕", "style": "普通话男声", "tags": ["客服", "中文"]},
    {"voice": "aixia", "name": "艾夏", "style": "普通话女声", "tags": ["客服", "数字人", "中文"]},
    {"voice": "cally", "name": "Cally", "style": "美式英文女声", "tags": ["英文"]},
    {"voice": "zhifeng_emo", "name": "知锋_多情感", "style": "多情感男声", "tags": ["多情感", "男声", "中文"]},
    {"voice": "zhibing_emo", "name": "知冰_多情感", "style": "多情感男声", "tags": ["多情感", "男声", "中文"]},
    {"voice": "zhimiao_emo", "name": "知妙_多情感", "style": "多情感女声", "tags": ["多情感", "女声", "中文"]},
    {"voice": "zhimi_emo", "name": "知米_多情感", "style": "多情感女声", "tags": ["多情感", "女声", "中文"]},
    {"voice": "zhiyan_emo", "name": "知燕_多情感", "style": "多情感女声", "tags": ["多情感", "女声", "中文"]},
    {"voice": "zhibei_emo", "name": "知贝_多情感", "style": "多情感童声", "tags": ["多情感", "童声", "中文"]},
    {"voice": "zhitian_emo", "name": "知甜_多情感", "style": "多情感女声", "tags": ["多情感", "女声", "中文"]},
    {"voice": "xiaoyun", "name": "小云", "style": "标准女声", "tags": ["lite", "女声", "中文"]},
    {"voice": "xiaogang", "name": "小刚", "style": "标准男声", "tags": ["lite", "男声", "中文"]},
    {"voice": "ruoxi", "name": "若兮", "style": "温柔女声", "tags": ["女声", "中文"]},
    {"voice": "siqi", "name": "思琪", "style": "温柔女声", "tags": ["女声", "中文"]},
    {"voice": "sijia", "name": "思佳", "style": "标准女声", "tags": ["女声", "中文"]},
    {"voice": "sicheng", "name": "思诚", "style": "标准男声", "tags": ["男声", "中文"]},
    {"voice": "aiqi", "name": "艾琪", "style": "温柔女声", "tags": ["女声", "中文"]},
    {"voice": "aijia", "name": "艾佳", "style": "标准女声", "tags": ["女声", "中文"]},
    {"voice": "aicheng", "name": "艾诚", "style": "标准男声", "tags": ["男声", "中文"]},
    {"voice": "aida", "name": "艾达", "style": "标准男声", "tags": ["男声", "中文"]},
    {"voice": "ninger", "name": "宁儿", "style": "标准女声", "tags": ["女声", "中文"]},
    {"voice": "ruilin", "name": "瑞琳", "style": "标准女声", "tags": ["女声", "中文"]},
    {"voice": "siyue", "name": "思悦", "style": "温柔女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "aiya", "name": "艾雅", "style": "严厉女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "aimei", "name": "艾美", "style": "甜美女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "aiyu", "name": "艾雨", "style": "自然女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "aiyue", "name": "艾悦", "style": "温柔女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "aijing", "name": "艾婧", "style": "严厉女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "xiaomei", "name": "小美", "style": "甜美女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "aina", "name": "艾娜", "style": "浙普女声", "tags": ["客服", "方言", "中文"]},
    {"voice": "yina", "name": "伊娜", "style": "浙普女声", "tags": ["客服", "方言", "中文"]},
    {"voice": "sijing", "name": "思婧", "style": "严厉女声", "tags": ["客服", "女声", "中文"]},
    {"voice": "sitong", "name": "思彤", "style": "儿童音", "tags": ["童声", "中文"]},
    {"voice": "xiaobei", "name": "小北", "style": "萝莉女声", "tags": ["童声", "中文"]},
    {"voice": "aitong", "name": "艾彤", "style": "儿童音", "tags": ["童声", "中文"]},
    {"voice": "aiwei", "name": "艾薇", "style": "萝莉女声", "tags": ["童声", "中文"]},
    {"voice": "aibao", "name": "艾宝", "style": "萝莉女声", "tags": ["童声", "中文"]},
    {"voice": "harry", "name": "Harry", "style": "英音男声", "tags": ["英文"]},
    {"voice": "abby", "name": "Abby", "style": "美音女声", "tags": ["英文"]},
    {"voice": "andy", "name": "Andy", "style": "美音男声", "tags": ["英文"]},
    {"voice": "eric", "name": "Eric", "style": "英音男声", "tags": ["英文"]},
    {"voice": "emily", "name": "Emily", "style": "英音女声", "tags": ["英文"]},
    {"voice": "luna", "name": "Luna", "style": "英音女声", "tags": ["英文"]},
    {"voice": "luca", "name": "Luca", "style": "英音男声", "tags": ["英文"]},
    {"voice": "wendy", "name": "Wendy", "style": "英音女声", "tags": ["英文"]},
    {"voice": "william", "name": "William", "style": "英音男声", "tags": ["英文"]},
    {"voice": "olivia", "name": "Olivia", "style": "英音女声", "tags": ["英文"]},
    {"voice": "shanshan", "name": "姗姗", "style": "粤语女声", "tags": ["方言", "粤语"]},
    {"voice": "aiyuan", "name": "艾媛", "style": "知心姐姐", "tags": ["文学", "精品", "中文"]},
    {"voice": "aiying", "name": "艾颖", "style": "软萌童声", "tags": ["文学", "精品", "童声"]},
    {"voice": "aixiang", "name": "艾祥", "style": "磁性男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aimo", "name": "艾墨", "style": "情感男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aiye", "name": "艾晔", "style": "青年男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aiting", "name": "艾婷", "style": "电台女声", "tags": ["文学", "精品", "女声"]},
    {"voice": "aifan", "name": "艾凡", "style": "情感女声", "tags": ["文学", "精品", "女声"]},
    {"voice": "lydia", "name": "Lydia", "style": "英中双语女声", "tags": ["英文", "双语"]},
    {"voice": "chuangirl", "name": "小玥", "style": "四川话女声", "tags": ["方言", "四川话"]},
    {"voice": "aishuo", "name": "艾硕", "style": "自然男声", "tags": ["客服", "男声", "中文"]},
    {"voice": "qingqing", "name": "青青", "style": "中国台湾话女声", "tags": ["方言", "台湾话"]},
    {"voice": "cuijie", "name": "翠姐", "style": "东北话女声", "tags": ["方言", "东北话"]},
    {"voice": "xiaoze", "name": "小泽", "style": "湖南重口音男声", "tags": ["方言", "湖南话"]},
    {"voice": "ainan", "name": "艾楠", "style": "广告男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aihao", "name": "艾浩", "style": "资讯男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aiming", "name": "艾茗", "style": "诙谐男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aixiao", "name": "艾笑", "style": "资讯女声", "tags": ["文学", "精品", "女声"]},
    {"voice": "aichu", "name": "艾厨", "style": "舌尖男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "aiqian", "name": "艾倩", "style": "资讯女声", "tags": ["文学", "精品", "女声"]},
    {"voice": "tomoka", "name": "智香", "style": "日语女声", "tags": ["多语种", "日语"]},
    {"voice": "tomoya", "name": "智也", "style": "日语男声", "tags": ["多语种", "日语"]},
    {"voice": "annie", "name": "Annie", "style": "美语女声", "tags": ["英文"]},
    {"voice": "aishu", "name": "艾树", "style": "资讯男声", "tags": ["文学", "精品", "男声"]},
    {"voice": "airu", "name": "艾茹", "style": "新闻女声", "tags": ["文学", "精品", "女声"]},
    {"voice": "jiajia", "name": "佳佳", "style": "粤语女声", "tags": ["方言", "粤语"]},
    {"voice": "indah", "name": "Indah", "style": "印尼语女声", "tags": ["多语种", "印尼语"]},
    {"voice": "taozi", "name": "桃子", "style": "粤语女声", "tags": ["方言", "粤语"]},
    {"voice": "guijie", "name": "柜姐", "style": "亲切女声", "tags": ["直播", "女声", "中文"]},
    {"voice": "stella", "name": "Stella", "style": "知性女声", "tags": ["通用", "女声", "中文"]},
    {"voice": "stanley", "name": "Stanley", "style": "沉稳男声", "tags": ["通用", "男声", "中文"]},
    {"voice": "kenny", "name": "Kenny", "style": "沉稳男声", "tags": ["通用", "男声", "中文"]},
    {"voice": "rosa", "name": "Rosa", "style": "自然女声", "tags": ["通用", "女声", "中文"]},
    {"voice": "farah", "name": "Farah", "style": "马来语女声", "tags": ["多语种", "马来语"]},
    {"voice": "mashu", "name": "马树", "style": "儿童剧男声", "tags": ["童声", "中文"]},
    {"voice": "zhiqi", "name": "知琪", "style": "温柔女声", "tags": ["超高清", "精品", "女声"]},
    {"voice": "zhichu", "name": "知厨", "style": "舌尖男声", "tags": ["超高清", "精品", "男声"]},
    {"voice": "xiaoxian", "name": "小仙", "style": "亲切女声", "tags": ["直播", "女声"]},
    {"voice": "yuer", "name": "悦儿", "style": "儿童剧女声", "tags": ["童声", "中文"]},
    {"voice": "maoxiaomei", "name": "猫小美", "style": "活力女声", "tags": ["直播", "女声"]},
    {"voice": "zhixiang", "name": "知祥", "style": "磁性男声", "tags": ["超高清", "精品", "男声"]},
    {"voice": "zhijia", "name": "知佳", "style": "标准女声", "tags": ["超高清", "精品", "女声"]},
    {"voice": "zhinan", "name": "知楠", "style": "广告男声", "tags": ["超高清", "精品", "男声"]},
    {"voice": "zhiqian", "name": "知倩", "style": "资讯女声", "tags": ["超高清", "精品", "女声"]},
    {"voice": "zhiru", "name": "知茹", "style": "新闻女声", "tags": ["超高清", "精品", "女声"]},
    {"voice": "zhide", "name": "知德", "style": "新闻男声", "tags": ["超高清", "精品", "男声"]},
    {"voice": "zhifei", "name": "知飞", "style": "激昂解说", "tags": ["超高清", "精品"]},
    {"voice": "aifei", "name": "艾飞", "style": "激昂解说", "tags": ["直播"]},
    {"voice": "yaqun", "name": "亚群", "style": "卖场广播", "tags": ["直播"]},
    {"voice": "qiaowei", "name": "巧薇", "style": "卖场广播", "tags": ["直播"]},
    {"voice": "dahu", "name": "大虎", "style": "东北话男声", "tags": ["方言", "东北话"]},
    {"voice": "ava", "name": "Ava", "style": "美语女生", "tags": ["英文"]},
    {"voice": "zhilun", "name": "知伦", "style": "悬疑解说", "tags": ["超高清", "精品"]},
    {"voice": "ailun", "name": "艾伦", "style": "悬疑解说", "tags": ["直播"]},
    {"voice": "jielidou", "name": "杰力豆", "style": "治愈童声", "tags": ["童声", "中文"]},
    {"voice": "zhiwei", "name": "知薇", "style": "萝莉女声", "tags": ["超高清", "精品", "童声"]},
    {"voice": "laotie", "name": "老铁", "style": "东北老铁", "tags": ["直播", "东北话"]},
    {"voice": "laomei", "name": "老妹", "style": "吆喝女声", "tags": ["直播"]},
    {"voice": "aikan", "name": "艾侃", "style": "天津话男声", "tags": ["方言", "天津话"]},
    {"voice": "tala", "name": "Tala", "style": "菲律宾语女声", "tags": ["多语种", "菲律宾语"]},
    {"voice": "zhitian", "name": "知甜", "style": "甜美女声", "tags": ["精品", "女声", "中文"]},
    {"voice": "zhiqing", "name": "知青", "style": "中国台湾话女生", "tags": ["精品", "方言", "台湾话"]},
    {"voice": "tien", "name": "Tien", "style": "越南语女声", "tags": ["多语种", "越南语"]},
    {"voice": "becca", "name": "Becca", "style": "美语客服女声", "tags": ["英文"]},
    {"voice": "kyong", "name": "Kyong", "style": "韩语女声", "tags": ["多语种", "韩语"]},
    {"voice": "masha", "name": "Masha", "style": "俄语女声", "tags": ["多语种", "俄语"]},
    {"voice": "camila", "name": "Camila", "style": "西班牙语女声", "tags": ["多语种", "西班牙语"]},
    {"voice": "perla", "name": "Perla", "style": "意大利语女声", "tags": ["多语种", "意大利语"]},
    {"voice": "zhimao", "name": "知猫", "style": "普通话女声", "tags": ["直播", "中文"]},
    {"voice": "zhiyuan", "name": "知媛", "style": "普通话女声", "tags": ["通用", "中文"]},
    {"voice": "zhiya", "name": "知雅", "style": "普通话女声", "tags": ["客服", "中文"]},
    {"voice": "zhiyue", "name": "知悦", "style": "普通话女声", "tags": ["通用", "中文"]},
    {"voice": "zhida", "name": "知达", "style": "普通话男声", "tags": ["通用", "中文"]},
    {"voice": "zhistella", "name": "知莎", "style": "普通话女声", "tags": ["通用", "中文"]},
    {"voice": "kelly", "name": "Kelly", "style": "香港粤语女声", "tags": ["方言", "粤语"]},
    {"voice": "clara", "name": "Clara", "style": "法语女声", "tags": ["多语种", "法语"]},
    {"voice": "hanna", "name": "Hanna", "style": "德语女声", "tags": ["多语种", "德语"]},
    {"voice": "waan", "name": "Waan", "style": "泰语女声", "tags": ["多语种", "泰语"]},
    {"voice": "betty", "name": "Betty", "style": "美式英文女声", "tags": ["英文"]},
    {"voice": "beth", "name": "Beth", "style": "美式英文女声", "tags": ["英文"]},
    {"voice": "cindy", "name": "Cindy", "style": "美式英文女声", "tags": ["英文"]},
    {"voice": "donna", "name": "Donna", "style": "美式英文女声", "tags": ["英文"]},
    {"voice": "eva", "name": "Eva", "style": "美式英文女声", "tags": ["英文"]},
    {"voice": "brian", "name": "Brian", "style": "美式英文男声", "tags": ["英文"]},
    {"voice": "david", "name": "David", "style": "美式英文男声", "tags": ["英文"]},
]

_ALIYUN_EMOTIONS: List[Dict[str, str]] = [
    {"value": "", "label": "不指定（使用音色默认情感）"},
    {"value": "neutral", "label": "中性 / neutral"},
    {"value": "happy", "label": "开心 / happy"},
    {"value": "angry", "label": "生气 / angry"},
    {"value": "sad", "label": "悲伤 / sad"},
    {"value": "fear", "label": "害怕 / fear"},
    {"value": "hate", "label": "憎恨 / hate"},
    {"value": "surprise", "label": "惊讶 / surprise"},
    {"value": "arousal", "label": "激动 / arousal"},
    {"value": "serious", "label": "严肃 / serious"},
    {"value": "disgust", "label": "厌恶 / disgust"},
    {"value": "jealousy", "label": "嫉妒 / jealousy"},
    {"value": "embarrassed", "label": "尴尬 / embarrassed"},
    {"value": "frustrated", "label": "沮丧 / frustrated"},
    {"value": "affectionate", "label": "深情 / affectionate"},
    {"value": "gentle", "label": "温柔 / gentle"},
    {"value": "newscast", "label": "播报 / newscast"},
    {"value": "customer-service", "label": "客服 / customer-service"},
    {"value": "story", "label": "小说 / story"},
    {"value": "living", "label": "直播 / living"},
]

# OpenRouter 只代理 chat，不支持 /audio/speech —— 黑名单排除避免误配
_PROVIDER_TTS_BLACKLIST = {"openrouter"}


def _list_eligible_providers() -> List[Dict[str, Any]]:
    """扫一遍 providers store，返"能跑 OpenAI 兼容 TTS"的 provider 子集。

    过滤条件：
    - transport == 'openai_chat'（/audio/speech 跟 /chat/completions 走同一 base）
    - enabled == True
    - id 不在 _PROVIDER_TTS_BLACKLIST

    每个 provider 附带：
    - has_api_key: 是否已存 key（用户判断"能不能直接用"）
    - is_aggregator: 聚合站标识（前端可弱化显示）
    - base_url: 用户参考
    - models: 该 provider 已配置的模型列表（用于 model 下拉）
    """
    try:
        from backend.providers.store import load_providers_config_cached
    except Exception:
        return []

    cfg = load_providers_config_cached()
    out: List[Dict[str, Any]] = []
    for pid, pc in cfg["providers"].items():
        if pid in _PROVIDER_TTS_BLACKLIST:
            continue
        if pc.transport != "openai_chat":
            continue
        if not pc.enabled:
            continue
        out.append(
            {
                "id": pc.id,
                "name": pc.name or pc.id,
                "has_api_key": bool(pc.api_key),
                "is_aggregator": pc.is_aggregator,
                "base_url": pc.base_url or "",
                # 只回传 id/name 子集，前端不需要完整 ModelEntry
                "models": [
                    {"id": m.id, "name": m.name or m.id}
                    for m in pc.models
                ],
            }
        )
    return out


# ── /tts/synthesize ──────────────────────────────────────────────


class SynthesizeBody(BaseModel):
    text: str = Field(..., description="要合成的原始文本（可含 <say> 标签）")
    mode: Optional[SpeakMode] = Field(
        default=None,
        description="临时覆盖朗读模式。None 表示使用持久化配置的 mode。",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="当前生效的角色 id。非空时合并该角色 meta.json 的 tts_override 字段（角色级覆盖全局）。",
    )


@router.post("/synthesize")
async def synthesize_endpoint(body: SynthesizeBody) -> Response:
    """正式合成：过 should_speak → 调 engine → 返 binary。

    返码：
    - 200: 成功，body 是音频 binary，Content-Type 见响应头
    - 204: 文本通过过滤后为空（含 mode=off / 被兜底规则拒绝）—— 调用方应静默
    - 400: 文本为空 / 字段错
    - 502: 上游 TTS provider 错（鉴权 / 网络 / 上游 5xx）
    - 500: 后端内部错
    """
    cfg = load_tts_config()
    mode: SpeakMode = body.mode or cfg.mode

    spoken = should_speak(body.text, mode)
    logger.info("[TTS-SYNTHESIZE] text=%r mode=%s spoken=%r", body.text, mode, spoken)
    if not spoken:
        # 不念也不报错——这是设计上的正常路径（mode=off / 长文本兜底）
        return Response(status_code=204)

    try:
        result = await synthesize(spoken, agent_id=body.agent_id)
    except TTSError as e:
        logger.warning("[TTS-ROUTE] provider error: %s", e.message, exc_info=False)
        raise HTTPException(status_code=502, detail=e.message)
    except Exception as e:  # noqa: BLE001
        logger.exception("[TTS-ROUTE] unexpected error")
        raise HTTPException(status_code=500, detail=f"内部错误：{e}")

    return Response(content=result.audio, media_type=result.content_type)


# ── /tts/config ──────────────────────────────────────────────────


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """前端面板加载入口。

    返：
    - config: 完整 TTS 配置（含新加的 provider_id；旧 base_url/api_key 兼容字段也回，
      用户切到 provider 模式后前端不再展示）
    - system_prompt_snippet: 用户复制粘贴到角色 system prompt 的片段
    - provider_types: 当前可选 provider_type 类型（M1 只有 openai_compat；M3+ 解锁更多）
    - eligible_providers: 当前 providers store 里能跑 TTS 的 provider 列表（含 models 子集）
    - recommended_models / recommended_voices: 各 provider 的推荐 model / voice 字典
    """
    return {
        "config": public_view(),
        "system_prompt_snippet": SYSTEM_PROMPT_SNIPPET,
        "provider_types": ["openai_compat", "gptsovits_compat", "tencent_compat", "baidu_compat", "aliyun_compat"],
        "eligible_providers": _list_eligible_providers(),
        "recommended_models": _RECOMMENDED_MODELS,
        "recommended_voices": _RECOMMENDED_VOICES,
        "baidu_voices": _BAIDU_VOICES,
        "aliyun_voices": _ALIYUN_VOICES,
        "aliyun_emotions": _ALIYUN_EMOTIONS,
    }


class UpdateConfigBody(BaseModel):
    """partial-update。所有字段都是可选；只更新提供的字段。"""

    enabled: Optional[bool] = None
    mode: Optional[SpeakMode] = None
    provider_type: Optional[Literal["openai_compat", "gptsovits_compat", "tencent_compat", "baidu_compat", "aliyun_compat"]] = None
    streaming_enabled: Optional[bool] = None
    streaming_min_segment_chars: Optional[int] = None
    streaming_sentence_enders: Optional[str] = None
    streaming_protect_say_tags: Optional[bool] = None
    streaming_flush_tail_on_complete: Optional[bool] = None
    provider_id: Optional[str] = None
    """推荐：指向 providers store 的 OpenAI 兼容 provider id。空串 = 清除（回退到旧字段）。"""
    base_url: Optional[str] = None
    """[兼容] 旧 M1.0 直填字段。新 UI 不展示，但 API 保留以便老用户改不改都行。"""
    api_key: Optional[str] = None
    """[兼容] 旧 M1.0 直填字段。设了 provider_id 时合成时被忽略。"""
    model: Optional[str] = None
    voice: Optional[str] = None
    response_format: Optional[Literal["mp3", "opus", "wav", "pcm"]] = None
    speed: Optional[float] = None
    gptsovits_base_url: Optional[str] = None
    gptsovits_refer_wav_path: Optional[str] = None
    gptsovits_prompt_text: Optional[str] = None
    gptsovits_text_lang: Optional[str] = None
    # 腾讯云
    tencent_secret_id: Optional[str] = None
    tencent_secret_key: Optional[str] = None
    tencent_voice_type: Optional[int] = None
    tencent_region: Optional[str] = None
    # 百度云
    baidu_api_key: Optional[str] = None
    baidu_secret_key: Optional[str] = None
    baidu_voice: Optional[int] = None
    baidu_speed: Optional[int] = None
    baidu_pitch: Optional[int] = None
    baidu_volume: Optional[int] = None
    baidu_cuid: Optional[str] = None
    # 阿里云
    aliyun_access_key_id: Optional[str] = None
    aliyun_access_key_secret: Optional[str] = None
    aliyun_appkey: Optional[str] = None
    aliyun_voice: Optional[str] = None
    aliyun_emotion: Optional[str] = None
    aliyun_region: Optional[str] = None
    aliyun_sample_rate: Optional[int] = None
    aliyun_volume: Optional[int] = None
    aliyun_speech_rate: Optional[int] = None
    aliyun_pitch_rate: Optional[int] = None
    timeout_sec: Optional[int] = None


@router.put("/config")
async def put_config(body: UpdateConfigBody) -> Dict[str, Any]:
    # 区分"不改动"(None) 与"清空"("")：None 字段不进 patch。
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        new_cfg = update_tts_config(patch)
    except Exception as e:  # pydantic 校验失败
        raise HTTPException(status_code=400, detail=f"配置无效：{e}")
    return {"status": "ok", "config": public_view(new_cfg)}


# ── /tts/test ────────────────────────────────────────────────────


class TestBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=500, description="试听文本")
    override: Optional[Dict[str, Any]] = Field(
        default=None,
        description="临时覆盖配置（不写盘）。用于面板里改了字段但未保存时试听。",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="角色 id。非空时合并该角色 meta.json 的 tts_override（角色试听用）。",
    )


@router.post("/test")
async def test_endpoint(body: TestBody) -> Response:
    """配置面板的"试听"按钮入口。

    跟 /tts/synthesize 不同：
    - **不过 should_speak**——既然用户主动点试听，就字面合成
    - 支持 override 临时配置（不写盘），方便调参不污染持久化
    - 文本上限 500 字符，避免试听被拿来当合成 API 滥用
    """
    cfg = load_tts_config()
    # 1) 角色级 override（meta.json 的 tts_override）先合并 —— 如果带 agent_id
    if body.agent_id:
        agent_override = load_agent_override(body.agent_id)
        cfg = merge_into_config(cfg, agent_override)
    # 2) 面板临时 override 后合并 —— 用户面板里"还没保存的"字段优先级更高
    if body.override:
        merged = cfg.model_dump()
        merged.update({k: v for k, v in body.override.items() if v is not None})
        try:
            cfg = TTSConfig.model_validate(merged)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"override 配置无效：{e}")

    # 3) 路径展开：合并完后，如果 refer_wav_path 是 ./tts/xxx 这种相对路径，
    # 按 agent_id 展开成 agents/<id>/tts/xxx 的绝对路径。
    # 主路由 /tts/synthesize 走 resolve_effective_config 自动展开，
    # 这里 cfg_override 路径不会自动调那条，必须自己做（M6 漏修补丁）。
    if body.agent_id and cfg.gptsovits_refer_wav_path and _is_relative_asset_path(
        cfg.gptsovits_refer_wav_path
    ):
        abs_path = expand_refer_path(cfg.gptsovits_refer_wav_path, body.agent_id)
        cfg = cfg.model_copy(update={"gptsovits_refer_wav_path": abs_path})

    try:
        result = await synthesize(body.text, cfg_override=cfg)
    except TTSError as e:
        logger.warning("[TTS-ROUTE] test error: %s", e.message)
        raise HTTPException(status_code=502, detail=e.message)
    except Exception as e:  # noqa: BLE001
        logger.exception("[TTS-ROUTE] test unexpected error")
        raise HTTPException(status_code=500, detail=f"内部错误：{e}")

    return Response(content=result.audio, media_type=result.content_type)


# ── /tts/agents/{agent_id}/override ──────────────────────────────
# 角色级 TTS 覆盖：每个角色一个音色（refer_wav_path / voice / model 等）。
# 数据存到 agent meta.json 的 tts_override 字段；详见 backend/tts/agent_override.py


class AgentOverrideUpdateBody(BaseModel):
    """角色级 override partial-update。

    特殊语义：
    - None：不动该字段
    - ""（空字符串）：从 override 里**移除**该字段（回退全局）
    - 其他：覆盖到该字段
    """

    provider_type: Optional[Literal["openai_compat", "gptsovits_compat", "tencent_compat", "baidu_compat"]] = None
    provider_id: Optional[str] = None
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    response_format: Optional[Literal["mp3", "opus", "wav", "pcm"]] = None
    gptsovits_base_url: Optional[str] = None
    gptsovits_refer_wav_path: Optional[str] = None
    gptsovits_prompt_text: Optional[str] = None
    gptsovits_text_lang: Optional[str] = None


@router.get("/agents/{agent_id}/override")
async def get_agent_override(agent_id: str) -> Dict[str, Any]:
    """读取角色级 override。无 override → 返 ``{"override": null}``。"""
    override = load_agent_override(agent_id)
    return {
        "agent_id": agent_id,
        "override": override.model_dump(exclude_none=True) if override else None,
    }


@router.put("/agents/{agent_id}/override")
async def put_agent_override(agent_id: str, body: AgentOverrideUpdateBody) -> Dict[str, Any]:
    """partial-update 角色级 override。

    返回更新后的完整 override（仅含已设字段）。
    """
    patch = body.model_dump()  # 保留 None / "" / value 三态供下游区分
    try:
        new_override = update_agent_override(agent_id, patch)
    except Exception as e:  # pydantic 校验失败 / 角色不存在
        raise HTTPException(status_code=400, detail=f"角色 override 配置无效：{e}")
    return {
        "status": "ok",
        "agent_id": agent_id,
        "override": new_override.model_dump(exclude_none=True),
    }


@router.delete("/agents/{agent_id}/override")
async def delete_agent_override_endpoint(agent_id: str) -> Dict[str, Any]:
    """完全清除角色级 override（角色回退到全局默认）。"""
    cleared = delete_agent_override(agent_id)
    return {"status": "ok", "cleared": cleared, "agent_id": agent_id}


# ── /tts/agents/{agent_id}/reference ─────────────────────────────
# 角色 TTS 参考音频文件管理：上传 / 列出 / 流播放 / 删除。
# 资产存储位置：``data_dir() / "agents" / <agent_id> / "tts" / <filename>``
# 配合 agent_override 的相对路径机制（meta.json 里 gptsovits_refer_wav_path
# 写 "./tts/reference.mp3"，引擎合成时自动展开成绝对路径）。


# 允许上传的扩展名（白名单）。GPT-SoVITS 通常吃 wav/mp3，留 ogg/flac 兼容性更好。
_ALLOWED_AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".flac", ".m4a")
# 上传文件大小上限（参考音频通常 5-30 秒，<5MB 足够；放宽到 20MB 以防高码率原始素材）
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _agent_tts_dir(agent_id: str):
    """返回 ``data_dir()/agents/<id>/tts/`` —— 不存在时**不**自动创建（写入时才建）。"""
    from backend import paths

    return paths.data_dir() / "agents" / agent_id / "tts"


def _validate_filename(filename: str) -> str:
    """文件名安全校验：禁止路径分隔符 / 父目录跳转 / 扩展名白名单。

    返回清洗后的文件名（仅 basename，强制小写扩展名）。
    """
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    # 任何包含路径分隔符或 ".." 的文件名都拒绝（防穿越）
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="文件名不合法（含路径分隔符）")
    # 扩展名白名单
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_AUDIO_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的音频格式 {ext}，仅允许 {', '.join(_ALLOWED_AUDIO_EXTS)}",
        )
    # 强制扩展名小写（避免大小写文件系统差异）
    base = Path(filename).stem
    return f"{base}{ext}"


@router.post("/agents/{agent_id}/reference")
async def upload_agent_reference(
    agent_id: str,
    file: UploadFile = File(..., description="参考音频文件（mp3/wav/ogg/flac/m4a）"),
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    """上传角色参考音频到 ``agents/<agent_id>/tts/<filename>``。

    Args:
        file: multipart 文件
        filename: 自定义保存名（含扩展名）。不传则用上传文件原名。
            最终路径写到 ``agents/<id>/tts/`` 下。
            **同名文件会被覆盖**（用户重新上传 = 替换之前的）。

    Returns:
        ``{"status": "ok", "filename": "<final_name>", "rel_path": "./tts/<final_name>"}``
        前端可直接拿 ``rel_path`` 写到 override 的 ``gptsovits_refer_wav_path``。
    """
    final_name = _validate_filename(filename or file.filename or "")

    # 大小检查：边读边校验，超限立刻断流
    target_dir = _agent_tts_dir(agent_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / final_name

    written = 0
    try:
        with open(target_path, "wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    fh.close()
                    target_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件过大（限制 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入失败：{e}")

    logger.info(
        "[TTS-ROUTE] uploaded reference for %s: %s (%d bytes)",
        agent_id, final_name, written,
    )
    return {
        "status": "ok",
        "agent_id": agent_id,
        "filename": final_name,
        "rel_path": f"./tts/{final_name}",
        "size": written,
    }


@router.get("/agents/{agent_id}/reference")
async def list_agent_references(agent_id: str) -> Dict[str, Any]:
    """列出该角色的所有参考音频文件。"""
    tts_dir = _agent_tts_dir(agent_id)
    if not tts_dir.is_dir():
        return {"agent_id": agent_id, "files": []}
    files = []
    for f in sorted(tts_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in _ALLOWED_AUDIO_EXTS:
            files.append({
                "filename": f.name,
                "rel_path": f"./tts/{f.name}",
                "size": f.stat().st_size,
            })
    return {"agent_id": agent_id, "files": files}


@router.get("/agents/{agent_id}/reference/{filename}")
async def stream_agent_reference(agent_id: str, filename: str) -> FileResponse:
    """直接返回参考音频文件流（前端 ``<audio>`` 标签可播放）。"""
    safe_name = _validate_filename(filename)
    file_path = _agent_tts_dir(agent_id) / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="参考音频不存在")
    # FastAPI FileResponse 会自动设 Content-Type 和 Range 支持
    return FileResponse(file_path, media_type=f"audio/{safe_name.split('.')[-1]}")


@router.delete("/agents/{agent_id}/reference/{filename}")
async def delete_agent_reference(agent_id: str, filename: str) -> Dict[str, Any]:
    """删除某个参考音频文件。"""
    safe_name = _validate_filename(filename)
    file_path = _agent_tts_dir(agent_id) / safe_name
    if not file_path.is_file():
        return {"status": "ok", "deleted": False, "filename": safe_name}
    file_path.unlink()
    logger.info("[TTS-ROUTE] deleted reference for %s: %s", agent_id, safe_name)
    return {"status": "ok", "deleted": True, "filename": safe_name}
