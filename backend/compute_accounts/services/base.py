"""第三方算力账号适配器抽象基类。
"""
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional


class BaseAccountAdapter(ABC):
    """算力账号适配器基类"""

    provider_id: str = ""
    display_name: str = ""
    sub_title: str = ""
    author: str = ""
    icon: str = ""

    @abstractmethod
    def generate_auth_params(self, redirect_uri: str) -> Dict[str, Any]:
        """生成 OAuth2 授权 URL 与必要的 PKCE 校验参数 (verifier, challenge, state 等)"""
        pass

    @abstractmethod
    async def exchange_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """通过授权码 code 兑换 access_token / refresh_token"""
        pass

    @abstractmethod
    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """通过 refresh_token 刷新 access_token"""
        pass

    @abstractmethod
    async def fetch_user_info(self, access_token: str) -> Dict[str, Any]:
        """拉取该账号的基本信息（邮箱、头像、昵称等）"""
        pass

    @abstractmethod
    async def list_models(self, access_token: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取该提供商激活的模型清单"""
        pass

    @abstractmethod
    async def test_connection(self, access_token: str) -> Dict[str, Any]:
        """测试当前 token 连通性与延迟"""
        pass

    @abstractmethod
    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        access_token: str,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """向官方底层发起流式模型请求，yield 统一的事件片段"""
        pass
