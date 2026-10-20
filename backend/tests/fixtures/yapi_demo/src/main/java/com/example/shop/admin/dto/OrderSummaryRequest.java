package com.example.shop.admin.dto;

import jakarta.validation.constraints.NotBlank;

public record OrderSummaryRequest(@NotBlank String keyword, Integer limit) {
}
