package com.example.shop.order;

import com.example.shop.common.BaseEntity;
import io.swagger.v3.oas.annotations.media.Schema;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

@Schema(description = "订单视图")
public class OrderVO extends BaseEntity {

    @Schema(description = "订单号", example = "SO20261020001")
    private String orderNo;

    @Schema(description = "订单金额")
    private BigDecimal amount;

    @Schema(description = "订单状态")
    private OrderStatus status;

    private List<OrderItem> items;

    private LocalDateTime paidAt;

    public String getOrderNo() {
        return orderNo;
    }
}
