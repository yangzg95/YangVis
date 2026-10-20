package com.example.shop.admin.dto;

import io.swagger.v3.oas.annotations.media.Schema;

public class PageParam {

    @Schema(description = "页码", example = "1")
    private int pageNo = 1;

    @Schema(description = "每页条数", example = "20")
    private int pageSize = 20;

    public int getPageNo() {
        return pageNo;
    }
}
