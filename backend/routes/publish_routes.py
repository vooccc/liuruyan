# -*- coding: utf-8 -*-
"""发布能力 API：把内容流水线终稿推送到微信公众号草稿箱。

- GET  /publish/wechat/status —— 是否已配置 appid/secret（前端按钮据此提示）
- POST /publish/wechat/draft  —— 新增图文到草稿箱，返回 media_id

个人订阅号即可入草稿箱；最后一步群发/发布由用户在「公众号助手」App / 后台手动完成。
密钥读取与具体逻辑见 backend/services/wechat_mp.py。同步 def 路由 → FastAPI 自动在线程池
执行，内部用同步 requests 不会阻塞事件循环。
"""
from fastapi import APIRouter
from pydantic import BaseModel

from backend.services import wechat_mp

router = APIRouter()


@router.get("/publish/wechat/status")
def wechat_status():
    return {"configured": wechat_mp.is_configured()}


class WeChatDraftRequest(BaseModel):
    title: str
    content: str
    author: str = ""
    digest: str = ""
    source_url: str = ""
    thumb_media_id: str = ""
    style: str = ""


@router.post("/publish/wechat/draft")
def wechat_draft(req: WeChatDraftRequest):
    try:
        result = wechat_mp.add_draft(
            title=req.title,
            content=req.content,
            author=req.author,
            digest=req.digest,
            source_url=req.source_url,
            thumb_media_id=req.thumb_media_id or None,
            style=req.style,
        )
        return {"ok": True, "media_id": result["media_id"]}
    except wechat_mp.WeChatError as e:
        return {"ok": False, "error": e.message, "errcode": e.errcode}
    except Exception as e:
        return {"ok": False, "error": f"未知错误：{e}", "errcode": None}
