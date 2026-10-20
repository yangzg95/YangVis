package com.example.shop.common;

import io.swagger.v3.oas.annotations.media.Schema;

/**
 * 统一响应。
 */
@Schema(description = "统一响应")
public class Result<T> {

    @Schema(description = "业务码，0 表示成功", example = "0")
    private int code;

    @Schema(description = "提示信息", example = "ok")
    private String message;

    @Schema(description = "业务数据")
    private T data;

    public static <T> Result<T> ok(T data) {
        Result<T> result = new Result<>();
        result.code = 0;
        result.data = data;
        return result;
    }

    public int getCode() {
        return code;
    }
}
