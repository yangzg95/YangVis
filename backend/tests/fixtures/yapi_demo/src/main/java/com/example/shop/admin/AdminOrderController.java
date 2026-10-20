package com.example.shop.admin;

import com.example.shop.admin.dto.OrderQuery;
import com.example.shop.admin.dto.OrderSummaryRequest;
import com.example.shop.common.Result;
import io.swagger.v3.oas.annotations.Operation;
import org.springframework.web.bind.annotation.ModelAttribute;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/admin/orders")
public class AdminOrderController {

    @PostMapping("/batch")
    @Operation(summary = "批量查询订单")
    public Result<Integer> batch(@ModelAttribute OrderQuery query) {
        return Result.ok(0);
    }

    @PostMapping("/summary")
    public Result<String> summary(@RequestBody OrderSummaryRequest request) {
        return Result.ok("");
    }
}
