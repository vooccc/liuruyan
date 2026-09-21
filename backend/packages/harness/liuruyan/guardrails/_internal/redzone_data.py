"""红区 / 敏感清单——纯数据。

跟 v1 `Guardian._RED_ZONE_STATIC` / `_SENSITIVE_FILES_STATIC` /
`_SENSITIVE_PREFIXES_STATIC` 内容一致；D-4 shim 后 v1 的副本删除（dead code）。

红区与敏感前缀语义区分：
- **红区**（red zone）= 段对齐 endswith / contains 匹配；自进化的"内核层"，
  写入 = 永久封禁，无视任何 override。
- **敏感前缀**（sensitive prefix）= startswith 匹配；操作系统/凭证类目录，
  写入 = DENY（不依赖角色配置）。
"""

from __future__ import annotations

# 红区静态清单——自进化的"内核层"，无视任何配置/override 都禁止写。
# 用项目相对路径（POSIX 风格），匹配时双分隔符兼容。
RED_ZONE_STATIC: tuple[str, ...] = (
    "backend/tools/defense/guardian.py",       # Guardian 核心（v1 入口）
    "backend/tools/defense/__init__.py",       # 防替换 import
    "backend/tools/defense/backup.py",         # 备份/审计模块
    "backend/tools/defense/dangerous_patterns.py",  # v1 危险模式
    "backend/guardian.py",                     # 兼容 shim
    "backend/guardian_config.py",              # 配置 IO 模块
    "backend/guardian_routes.py",              # HTTP 路由
    "backend/tools/builtin/clean_registry.py", # ToolRegistry 拦截调用点
    "backend/paths.py",                        # 路径模块（被改可换 data_dir）
    # v2 新增红区——自进化"内核层"扩张
    "backend/packages/harness/liuruyan/guardrails/protocol.py",
    "backend/packages/harness/liuruyan/guardrails/_internal/dangerous_patterns.py",
    "backend/packages/harness/liuruyan/guardrails/_internal/redzone_data.py",
)

# 敏感文件精确名（端 endswith 匹配；与红区共用同一引擎）。
# 参考 hermes/agent/file_safety.py，覆盖凭证/shell rc/系统配置。
SENSITIVE_FILES_STATIC: tuple[str, ...] = (
    ".env",
    "models.secrets.json",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".zprofile",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    "authorized_keys",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "/etc/sudoers",
    "/etc/passwd",
    "/etc/shadow",
    "/etc/hosts",
)

# 敏感目录前缀（startswith 匹配）。~ 展开在 provider 实例化时处理。
SENSITIVE_PREFIXES_STATIC: tuple[str, ...] = (
    "~/.ssh/",
    "~/.aws/",
    "~/.gnupg/",
    "~/.kube/",
    "~/.docker/",
    "~/.azure/",
    "~/.config/gh/",
    "/etc/sudoers.d/",
    "/etc/systemd/",
    "agents/templates/",  # 模板被改 = 影响所有新建角色
)


__all__ = [
    "RED_ZONE_STATIC",
    "SENSITIVE_FILES_STATIC",
    "SENSITIVE_PREFIXES_STATIC",
]
