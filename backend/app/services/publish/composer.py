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
本模块只负责把它们放上时间轴（``adelay``）、说太长的句子加速（``atempo``）、
混成一条 AAC（``amix``），再无损封装进成片。语音从哪来是 :mod:`app.services.media.tts`
与 ``runner`` 的事，这里只认识「音频文件 + 起点 + 槽位」。

调用方（``runner``）负责临时目录生命周期与产物登记，本模块只产出文件。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.config import Settings, get_settings

logger = logging.getLogger("yangvis.publish.composer")

settings: Settings = get_settings()

# 出错时往 error_msg 里带多少 ffmpeg stderr：够排障，又不撑爆 512 的列。
_STERR_TAIL = 400

# ffmpeg -i 的 stderr 里时长行形状：「Duration: 00:00:05.04, start: 0.000000, ...」。
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
# 音频流行形状：「Stream #0:0: Audio: mp3, 44100 Hz, mono, fltp, 128 kb/s」。
# 声道布局那一栏可能是 mono/stereo、5.1(side) 或「2 channels」，截到逗号/括号前。
_AUDIO_STREAM_RE = re.compile(r"Audio:\s*[^,]+,\s*\d+\s*Hz,\s*([^,\[(]+)")
# 常见布局名 → 声道数；TTS 产物实际只会落在 mono/stereo，其余只是兜底。
_LAYOUT_CHANNELS = {
    "mono": 1,
    "stereo": 2,
    "2.1": 3,
    "3.0": 3,
    "4.0": 4,
    "quad": 4,
    "5.0": 5,
    "5.1": 6,
    "6.0": 6,
    "6.1": 7,
    "7.0": 7,
    "7.1": 8,
}
# 实测比槽位长这一点点以内不加速：帧级误差不值得为它挂一个 atempo 滤镜。
_SLOT_TOLERANCE_S = 0.05
_AUDIO_BITRATE = "128k"


class PublishComposeError(RuntimeError):
    """合成失败：ffmpeg 缺失、超时或返回非零。路由按业务拒绝（``code=-1``）呈现。"""


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


async def _run_ffmpeg(args: Sequence[str], *, stage: str = "处理") -> tuple[int, str]:
    """跑一次 ffmpeg，返回 ``(返回码, stderr 文本)``；超时杀进程并抛错。"""
    started = time.monotonic()
    logger.info("ffmpeg %s invoke: %s", stage, " ".join(str(arg) for arg in args[:8]))
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PublishComposeError(f"ffmpeg 启动失败（{stage}）：{exc}") from exc

    communication = asyncio.create_task(process.communicate())
    try:
        _, stderr_bytes = await asyncio.wait_for(
            asyncio.shield(communication), timeout=settings.PUBLISH_COMPOSE_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
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
    logger.info(
        "ffmpeg %s finished: code=%s elapsed=%.1fs",
        stage,
        process.returncode,
        time.monotonic() - started,
    )
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


async def _concat_silent(exe: str, list_file: Path, out_path: Path) -> None:
    """按镜头清单拼出一条**无声**中间片：直拼优先，失败回退重编码。"""
    code, stderr = await _run_ffmpeg(
        _copy_args(exe, list_file, out_path), stage="片段直拼"
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
    code, stderr = await _run_ffmpeg(
        _reencode_args(exe, list_file, out_path), stage="片段重编码"
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
        [exe, "-nostdin", "-hide_banner", "-i", str(path)], stage="成片时长探测"
    )
    match = _DURATION_RE.search(stderr)
    if not match:
        raise PublishComposeError(
            f"ffmpeg 读不到文件时长（{path.name}），无法对齐配音时间轴"
        )
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_audio_channels(stderr: str) -> int:
    """从 ``ffmpeg -i`` 的 stderr 里认音频声道数；认不出来按单声道兜底。

    TTS 两路协议产出的都是单声道 mp3，兜底成 1 与现实一致；真碰到冷门布局
    （5.1 之类）只打 warning，不为它阻断整条发布作业。
    """
    match = _AUDIO_STREAM_RE.search(stderr)
    if not match:
        logger.warning("ffmpeg 输出里找不到音频流信息，按单声道处理配音延迟")
        return 1
    token = match.group(1).strip().lower()
    if token in _LAYOUT_CHANNELS:
        return _LAYOUT_CHANNELS[token]
    explicit = re.fullmatch(r"(\d+)\s*channels?", token)
    if explicit:
        return int(explicit.group(1))
    logger.warning("无法识别的音频声道布局 %r，按单声道处理配音延迟", token)
    return 1


async def probe_audio(exe: str, path: Path) -> tuple[float, int]:
    """一次 ``ffmpeg -i`` 同时读音频时长（秒）与声道数。

    声道数是给 ``adelay`` 逐声道列延迟用的：``adelay=D:all=1`` 只有
    FFmpeg ≥ 4.4 认识，部署机上的旧 ffmpeg 会整张滤镜图拒掉（报
    "Option 'all' not found"）；显式写成 ``adelay=D``（mono）或
    ``adelay=D|D``（stereo）则哪个版本都吃。
    """
    _, stderr = await _run_ffmpeg(
        [exe, "-nostdin", "-hide_banner", "-i", str(path)],
        stage=f"配音时长探测（{path.name}）",
    )
    match = _DURATION_RE.search(stderr)
    if not match:
        raise PublishComposeError(
            f"ffmpeg 读不到文件时长（{path.name}），无法对齐配音时间轴"
        )
    hours, minutes, seconds = match.groups()
    duration_s = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return duration_s, _parse_audio_channels(stderr)


async def build_voiceover(
    exe: str, plan: VoiceoverPlan, total_s: float, out_path: Path
) -> None:
    """把各句 mp3 按镜头时间轴混成一条与成片等长的 AAC（m4a 封装）。

    滤镜顺序有讲究：``atempo`` 必须在 ``adelay`` 之前——否则加速会把前面那段
    定位静音一起缩短，句子的起点就漂了。``adelay`` 按探针读到的声道数逐声道列
    延迟（``D|D``），不用 ``all=1``——那是 FFmpeg 4.4 才有的选项，旧 ffmpeg 会
    直接拒掉整张滤镜图。``amix`` 同样不用 4.3 才有的 ``normalize=0``：旧版 amix
    固定按输入数 N 把音量等比调小，混音后挂 ``volume=N`` 补回增益，单人配音各句
    互不重叠，不会削顶。最后 ``apad + atrim`` 把整条轨垫到、也截到与视频完全等长，
    封装阶段 ``-shortest`` 只是双保险。
    """
    if not plan.lines:
        raise PublishComposeError("配音计划为空：没有任何镜头带台词")

    inputs: list[str] = []
    filters: list[str] = []
    for index, line in enumerate(plan.lines):
        if not line.path.is_file():
            raise PublishComposeError(f"第 {line.sort_order} 镜的配音文件不存在：{line.path.name}")
        if line.slot_s <= 0:
            raise PublishComposeError(f"第 {line.sort_order} 镜的时长为 0，无法放置配音")
        inputs.extend(["-i", str(line.path)])

        duration_s, channels = await probe_audio(exe, line.path)
        chain = f"[{index}:a]"
        if duration_s - line.slot_s > _SLOT_TOLERANCE_S:
            # 先加速再定位：atempo 只作用在人声本体上，不吃掉前面的定位静音。
            factor = duration_s / line.slot_s
            if factor > settings.TTS_MAX_ATEMPO:
                raise PublishComposeError(
                    f"第 {line.sort_order} 镜台词太长：配音约 {duration_s:.1f} 秒，镜头槽位只有 "
                    f"{line.slot_s:.0f} 秒，加速到上限 {settings.TTS_MAX_ATEMPO:g}x 仍放不下，"
                    "请删减该镜台词或加长镜头后重新合成"
                )
            chain += f"atempo={factor:.4f},"
        delay_ms = max(0, round(line.start_s * 1000))
        # 逐声道列延迟（mono: adelay=D；stereo: adelay=D|D），不用 4.4 才有的 all=1。
        delay_args = "|".join([str(delay_ms)] * channels)
        chain += f"adelay={delay_args}[a{index}]"
        filters.append(chain)

    line_count = len(plan.lines)
    mix_inputs = "".join(f"[a{index}]" for index in range(line_count))
    # 不用 amix 的 normalize=0：那是 FFmpeg 4.3 才有的选项，旧 ffmpeg 会整张滤镜图
    # 拒掉（报 Option 'normalize' not found）。旧版 amix 固定把混合结果除以输入数 N，
    # 混音后挂 volume=N 把增益补回来——各句在时间轴上互不重叠，任一时刻只有一句在响，
    # 补完与 normalize=0 数学等价且不会削顶；volume 滤镜所有 ffmpeg 版本都自带。
    filters.append(
        f"{mix_inputs}amix=inputs={line_count}:duration=longest:"
        f"dropout_transition=0,volume={line_count}[mixed]"
    )
    filters.append(
        f"[mixed]apad,atrim=0:{total_s:.3f},aresample={settings.TTS_SAMPLE_RATE}[aout]"
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
    code, stderr = await _run_ffmpeg(args, stage="配音混音")
    if code != 0 or not _output_ok(out_path):
        detail = stderr.strip()[-_STERR_TAIL:] or f"退出码 {code}"
        raise PublishComposeError(f"ffmpeg 配音混音失败：{detail}")
    logger.info("ffmpeg voiceover built: lines=%s out=%s", len(plan.lines), out_path.name)


async def mux_voiceover(exe: str, video_path: Path, audio_path: Path, out_path: Path) -> None:
    """把配音轨封进无声中间片：视频与音频都只做流拷贝，不再重编码。"""
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
    code, stderr = await _run_ffmpeg(args, stage="音视频封装")
    if code != 0 or not _output_ok(out_path):
        detail = stderr.strip()[-_STERR_TAIL:] or f"退出码 {code}"
        raise PublishComposeError(f"ffmpeg 音视频封装失败：{detail}")
    logger.info("ffmpeg mux ok: video=%s audio=%s", video_path.name, audio_path.name)


async def compose(
    sources: Sequence[Path],
    out_path: Path,
    voiceover: VoiceoverPlan | None = None,
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
        await _concat_silent(exe, list_file, out_path)
        return

    # 配音链路先落无声中间片：视频流只编/拷一次，最后封装是纯 remux，失败也能
    # 从中间片分清是「拼接」还是「混音」哪一段出的错。
    silent_path = out_path.parent / "silent.mp4"
    await _concat_silent(exe, list_file, silent_path)
    total_s = await probe_duration(exe, silent_path)
    track_path = out_path.parent / "voiceover.m4a"
    await build_voiceover(exe, voiceover, total_s, track_path)
    await mux_voiceover(exe, silent_path, track_path, out_path)
