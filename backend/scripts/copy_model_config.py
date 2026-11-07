"""把一个账号下指定标题的模型配置复制给另一个账号（一次性运维脚本）。

背景：模型配置按 owner_id 隔离，新账号名下没有任何对话模型时，App/Web 对话
会报「尚未配置可用的对话模型」。api_key 用服务端全局 Fernet 加密，密文可直接
跨账号复制，解密不受 owner 影响。

用法（在 backend 目录下）：
    $env:PYTHONPATH='.'
    ..\\.venv\\Scripts\\python.exe scripts\\copy_model_config.py \\
        --source-owner 1 --target luke --title yangvis

幂等：目标账号已存在同 purpose + 同 title 的配置时跳过，不重复插入。
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.models.entities import ModelConfig, SysUser


def main() -> None:
    parser = argparse.ArgumentParser(description="复制模型配置到目标账号")
    parser.add_argument("--source-owner", type=int, required=True, help="源账号 user id")
    parser.add_argument("--target", required=True, help="目标账号用户名")
    parser.add_argument("--title", required=True, help="要复制的配置标题（精确匹配）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = parser.parse_args()

    with SessionLocal() as db:
        target = db.scalar(select(SysUser).where(SysUser.username == args.target))
        if target is None:
            raise SystemExit(f"目标账号不存在：{args.target}")

        sources = db.scalars(
            select(ModelConfig).where(
                ModelConfig.owner_id == args.source_owner,
                ModelConfig.title == args.title,
            )
        ).all()
        if not sources:
            raise SystemExit(
                f"源账号 {args.source_owner} 下找不到标题为 {args.title!r} 的配置"
            )

        for src in sources:
            exists = db.scalar(
                select(ModelConfig).where(
                    ModelConfig.owner_id == target.id,
                    ModelConfig.purpose == src.purpose,
                    ModelConfig.title == src.title,
                )
            )
            if exists is not None:
                print(f"skip: 目标已有同配置 id={exists.id} ({src.purpose}/{src.title})")
                continue

            # 目标在该用途下已有默认配置时，新副本不再抢占默认位。
            has_default = db.scalar(
                select(ModelConfig).where(
                    ModelConfig.owner_id == target.id,
                    ModelConfig.purpose == src.purpose,
                    ModelConfig.is_default.is_(True),
                )
            )
            row = ModelConfig(
                owner_id=target.id,
                purpose=src.purpose,
                title=src.title,
                model_name=src.model_name,
                base_url=src.base_url,
                # 全局 Fernet 密文，跨账号可直接解密。
                api_key_enc=src.api_key_enc,
                remark=src.remark,
                is_default=src.is_default and has_default is None,
                vector_size=src.vector_size,
                # 同样的上游与凭据，连通性结论直接沿用；目标账号也可在 Web 端重测。
                last_tested_at=src.last_tested_at,
                last_test_ok=src.last_test_ok,
                last_test_error=src.last_test_error,
                reasoning_mode=src.reasoning_mode,
                request_extra=src.request_extra,
                temperature=src.temperature,
                supports_vision=src.supports_vision,
            )
            db.add(row)
            print(
                f"copy: {src.title!r} purpose={src.purpose} "
                f"owner {args.source_owner} -> {target.id} "
                f"default={row.is_default} test_ok={row.last_test_ok}"
            )

        if args.dry_run:
            db.rollback()
            print("dry-run: 已回滚，未写库")
        else:
            db.commit()
            print("done: 已提交")


if __name__ == "__main__":
    main()
