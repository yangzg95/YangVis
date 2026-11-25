"""ffmpeg 成片合成：把各镜片段按镜头顺序拼成一个 MP4，可选叠一条 TTS 配音轨。

为什么用 ``imageio-ffmpeg`` 而不是系统 ffmpeg：它的 wheel 里**自带**一份 ffmpeg
可执行文件，``get_ffmpeg_exe()`` 直接给出路径，不要求部署机器装系统级 ffmpeg，
Windows 上也没有 PATH / 动态库那一摊事——与 requirements 里 pillow「wheel 自带
FreeType」是同一种取舍。

两段式策略（``-c copy`` 直拼，失败再重编码）：

* 同一型号、同一档参数渲出来的片段，编码/分辨率/帧率一致，concat demuxer 直接封装
  即可，**无损且只要几秒**，这是常态路径；
* 片段参数不一致（混过型号、混过分辨率）时直拼会被 ffmpeg 拒掉，回退到 libx264
  统一重编码。视频片段本身不带音轨（方舟侧 ``generate_audio`` 显式关着），回退档
  ``-an``，不让编码器自己造一条静音轨。

配音（TTS）是叠在视频拼接**之后**的第三段：每句台词已经按镜头单独合成成 mp3，
本模块把它们按镜头槽位补齐/截短后依次拼接（``concat``），说太长的句子先加速
（``atempo``），最后统一重采样成 AAC 并封装进成片。语音从哪来是
:mod:`app.services.media.tts` 与 ``runner`` 的事，这里只认识「音频文件 + 槽位」。

调用方（``runner``）负责临时目录生命周期与产物登记，本模块只产出文件。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from app.config import Settings, get_settings

logger = logging.getLogger("yangvis.publish.composer")

settings: Settings = get_settings()

# 出错时往 error_msg 里带多少 ffmpeg stderr：够排障，又不撑爆 512 的列。
_STERR_TAIL = 400

# ffmpeg -i 的 stderr 里时长行形状：「Duration: 00:00:05.04, start: 0.000000, ...」。
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
# 实测比槽位长这一点点以内不加速：帧级误差不值得为它挂一个 atempo 滤镜。
_SLOT_TOLERANCE_S = 0.05
_AUDIO_BITRATE = "128k"


class PublishComposeError(RuntimeError):
    """合成失败：ffmpeg 缺失、超时或返回非零。路由按业务拒绝（``code=-1``）呈现。"""


@dataclass(frozen=True)
class ComposeProgress:
    """一次合成里的可展示进度；stage 是稳定键，detail 是给人看的现场。"""

    stage: str
    percent: int
    detail: str
    current: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ComposeProgress], Awaitable[None]]


@dataclass(frozen=True)
class VoiceoverLine:
    """一句已经合成好的配音在时间轴上的位置。

    ``start_s`` 是所属镜头的起点（各镜时长累加），``slot_s`` 是该镜头的时长——
    音频比槽位长时按它算加速倍率。``sort_order`` 只用于把报错说成「第几镜」。
    """

    path: Path
    sort_order: int
    start_s: float
    slot_s: float


@dataclass(frozen=True)
class VoiceoverPlan:
    """一次成片的全部配音句；顺序即镜头顺序，空句（无台词的镜头）不该出现在这里。"""

    lines: Sequence[VoiceoverLine]


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


async def _consume_lines(stream, chunks: list[bytes], on_line=None) -> bytes:
    while line := await stream.readline():
        chunks.append(line)
        if on_line is not None:
            await on_line(line.decode("utf-8", "replace"))
    return b"".join(chunks)


async def _run_ffmpeg(
    args: Sequence[str],
    *,
    stage: str = "处理",
    on_progress: Callable[[float], Awaitable[None]] | None = None,
    duration_s: float | None = None,
) -> tuple[int, str]:
    """跑一次 ffmpeg，返回 ``(返回码, stderr 文本)``；超时杀进程并抛错。

    传了 ``on_progress`` 时让 ffmpeg 把机器可读进度写到 stdout，回调参数是
    ``0..1``。总时长优先用调用方算好的配音/视频时长，否则从 stderr 的 Duration 行读；
    这样百分比是已处理的输出时间，不是按阶段估出来的假进度。
    """
    started = time.monotonic()
    invoke_args = list(args)
    if on_progress is not None:
        invoke_args[1:1] = ["-nostats", "-progress", "pipe:1"]
        if "-loglevel" in invoke_args:
            invoke_args[invoke_args.index("-loglevel") + 1] = "info"
    logger.info("ffmpeg %s invoke: %s", stage, " ".join(str(arg) for arg in invoke_args[:10]))
    try:
        process = await asyncio.create_subprocess_exec(
            *invoke_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if on_progress is not None else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PublishComposeError(f"ffmpeg 启动失败（{stage}）：{exc}") from exc

    readers: list[asyncio.Task] = []
    if on_progress is None:
        communication = asyncio.create_task(process.communicate())
    else:
        stderr_chunks: list[bytes] = []
        stdout_chunks: list[bytes] = []
        total_s = duration_s
        latest_out_s = 0.0

        async def consume_stderr(line: str) -> None:
            nonlocal total_s
            if total_s is not None:
                return
            match = _DURATION_RE.search(line)
            if match:
                hours, minutes, seconds = match.groups()
                total_s = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

        async def consume_stdout(line: str) -> None:
            nonlocal latest_out_s
            key, separator, value = line.strip().partition("=")
            if not separator or key != "out_time_us":
                return
            try:
                latest_out_s = max(latest_out_s, int(value) / 1_000_000)
            except ValueError:
                return
            if total_s and total_s > 0:
                await on_progress(min(1.0, max(0.0, latest_out_s / total_s)))

        stderr_reader = asyncio.create_task(
            _consume_lines(process.stderr, stderr_chunks, consume_stderr)
        )
        stdout_reader = asyncio.create_task(
            _consume_lines(process.stdout, stdout_chunks, consume_stdout)
        )
        readers = [stderr_reader, stdout_reader]
        communication = asyncio.gather(*readers)

    try:
        result = await asyncio.wait_for(
            asyncio.shield(communication), timeout=settings.PUBLISH_COMPOSE_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        if readers:
            stderr_bytes, _ = await asyncio.gather(*readers)
        else:
            _, stderr_bytes = await communication
        detail = stderr_bytes.decode("utf-8", "replace").strip()
        logger.warning(
            "ffmpeg %s timed out: elapsed=%.1fs stderr=%s",
            stage,
            time.monotonic() - started,
            detail[-_STERR_TAIL:],
        )
        suffix = f"；ffmpeg 输出：{detail[-_STERR_TAIL:]}" if detail else ""
        raise PublishComposeError(
            f"ffmpeg {stage}超过 {settings.PUBLISH_COMPOSE_TIMEOUT:g} 秒，已终止{suffix}"
        ) from exc
    except BaseException:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        else:
            await asyncio.gather(communication, return_exceptions=True)
        raise

    if readers:
        stderr_bytes, _ = result
    else:
        _, stderr_bytes = result
    code = process.returncode or 0
    if code == 0 and on_progress is not None:
        await on_progress(1.0)
    logger.info(
        "ffmpeg %s finished: code=%s elapsed=%.1fs",
        stage,
        process.returncode,
        time.monotonic() - started,
    )
    return code, stderr_bytes.decode("utf-8", "replace")


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
        # 片段没有可用音轨：直拼失败时各片段口径本来就对不上，统一不要声音，
        # 配音在后续混音阶段单独装配，不在这里造静音轨。
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


def _stage_percent(start: int, end: int, fraction: float) -> int:
    return start + round((end - start) * min(1.0, max(0.0, fraction)))


async def _emit(progress: ProgressCallback | None, update: ComposeProgress) -> None:
    if progress is not None:
        await progress(update)


async def _concat_silent(
    exe: str,
    list_file: Path,
    out_path: Path,
    *,
    progress: ProgressCallback | None = None,
    percent_range: tuple[int, int] = (30, 60),
    source_count: int = 0,
) -> None:
    """按镜头清单拼出一条**无声**中间片：直拼优先，失败回退重编码。"""
    start, end = percent_range
    await _emit(
        progress,
        ComposeProgress(
            "concat",
            start,
            f"正在直接拼接 {source_count} 个片段（不重新编码）",
            current=0,
            total=source_count,
        ),
    )
    code, stderr = await _run_ffmpeg(
        _copy_args(exe, list_file, out_path),
        stage="片段直拼",
        on_progress=(lambda fraction: _emit(
            progress,
            ComposeProgress(
                "concat",
                _stage_percent(start, end, fraction),
                f"正在直接拼接 {source_count} 个片段（不重新编码）",
                current=0,
                total=source_count,
            ),
        )) if progress is not None else None,
    )
    if code == 0 and _output_ok(out_path):
        logger.info("ffmpeg concat-copy ok: out=%s", out_path.name)
        return

    logger.warning(
        "ffmpeg concat-copy failed (code=%s), fallback to re-encode: %s",
        code,
        stderr[-_STERR_TAIL:],
    )
    out_path.unlink(missing_ok=True)
    await _emit(
        progress,
        ComposeProgress(
            "reencode",
            start,
            "片段参数不一致，正在统一重编码",
            current=0,
            total=source_count,
        ),
    )
    code, stderr = await _run_ffmpeg(
        _reencode_args(exe, list_file, out_path),
        stage="片段重编码",
        on_progress=(lambda fraction: _emit(
            progress,
            ComposeProgress(
                "reencode",
                _stage_percent(start, end, fraction),
                "片段参数不一致，正在统一重编码",
                current=0,
                total=source_count,
            ),
        )) if progress is not None else None,
    )
    if code != 0 or not _output_ok(out_path):
        detail = stderr.strip()[-_STERR_TAIL:] or f"退出码 {code}"
        raise PublishComposeError(f"ffmpeg 合成失败：{detail}")
    logger.info("ffmpeg re-encode ok: out=%s", out_path.name)


async def probe_duration(exe: str, path: Path) -> float:
    """读一个音/视频文件的时长（秒）。

    imageio-ffmpeg 的 wheel 只带 ffmpeg 不带 ffprobe，而 ``ffmpeg -i`` 不给输出
    文件时会以退出码 1 结束、却照样把输入信息（含 ``Duration:`` 行）打到 stderr——
    所以这里不看返回码，只认正则；解析不到就是真失败，抛错让作业落失败状态。
    """
    _, stderr = await _run_ffmpeg(
        [exe, "-nostdin", "-hide_banner", "-i", str(path)], stage="文件时长探测"
    )
    match = _DURATION_RE.search(stderr)
    if not match:
        raise PublishComposeError(
            f"ffmpeg 读不到文件时长（{path.name}），无法对齐配音时间轴"
        )
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def build_voiceover(
    exe: str,
    plan: VoiceoverPlan,
    total_s: float,
    out_path: Path,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    """把各句 mp3 按镜头槽位串成一条与成片等长的 AAC（m4a 封装）。

    每句先按需 ``atempo``，再用 ``apad + atrim`` 补齐到自己的镜头槽位；无台词镜头
    不在 ``plan.lines`` 里，因此按 ``start_s`` 显式插入等长静音段，最后 ``concat``
    依次拼接。这里不能用 ``adelay + amix=duration=longest``：线上 FFmpeg 4.2.2
    在多个延迟输入混音时会一直等不到 EOF，30 秒的片子也会卡死。槽位串拼不需要
    等待其他输入结束，兼容旧版 FFmpeg，也不会像 ``duration=first`` 那样把第一句
    之后的台词全丢掉。
    """
    if not plan.lines:
        raise PublishComposeError("配音计划为空：没有任何镜头带台词")

    line_count = len(plan.lines)
    await _emit(
        progress,
        ComposeProgress("voiceover", 62, "正在检查各句配音的时长", 0, line_count),
    )
    inputs: list[str] = []
    filters: list[str] = []
    timeline_inputs: list[str] = []
    cursor_s = 0.0
    silence_index = 0
    for index, line in enumerate(plan.lines):
        if not line.path.is_file():
            raise PublishComposeError(f"第 {line.sort_order} 镜的配音文件不存在：{line.path.name}")
        if line.slot_s <= 0:
            raise PublishComposeError(f"第 {line.sort_order} 镜的时长为 0，无法放置配音")
        gap_s = line.start_s - cursor_s
        if gap_s < -_SLOT_TOLERANCE_S:
            raise PublishComposeError(
                f"第 {line.sort_order} 镜的配音起点 {line.start_s:.2f} 秒早于上一槽位终点 "
                f"{cursor_s:.2f} 秒，配音时间轴重叠"
            )
        if gap_s > _SLOT_TOLERANCE_S:
            label = f"g{silence_index}"
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate={settings.TTS_SAMPLE_RATE},"
                f"atrim=0:{gap_s:.3f}[{label}]"
            )
            timeline_inputs.append(f"[{label}]")
            silence_index += 1
        inputs.extend(["-i", str(line.path)])

        duration_s = await probe_duration(exe, line.path)
        await _emit(
            progress,
            ComposeProgress(
                "voiceover", 62, "正在检查各句配音的时长", index + 1, line_count
            ),
        )
        chain = f"[{index}:a]"
        if duration_s - line.slot_s > _SLOT_TOLERANCE_S:
            # atempo 只作用在人声本体上；槽位不足时先压到槽位内。
            factor = duration_s / line.slot_s
            if factor > settings.TTS_MAX_ATEMPO:
                raise PublishComposeError(
                    f"第 {line.sort_order} 镜台词太长：配音约 {duration_s:.1f} 秒，镜头槽位只有 "
                    f"{line.slot_s:.0f} 秒，加速到上限 {settings.TTS_MAX_ATEMPO:g}x 仍放不下，"
                    "请删减该镜台词或加长镜头后重新合成"
                )
            chain += f"atempo={factor:.4f},"
        chain += (
            f"apad,atrim=0:{line.slot_s:.3f},aresample={settings.TTS_SAMPLE_RATE},"
            f"aformat=channel_layouts=stereo[s{index}]"
        )
        filters.append(chain)
        timeline_inputs.append(f"[s{index}]")
        cursor_s = line.start_s + line.slot_s

    filters.append(
        f"{''.join(timeline_inputs)}concat=n={len(timeline_inputs)}:v=0:a=1[joined]"
    )
    filters.append(
        f"[joined]apad,atrim=0:{total_s:.3f},aresample={settings.TTS_SAMPLE_RATE}[aout]"
    )

    args = [
        exe,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[aout]",
        "-c:a",
        "aac",
        "-b:a",
        _AUDIO_BITRATE,
        str(out_path),
    ]
    await _emit(
        progress,
        ComposeProgress(
            "voiceover", 65, f"正在混音 {line_count} 句配音", line_count, line_count
        ),
    )
    code, stderr = await _run_ffmpeg(
        args,
        stage="配音混音",
        duration_s=total_s,
        on_progress=(lambda fraction: _emit(
            progress,
            ComposeProgress(
                "voiceover",
                _stage_percent(65, 85, fraction),
                f"正在混音 {line_count} 句配音",
                line_count,
                line_count,
            ),
        )) if progress is not None else None,
    )
    if code != 0 or not _output_ok(out_path):
        detail = stderr.strip()[-_STERR_TAIL:] or f"退出码 {code}"
        raise PublishComposeError(f"ffmpeg 配音混音失败：{detail}")
    logger.info("ffmpeg voiceover built: lines=%s out=%s", len(plan.lines), out_path.name)


async def mux_voiceover(
    exe: str,
    video_path: Path,
    audio_path: Path,
    out_path: Path,
    *,
    total_s: float,
    progress: ProgressCallback | None = None,
) -> None:
    """把配音轨封进无声中间片：视频与音频都只做流拷贝，不再重编码。"""
    await _emit(progress, ComposeProgress("mux", 88, "正在把配音封装进成片"))
    args = [
        exe,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    code, stderr = await _run_ffmpeg(
        args,
        stage="音视频封装",
        duration_s=total_s,
        on_progress=(lambda fraction: _emit(
            progress,
            ComposeProgress("mux", _stage_percent(88, 96, fraction), "正在把配音封装进成片"),
        )) if progress is not None else None,
    )
    if code != 0 or not _output_ok(out_path):
        detail = stderr.strip()[-_STERR_TAIL:] or f"退出码 {code}"
        raise PublishComposeError(f"ffmpeg 音视频封装失败：{detail}")
    logger.info("ffmpeg mux ok: video=%s audio=%s", video_path.name, audio_path.name)


async def compose(
    sources: Sequence[Path],
    out_path: Path,
    voiceover: VoiceoverPlan | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    """把 ``sources`` 按顺序合成到 ``out_path``；失败抛 :class:`PublishComposeError`。

    清单文件写在 ``out_path`` 同目录（调用方给的是临时目录），不污染媒体目录。
    带 ``voiceover`` 时走三段式：无声拼接 → 配音混音 → 音视频封装；不带时与以前
    完全一致（直拼或重编码，产物无音轨）。
    """
    if not sources:
        raise PublishComposeError("没有可合成的片段")
    exe = ffmpeg_exe()
    list_file = out_path.parent / "concat.txt"
    write_concat_list(sources, list_file)

    if voiceover is None:
        await _concat_silent(
            exe,
            list_file,
            out_path,
            progress=progress,
            percent_range=(5, 92),
            source_count=len(sources),
        )
        return

    # 配音链路先落无声中间片：视频流只编/拷一次，最后封装是纯 remux，失败也能
    # 从中间片分清是「拼接」还是「混音」哪一段出的错。
    silent_path = out_path.parent / "silent.mp4"
    await _concat_silent(
        exe, list_file, silent_path, progress=progress, source_count=len(sources)
    )
    await _emit(progress, ComposeProgress("voiceover", 61, "正在读取成片时长"))
    total_s = await probe_duration(exe, silent_path)
    track_path = out_path.parent / "voiceover.m4a"
    await build_voiceover(exe, voiceover, total_s, track_path, progress=progress)
    await mux_voiceover(
        exe, silent_path, track_path, out_path, total_s=total_s, progress=progress
    )
