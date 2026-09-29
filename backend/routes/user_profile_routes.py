"""用户档案 API（/user/profile，Phase 3.1）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
档案落盘在 paths.user_profile_path()，本模块自带读写 helper，零 main.py 依赖。

端点：
- GET /user/profile  读用户档案（性别 / 偏好称呼 / 互动风格）
- PUT /user/profile  更新用户档案
"""
from __future__ import annotations

import json
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import paths

router = APIRouter(tags=["user-profile"])

_USER_PROFILE_PATH = paths.user_profile_path()


def _load_user_profile() -> Dict:
    """加载用户档案"""
    if _USER_PROFILE_PATH.exists():
        with open(_USER_PROFILE_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {
        "gender": "unspecified",
        "preferred_name": "",
        "interaction_style": "auto",
    }


def _save_user_profile(profile: Dict):
    """保存用户档案"""
    with open(_USER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


class UserProfileUpdate(BaseModel):
    gender: Optional[str] = None        # "male" | "female" | "unspecified"
    preferred_name: Optional[str] = None
    interaction_style: Optional[str] = None  # "auto" | "formal" | "casual"


@router.get("/user/profile")
async def get_user_profile():
    """获取用户档案"""
    return _load_user_profile()


@router.put("/user/profile")
async def update_user_profile(body: UserProfileUpdate):
    """更新用户档案（性别、偏好等）"""
    profile = _load_user_profile()
    if body.gender is not None:
        if body.gender not in ("male", "female", "unspecified"):
            raise HTTPException(status_code=400, detail="gender 必须为 male/female/unspecified")
        profile["gender"] = body.gender
    if body.preferred_name is not None:
        profile["preferred_name"] = body.preferred_name
    if body.interaction_style is not None:
        if body.interaction_style not in ("auto", "formal", "casual"):
            raise HTTPException(status_code=400, detail="interaction_style 必须为 auto/formal/casual")
        profile["interaction_style"] = body.interaction_style
    _save_user_profile(profile)
    return {"status": "success", "profile": profile}
