"""
下游 user token 管理 API（多租户）。

面板口令保护的 CRUD：列出 / 新建 / 更新 / 删除 token。
新建后返回完整 token（运营者需复制一次）。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import authenticate
from ..services.token_manager import get_token_manager


router = APIRouter(
    prefix="/api/tokens", tags=["User Tokens"], dependencies=[Depends(authenticate)]
)


class CreateTokenRequest(BaseModel):
    name: str = ""
    quota: Optional[int] = None              # None = 无限
    allowed_models: Optional[List[str]] = None  # None = 全部
    expires_at: Optional[float] = None       # unix 秒；None = 不过期


class UpdateTokenRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    quota: Optional[int] = None
    allowed_models: Optional[List[str]] = None
    expires_at: Optional[float] = None
    reset_used: Optional[bool] = None


@router.get("")
async def list_tokens():
    tm = await get_token_manager()
    return {"tokens": await tm.list_tokens()}


@router.post("")
async def create_token(req: CreateTokenRequest):
    tm = await get_token_manager()
    meta = await tm.create_token(
        name=req.name,
        quota=req.quota,
        allowed_models=req.allowed_models,
        expires_at=req.expires_at,
    )
    return meta


@router.put("/{token}")
async def update_token(token: str, req: UpdateTokenRequest):
    tm = await get_token_manager()
    # 只应用显式提供的字段（exclude_unset 区分"未传"与"显式 None"）
    changes = req.model_dump(exclude_unset=True)
    meta = await tm.update_token(token, changes)
    if meta is None:
        raise HTTPException(status_code=404, detail="token not found")
    return meta


@router.delete("/{token}")
async def delete_token(token: str):
    tm = await get_token_manager()
    ok = await tm.delete_token(token)
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    return {"deleted": True}
