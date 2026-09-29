"""v2 · H · P2-A · OutgoingPlugin Protocol

Plugin 改写 CoreRequest → CoreRequest。多个 plugin 链式应用，顺序由
PluginBuilder 注册决定（先注册先调用）。

实现纪律：
1. 函数式：CoreRequest 是 frozen dataclass，plugin 必须返**新** CoreRequest
   实例（用 dataclasses.replace），不要试图 in-place 修改。
2. 无副作用：不读 ContextVar / 不调外部 HTTP / 不写文件。
3. 错误穿透：plugin 异常不要 swallow，让 ApiClient 调用方决定（caller 通常
   会包 LLMError 重试或终止）。
4. extras dict 操作要用合并语义（{**req.extras, "key": value}）而不是
   in-place mutation——req.extras 是 caller 传来的，可能被多个 plugin 共享。

实现形式自由：
- 类带 `transform_request` 方法（推荐，便于配置 + 状态化）
- 函数 closure：返回一个满足 OutgoingPlugin Protocol 的 callable 对象

`name` 是诊断字段：日志 / trace / 错误归因用。
"""

from __future__ import annotations

from typing import Protocol

from ..api_client import CoreRequest, RequestContext


class OutgoingPlugin(Protocol):
    """CoreRequest → CoreRequest 改写插件协议。

    生命周期：
    1. ApiClient.send_stream 入口处 builder.build() 拿 plugin 链
    2. for plugin in plugins: req = plugin.transform_request(req, ctx)
    3. 然后 _build_sdk_params(req) → SDK 调用

    Examples（伪代码）::

        class AnthropicCachePlugin:
            name = "anthropic_cache"

            def transform_request(self, req, ctx):
                # 把 req.system 转成 list[block] 含 cache_control
                cached_blocks = [
                    {"type": "text", "text": req.system,
                     "cache_control": {"type": "ephemeral"}},
                ]
                new_extras = {**req.extras, "system": cached_blocks}
                return dataclasses.replace(req, extras=new_extras)
    """

    name: str

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest: ...


__all__ = ["OutgoingPlugin"]
