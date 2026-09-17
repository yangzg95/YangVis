"""测试公共配置。

后端包在 ``backend/`` 下（``app.*``），从仓库根跑 pytest 时把它加进
``sys.path``，测试文件直接 ``from app.... import ...`` 即可。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
