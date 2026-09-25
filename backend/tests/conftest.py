"""测试公共配置。

后端包在 ``backend/`` 下（``app.*``），从仓库根跑 pytest 时把它加进
``sys.path``，测试文件直接 ``from app.... import ...`` 即可。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# app.config 在导入时就把环境变量固化成 Settings（lru_cache），而 crypto /
# security 这些模块一旦被导入就会读它。所以这两项必须赶在任何 app.* 导入之前
# 就位：测试用的是随机生成的一次性密钥，不碰真实环境的 .env。
os.environ.setdefault("AUTH_JWT_SECRET", "unit-test-only-secret-value-0123456789")
if not os.environ.get("ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet

    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
