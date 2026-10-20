package com.example.shop.admin.dto;

import com.example.shop.order.OrderStatus;
import io.swagger.v3.oas.annotations.media.Schema;

public class OrderQuery {

    @Schema(description = "关键字")
    private String keyword;

    @Schema(description = "订单状态")
    private OrderStatus status;

    @Schema(description = "分页参数")
    private PageParam page;

    public String getKeyword() {
        return keyword;
    }
}
