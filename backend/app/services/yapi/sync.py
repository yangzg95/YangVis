"""把扫描结果写进 YApi：一个 Controller 一个分类，接口按「方法 + 路径」判重后新增或更新。"""
from __future__ import annotations

import html
import json
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from app.models.schemas import (
    YapiScanCategory,
    YapiScanEndpoint,
    YapiSyncEndpoint,
    YapiSyncRequest,
    YapiSyncResult,
)
from app.services.yapi.client import YapiClient, YapiError, normalize_base_url
from app.services.yapi.scanner import scan_java_path

# YApi 对 path 有白名单校验，中文、空格、% 等都会让 save 直接失败。
_PATH_PATTERN = re.compile(r"^/[a-zA-Z0-9\-/_:!\.\{\}=]*$")


def sync_to_yapi(payload: YapiSyncRequest) -> YapiSyncResult:
    started = time.monotonic()
    base_url = normalize_base_url(payload.base_url)
    scan = scan_java_path(payload.root_path)
    warnings = list(scan.warnings)

    if not scan.endpoints:
        return YapiSyncResult(
            base_url=base_url,
            project_id=payload.project_id,
            warnings=warnings,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    endpoints_by_category: Dict[str, List[YapiScanEndpoint]] = defaultdict(list)
    for endpoint in scan.endpoints:
        endpoints_by_category[endpoint.category_name].append(endpoint)

    results: List[YapiSyncEndpoint] = []
    created = 0
    updated = 0
    failed = 0
    categories_created = 0

    with YapiClient(base_url, payload.project_id, payload.token) as client:
        menu = client.list_menu()
        existing_categories = _category_map(menu)
        existing_interfaces = _interface_map(menu)

        for category in scan.categories:
            category_id = existing_categories.get(category.category_name)
            if category_id is None:
                category_id = client.add_category(
                    category.category_name, _category_description(category)
                )
                existing_categories[category.category_name] = category_id
                categories_created += 1

            for endpoint in endpoints_by_category[category.category_name]:
                key = (endpoint.http_method, endpoint.path)
                was_existing = key in existing_interfaces
                if not _PATH_PATTERN.match(endpoint.path):
                    failed += 1
                    results.append(
                        _result(endpoint, "failed", "路径包含 YApi 不接受的字符")
                    )
                    continue
                try:
                    client.save_interface(_build_interface(endpoint, category_id))
                except YapiError as exc:
                    failed += 1
                    results.append(_result(endpoint, "failed", str(exc)))
                    continue

                existing_interfaces[key] = endpoint
                if was_existing:
                    updated += 1
                    results.append(_result(endpoint, "updated"))
                else:
                    created += 1
                    results.append(_result(endpoint, "created"))

    if failed:
        warnings.append(f"{failed} 个接口同步失败，详见接口列表中的错误信息")

    return YapiSyncResult(
        base_url=base_url,
        project_id=payload.project_id,
        created=created,
        updated=updated,
        failed=failed,
        categories_created=categories_created,
        endpoint_count=len(scan.endpoints),
        endpoints=results,
        warnings=warnings,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _category_map(menu: List[Dict[str, Any]]) -> Dict[str, Any]:
    categories: Dict[str, Any] = {}
    for item in menu:
        name = str(item.get("name") or "").strip()
        if not name or name in categories:
            continue
        category_id = item.get("_id")
        if category_id is None:
            continue
        categories[name] = category_id
    return categories


def _interface_map(menu: Any) -> Dict[Tuple[str, str], Any]:
    """现有接口索引：(METHOD, path) → 接口。判重口径与 YApi save 一致。"""
    existing: Dict[Tuple[str, str], Any] = {}
    for category in menu:
        if not isinstance(category, dict):
            continue
        for item in category.get("list") or []:
            if not isinstance(item, dict):
                continue
            method = str(item.get("method") or "").upper()
            path = str(item.get("path") or "")
            if method and path:
                existing[(method, path)] = item
    return existing


def _category_description(category: YapiScanCategory) -> str:
    parts = [f"Java 包：{category.package}" if category.package else "Java 默认包"]
    if category.controllers:
        parts.append("Controller：" + "、".join(category.controllers))
    return " · ".join(parts)


def _interface_description(endpoint: YapiScanEndpoint) -> str:
    blocks: List[str] = []
    text = endpoint.description or endpoint.summary
    if text:
        blocks.append(f"<p>{html.escape(text, quote=False)}</p>")
    meta = [f"Controller：{endpoint.controller}.{endpoint.method}()"]
    if endpoint.tag:
        meta.insert(0, f"标签：{endpoint.tag}")
    if endpoint.package:
        meta.append(f"Java 包：{endpoint.package}")
    if endpoint.source:
        location = f"{endpoint.source}:{endpoint.line}" if endpoint.line else endpoint.source
        meta.append(f"源码：{location}")
    blocks.append(
        "<p>" + "<br/>".join(html.escape(line, quote=False) for line in meta) + "</p>"
    )
    return "".join(blocks)


def _build_interface(endpoint: YapiScanEndpoint, category_id: Any) -> Dict[str, Any]:
    query: List[Dict[str, Any]] = []
    headers: List[Dict[str, Any]] = []
    path_params: List[Dict[str, Any]] = []
    form: List[Dict[str, Any]] = []

    for param in endpoint.params:
        if param.location == "query":
            query.append(_query_item(param))
        elif param.location == "header":
            headers.append(_header_item(param))
        elif param.location == "path":
            path_params.append(
                {
                    "name": param.name,
                    "desc": param.description,
                    "example": param.example,
                }
            )
        elif param.location == "form":
            form.append(_form_item(param))

    payload: Dict[str, Any] = {
        "catid": category_id,
        "title": endpoint.title or f"{endpoint.controller}.{endpoint.method}",
        "path": endpoint.path,
        "method": endpoint.http_method,
        "status": "done",
        "desc": _interface_description(endpoint),
        "req_query": query,
        "req_headers": headers,
        "req_params": path_params,
        "req_body_form": form,
        "req_body_type": "json"
        if endpoint.request_body_type == "json"
        else "form",
        "req_body_other": "",
    }

    if endpoint.request_body_type == "json" and endpoint.request_body_schema is not None:
        payload["req_body_other"] = json.dumps(
            endpoint.request_body_schema, ensure_ascii=False
        )
        payload["req_body_is_json_schema"] = True

    if endpoint.response_schema is not None:
        payload["res_body"] = json.dumps(endpoint.response_schema, ensure_ascii=False)
        payload["res_body_type"] = "json"
        payload["res_body_is_json_schema"] = True

    return payload


def _query_item(param: Any) -> Dict[str, Any]:
    return {
        "name": param.name,
        "desc": param.description,
        "required": "1" if param.required else "0",
        "example": param.example,
    }


def _header_item(param: Any) -> Dict[str, Any]:
    return {
        "name": param.name,
        "value": param.example,
        "desc": param.description,
        "required": "1" if param.required else "0",
    }


def _form_item(param: Any) -> Dict[str, Any]:
    return {
        "name": param.name,
        "type": "file" if param.type == "file" else "text",
        "desc": param.description,
        "required": "1" if param.required else "0",
        "example": param.example,
    }


def _result(
    endpoint: YapiScanEndpoint, action: str, error: Optional[str] = None
) -> YapiSyncEndpoint:
    return YapiSyncEndpoint(
        **endpoint.model_dump(),
        action=action,
        error=error,
    )
