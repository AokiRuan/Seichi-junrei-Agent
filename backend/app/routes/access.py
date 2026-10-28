"""POST /api/access —— 输入访问码，换一个访问 cookie。讲解见 docs/12-going-public.md。"""
from fastapi import APIRouter, Depends, HTTPException, Response

from ..access import ACCESS_COOKIE, AccessConfig, access_config
from ..schemas import AccessRequest

router = APIRouter()


@router.post("/access", status_code=204)
async def grant_access(body: AccessRequest, response: Response, cfg: AccessConfig = Depends(access_config)):
    if not cfg.enabled:
        return  # 没设访问码：直接放行，前端照常进入
    if not cfg.check_code(body.code):
        raise HTTPException(status_code=401, detail="访问码不对")
    cfg.set_cookie(response, ACCESS_COOKIE, cfg.token())
