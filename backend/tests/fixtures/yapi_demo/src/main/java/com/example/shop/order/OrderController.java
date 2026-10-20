package com.example.shop.order;

import com.example.shop.common.PageResult;
import com.example.shop.common.Result;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.Parameter;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@Tag(name = "订单")
@RestController
@RequestMapping("/api/orders")
public class OrderController {

    /**
     * 查询订单详情。
     *
     * @param id 订单主键
     */
    @GetMapping("/{id}")
    public Result<OrderVO> detail(@PathVariable Long id) {
        return Result.ok(null);
    }

    @PostMapping
    @Operation(summary = "创建订单", description = "创建后返回订单号")
    public Result<Long> create(@RequestBody OrderCreateRequest request) {
        return Result.ok(1L);
    }

    @GetMapping("/page")
    public Result<PageResult<OrderVO>> page(
            @RequestParam(required = false) String keyword,
            @RequestParam(defaultValue = "1") int pageNo,
            @RequestHeader("X-Tenant") String tenant) {
        return Result.ok(null);
    }

    @PostMapping("/{id}/upload")
    public Result<String> upload(
            @PathVariable Long id,
            @RequestPart("file") MultipartFile file,
            @RequestParam("remark") String remark) {
        return Result.ok("ok");
    }

    @PutMapping("/{id}")
    public ResponseEntity<Result<Void>> update(
            @PathVariable Long id,
            @RequestBody OrderCreateRequest request) {
        return ResponseEntity.ok(Result.ok(null));
    }

    @DeleteMapping("/{id}")
    public void delete(@PathVariable Long id) {
    }

    @RequestMapping(value = "/health", method = RequestMethod.HEAD)
    public Result<String> health() {
        return Result.ok("ok");
    }

    /**
     * 内部方法，没有映射注解，不应被扫描。
     */
    public String helper() {
        return "";
    }
}
