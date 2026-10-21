"""工具目录接口：本期只有本地工具的只读目录。

本地工具以代码为唯一事实源（services/tools/builtins/），这里没有任何写接口；
MCP 服务的管理沿用 /api/mcp。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from typing import List

from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    CurrentUser,
    ListResponse,
    LocalToolItem,
    LocalToolParamItem,
)
from app.services.tools import specs

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("/local", response_model=APIResponse[ListResponse[LocalToolItem]])
def list_local_tools(
    _: CurrentUser = Depends(require_user),
) -> APIResponse[ListResponse[LocalToolItem]]:
    """全部已注册本地工具的只读目录，按 key 排序。"""
    items: List[LocalToolItem] = [
        LocalToolItem(
            key=spec.key,
            title=spec.title,
            description=spec.description,
            params=[
                LocalToolParamItem(
                    name=param.name,
                    description=param.description,
                    type=param.json_type,
                    required=param.required,
                    default=param.default,
                )
                for param in spec.params
            ],
            source=spec.source,
            is_async=spec.is_async,
        )
        for spec in specs()
    ]
    return APIResponse(data=ListResponse(items=items, total=len(items)))
