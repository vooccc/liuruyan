"""工具参数校验 / coerce 层（从 clean_registry.py 抽出）。

调度层在 dispatch 前用这层做两件事，行为与原 clean_registry 完全一致：
1. 别名归一化 + 严格字段校验（对齐 Claude Code z.strictObject）：未知字段
   抛 InputValidationError，required 缺失抛 InputValidationError。
2. 按 JSON Schema type 做 best-effort 类型 coerce（抹平 LLM 把数字/布尔
   传成字符串的噪音）。

纯函数，仅依赖 typing；clean_registry 再导入这些符号保持公共 API 不变。
"""
from typing import Any, Dict, Optional

_BOOL_TRUE = {"true", "1", "yes", "y", "on"}
_BOOL_FALSE = {"false", "0", "no", "n", "off", ""}


# ============================================================================
# 严格参数校验（对齐 Claude Code 的 z.strictObject 行为）
# ============================================================================
#
# 原方案：handler 用 **kwargs/**_unused 兜底，未知字段被静默丢弃。后果：
# 模型把 grep 的 file_glob/pattern 误传给 read_file → handler 只用 path
# 字段执行 → 返回 "is_directory" 之类的下游错 → 模型不知道自己用错了工具，
# 浪费 N 轮纠错。
#
# 新方案：dispatch 前先做一层 schema strict 校验：
#   1) 按工具注册时声明的 arg_aliases 做别名归一化（兼容 LLM 常见笔误，
#      如 directory→path / files→paths / regex→pattern）
#   2) 余下的字段必须落在 schema.properties 里（context 注入键豁免）
#   3) 不合法 → 立即抛 InputValidationError，error 字符串列出合法字段，
#      并提示"是否用错了工具"，让模型一轮内纠正
#
# 别名一律在 registry 注册项里集中声明，handler 不再各自维护私有别名表。

class InputValidationError(Exception):
    """工具输入参数与 schema 不符（未知字段）。

    跟 Claude Code 的 z.strictObject 拒绝行为对齐：模型应当看到清晰错误并
    重试，不应在 handler 里被 **_unused 静默吞掉。
    """
    pass


# context 注入的键不算"工具参数"——agent_id/session_id 是 dispatch 给 handler 的，
# 模型从来不会主动传它们；strict 校验时统一豁免。
_CONTEXT_INJECT_KEYS: frozenset = frozenset({
    "agent_id", "session_id", "llm_client", "trace_id"
})


# ── 别名表（按 Claude/Cascade 兼容口径维护）───────────────────────────────────
# LLM 各家"母语"不同：openai 系常传 directory/dir，anthropic 常传 query 当 pattern，
# glm/qwen 还会传 regex/term/q/phrase。下面是历史上踩过的所有变体。
# 加新别名 = 在对应 dict 里加一行，不要散落到 handler 里。

# 单数路径的各家写法。各 agent 的文件工具参数名不同：Claude / Kiro 叫 file_path，
# Cascade 叫 TargetFile / AbsolutePath，Gemini 叫 absolute_path —— 模型按自己的母语传。
_SINGULAR_PATH_WORDS = ("path", "file_path", "filepath", "file", "target_file", "absolute_path")
# "目录"语义的误传（openai 系高发：把单文件参数当目录传）
_DIR_WORDS = ("directory", "dir", "folder", "search_path")
# 复数/批量写法。只给 canonical 本身就是批量的工具用（outline_file / get_diagnostics）；
# 单文件工具必须让 strict 门拒掉，否则模型会以为 read_file 能一次读 N 个文件。
_PLURAL_PATH_WORDS = ("paths", "files", "file_paths", "path_list", "paths_list")


def path_aliases_to(
    canonical: str, *, dir_words: bool = False, plural_words: bool = False
) -> Dict[str, str]:
    """生成「各种路径写法 → canonical」的别名表。

    canonical 自己会被排除（不产生自环）。新增一种 LLM 母语写法 = 往上面的元组里
    加一个词，四张表同时生效，不用逐个 dict 抄。
    """
    words = list(_SINGULAR_PATH_WORDS)
    if dir_words:
        words += list(_DIR_WORDS)
    if plural_words:
        words += list(_PLURAL_PATH_WORDS)
    return {w: canonical for w in words if w != canonical}


# read_file / write_file / edit / multi_edit / grep / glob_search / git_* /
# check_typescript / read_logs / code_intelligence
_PATH_ALIAS_MAP: Dict[str, str] = path_aliases_to("path", dir_words=True)
# outline_file / get_diagnostics（canonical 是复数 paths；handler 接受 str 自动包成单元素列表）
_PATHS_PLURAL_ALIAS_MAP: Dict[str, str] = path_aliases_to("paths", plural_words=True)
# notebook_edit（canonical notebook_path，对齐 Claude NotebookEdit）
_NOTEBOOK_PATH_ALIAS_MAP: Dict[str, str] = path_aliases_to("notebook_path")
# ide_open_file / ide_apply_edit（canonical uri 或 fsPath；纯路径归到 fsPath，
# 拼 file:// URI 的活交给 IDE 侧）
_FSPATH_ALIAS_MAP: Dict[str, str] = path_aliases_to("fsPath")

# pattern 字段（用于 grep）的常见笔误
_PATTERN_ALIAS_MAP: Dict[str, str] = {
    "query": "pattern",
    "keyword": "pattern",
    "search": "pattern",
    "text": "pattern",
    "regex": "pattern",
    "term": "pattern",
    "q": "pattern",
    "phrase": "pattern",
    "expression": "pattern",
    "expr": "pattern",
    "needle": "pattern",
    "string": "pattern",
    "match": "pattern",
    "match_pattern": "pattern",
    "search_pattern": "pattern",
}


def _normalize_aliases_and_validate(
    name: str,
    arguments: Dict[str, Any],
    schema: Optional[Dict[str, Any]],
    arg_aliases: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """对齐 Claude `z.strictObject`：别名归一化 + 严格字段校验。

    Args:
        name: 工具名（错误消息里用）
        arguments: 模型传来的原始参数 dict
        schema: 工具 input_schema（含 properties）
        arg_aliases: 别名 → 标准名 映射（如 {"directory": "path"}）

    Returns:
        归一化后的新 dict（原 dict 不变）

    Raises:
        InputValidationError: 有未知字段时；消息里列出合法字段 + 已知别名 + 用错工具提示
    """
    if not isinstance(arguments, dict):
        return arguments

    normalized = dict(arguments)

    # Step 1: 别名归一化（仅在标准名缺失时生效；都传时丢弃 alias 让 canonical 胜出）
    if arg_aliases:
        for alias, canonical in arg_aliases.items():
            if alias in normalized:
                if canonical in normalized:
                    # 模型同时传了 alias 和 canonical → 信任 canonical，丢弃 alias
                    normalized.pop(alias, None)
                else:
                    normalized[canonical] = normalized.pop(alias)

    # Step 2: 严格校验
    if schema and isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            valid_keys = set(props.keys())
            unknown = sorted(
                k for k in normalized.keys()
                if k not in valid_keys and k not in _CONTEXT_INJECT_KEYS
            )
            if unknown:
                # 不附加一刀切 hint —— 历史上这里固定写"参数应传给 grep，不是 read_file"，
                # 但 execute_command/edit/glob_search 等工具触发 invalid_input 时夹这句话
                # 是答非所问，反而污染 LLM 上下文。Valid parameters 列表已经精确告诉模型
                # 该用什么参数，让模型自己一轮纠正。
                raise InputValidationError(
                    f"InputValidationError: tool '{name}' got unexpected parameter(s) "
                    f"{unknown}. Valid parameters: {sorted(valid_keys)}."
                )

        # Step 3: required 字段检查（对齐 z.required()）。
        # 别名归一化跑过了，这里 required key 应该已就位；如果还缺 = 模型确实没传。
        required = schema.get("required")
        if isinstance(required, list):
            missing = sorted(k for k in required if k not in normalized)
            if missing:
                raise InputValidationError(
                    f"InputValidationError: tool '{name}' missing required parameter(s) "
                    f"{missing}. Got: {sorted(normalized.keys())}."
                )

    return normalized


def _coerce_one(value: Any, schema_type: Any) -> Any:
    """单个值按 JSON Schema type 做 best-effort 转换。
    转换失败返回原值——让 handler 自己处理或抛错，避免静默掩盖语义错误。
    schema_type 可以是 str（"integer"）或 list（["integer", "null"]）。
    """
    if value is None:
        return None
    # type 是 list 时（["integer","null"] 等），命中任意一种即尝试
    types = schema_type if isinstance(schema_type, list) else [schema_type]

    if "integer" in types:
        if isinstance(value, bool):  # bool 是 int 的子类，避免 True→1 的偷渡
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except (TypeError, ValueError):
                # 也试 float 字符串："1.0"
                try:
                    f = float(value.strip())
                    if f.is_integer():
                        return int(f)
                except (TypeError, ValueError):
                    pass
        return value

    if "number" in types:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                pass
        return value

    if "boolean" in types:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in _BOOL_TRUE:
                return True
            if v in _BOOL_FALSE:
                return False
        return value

    if "string" in types:
        if isinstance(value, str):
            return value
        # 数字/布尔 → str（LLM 偶尔把 path / id 写成数字字面量）
        if isinstance(value, (int, float, bool)):
            return str(value)
        return value

    # array / object 等结构化类型：交给 handler 自己处理
    return value


def _coerce_arguments_by_schema(arguments: Dict[str, Any], input_schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按 input_schema.properties 对 arguments 字段做 best-effort 类型 coerce。
    schema 缺失 / 不规范时整体直返原值，永不抛异常——调度层不该因为 schema 问题崩。
    """
    if not isinstance(arguments, dict) or not arguments:
        return arguments
    if not isinstance(input_schema, dict):
        return arguments
    props = input_schema.get("properties")
    if not isinstance(props, dict):
        return arguments

    result = {}
    for key, value in arguments.items():
        prop = props.get(key)
        if isinstance(prop, dict) and "type" in prop:
            try:
                result[key] = _coerce_one(value, prop["type"])
            except Exception:
                # 防御 _coerce_one 自身意外，永远不让 coerce 把整个调用搞崩
                result[key] = value
        else:
            # schema 没声明该字段 → 不动（kwargs 兜底 / 历史宽松字段）
            result[key] = value
    return result
