"""开发工具：本地 Java 接口扫描与 YApi 同步。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends

from app.deps import require_user
from app.models.schemas import (
    APIResponse,
    YapiScanRequest,
    YapiScanResult,
    YapiSyncRequest,
    YapiSyncResult,
)
from app.services.directory_dialog import DirectoryDialogError, pick_directory
from app.services.yapi import YapiError, scan_java_path, sync_to_yapi

logger = logging.getLogger("yangvis.devtools")

router = APIRouter(prefix="/devtools", tags=["devtools"])


@router.post("/pick-directory", response_model=APIResponse[Optional[str]])
def pick_local_directory(
    _user: object = Depends(require_user),
) -> APIResponse[Optional[str]]:
    try:
        return APIResponse(data=pick_directory())
    except DirectoryDialogError as exc:
        return APIResponse(code=-1, message=str(exc))


@router.post("/yapi/scan", response_model=APIResponse[YapiScanResult])
def scan_yapi_interfaces(
    payload: YapiScanRequest,
    _user: object = Depends(require_user),
) -> APIResponse[YapiScanResult]:
    try:
        result = scan_java_path(payload.root_path)
    except ValueError as exc:
        return APIResponse(code=-1, message=str(exc))
    except OSError as exc:
        logger.warning("scan yapi source failed: %s", exc)
        return APIResponse(code=-1, message=f"读取目录失败：{exc}")
    return APIResponse(data=result)


@router.post("/yapi/sync", response_model=APIResponse[YapiSyncResult])
def sync_yapi_interfaces(
    payload: YapiSyncRequest,
    _user: object = Depends(require_user),
) -> APIResponse[YapiSyncResult]:
    try:
        result = sync_to_yapi(payload)
    except (ValueError, YapiError) as exc:
        return APIResponse(code=-1, message=str(exc))
    except OSError as exc:
        logger.warning("sync yapi source failed: %s", exc)
        return APIResponse(code=-1, message=f"读取目录失败：{exc}")
    return APIResponse(data=result)
