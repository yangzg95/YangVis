package com.example.shop.common;

import io.swagger.v3.oas.annotations.media.Schema;
import java.util.List;

@Schema(description = "分页结果")
public class PageResult<T> {

    @Schema(description = "总条数")
    private long total;

    @Schema(description = "当前页码")
    private int pageNo;

    @Schema(description = "每页条数")
    private int pageSize;

    private List<T> records;

    public long getTotal() {
        return total;
    }
}
