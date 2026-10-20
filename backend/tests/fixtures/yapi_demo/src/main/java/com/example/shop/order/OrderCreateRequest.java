package com.example.shop.order;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;
import java.util.List;

/**
 * 创建订单入参。
 */
public class OrderCreateRequest {

    /**
     * 买家名称
     */
    @NotBlank
    private String buyerName;

    @Schema(description = "备注", example = "尽快发货")
    private String remark;

    @Schema(description = "商品明细")
    private List<OrderItem> items;

    public String getBuyerName() {
        return buyerName;
    }
}
