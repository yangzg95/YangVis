package com.example.shop.order;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "订单状态")
public enum OrderStatus {
    CREATED,
    PAID,
    CANCELLED
}
