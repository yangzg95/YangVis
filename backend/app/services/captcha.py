"""图形验证码的生成与校验（无状态）。

captcha_id 是用 ENCRYPTION_KEY 加密的 Fernet 令牌，payload 就是答案本身：

- 答案不出现在客户端可读的位置。必须是加密而不是签名/MAC：几位的答案
  空间太小，任何确定性的派生值都能被离线穷举出来；
- 无状态意味着 gunicorn 多 worker 下任意进程都能校验，不需要共享存储；
- 过期由 Fernet 的 TTL 机制校验，令牌被篡改同样解不开。

代价是令牌在 TTL 内可重放：攻击者解出一张验证码后，几分钟内能反复用它
提交登录。这个折中可接受——尝试次数的真正上限由 login_guard 的失败计数
卡住，且锁定窗口（默认 10 分钟）长于验证码 TTL（默认 5 分钟），重放
跨不过一个锁定周期，每个周期仍需重新过人机校验。
"""
from __future__ import annotations

import hmac
import io
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings

settings = get_settings()

# 去掉易混淆字符（0/O、1/I/L），用户不用猜到底是哪个。
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

_WIDTH = 132
_HEIGHT = 48


@lru_cache
def _cipher() -> Fernet:
    """与 app.crypto 同一把 ENCRYPTION_KEY；验证码令牌不进库，独立缓存。"""
    return Fernet(settings.ENCRYPTION_KEY.encode("utf-8"))


def _render(code: str) -> bytes:
    """把答案绘成带干扰的 PNG。"""
    image = Image.new("RGB", (_WIDTH, _HEIGHT), (245, 247, 250))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)

    def _pale() -> tuple[int, int, int]:
        return tuple(secrets.randbelow(70) + 140 for _ in range(3))  # type: ignore[return-value]

    # 干扰线先画，让字符压在它上面。
    for _ in range(4):
        draw.line(
            [
                (secrets.randbelow(_WIDTH), secrets.randbelow(_HEIGHT)),
                (secrets.randbelow(_WIDTH), secrets.randbelow(_HEIGHT)),
            ],
            fill=_pale(),
            width=1,
        )
    for _ in range(40):
        draw.point(
            (secrets.randbelow(_WIDTH), secrets.randbelow(_HEIGHT)),
            fill=_pale(),
        )

    step = _WIDTH // (len(code) + 1)
    for i, ch in enumerate(code):
        # 每个字符画在独立小画布上旋转后再贴回去，倾斜角度互不相关。
        tile = Image.new("RGBA", (44, 44), (0, 0, 0, 0))
        color = tuple(secrets.randbelow(110) + 20 for _ in range(3))
        ImageDraw.Draw(tile).text((7, 3), ch, font=font, fill=color)
        tile = tile.rotate(
            secrets.randbelow(57) - 28, resample=Image.BICUBIC, expand=False
        )
        image.paste(tile, (step * i + secrets.randbelow(9) - 2, secrets.randbelow(9)), tile)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def issue_captcha() -> tuple[str, bytes]:
    """签发一张验证码，返回 (captcha_id, PNG 字节)。"""
    code = "".join(secrets.choice(_ALPHABET) for _ in range(settings.AUTH_CAPTCHA_LENGTH))
    token = _cipher().encrypt(code.lower().encode("utf-8")).decode("ascii")
    return token, _render(code)


def verify_captcha(captcha_id: str, code: str) -> bool:
    """校验验证码：令牌须能解密、未过期，且答案一致（大小写不敏感）。"""
    try:
        expected = _cipher().decrypt(
            captcha_id.encode("ascii"), ttl=settings.AUTH_CAPTCHA_TTL
        ).decode("utf-8")
    except (InvalidToken, ValueError):
        return False
    return hmac.compare_digest(expected, code.strip().lower())
