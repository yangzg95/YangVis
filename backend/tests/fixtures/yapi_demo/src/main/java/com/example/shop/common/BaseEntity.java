package com.example.shop.common;

import io.swagger.v3.oas.annotations.media.Schema;
import java.time.LocalDateTime;

public class BaseEntity {

    @Schema(description = "主键")
    private Long id;

    @Schema(description = "创建时间")
    private LocalDateTime createdAt;

    public Long getId() {
        return id;
    }
}
