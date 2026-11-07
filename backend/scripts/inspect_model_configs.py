"""只读巡检：列出账号与各账号的模型配置，用于排查「尚未配置可用的对话模型」。

用法（在 backend 目录下）：
    ..\\.venv\\Scripts\\python.exe scripts\\inspect_model_configs.py

本脚本只做 SELECT，不写任何数据。
"""

from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models.entities import ModelConfig, SysUser


def main() -> None:
    with SessionLocal() as db:
        print("--- sys_user ---")
        for u in db.scalars(select(SysUser).order_by(SysUser.id)).all():
            print(f"id={u.id} username={u.username!r} admin={u.is_admin} status={u.status}")

        print("\n--- model_config ---")
        configs = db.scalars(select(ModelConfig).order_by(ModelConfig.owner_id, ModelConfig.id)).all()
        if not configs:
            print("(empty)")
        for c in configs:
            print(
                f"id={c.id} owner={c.owner_id} purpose={c.purpose!r} default={c.is_default} "
                f"test_ok={c.last_test_ok} title={c.title!r} model={c.model_name!r} "
                f"base_url={c.base_url!r} has_key={c.api_key_enc is not None}"
            )


if __name__ == "__main__":
    main()
