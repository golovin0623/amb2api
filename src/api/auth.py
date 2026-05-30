"""
共享鉴权依赖。

面板与管理类路由（admin / account / keys / playground）统一通过本模块的
``authenticate`` 依赖进行 Bearer 口令校验：优先匹配面板口令（PANEL_PASSWORD），
兼容 API 口令（API_PASSWORD）。

集中到一处后，新增受保护路由只需 ``dependencies=[Depends(authenticate)]`` 或
``token: str = Depends(authenticate)``，避免出现"忘了接鉴权"的裸奔端点。
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import get_api_password, get_panel_password

# auto_error=True：缺失/格式错误的 Authorization 头直接 403，不进入业务逻辑
security = HTTPBearer()


async def authenticate(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """校验 Bearer 口令，返回通过校验的 token。

    面板口令优先，兼容 API 口令（两者可由通用 PASSWORD 覆盖）。
    """
    token = credentials.credentials
    panel_pwd = await get_panel_password()
    if token != panel_pwd:
        api_pwd = await get_api_password()
        if token != api_pwd:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="密码错误"
            )
    return token


__all__ = ["authenticate", "security"]
