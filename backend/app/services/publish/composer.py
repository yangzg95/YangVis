"""ffmpeg 成片合成：把各镜片段按镜头顺序拼成一个 MP4。

为什么用 ``imageio-ffmpeg`` 而不是系统 ffmpeg：它的 wheel 里**自带**一份 ffmpeg
可执行文件，``get_ffmpeg_exe()`` 直接给出路径，不要求部署机器装系统级 ffmpeg，
Windows 上也没有 PATH / 动态库那一摊事——与 requirements 里 pillow「wheel 自带
FreeType」是同一种取舍。

两段式策略（``-c copy`` 直拼，失败再重编码）：

* 同一型号、同一档参数渲出来的片段，编码/分辨率/帧率一致，concat demuxer 直接封装
  即可，**无损且只要几秒**，这是常态路径；
* 片段参数不一致（混过型号、混过分辨率）时直拼会被 ffmpeg 拒掉，回退到 libx264
  统一重编码。本期定的是「不做口播、只做字幕」，片段大概率没有音轨，回退档显式
  ``-an``，不让编码器自己造一条静音轨。

调用方（``runner``）负责临时目录生命周期与产物登记，本模块只产出文件。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Sequence

from app.config import Settings, get_settings

logger = logging.getLogger("yangvis.publish.composer")

settings: Settings = get_settings()

# 出错时往 error_msg 里带多少 ffmpeg stderr：够排障，又不撑爆 512 的列。
_STERR_TAIL = 400


class PublishComposeError(RuntimeError):
    """合成失败：ffmpeg 缺失、超时或返回非零。路由按业务拒绝（``code=-1``）呈现。"""


def ffmpeg_exe() -> str:
    """返回 wheel 自带 ffmpeg 的可执行路径；依赖缺失时给一条能照着修的错。"""
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - 依赖在 requirements 里钉着
        raise PublishComposeError(
            "服务器缺少 imageio-ffmpeg（ffmpeg 可执行文件），请先安装后端依赖"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def _escape_concat_path(path: Path) -> str:
    """concat 清单里的路径转义。

    demuxer 的语法是 ``file '路径'``：单引号要写成 ``'\\''``，反斜杠是它的转义符
    所以 Windows 路径统一转正斜杠——否则 ``\\1`` 这种会当成转义序列吃掉。
    """
    normalized = str(path.resolve()).replace("\\", "/")
    return normalized.replace("'", "'\\''")


def write_concat_list(sources: Sequence[Path], list_file: Path) -> None:
    """把片段路径写成 ffmpeg concat demuxer 的清单文件（UTF-8）。"""
    lines = [f"file '{_escape_concat_path(path)}'" for path in sources]
    list_file.write_text("\n".join(lines), encoding="utf-8")


async def _run_ffmpeg(args: Sequence[str]) -> tuple[int, str]:
    """跑一次 ffmpeg，返回 ``(返回码, stderr 文本)``；超时杀进程并抛错。"""
    logger.info("ffmpeg invoke: %s", " ".join(str(arg) for arg in args[:8]))
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PublishComposeError(f"ffmpeg 启动失败：{exc}") from exc

    try:
        _, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=settings.PUBLISH_COMPOSE_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise PublishComposeError(
            f"ffmpeg 合成超过 {settings.PUBLISH_COMPOSE_TIMEOUT:.0f} 秒，已终止"
        ) from exc
    return process.returncode or 0, stderr_bytes.decode("utf-8", "replace")


def _copy_args(exe: str, list_file: Path, out_path: Path) -> list[str]:
    """常态档：同编码直接封装，不重采样，秒级完成。"""
    return [
        exe,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(out_path),
    ]


def _reencode_args(exe: str, list_file: Path, out_path: Path) -> list[str]:
    """回退档：参数不一致时统一重编成抖音收的 H.264 / yuv420p。"""
    return [
        exe,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        # 本期不做口播：直拼失败时各片段音轨口径本来就对不上，统一不要声音，
        # 字幕走单独的 SRT/ASS，不烧进画面。
        "-an",
        "-movflags",
        "+faststart",
        str(out_path),
    ]


def _output_ok(out_path: Path) -> bool:
    try:
        return out_path.is_file() and out_path.stat().st_size > 0
    except OSError:
        return False


async def compose(sources: Sequence[Path], out_path: Path) -> None:
    """把 ``sources`` 按顺序合成到 ``out_path``；失败抛 :class:`PublishComposeError`。

    清单文件写在 ``out_path`` 同目录（调用方给的是临时目录），不污染媒体目录。
    """
    if not sources:
        raise PublishComposeError("没有可合成的片段")
    exe = ffmpeg_exe()
    list_file = out_path.parent / "concat.txt"
    write_concat_list(sources, list_file)

    code, stderr = await _run_ffmpeg(_copy_args(exe, list_file, out_path))
    if code == 0 and _output_ok(out_path):
        logger.info("ffmpeg concat-copy ok: clips=%s out=%s", len(sources), out_path.name)
        return

    logger.warning(
        "ffmpeg concat-copy failed (code=%s), fallback to re-encode: %s",
        code,
        stderr[-_STERR_TAIL:],
    )
    out_path.unlink(missing_ok=True)
    code, stderr = await _run_ffmpeg(_reencode_args(exe, list_file, out_path))
    if code != 0 or not _output_ok(out_path):
        detail = stderr.strip()[-_STERR_TAIL:] or f"退出码 {code}"
        raise PublishComposeError(f"ffmpeg 合成失败：{detail}")
    logger.info("ffmpeg re-encode ok: clips=%s out=%s", len(sources), out_path.name)
