package com.example.shop.order;

import io.swagger.v3.oas.annotations.media.Schema;

@Schema(description = "订单明细")
public class OrderItem {

    @Schema(description = "商品编码", example = "SKU-1")
    private String sku;

    @Schema(description = "购买数量", example = "2")
    private int quantity;

    public String getSku() {
        return sku;
    }
}
