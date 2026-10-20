"""扫描本地 Java 目录，产出可以同步到 YApi 的接口清单。

包层级只影响分类的归属：YApi 的分类是平铺的，因此每个「直接包含接口的包」
各建一个分类，分类名取包的最后一段，完整包名放进分类描述。
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from app.models.schemas import (
    YapiEndpointParam,
    YapiScanCategory,
    YapiScanEndpoint,
    YapiScanResult,
)
from app.services.yapi.java_parser import (
    Annotation,
    JavaField,
    JavaMethod,
    JavaParam,
    JavaType,
    annotation,
    annotation_bool,
    annotation_text,
    annotation_values,
    parse_java_source,
)

logger = logging.getLogger("yangvis.yapi_scanner")

MAX_SCAN_FILES = 20000
MAX_SCHEMA_DEPTH = 6
MAX_WARNINGS = 60

SKIP_DIRS = {
    "target",
    "build",
    "out",
    "bin",
    "node_modules",
    "generated",
    "generated-sources",
    "generated-test-sources",
    ".git",
    ".idea",
    ".gradle",
    ".mvn",
    ".settings",
    "dist",
    "coverage",
}

MAPPING_SHORTCUTS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"}

PRIMITIVES = {"byte", "short", "int", "long", "float", "double", "boolean", "char"}

SKIP_PARAM_TYPES = {
    "HttpServletRequest",
    "HttpServletResponse",
    "ServletRequest",
    "ServletResponse",
    "BindingResult",
    "Errors",
    "Model",
    "ModelMap",
    "RedirectAttributes",
    "Principal",
    "Authentication",
    "Locale",
    "TimeZone",
    "SessionStatus",
    "UriComponentsBuilder",
    "HttpSession",
    "WebRequest",
    "NativeWebRequest",
    "ServerHttpRequest",
    "ServerHttpResponse",
}

SCALARS: Dict[str, Dict[str, Any]] = {
    "String": {"type": "string"},
    "CharSequence": {"type": "string"},
    "char": {"type": "string"},
    "Character": {"type": "string"},
    "UUID": {"type": "string", "format": "uuid"},
    "byte": {"type": "integer", "format": "int32"},
    "short": {"type": "integer", "format": "int32"},
    "int": {"type": "integer", "format": "int32"},
    "Integer": {"type": "integer", "format": "int32"},
    "long": {"type": "integer", "format": "int64"},
    "Long": {"type": "integer", "format": "int64"},
    "BigInteger": {"type": "integer"},
    "float": {"type": "number", "format": "float"},
    "Float": {"type": "number", "format": "float"},
    "double": {"type": "number", "format": "double"},
    "Double": {"type": "number", "format": "double"},
    "BigDecimal": {"type": "number"},
    "Number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "Boolean": {"type": "boolean"},
    "LocalDate": {"type": "string", "format": "date"},
    "Date": {"type": "string", "format": "date-time"},
    "LocalDateTime": {"type": "string", "format": "date-time"},
    "LocalTime": {"type": "string", "format": "time"},
    "ZonedDateTime": {"type": "string", "format": "date-time"},
    "OffsetDateTime": {"type": "string", "format": "date-time"},
    "Instant": {"type": "string", "format": "date-time"},
    "Timestamp": {"type": "string", "format": "date-time"},
    "Calendar": {"type": "string", "format": "date-time"},
    "MultipartFile": {"type": "string", "format": "binary"},
    "InputStream": {"type": "string", "format": "binary"},
    "Resource": {"type": "string", "format": "binary"},
    "JSONObject": {"type": "object"},
    "JSONArray": {"type": "array", "items": {}},
}

COLLECTIONS = {
    "List",
    "ArrayList",
    "LinkedList",
    "Set",
    "HashSet",
    "LinkedHashSet",
    "TreeSet",
    "Collection",
    "Iterable",
    "Queue",
    "Deque",
    "ArrayDeque",
    "Stream",
}

MAPS = {"Map", "HashMap", "LinkedHashMap", "TreeMap", "SortedMap", "ConcurrentHashMap"}

UNWRAP_TYPES = {"Optional", "Mono", "CompletableFuture", "Future", "ResponseEntity", "HttpEntity"}
ARRAY_STREAM_TYPES = {"Flux"}

RESPONSE_WRAPPERS = {
    "Result",
    "R",
    "Response",
    "CommonResult",
    "ApiResponse",
    "ApiResult",
    "BaseResponse",
    "ResultData",
}

# 分页包装通常放在公共模块里，扫描路径下拿不到源码时按约定结构补全，
# 否则整个响应的字段都会丢成一个空对象。
_PAGE_WRAPPER_ITEM_FIELD = {
    "PageResult": "list",
    "PageInfo": "list",
}

_FIELD_DESC_ANNOTATIONS = ("Schema", "ApiModelProperty", "Parameter")
_FIELD_EXAMPLE_ANNOTATIONS = ("Schema", "ApiModelProperty", "Parameter", "ApiParam")
_REQUIRED_ANNOTATIONS = ("NotNull", "NotBlank", "NotEmpty", "NonNull")


@dataclass
class TypeRef:
    base: str
    args: List["TypeRef"] = field(default_factory=list)
    array_dims: int = 0


def _split_type_args(raw: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(raw):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append(raw[start:i])
            start = i + 1
    parts.append(raw[start:])
    return [part for part in parts if part]


def parse_type_ref(text: str) -> TypeRef:
    raw = re.sub(r"\s+", "", text or "")
    raw = re.sub(r"@[\w.]+(?:\([^)]*\))?", "", raw)
    for prefix in ("?extends", "?super"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.lstrip("?")

    dims = 0
    while raw.endswith("[]"):
        dims += 1
        raw = raw[:-2]
    while raw.endswith("..."):
        dims += 1
        raw = raw[:-3]

    if not raw:
        return TypeRef("", [], dims)
    open_at = raw.find("<")
    if open_at > 0 and raw.endswith(">"):
        base = raw[:open_at]
        inner = raw[open_at + 1 : -1]
        return TypeRef(base, [parse_type_ref(part) for part in _split_type_args(inner)], dims)
    return TypeRef(raw, [], dims)


def _simple_name(base: str) -> str:
    return (base or "").rsplit(".", 1)[-1]


def _with_dims(ref: TypeRef, dims: int) -> TypeRef:
    if not dims:
        return ref
    return TypeRef(ref.base, ref.args, ref.array_dims + dims)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = _clean_text(line)
        if cleaned:
            return cleaned
    return ""


class TypeIndex:
    def __init__(self) -> None:
        self.by_fqn: Dict[str, JavaType] = {}
        self.by_simple: Dict[str, List[JavaType]] = {}

    def add(self, java_type: JavaType) -> None:
        self.by_fqn.setdefault(java_type.fqn, java_type)
        self.by_simple.setdefault(java_type.name, []).append(java_type)

    def resolve(self, ref: TypeRef, context: JavaType) -> Optional[JavaType]:
        base = (ref.base or "").strip()
        if not base:
            return None
        if base in self.by_fqn:
            return self.by_fqn[base]

        simple = _simple_name(base)
        if "." in base:
            suffix = "." + base
            for fqn, java_type in self.by_fqn.items():
                if fqn.endswith(suffix):
                    return java_type

        imported = context.imports.get(simple)
        if imported:
            # 显式 import 的类不在扫描路径里时按未知类型处理，
            # 不能掉到同名类型兜底，否则会错误命中别的包里的同名类。
            return self.by_fqn.get(imported)

        if context.package:
            same_package = f"{context.package}.{simple}"
            if same_package in self.by_fqn:
                return self.by_fqn[same_package]

        for wildcard in context.wildcard_imports:
            candidate = f"{wildcard}.{simple}"
            if candidate in self.by_fqn:
                return self.by_fqn[candidate]

        candidates = self.by_simple.get(simple, [])
        if not candidates:
            return None
        if context.package:
            for candidate in candidates:
                if candidate.package == context.package:
                    return candidate
        return candidates[0]


class SchemaBuilder:
    def __init__(self, index: TypeIndex) -> None:
        self.index = index
        self.unknown_types: Set[str] = set()

    def for_type(
        self,
        type_text: str,
        context: JavaType,
        *,
        seen: Optional[Set[str]] = None,
        type_vars: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.for_ref(
            parse_type_ref(type_text),
            context,
            depth=0,
            seen=frozenset(seen or ()),
            bindings={},
            type_vars=frozenset(type_vars or ()),
        )

    def for_ref(
        self,
        ref: TypeRef,
        context: JavaType,
        *,
        depth: int,
        seen: FrozenSet[str],
        bindings: Dict[str, TypeRef],
        type_vars: FrozenSet[str],
    ) -> Optional[Dict[str, Any]]:
        if not ref.base:
            return {}

        if ref.base in bindings:
            bound = bindings[ref.base]
            if bound.base == ref.base and not bound.args and not bound.array_dims:
                return {}
            remaining = {key: value for key, value in bindings.items() if key != ref.base}
            return self.for_ref(
                _with_dims(bound, ref.array_dims),
                context,
                depth=depth,
                seen=seen,
                bindings=remaining,
                type_vars=type_vars,
            )

        if ref.array_dims:
            if ref.base == "byte" and ref.array_dims == 1:
                return {"type": "string", "format": "byte"}
            item = TypeRef(ref.base, ref.args, ref.array_dims - 1)
            schema = self.for_ref(
                item,
                context,
                depth=depth,
                seen=seen,
                bindings=bindings,
                type_vars=type_vars,
            )
            return {"type": "array", "items": schema or {}}

        simple = _simple_name(ref.base)
        if simple == "Object":
            return {}
        if simple in UNWRAP_TYPES and ref.args:
            return self.for_ref(
                ref.args[0],
                context,
                depth=depth,
                seen=seen,
                bindings=bindings,
                type_vars=type_vars,
            )
        if simple in ARRAY_STREAM_TYPES and ref.args:
            item = self.for_ref(
                ref.args[0],
                context,
                depth=depth,
                seen=seen,
                bindings=bindings,
                type_vars=type_vars,
            )
            return {"type": "array", "items": item or {}}
        if simple in COLLECTIONS:
            item_ref = ref.args[0] if ref.args else TypeRef("Object")
            item = self.for_ref(
                item_ref,
                context,
                depth=depth,
                seen=seen,
                bindings=bindings,
                type_vars=type_vars,
            )
            return {"type": "array", "items": item or {}}
        if simple in MAPS:
            value_ref = ref.args[1] if len(ref.args) >= 2 else (ref.args[0] if ref.args else TypeRef("Object"))
            value = self.for_ref(
                value_ref,
                context,
                depth=depth,
                seen=seen,
                bindings=bindings,
                type_vars=type_vars,
            )
            return {"type": "object", "additionalProperties": value or {}}
        if simple in SCALARS:
            return dict(SCALARS[simple])
        if simple in ("void", "Void"):
            return None
        if depth >= MAX_SCHEMA_DEPTH:
            return {"type": "object"}

        java_type = self.index.resolve(ref, context)
        if java_type is None:
            if ref.base in type_vars or simple in type_vars:
                return {}
            item_field = _PAGE_WRAPPER_ITEM_FIELD.get(simple)
            if item_field:
                item_ref = ref.args[0] if ref.args else TypeRef("Object")
                item = self.for_ref(
                    item_ref,
                    context,
                    depth=depth + 1,
                    seen=seen,
                    bindings=bindings,
                    type_vars=type_vars,
                )
                return {
                    "type": "object",
                    "properties": {
                        item_field: {"type": "array", "items": item or {}},
                        "total": {"type": "integer", "format": "int64"},
                        "pageNum": {"type": "integer", "format": "int64"},
                        "pageSize": {"type": "integer", "format": "int64"},
                        "pages": {"type": "integer", "format": "int64"},
                    },
                }
            self.unknown_types.add(ref.base)
            return {"type": "object"}
        if java_type.kind == "enum":
            schema: Dict[str, Any] = {"type": "string"}
            if java_type.enum_values:
                schema["enum"] = list(java_type.enum_values)
            description = self._type_description(java_type)
            if description:
                schema["description"] = description
            return schema
        return self._object_schema(
            java_type,
            ref,
            depth=depth,
            seen=seen,
            bindings=bindings,
            type_vars=type_vars,
        )

    def _object_schema(
        self,
        java_type: JavaType,
        ref: TypeRef,
        *,
        depth: int,
        seen: FrozenSet[str],
        bindings: Dict[str, TypeRef],
        type_vars: FrozenSet[str],
    ) -> Dict[str, Any]:
        if java_type.fqn in seen:
            return {"type": "object"}
        local_bindings = dict(bindings)
        for name, arg in zip(java_type.type_params, ref.args):
            local_bindings[name] = arg
        local_vars = frozenset(set(type_vars) | set(java_type.type_params))
        visited = set(seen)

        properties: Dict[str, Any] = {}
        required: List[str] = []
        for field_info, owner, owner_bindings, owner_vars in self._iter_fields(
            java_type, local_bindings, local_vars, visited
        ):
            if "static" in field_info.modifiers or "transient" in field_info.modifiers:
                continue
            if field_info.name in ("serialVersionUID", "this$0"):
                continue
            schema = self.for_ref(
                parse_type_ref(field_info.type_text),
                owner,
                depth=depth + 1,
                seen=seen | {java_type.fqn},
                bindings=owner_bindings,
                type_vars=owner_vars,
            )
            if schema is None:
                continue
            description = self._field_description(field_info)
            if description:
                schema = {**schema, "description": description}
            example = self._field_example(field_info)
            if example and "example" not in schema:
                schema = {**schema, "example": example}
            properties[field_info.name] = schema
            if self._field_required(field_info):
                required.append(field_info.name)

        result: Dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            result["required"] = required
        description = self._type_description(java_type)
        if description:
            result["description"] = description
        return result

    def _iter_fields(
        self,
        java_type: JavaType,
        bindings: Dict[str, TypeRef],
        type_vars: FrozenSet[str],
        visited: Set[str],
    ) -> Iterator[Tuple[JavaField, JavaType, Dict[str, TypeRef], FrozenSet[str]]]:
        if java_type.fqn in visited:
            return
        visited.add(java_type.fqn)
        vars_here = frozenset(set(type_vars) | set(java_type.type_params))
        for parent_text in java_type.extends:
            parent_ref = parse_type_ref(parent_text)
            parent = self.index.resolve(parent_ref, java_type)
            if parent is None:
                continue
            parent_bindings = dict(bindings)
            for name, arg in zip(parent.type_params, parent_ref.args):
                parent_bindings[name] = arg
            yield from self._iter_fields(parent, parent_bindings, vars_here, visited)
        for field_info in java_type.fields:
            yield field_info, java_type, bindings, vars_here

    def _type_description(self, java_type: JavaType) -> str:
        schema_ann = annotation(java_type.annotations, "Schema")
        description = annotation_text(schema_ann, "description")
        if description:
            return _clean_text(description)
        api_model = annotation(java_type.annotations, "ApiModel")
        description = annotation_text(api_model, "value", "description")
        if description:
            return _clean_text(description)
        return _first_line(java_type.javadoc.text)

    def _field_description(self, field_info: JavaField) -> str:
        for name in _FIELD_DESC_ANNOTATIONS:
            item = annotation(field_info.annotations, name)
            text = annotation_text(item, "description", "value")
            if text:
                return _clean_text(text)
        return _clean_text(field_info.javadoc.text)

    def _field_example(self, field_info: JavaField) -> str:
        for name in _FIELD_EXAMPLE_ANNOTATIONS:
            item = annotation(field_info.annotations, name)
            text = annotation_text(item, "example")
            if text:
                return text
        return ""

    def _field_required(self, field_info: JavaField) -> bool:
        ref = parse_type_ref(field_info.type_text)
        if not ref.args and not ref.array_dims and ref.base in PRIMITIVES:
            return True
        for name in _REQUIRED_ANNOTATIONS:
            if annotation(field_info.annotations, name) is not None:
                return True
        json_property = annotation(field_info.annotations, "JsonProperty")
        if annotation_bool(json_property, "required"):
            return True
        schema_ann = annotation(field_info.annotations, "Schema")
        if annotation_bool(schema_ann, "required"):
            return True
        required_mode = annotation_text(schema_ann, "requiredMode")
        if required_mode.upper().endswith("REQUIRED"):
            return True
        api_model = annotation(field_info.annotations, "ApiModelProperty")
        if annotation_bool(api_model, "required"):
            return True
        return False


@dataclass
class _ParamBucket:
    items: Dict[str, YapiEndpointParam] = field(default_factory=dict)

    def add(self, param: YapiEndpointParam) -> None:
        existing = self.items.get(param.name)
        if existing is None:
            self.items[param.name] = param
            return
        existing.required = existing.required or param.required
        if not existing.description:
            existing.description = param.description
        if not existing.example:
            existing.example = param.example

    def as_list(self) -> List[YapiEndpointParam]:
        return list(self.items.values())


class JavaSourceScanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.index = TypeIndex()
        self.builder = SchemaBuilder(self.index)
        self.warnings: List[str] = []
        self._suppressed_warnings = 0
        self.java_files = 0
        self._defaulted_get = 0

    # -- 入口 -----------------------------------------------------------------

    def scan(self) -> YapiScanResult:
        started = time.monotonic()
        files = self._collect_files()
        controllers: List[JavaType] = []

        for path, relative in files:
            try:
                text = _read_text(path)
            except OSError as exc:
                self._warn(f"{relative}：读取失败（{exc}）")
                continue
            try:
                unit = parse_java_source(text, relative)
            except Exception as exc:  # 单文件坏掉不能拖垮整次扫描
                logger.warning("failed to parse %s", relative, exc_info=True)
                self._warn(f"{relative}：解析失败（{exc.__class__.__name__}: {exc}）")
                continue
            self.java_files += 1
            for java_type in unit.types:
                self.index.add(java_type)
                if self._is_controller(java_type):
                    controllers.append(java_type)

        if self.java_files == 0:
            self._warn("路径下没有找到可解析的 .java 文件")
        elif not controllers:
            self._warn("没有识别到 @RestController / @Controller 类")

        endpoints: List[YapiScanEndpoint] = []
        seen_endpoints: Dict[Tuple[str, str], YapiScanEndpoint] = {}
        for controller in controllers:
            if "abstract" in controller.modifiers:
                self._warn(f"跳过抽象 Controller：{controller.fqn}")
                continue
            for endpoint in self._controller_endpoints(controller):
                key = (endpoint.http_method, endpoint.path)
                previous = seen_endpoints.get(key)
                if previous is not None:
                    self._warn(
                        f"重复接口 {endpoint.http_method} {endpoint.path}："
                        f"{previous.controller} 与 {endpoint.controller}，保留前者"
                    )
                    continue
                seen_endpoints[key] = endpoint
                endpoints.append(endpoint)

        if self._defaulted_get:
            self._warn(
                f"{self._defaulted_get} 个 @RequestMapping 未声明 HTTP 方法，已按 GET 同步"
            )
        if self.builder.unknown_types:
            names = sorted(self.builder.unknown_types)[:8]
            more = len(self.builder.unknown_types) - len(names)
            suffix = f" 等 {len(self.builder.unknown_types)} 个" if more > 0 else ""
            self._warn(
                "以下类型未在扫描路径下找到源码，字段结构按空对象处理："
                + "、".join(names)
                + suffix
            )

        self._resolve_category_names(endpoints)
        endpoints.sort(key=lambda item: (item.category_name, item.path, item.http_method))
        categories = self._group_categories(endpoints)
        if self._suppressed_warnings:
            self.warnings.append(f"另有 {self._suppressed_warnings} 条警告未显示")

        return YapiScanResult(
            root_path=str(self.root),
            java_files=self.java_files,
            controller_count=len(
                {(item.package, item.controller) for item in endpoints}
            ),
            endpoint_count=len(endpoints),
            categories=categories,
            endpoints=endpoints,
            warnings=self.warnings,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # -- 文件 -----------------------------------------------------------------

    def _collect_files(self) -> List[Tuple[Path, str]]:
        files: List[Tuple[Path, str]] = []

        def on_error(exc: OSError) -> None:
            self._warn(f"目录读取失败：{exc}")

        for dirpath, dirnames, filenames in os.walk(
            self.root, onerror=on_error, followlinks=False
        ):
            dirnames[:] = sorted(name for name in dirnames if self._keep_dir(dirpath, name))
            for filename in sorted(filenames):
                if not filename.endswith(".java"):
                    continue
                if filename in ("package-info.java", "module-info.java"):
                    continue
                path = Path(dirpath) / filename
                relative = path.relative_to(self.root).as_posix()
                files.append((path, relative))
                if len(files) >= MAX_SCAN_FILES:
                    self._warn(f"Java 文件超过 {MAX_SCAN_FILES} 个，只扫描前 {MAX_SCAN_FILES} 个")
                    return files
        return files

    def _keep_dir(self, dirpath: str, name: str) -> bool:
        if name.startswith(".") or name in SKIP_DIRS:
            return False
        relative = Path(dirpath, name).relative_to(self.root).as_posix()
        parts = relative.split("/")
        for index in range(len(parts) - 1):
            if parts[index] == "src" and parts[index + 1] in (
                "test",
                "testFixtures",
                "integrationTest",
                "jmh",
            ):
                return False
        return True

    # -- Controller -----------------------------------------------------------

    def _is_controller(self, java_type: JavaType) -> bool:
        if java_type.kind != "class":
            return False
        return (
            annotation(java_type.annotations, "RestController") is not None
            or annotation(java_type.annotations, "Controller") is not None
        )

    def _controller_endpoints(self, controller: JavaType) -> Iterator[YapiScanEndpoint]:
        class_mapping = annotation(controller.annotations, "RequestMapping")
        base_paths = self._mapping_paths(class_mapping) if class_mapping else [""]
        class_methods = self._mapping_methods(class_mapping) if class_mapping else []
        tag = self._controller_tag(controller)

        for method in controller.methods:
            mappings = [
                item
                for item in method.annotations
                if item.name == "RequestMapping" or item.name in MAPPING_SHORTCUTS
            ]
            if not mappings:
                continue

            combos: List[Tuple[str, str]] = []
            for mapping in mappings:
                if mapping.name == "RequestMapping":
                    http_methods = self._mapping_methods(mapping) or class_methods
                    if not http_methods:
                        http_methods = ["GET"]
                        self._defaulted_get += 1
                else:
                    http_methods = [MAPPING_SHORTCUTS[mapping.name]]
                sub_paths = self._mapping_paths(mapping)
                for base in base_paths:
                    for sub in sub_paths:
                        path = _join_path(base, sub)
                        for http_method in http_methods:
                            if http_method not in HTTP_METHODS:
                                self._warn(
                                    f"{controller.fqn}#{method.name}：未知 HTTP 方法 {http_method}"
                                )
                                continue
                            combos.append((http_method, path))

            seen_paths: Set[Tuple[str, str]] = set()
            for http_method, path in combos:
                if (http_method, path) in seen_paths:
                    continue
                seen_paths.add((http_method, path))
                title, summary, description = self._method_text(method)
                params, body_type, body_schema = self._build_params(
                    controller, method, http_method, path
                )
                response_schema = self._response_schema(controller, method)
                package = controller.package
                yield YapiScanEndpoint(
                    package=package,
                    category_name=self._controller_category(controller),
                    controller=controller.name,
                    method=method.name,
                    title=title,
                    http_method=http_method,
                    path=path,
                    tag=tag,
                    summary=summary,
                    description=description,
                    source=controller.source,
                    line=method.line,
                    params=params,
                    request_body_type=body_type,
                    request_body_schema=body_schema,
                    response_schema=response_schema,
                )

    def _mapping_paths(self, item: Optional[Annotation]) -> List[str]:
        if item is None:
            return [""]
        values = annotation_values(item, "value", "path")
        paths = [str(value) for value in values if value is not None]
        return paths or [""]

    def _mapping_methods(self, item: Optional[Annotation]) -> List[str]:
        if item is None:
            return []
        methods: List[str] = []
        for value in annotation_values(item, "method"):
            text = str(value).upper()
            if text.startswith("REQUESTMETHOD."):
                text = text.rsplit(".", 1)[-1]
            if text in HTTP_METHODS and text not in methods:
                methods.append(text)
        return methods

    def _controller_tag(self, controller: JavaType) -> str:
        tag = annotation(controller.annotations, "Tag")
        text = annotation_text(tag, "name")
        if text:
            return text
        api = annotation(controller.annotations, "Api")
        return annotation_text(api, "tags", "value")

    def _controller_category(self, controller: JavaType) -> str:
        tag = _clean_text(self._controller_tag(controller))
        if tag:
            return tag
        # 没写 Swagger 注解时用类名兜底；去掉 Controller/Api 后缀更像分类名。
        name = controller.name
        for suffix in ("Controller", "Api"):
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                break
        return name or controller.name

    def _method_text(self, method: JavaMethod) -> Tuple[str, str, str]:
        operation = annotation(method.annotations, "Operation")
        summary = annotation_text(operation, "summary")
        description = annotation_text(operation, "description")
        if not summary:
            summary = annotation_text(annotation(method.annotations, "ApiOperation"), "value")
        if not description:
            description = annotation_text(annotation(method.annotations, "ApiOperation"), "notes")
        summary = _clean_text(summary)
        description = _clean_text(description)
        if not summary:
            summary = _first_line(method.javadoc.text) or method.name
        if not description:
            description = _clean_text(method.javadoc.text)
        return summary, summary, description

    # -- 参数与请求体 -----------------------------------------------------------

    def _build_params(
        self,
        controller: JavaType,
        method: JavaMethod,
        http_method: str,
        path: str,
    ) -> Tuple[List[YapiEndpointParam], str, Optional[Dict[str, Any]]]:
        query = _ParamBucket()
        path_params = _ParamBucket()
        headers = _ParamBucket()
        form = _ParamBucket()
        body_param: Optional[YapiEndpointParam] = None
        body_schema: Optional[Dict[str, Any]] = None
        body_type = ""

        for param in method.params:
            type_ref = parse_type_ref(param.type_text)
            simple = _simple_name(type_ref.base)
            if simple in SKIP_PARAM_TYPES:
                continue

            request_body = annotation(param.annotations, "RequestBody")
            request_param = annotation(param.annotations, "RequestParam")
            path_variable = annotation(param.annotations, "PathVariable")
            request_header = annotation(param.annotations, "RequestHeader")
            cookie_value = annotation(param.annotations, "CookieValue")
            model_attribute = annotation(param.annotations, "ModelAttribute")
            request_part = annotation(param.annotations, "RequestPart")

            description = self._param_description(param, method)
            example = self._param_example(param)
            parameter_ann = annotation(param.annotations, "Parameter")

            if request_body is not None:
                if body_param is not None:
                    self._warn(f"{controller.fqn}#{method.name}：存在多个 @RequestBody，只取第一个")
                    continue
                required = _annotation_required(request_body, default=True)
                if parameter_ann is not None and "required" in parameter_ann.values:
                    required = annotation_bool(parameter_ann, "required", required)
                body_schema = self.builder.for_type(
                    param.type_text,
                    controller,
                    seen={controller.fqn},
                    type_vars=set(controller.type_params),
                )
                body_type = "json"
                body_param = YapiEndpointParam(
                    name=param.name,
                    location="body",
                    type=param.type_text,
                    required=required,
                    description=description,
                    example=example,
                )
                continue

            if model_attribute is not None:
                self._flatten_into(
                    form,
                    param.type_text,
                    controller,
                    location="form",
                    description=description,
                    depth=0,
                )
                body_type = "form"
                continue

            is_file = simple == "MultipartFile" or (
                simple in COLLECTIONS
                and type_ref.args
                and _simple_name(type_ref.args[0].base) == "MultipartFile"
            )
            if request_part is not None or is_file:
                required = (
                    _annotation_required(request_part, default=True)
                    if request_part is not None
                    else False
                )
                form.add(
                    YapiEndpointParam(
                        name=annotation_text(request_part, "name", "value") or param.name,
                        location="form",
                        type="file" if is_file else param.type_text,
                        required=required,
                        description=description,
                        example=example,
                    )
                )
                body_type = "form"
                continue

            if path_variable is not None:
                required = _annotation_required(path_variable, default=True)
                path_params.add(
                    YapiEndpointParam(
                        name=annotation_text(path_variable, "name", "value") or param.name,
                        location="path",
                        type=param.type_text,
                        required=required,
                        description=description,
                        example=example,
                    )
                )
                continue

            if request_header is not None:
                required = _annotation_required(request_header, default=True)
                headers.add(
                    YapiEndpointParam(
                        name=annotation_text(request_header, "name", "value") or param.name,
                        location="header",
                        type=param.type_text,
                        required=required,
                        description=description,
                        example=example,
                    )
                )
                continue

            if cookie_value is not None:
                cookie_name = annotation_text(cookie_value, "name", "value") or param.name
                required = _annotation_required(cookie_value, default=True)
                headers.add(
                    YapiEndpointParam(
                        name="Cookie",
                        location="header",
                        type="string",
                        required=required,
                        description=description or f"Cookie: {cookie_name}",
                        example=example,
                    )
                )
                continue

            if request_param is not None:
                required = _annotation_required(request_param, default=True)
                if "defaultValue" in request_param.values:
                    required = False
                    if not example:
                        example = annotation_text(request_param, "defaultValue")
                if parameter_ann is not None and "required" in parameter_ann.values:
                    required = annotation_bool(parameter_ann, "required", required)
                query.add(
                    YapiEndpointParam(
                        name=annotation_text(request_param, "name", "value") or param.name,
                        location="query",
                        type=param.type_text,
                        required=required,
                        description=description,
                        example=example,
                    )
                )
                continue

            if type_ref.base in SCALARS or simple in SCALARS or simple in COLLECTIONS or simple in MAPS:
                query.add(
                    YapiEndpointParam(
                        name=param.name,
                        location="query",
                        type=param.type_text,
                        required=type_ref.base in PRIMITIVES,
                        description=description,
                        example=example,
                    )
                )
                continue

            if http_method in ("POST", "PUT", "PATCH") and _resolves_to_object(
                self.index, type_ref, controller
            ):
                self._flatten_into(
                    query,
                    param.type_text,
                    controller,
                    location="query",
                    description=description,
                    depth=0,
                )
            else:
                query.add(
                    YapiEndpointParam(
                        name=param.name,
                        location="query",
                        type=param.type_text,
                        required=False,
                        description=description,
                        example=example,
                    )
                )

        for name in _path_variables(path):
            if name not in path_params.items:
                path_params.add(
                    YapiEndpointParam(
                        name=name,
                        location="path",
                        type="string",
                        required=True,
                    )
                )

        if body_type == "json" and form.items:
            self._warn(
                f"{controller.fqn}#{method.name}：同时存在 JSON 请求体与表单参数，已按 JSON 处理"
            )
            form = _ParamBucket()
        elif body_type == "" and form.items:
            body_type = "form"
        if body_type == "form" and query.items:
            # multipart / 表单请求里，@RequestParam 绑定的是表单字段而不是查询串。
            for item in query.as_list():
                form.add(item.model_copy(update={"location": "form"}))
            query = _ParamBucket()

        ordered = path_params.as_list() + query.as_list() + headers.as_list()
        if body_param is not None:
            ordered.append(body_param)
        ordered.extend(form.as_list())
        return ordered, body_type, body_schema

    def _flatten_into(
        self,
        bucket: _ParamBucket,
        type_text: str,
        controller: JavaType,
        *,
        location: str,
        description: str,
        depth: int,
    ) -> None:
        ref = parse_type_ref(type_text)
        java_type = self.index.resolve(ref, controller)
        if java_type is None or depth >= 2 or ref.array_dims or _simple_name(ref.base) in COLLECTIONS:
            bucket.add(
                YapiEndpointParam(
                    name=_simple_name(ref.base) or "param",
                    location=location,
                    type=type_text,
                    required=False,
                    description=description,
                )
            )
            return
        for field_info in java_type.fields:
            if "static" in field_info.modifiers or "transient" in field_info.modifiers:
                continue
            if field_info.name in ("serialVersionUID", "this$0"):
                continue
            field_ref = parse_type_ref(field_info.type_text)
            field_desc = self.builder._field_description(field_info) or ""
            example = self.builder._field_example(field_info)
            required = self.builder._field_required(field_info)
            if _resolves_to_object(self.index, field_ref, java_type) and depth + 1 < 2:
                self._flatten_into(
                    bucket,
                    field_info.type_text,
                    java_type,
                    location=location,
                    description=field_desc,
                    depth=depth + 1,
                )
            else:
                bucket.add(
                    YapiEndpointParam(
                        name=field_info.name,
                        location=location,
                        type=field_info.type_text,
                        required=required,
                        description=field_desc,
                        example=example,
                    )
                )

    def _param_description(self, param: JavaParam, method: JavaMethod) -> str:
        parameter_ann = annotation(param.annotations, "Parameter")
        text = annotation_text(parameter_ann, "description")
        if text:
            return _clean_text(text)
        schema_ann = annotation(param.annotations, "Schema")
        text = annotation_text(schema_ann, "description")
        if text:
            return _clean_text(text)
        api_param = annotation(param.annotations, "ApiParam")
        text = annotation_text(api_param, "value")
        if text:
            return _clean_text(text)
        return _clean_text(method.javadoc.params.get(param.name, ""))

    def _param_example(self, param: JavaParam) -> str:
        for name in ("Parameter", "Schema", "ApiModelProperty", "ApiParam"):
            item = annotation(param.annotations, name)
            text = annotation_text(item, "example")
            if text:
                return text
        return ""

    def _response_schema(
        self, controller: JavaType, method: JavaMethod
    ) -> Optional[Dict[str, Any]]:
        ref = parse_type_ref(method.return_type)
        if not ref.base:
            return None
        wrapper: Optional[str] = None
        while (
            not ref.array_dims
            and _simple_name(ref.base) in RESPONSE_WRAPPERS | UNWRAP_TYPES
            and ref.args
        ):
            simple = _simple_name(ref.base)
            if simple in RESPONSE_WRAPPERS:
                wrapper = ref.base
            first = ref.args[0]
            if _simple_name(first.base) in ("Void", "void"):
                return self._unified_response(controller, wrapper)
            ref = first
        if not ref.array_dims and _simple_name(ref.base) in ("void", "Void"):
            return None
        return self.builder.for_ref(
            ref,
            controller,
            depth=0,
            seen=frozenset({controller.fqn}),
            bindings={},
            type_vars=frozenset(controller.type_params),
        )

    def _unified_response(
        self, controller: JavaType, wrapper: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Result<Void> 没有业务数据，但仍返回统一包装，不能整个响应留空。"""
        if not wrapper:
            return None
        wrapper_type = self.index.resolve(TypeRef(wrapper), controller)
        if wrapper_type is not None:
            schema = self.builder.for_ref(
                TypeRef(wrapper_type.fqn, [TypeRef("Void")]),
                controller,
                depth=0,
                seen=frozenset({controller.fqn}),
                bindings={},
                type_vars=frozenset(controller.type_params),
            )
            if schema and schema.get("properties"):
                return schema
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "integer",
                    "format": "int32",
                    "description": "响应码，0 表示成功",
                },
                "message": {"type": "string", "description": "响应信息"},
                "traceId": {"type": "string", "description": "链路追踪 ID"},
                "success": {"type": "boolean", "description": "是否成功"},
            },
        }

    # -- 其它 -----------------------------------------------------------------

    def _resolve_category_names(self, endpoints: List[YapiScanEndpoint]) -> None:
        """YApi 分类是平铺的，同名的两个 Controller 会互相覆盖，重名时追加完整类名。"""
        desired: Dict[Tuple[str, str], str] = {}
        for endpoint in endpoints:
            desired.setdefault(
                (endpoint.package, endpoint.controller), endpoint.category_name
            )
        counts = Counter(desired.values())
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            self._warn(
                "以下 Swagger 标签/Controller 名重复，分类名已追加完整类名："
                + "、".join(duplicates)
            )
        for endpoint in endpoints:
            key = (endpoint.package, endpoint.controller)
            name = desired[key]
            if counts[name] > 1:
                name = f"{name}（{key[0]}.{key[1]}）"
            endpoint.category_name = name

    def _group_categories(
        self, endpoints: Sequence[YapiScanEndpoint]
    ) -> List[YapiScanCategory]:
        grouped: Dict[Tuple[str, str], YapiScanCategory] = {}
        for endpoint in endpoints:
            key = (endpoint.package, endpoint.controller)
            item = grouped.get(key)
            if item is None:
                item = YapiScanCategory(
                    category_name=endpoint.category_name,
                    package=endpoint.package,
                    controllers=[endpoint.controller],
                )
                grouped[key] = item
            item.endpoint_count += 1
        for item in grouped.values():
            item.controller_count = len(item.controllers)
        return [grouped[key] for key in sorted(grouped)]

    def _warn(self, message: str) -> None:
        if len(self.warnings) >= MAX_WARNINGS:
            self._suppressed_warnings += 1
            return
        self.warnings.append(message)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _annotation_required(item: Optional[Annotation], default: bool) -> bool:
    if item is None:
        return default
    if "defaultValue" in item.values:
        return False
    return annotation_bool(item, "required", default)


def _join_path(base: str, sub: str) -> str:
    base = (base or "").strip()
    sub = (sub or "").strip()
    if not base:
        combined = sub or "/"
    elif not sub:
        combined = base
    elif sub.startswith("/"):
        combined = base.rstrip("/") + sub
    else:
        combined = base.rstrip("/") + "/" + sub
    return _normalize_path(combined)


_PATH_VAR_RE = re.compile(r"\{([^{}]*)\}")


def _normalize_path(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"\s+", "", path).replace("\\", "/")
    path = re.sub(r"/+", "/", path)

    def replace_variable(match: re.Match[str]) -> str:
        inner = match.group(1)
        name = inner.split(":", 1)[0].lstrip("*")
        found = re.search(r"[A-Za-z_][A-Za-z0-9_.-]*", name)
        return "{" + (found.group(0) if found else "param") + "}"

    path = _PATH_VAR_RE.sub(replace_variable, path)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _path_variables(path: str) -> List[str]:
    names: List[str] = []
    for match in _PATH_VAR_RE.finditer(path):
        name = match.group(1)
        if name and name not in names:
            names.append(name)
    return names


def _resolves_to_object(index: TypeIndex, ref: TypeRef, context: JavaType) -> bool:
    if ref.array_dims or _simple_name(ref.base) in SCALARS or _simple_name(ref.base) in COLLECTIONS:
        return False
    if _simple_name(ref.base) in MAPS:
        return False
    java_type = index.resolve(ref, context)
    return java_type is not None and java_type.kind in ("class", "record") and bool(java_type.fields)


def scan_java_path(root_path: str) -> YapiScanResult:
    root = Path(root_path).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"路径不存在：{root}")
    if not root.is_dir():
        raise ValueError(f"路径不是目录：{root}")
    return JavaSourceScanner(root).scan()
