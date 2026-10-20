"""Java 源码扫描：包分组、参数归类、泛型 DTO 展开。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.yapi.scanner import scan_java_path

FIXTURE = Path(__file__).parent / "fixtures" / "yapi_demo"


@pytest.fixture(scope="module")
def scanned():
    return scan_java_path(str(FIXTURE))


def _by_key(result):
    return {(item.http_method, item.path): item for item in result.endpoints}


def test_scans_controllers_and_skips_non_source(scanned):
    assert scanned.java_files == 12
    assert scanned.controller_count == 2
    assert scanned.endpoint_count == 9
    assert [(item.package, item.category_name) for item in scanned.categories] == [
        ("com.example.shop.admin", "AdminOrder"),
        ("com.example.shop.order", "订单"),
    ]
    assert {(item.package, item.category_name) for item in scanned.endpoints} == {
        ("com.example.shop.admin", "AdminOrder"),
        ("com.example.shop.order", "订单"),
    }
    assert scanned.warnings == []
    # src/test 下的 Controller、package-info.java 都不该算进来。
    assert all("test-only" not in item.path for item in scanned.endpoints)


def test_generic_response_wrapper_is_unwrapped(scanned):
    endpoint = _by_key(scanned)[("GET", "/api/orders/{id}")]
    assert endpoint.title.startswith("查询订单详情")
    assert [(item.name, item.location) for item in endpoint.params] == [("id", "path")]
    assert endpoint.source == "src/main/java/com/example/shop/order/OrderController.java"
    assert endpoint.line > 0

    properties = endpoint.response_schema["properties"]
    assert set(properties) == {
        "id",
        "createdAt",
        "orderNo",
        "amount",
        "status",
        "items",
        "paidAt",
    }
    assert properties["createdAt"] == {
        "type": "string",
        "format": "date-time",
        "description": "创建时间",
    }
    assert properties["status"]["enum"] == ["CREATED", "PAID", "CANCELLED"]
    assert properties["orderNo"]["example"] == "SO20261020001"


def test_json_body_schema_follows_generics(scanned):
    endpoint = _by_key(scanned)[("POST", "/api/orders")]
    assert endpoint.request_body_type == "json"
    assert [(item.name, item.location) for item in endpoint.params] == [
        ("request", "body")
    ]

    schema = endpoint.request_body_schema
    assert schema["required"] == ["buyerName"]
    assert schema["properties"]["remark"]["example"] == "尽快发货"
    items = schema["properties"]["items"]
    assert items["type"] == "array"
    assert items["items"]["properties"]["sku"]["example"] == "SKU-1"


def test_page_response_keeps_item_type(scanned):
    endpoint = _by_key(scanned)[("GET", "/api/orders/page")]
    params = {item.name: item for item in endpoint.params}
    assert params["keyword"].required is False
    assert params["pageNo"].required is False
    assert params["X-Tenant"].location == "header"
    assert params["X-Tenant"].required is True

    records = endpoint.response_schema["properties"]["records"]
    assert records["type"] == "array"
    assert records["items"]["properties"]["orderNo"]["type"] == "string"


def test_multipart_params_land_in_form(scanned):
    endpoint = _by_key(scanned)[("POST", "/api/orders/{id}/upload")]
    assert endpoint.request_body_type == "form"
    params = {item.name: item for item in endpoint.params}
    assert params["id"].location == "path"
    assert params["file"].location == "form"
    assert params["file"].type == "file"
    assert params["remark"].location == "form"
    assert endpoint.response_schema == {"type": "string"}


def test_void_wrappers_keep_unified_envelope(scanned):
    endpoints = _by_key(scanned)
    update = endpoints[("PUT", "/api/orders/{id}")]
    assert set(update.response_schema["properties"]) == {"code", "message"}
    assert endpoints[("DELETE", "/api/orders/{id}")].response_schema is None


def test_record_components_use_validation_annotations(scanned):
    endpoint = _by_key(scanned)[("POST", "/admin/orders/summary")]
    schema = endpoint.request_body_schema
    assert schema["required"] == ["keyword"]
    assert set(schema["properties"]) == {"keyword", "limit"}


def test_model_attribute_is_flattened_into_form(scanned):
    endpoint = _by_key(scanned)[("POST", "/admin/orders/batch")]
    assert endpoint.request_body_type == "form"
    assert [(item.name, item.location) for item in endpoint.params] == [
        ("keyword", "form"),
        ("status", "form"),
        ("pageNo", "form"),
        ("pageSize", "form"),
    ]


def test_external_page_result_uses_standard_page_shape(tmp_path):
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    (package / "Foo.java").write_text(
        "package demo;\npublic class Foo {\n    private String name;\n}\n",
        encoding="utf-8",
    )
    (package / "DemoController.java").write_text(
        """
package demo;
import com.company.common.PageResult;
import com.company.common.Result;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class DemoController {
    @GetMapping("/page")
    public Result<PageResult<Foo>> page() { return null; }
}
""",
        encoding="utf-8",
    )

    result = scan_java_path(str(tmp_path))

    schema = result.endpoints[0].response_schema
    assert set(schema["properties"]) == {"list", "total", "pageNum", "pageSize", "pages"}
    items = schema["properties"]["list"]["items"]
    assert items["properties"]["name"]["type"] == "string"
    assert not any("PageResult" in warning for warning in result.warnings)


def test_unresolved_void_wrapper_uses_standard_envelope(tmp_path):
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    (package / "SpuNameParser.java").write_text(
        """
package demo;
public class SpuNameParser {
    public record Result(String spuName, String specText) {}
}
""",
        encoding="utf-8",
    )
    (package / "DemoController.java").write_text(
        """
package demo;
import com.company.common.Result;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class DemoController {
    @DeleteMapping("/items/{id}")
    public Result<Void> delete() { return null; }
}
""",
        encoding="utf-8",
    )

    result = scan_java_path(str(tmp_path))
    endpoint = next(item for item in result.endpoints if item.method == "delete")

    schema = endpoint.response_schema
    assert set(schema["properties"]) == {"code", "message", "traceId", "success"}
    assert not any("Result" in warning for warning in result.warnings)


def test_missing_path_is_rejected():
    with pytest.raises(ValueError, match="路径不存在"):
        scan_java_path(str(FIXTURE / "missing"))


def test_duplicate_method_and_path_keeps_first(tmp_path):
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    template = """
package demo;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class {name} {{
    @GetMapping("/dup")
    public String run() {{ return ""; }}
}}
"""
    (package / "FirstController.java").write_text(
        template.format(name="FirstController"), encoding="utf-8"
    )
    (package / "SecondController.java").write_text(
        template.format(name="SecondController"), encoding="utf-8"
    )

    result = scan_java_path(str(tmp_path))

    assert result.endpoint_count == 1
    assert result.endpoints[0].controller == "FirstController"
    assert any("重复接口" in warning for warning in result.warnings)


def test_each_controller_in_one_package_becomes_its_own_category(tmp_path):
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    template = """
package demo;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

{annotation}
@RestController
public class {name} {{
    @GetMapping("{path}")
    public String run() {{ return ""; }}
}}
"""
    (package / "OrderController.java").write_text(
        template.format(
            name="OrderController",
            annotation='@Tag(name = "订单")',
            path="/orders",
        ),
        encoding="utf-8",
    )
    (package / "AdminOrderController.java").write_text(
        template.format(
            name="AdminOrderController",
            annotation='@Tag(name = "后台订单")',
            path="/admin/orders",
        ),
        encoding="utf-8",
    )

    result = scan_java_path(str(tmp_path))

    assert result.controller_count == 2
    assert result.endpoint_count == 2
    assert [item.category_name for item in result.categories] == ["后台订单", "订单"]
    assert {item.controller: item.category_name for item in result.endpoints} == {
        "OrderController": "订单",
        "AdminOrderController": "后台订单",
    }
    assert result.warnings == []


def test_duplicate_swagger_tags_get_controller_suffix(tmp_path):
    package = tmp_path / "src" / "main" / "java" / "demo"
    package.mkdir(parents=True)
    template = """
package demo;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@Tag(name = "订单")
@RestController
public class {name} {{
    @GetMapping("{path}")
    public String run() {{ return ""; }}
}}
"""
    (package / "FirstController.java").write_text(
        template.format(name="FirstController", path="/first"), encoding="utf-8"
    )
    (package / "SecondController.java").write_text(
        template.format(name="SecondController", path="/second"), encoding="utf-8"
    )

    result = scan_java_path(str(tmp_path))

    assert [item.category_name for item in result.categories] == [
        "订单（demo.FirstController）",
        "订单（demo.SecondController）",
    ]
    assert any("重复" in warning for warning in result.warnings)
