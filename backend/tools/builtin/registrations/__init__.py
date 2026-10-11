"""工具注册按域拆分子包（从 clean_registry.py 拆出）。

每个域模块暴露 ``register(registry)``，由 clean_registry 在 import 时依次调用，
把该域的 ``registry.register(...)`` 集中到一处。行为与原 clean_registry 完全一致。

设计：register(registry) 显式传入全局 singleton，避免 registrations → clean_registry
反向循环依赖；域模块只依赖 registry_core / registry_validation 与各自的 *_tool 模块。
"""
