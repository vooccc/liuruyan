"""
Guardian — 角色权限守卫中间件

设计原则：
1. 单一拦截点：所有工具调用必经 Guardian，工具本身零感知权限
2. 声明式规则：权限 = 配置，不是代码
3. 三层判定：ALLOW / CONFIRM / DENY（M1 阶段只跑 ALLOW/DENY，CONFIRM 留待 M2）
4. OCap 精神：角色从未获得不被授予的能力

参考：
- OWASP AI Agent 安全指南（2026）：分层管道 + 工具声明 allowed_paths
- 微软多智能体参考架构：身份→角色→权限→过滤四层链路
- 对象能力模型（OCap）：持有即权限，未被授予的根本不存在

配置：file→default 两层 fallback，落盘 `data_dir()/guardian_config.json`。
M1 支持 per-agent overrides（路径维度的 5 个开关）。
"""

import os
import json
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

# D-4 shim：数据 + 工具函数都走 v2 单源；v1 算法（AgentCapability / Guardian.intercept）
# 不动，仅"清单 + 工具"反向 import。允许方向 backend → harness（boundary CI 红线
# 反向方向 harness → backend 才禁）。
from backend.packages.harness.liuruyan.guardrails._internal.path_utils import (
    matches_backend_path as _matches_backend_path,
    normalize_path as _normalize_path,
    segment_contains as _segment_contains,
    segment_endswith as _segment_endswith,
)
from backend.packages.harness.liuruyan.guardrails._internal.redzone_data import (
    RED_ZONE_STATIC as _V2_RED_ZONE_STATIC,
    SENSITIVE_FILES_STATIC as _V2_SENSITIVE_FILES_STATIC,
    SENSITIVE_PREFIXES_STATIC as _V2_SENSITIVE_PREFIXES_STATIC,
)

logger = logging.getLogger(__name__)


class Action(Enum):
    """权限判定结果"""
    ALLOW = "allow"       # 直接放行
    CONFIRM = "confirm"   # 需要用户确认（M2 启用）
    DENY = "deny"         # 直接拒绝


@dataclass
class AccessRule:
    """单条访问规则（兜底通用规则用，path 维度）"""
    pattern: str
    operation: str
    action: Action
    reason: str = ""


@dataclass
class AgentCapability:
    """角色能力令牌——定义该角色能做什么。

    字段分两层：
    - rules: 通用兜底规则（path glob 匹配）
    - 5 个 effective 开关：从 guardian_config 解析后的最终值（defaults ← overrides）
    - private/public_filenames: 隐私/公开文件名集合（也可被 config 覆盖）
    """
    agent_id: str
    rules: List[AccessRule] = field(default_factory=list)
    private_filenames: Set[str] = field(default_factory=set)
    public_filenames: Set[str] = field(default_factory=set)
    red_zone_paths: Set[str] = field(default_factory=set)
    sensitive_prefixes: Set[str] = field(default_factory=set)
    safe_root: Optional[str] = None  # I 任务：env 配的 break-glass，写超出此根 → DENY
    other_private: str = "DENY"
    write_other_agent: str = "DENY"
    delete: str = "ALLOW"
    execute_command: str = "ALLOW"
    write_backend: str = "DENY"

    def check(self, tool_name: str, path: str, operation: str) -> Tuple[Action, str]:
        """
        路径维度检查。
        红区先于一切——任何写/删操作命中红区直接 DENY，无视所有 override。
        然后按自己目录 / 其他角色目录 / 后端 / rules 顺序判定。
        """
        normalized = _normalize_path(path)

        # 0. 红区硬保护（自进化的最后一道防线）
        if operation == "write":
            if self._matches_red_zone(normalized, path):
                return Action.DENY, "Guardian 红区硬保护：禁止修改自身代码/配置（无视任何 override）"
            sensitive = self._matches_sensitive_prefix(normalized, path)
            if sensitive:
                return Action.DENY, f"敏感目录硬保护：禁止写入 {sensitive}（凭证/系统配置）"
            if self.safe_root and not self._is_under_safe_root(normalized):
                return Action.DENY, f"超出 SAFE_ROOT 范围：所有写操作必须在 {self.safe_root} 下"

        # 1. 自己目录 → 直接 ALLOW
        if self._is_own_directory(normalized):
            return Action.ALLOW, "自己的角色目录，完全访问"

        # 2. 其他角色目录
        if self._is_other_agent_directory(normalized):
            basename = os.path.basename(normalized)
            # 公开文件 → 只读 ALLOW
            if basename in self.public_filenames and operation == "read":
                return Action.ALLOW, f"其他角色的公开档案 ({basename})，允许只读"
            # 隐私文件 → 看 other_private 开关
            if basename in self.private_filenames:
                if self.other_private == "ALLOW":
                    return Action.ALLOW, f"override: 允许读其他角色 {basename}"
                return Action.DENY, f"其他角色的隐私文件 ({basename})，禁止访问"
            # 其他文件（写入/读取非公开非隐私）→ 写看 write_other_agent，读 DENY 兜底
            if operation == "write":
                if self.write_other_agent == "ALLOW":
                    return Action.ALLOW, f"override: 允许写其他角色目录"
                return Action.DENY, "其他角色目录禁止写入"
            return Action.DENY, "其他角色的非公开文件，禁止访问"

        # 3. 后端代码目录写保护（path 维度，由 write_backend 决定）
        if operation == "write" and _matches_backend_path(normalized) and self.write_backend == "DENY":
            return Action.DENY, "后端代码目录，禁止写入"

        # 4. 兜底通用规则匹配
        for rule in self.rules:
            if rule.operation not in (operation, "any"):
                continue
            if fnmatch.fnmatch(normalized, rule.pattern) or fnmatch.fnmatch(path, rule.pattern):
                return rule.action, rule.reason

        return Action.ALLOW, ""

    def _is_own_directory(self, path: str) -> bool:
        parts = Path(path).parts
        for i, part in enumerate(parts):
            if part == "agents" and i + 1 < len(parts):
                dir_name = parts[i + 1]
                if self.agent_id == "default" and dir_name == "default":
                    return True
                if self.agent_id in dir_name and dir_name.startswith("agent_"):
                    return True
        return False

    def _is_other_agent_directory(self, path: str) -> bool:
        parts = Path(path).parts
        if "agents" not in parts:
            return False
        idx = parts.index("agents")
        if idx == len(parts) - 1:
            return False
        dir_name = parts[idx + 1]
        if dir_name == "templates":
            return False
        if dir_name == "default":
            return self.agent_id != "default"
        if dir_name.startswith("agent_") and self.agent_id not in dir_name:
            return True
        return False

    def _matches_red_zone(self, normalized: str, original: str) -> bool:
        """红区匹配：normalized / original 路径以任一红区路径片段结尾或包含完整段。

        匹配要求"完整路径段"——`/etc/sudoers.d/myrule` 不会撞 `/etc/sudoers`；
        `myguardian.py` 不会撞 `guardian.py`。
        """
        if not self.red_zone_paths:
            return False
        norm_unix = normalized.replace("\\", "/")
        orig_unix = original.replace("\\", "/")
        for red in self.red_zone_paths:
            if not red:
                continue
            red_unix = red.replace("\\", "/")
            if _segment_endswith(norm_unix, red_unix) or _segment_endswith(orig_unix, red_unix):
                return True
            sentinel = "/" + red_unix.lstrip("/")
            if _segment_contains(norm_unix, sentinel) or _segment_contains(orig_unix, sentinel):
                return True
        return False

    def _is_under_safe_root(self, normalized: str) -> bool:
        """安全写根 break-glass：path 必须在 self.safe_root 下。"""
        if not self.safe_root:
            return True
        norm_unix = normalized.replace("\\", "/")
        root_unix = self.safe_root.replace("\\", "/").rstrip("/") + "/"
        return norm_unix == root_unix.rstrip("/") or norm_unix.startswith(root_unix)

    def _matches_sensitive_prefix(self, normalized: str, original: str) -> Optional[str]:
        """敏感目录前缀匹配：路径在任一敏感前缀下视为受保护。

        startswith 比 endswith 严格——前缀必须在路径开头位置。
        命中返回前缀字符串（用作 reason），未命中返 None。
        """
        if not self.sensitive_prefixes:
            return None
        norm_unix = normalized.replace("\\", "/")
        orig_unix = original.replace("\\", "/")
        for prefix in self.sensitive_prefixes:
            if not prefix:
                continue
            pref_unix = prefix.replace("\\", "/")
            if not pref_unix.endswith("/"):
                pref_unix = pref_unix + "/"
            # startswith：路径以前缀开头（处理绝对路径）
            if norm_unix.startswith(pref_unix) or orig_unix.startswith(pref_unix):
                return prefix
            # 兜底：路径里出现 /<prefix>/ 片段（处理 normalized 里 ~ 已展开的情况）
            sentinel = "/" + pref_unix
            if sentinel in norm_unix or sentinel in orig_unix:
                return prefix
        return None


# _normalize_path / _segment_endswith / _segment_contains / _matches_backend_path
# 已 D-4 shim 化：从 v2 _internal/path_utils 反向 import（见文件头）。


class Guardian:
    """
    守卫中间件——所有工具调用的权限检查点

    使用：
        guardian = get_guardian()
        guardian.issue("liuruyan")
        action, reason = guardian.intercept("liuruyan", "read_file", {"file_path": "..."})

    配置：
        Guardian 启动时不主动加载配置（避免循环 import），首次 intercept 或显式 reload_config 时拉。
        改了 guardian_config.json 后调 reload_config() 让 in-memory 单例立刻生效。
    """

    PATH_PARAMS: Set[str] = {
        "file_path", "path", "directory",
        "source", "destination",
        "notebook_path",
    }

    # 工具维度开关命中名单
    _TOOL_SWITCH = {
        "delete": "delete",
        "execute_command": "execute_command",
        "execute_powershell": "execute_command",
    }

    # D-4 shim：清单数据走 v2 单源（_internal/redzone_data.py）。
    # 新增红区/敏感项去那边改，v1 自动跟进。
    _RED_ZONE_STATIC: Tuple[str, ...] = _V2_RED_ZONE_STATIC
    _SENSITIVE_FILES_STATIC: Tuple[str, ...] = _V2_SENSITIVE_FILES_STATIC
    _SENSITIVE_PREFIXES_STATIC: Tuple[str, ...] = _V2_SENSITIVE_PREFIXES_STATIC

    def __init__(self):
        self.capabilities: Dict[str, AgentCapability] = {}
        self._project_root: Optional[str] = None
        self._file_cfg: Dict[str, Any] = {}
        self._config_loaded: bool = False
        self._red_zone_paths: Set[str] = set()
        self._sensitive_prefixes: Set[str] = set()
        self._safe_root: Optional[str] = None  # I 任务：从 LIURUYAN_WRITE_SAFE_ROOT env 读

    def set_project_root(self, root: str):
        self._project_root = os.path.normpath(root)

    def reload_config(self) -> None:
        """重读 guardian_config.json，清空 capabilities 缓存让下次 issue 拿新规则。

        同时重算：
        - 红区清单（静态 + guardian_config.json 自身 + 敏感文件名 endswith 命中）
        - 敏感目录前缀（~ 展开后；含 hermes 风格的 ssh/aws/gnupg/kube/docker/azure 等）
        """
        from backend import guardian_config as _gc
        from backend import paths as _paths
        self._file_cfg = _gc.load_guardian_config()
        self._config_loaded = True

        red_zone: Set[str] = set(self._RED_ZONE_STATIC)
        red_zone.update(self._SENSITIVE_FILES_STATIC)
        try:
            cfg_path = _paths.guardian_config_path()
            red_zone.add(str(cfg_path))
            red_zone.add(cfg_path.name)
        except Exception:
            pass
        try:
            secrets_path = _paths.models_secrets_path()
            red_zone.add(str(secrets_path))
        except Exception:
            pass
        self._red_zone_paths = red_zone

        # 敏感前缀：~ 展开 + 项目相对前缀都加
        prefixes: Set[str] = set()
        for raw in self._SENSITIVE_PREFIXES_STATIC:
            try:
                expanded = os.path.expanduser(raw)
                prefixes.add(expanded)
                if expanded != raw:  # ~ 展开过的也保留原始形态
                    prefixes.add(raw)
            except Exception:
                prefixes.add(raw)
        self._sensitive_prefixes = prefixes

        # SAFE_ROOT env：配了就锁死写范围，break-glass 兜底
        safe_root_env = os.environ.get("LIURUYAN_WRITE_SAFE_ROOT", "").strip()
        if safe_root_env:
            try:
                self._safe_root = os.path.realpath(os.path.expanduser(safe_root_env))
            except Exception:
                self._safe_root = None
        else:
            self._safe_root = None

        self.capabilities.clear()

    def _ensure_config_loaded(self) -> None:
        if not self._config_loaded:
            self.reload_config()

    def issue(self, agent_id: str) -> AgentCapability:
        """
        为角色颁发能力令牌。

        从 guardian_config 解析该 agent 的 effective rules（defaults ← overrides），
        填充 cap 的 5 个开关 + 隐私/公开文件名集合。
        """
        self._ensure_config_loaded()
        from backend import guardian_config as _gc

        effective = _gc.resolve_for_agent(self._file_cfg, agent_id)

        cap = AgentCapability(
            agent_id=agent_id,
            private_filenames=set(effective.get("private_files", [])),
            public_filenames=set(effective.get("public_files", [])),
            red_zone_paths=set(self._red_zone_paths),
            sensitive_prefixes=set(self._sensitive_prefixes),
            safe_root=self._safe_root,
            other_private=effective.get("other_private", "DENY"),
            write_other_agent=effective.get("write_other_agent", "DENY"),
            delete=effective.get("delete", "ALLOW"),
            execute_command=effective.get("execute_command", "ALLOW"),
            write_backend=effective.get("write_backend", "DENY"),
        )

        self.capabilities[agent_id] = cap
        return cap

    def revoke(self, agent_id: str):
        self.capabilities.pop(agent_id, None)

    def intercept(self, agent_id: str, tool_name: str, arguments: Dict) -> Tuple[Action, Optional[str]]:
        """
        统一拦截入口。返回 (Action, reason)。Action=ALLOW 时 reason 可能为 None。

        判定顺序：
          1. 确保有 cap（无则 issue）
          2. 工具维度开关（delete / execute_command）
          3. 提取路径
          4. 路径维度循环 cap.check
          5. list/search/grep 列其他角色目录的额外保护
        """
        if agent_id not in self.capabilities:
            self.issue(agent_id)
        cap = self.capabilities[agent_id]

        # 2. 工具维度开关
        switch_key = self._TOOL_SWITCH.get(tool_name)
        if switch_key:
            switch_value = getattr(cap, switch_key, "ALLOW")
            if switch_value == "DENY":
                return Action.DENY, f"工具 '{tool_name}' 在权限配置中被禁用 ({switch_key}=DENY)"

        # 2b. 危险命令 regex 检测（仅 execute_* 类）——execute_command=ALLOW 时仍拦
        if tool_name in ("execute_command", "execute_powershell"):
            cmd_str = arguments.get("command", "")
            if isinstance(cmd_str, str) and cmd_str:
                from backend.tools.defense.dangerous_patterns import detect_dangerous_command
                match = detect_dangerous_command(cmd_str)
                if match:
                    return Action.DENY, f"危险命令拦截：{match}"

        # 3. 提取路径
        paths = self._extract_paths(arguments)
        if not paths:
            logger.debug(f"[GUARDIAN] {agent_id} → {tool_name} (no paths, ALLOW)")
            return Action.ALLOW, None

        # 4. 路径维度
        operation = self._infer_operation(tool_name)
        for path in paths:
            action, reason = cap.check(tool_name, path, operation)
            logger.debug(f"[GUARDIAN] {agent_id} → {tool_name}({path}) op={operation} → {action.value}: {reason}")
            if action != Action.ALLOW:
                return action, reason

        # 5. 列目录类的额外保护
        if tool_name in ("list_directory", "grep", "grep_content"):
            for path in paths:
                if self._is_listing_other_agent_dir(agent_id, path):
                    return Action.DENY, "禁止浏览其他角色的目录结构"

        return Action.ALLOW, None

    def _extract_paths(self, arguments: Dict) -> List[str]:
        paths = []
        for param_name in self.PATH_PARAMS:
            if param_name in arguments:
                val = arguments[param_name]
                if isinstance(val, str) and val:
                    paths.append(val)
        return paths

    def _infer_operation(self, tool_name: str) -> str:
        write_tools = {
            "write_file", "edit_file", "delete", "copy", "move", "create_directory",
            "edit",
            "multi_edit",
            "notebook_edit",
        }
        execute_tools = {"execute_command", "execute_powershell"}
        if tool_name in write_tools:
            return "write"
        if tool_name in execute_tools:
            return "execute"
        return "read"

    def sanitize_output(self, agent_id: str, tool_name: str, output: str) -> str:
        """
        输出过滤——对工具返回内容做隐私裁剪。
        逐行检查：包含其他角色隐私目录路径 → 过滤；公开文件名（GENOME/SKILLS/avatar）→ 保留。
        """
        if not output:
            return output

        cap = self.capabilities.get(agent_id)
        public_set = cap.public_filenames if cap else set()

        agent_dirs = self._find_agent_dirs()
        my_dir = agent_dirs.get(agent_id)

        lines = output.split('\n')
        filtered = []
        for line in lines:
            if self._should_filter_line(agent_id, line, agent_dirs, my_dir, public_set):
                continue
            filtered.append(line)

        if not filtered:
            return "（结果已被隐私过滤，无可用内容）"
        return '\n'.join(filtered)

    def _find_agent_dirs(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if not self._project_root:
            return result
        agents_root = os.path.join(self._project_root, "agents")
        if not os.path.isdir(agents_root):
            return result

        for entry in os.listdir(agents_root):
            entry_path = os.path.join(agents_root, entry)
            if not os.path.isdir(entry_path):
                continue
            if entry == "templates":
                continue
            if entry == "default":
                result["default"] = entry
            elif entry.startswith("agent_"):
                meta_path = os.path.join(entry_path, "meta.json")
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, 'r', encoding='utf-8-sig') as f:
                            meta = json.loads(f.read())
                        aid = meta.get("id", entry)
                        result[aid] = entry
                    except Exception:
                        result[entry] = entry
                else:
                    result[entry] = entry

        return result

    def _should_filter_line(
        self,
        agent_id: str,
        line: str,
        agent_dirs: Dict[str, str],
        my_dir: Optional[str],
        public_set: Set[str],
    ) -> bool:
        # 快速预筛：行里不含 "agents/" 或 "agents\" 直接放行。
        # 防 substring 误伤：旧版用 `if "agents" in line` 会命中 "all agents"/"need agents" 等
        # 普通英文短语；用完整路径分隔符片段更严谨。
        low = line.replace('\\', '/').lower()
        if 'agents/' not in low:
            return False
        # 严格匹配 agents/<other_dir>/（或行尾 / 空白终结）。
        # 旧版 `if other_dir in line` 对 default agent 灾难性——"default" 是
        # 超泛的英文词，文档里任何 "默认值 / default config / is_default=True" 全被误屏。
        # 真正要防的场景是搜索结果路径里出现 "agents/agent_xxx/secret.json"，所以只要
        # 紧跟 agents/ 路径分隔符即可，不会误伤普通词。
        for other_id, other_dir in agent_dirs.items():
            if other_id == agent_id:
                continue
            other_low = other_dir.lower()
            # 命中完整路径片段：agents/<other_dir>/  或  agents/<other_dir>$  或
            # agents/<other_dir> 后接空白/标点（终结路径）
            needle = f'agents/{other_low}'
            idx = low.find(needle)
            if idx < 0:
                continue
            tail_pos = idx + len(needle)
            if tail_pos < len(low):
                tail = low[tail_pos]
                # 只有 / \ 空白 标点 才算路径真正结束；a-z0-9_- 继续算同词
                if tail.isalnum() or tail in ('_', '-', '.'):
                    continue
            if any(pub in line for pub in public_set):
                continue
            return True
        return False

    def _is_listing_other_agent_dir(self, agent_id: str, path: str) -> bool:
        normalized = _normalize_path(path)
        cap = self.capabilities.get(agent_id)
        if not cap:
            return False

        if cap._is_other_agent_directory(normalized):
            return True

        parts = Path(normalized).parts
        for i, part in enumerate(parts):
            if part == "agents":
                if i + 1 == len(parts):
                    return True
                subdir = parts[i + 1]
                if subdir == "templates":
                    return False
                if subdir == "default" and agent_id != "default":
                    return True
                if subdir.startswith("agent_") and agent_id not in subdir:
                    return True
        return False


# ===== 全局单例 =====
_guardian: Optional[Guardian] = None


def get_guardian() -> Guardian:
    global _guardian
    if _guardian is None:
        _guardian = Guardian()
    return _guardian
