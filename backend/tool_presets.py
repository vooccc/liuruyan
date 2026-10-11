"""工具档位预设（单一事实源）。

设计：
- 每个档位 = 一个命名过的白名单。agent meta.json 存 `tool_preset` 字段（档位 id），
  不存展开列表——档位定义在后端演进时，存量 agent 自动跟上，不会冻结在旧名单上。
- 只有「自定义」才写 meta.json 的 `tools` 列表（现有语义不变）。
- 读取侧（agent_config.get_allowed_tools）负责把 preset id 展开成工具列表；
  展开时与 registry 求交集，registry 里已下线的名字静默忽略（降级为"少数工具可用"
  而不是"把不存在的名字塞给 LLM"）。

约束：档位工具集必须覆盖 agent.py 工作模式 hint 引用的工具名
（read_file / fast_context_search / grep；multi_edit 由 hint 条件渲染兜底），
否则提示词在指挥模型调不存在的工具。
"""
from __future__ import annotations

from typing import Any, Dict, List

PRESETS: Dict[str, Dict[str, Any]] = {
    "minimal": {
        "name": "极简",
        "description": "只加载干最小活所需的 6 个工具：读/写/改文件、grep、"
                       "fast_context_search 智能检索、execute_command 跑命令。"
                       "prompt token 占用最小，适合轻量任务与低配模型。",
        "tools": [
            "read_file",
            "write_file",
            "edit",
            "grep",
            "fast_context_search",
            "execute_command",
        ],
    },
}

DEFAULT_PRESET_ID = "minimal"


def get_preset(preset_id: str | None) -> Dict[str, Any] | None:
    if not preset_id:
        return None
    return PRESETS.get(preset_id)


def expand_preset(preset_id: str) -> List[str]:
    """展开档位为工具名列表（不做 registry 校验——调用方按需过滤）。"""
    preset = PRESETS.get(preset_id)
    return list(preset["tools"]) if preset else []
