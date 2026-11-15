"""智能应用 · 短视频：项目存取、脚本触发与首帧/片段渲染入队。

数据访问按单个用户隔离（与 :class:`ResumeService`、:class:`SlideService` 同一
约定）：所有查询都经过 :meth:`VideoService._scope`，handler 不直接接触 session。

**两阶段、两套事实源**——这是这个功能唯一需要记住的架构，也是它和幻灯片最不像的地方：

* 脚本阶段（想法 → 创作卡 → 分镜）是几十秒量级的一次模型调用，走工作流引擎，
  进度在认领本项目的那条 ``workflow_run`` 上，界面上的 ``status`` / ``error_msg``
  由 :func:`subject_state` 从它翻译出来；
* 渲染阶段（镜头 → 首帧图）是**分钟级的外部异步任务**，走 ``media_job``，状态与
  产物路径都只在作业行上，由 :mod:`app.services.media.jobs` 的常驻循环推进。

于是 ``video_project`` 和 ``video_shot`` 两行都没有状态列：镜头行只留一个指向
最近一次作业的指针，封面连指针都不留（按 ``biz_type`` + ``biz_id`` 查最新那条
成功的作业）。想加一列 status 的冲动在这里一定是错的——那就有第三份事实源要同步，
而重启时它们必然给出三个答案。

渲染阶段有三个入队口：``render_frames``（首帧图）、``render_clips``（视频片段）、
``render_cover``（封面图）。片段吃的是首帧的产物，所以顺序上先出图再出片；重做片段时
可选把本镜上一版成片挂成参考视频（``reference_previous``，要 ``PUBLIC_MEDIA_BASE_URL``）。
``first_last_frame`` 那一档还没开——尾帧要另起一条产物链路，入队时当场拒绝。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import (
    MaterialAsset,
    MediaJob,
    ModelConfig,
    PublishJob,
    VideoProject,
    VideoShot,
    WorkflowRun,
)
from app.models.schemas import (
    MediaCapacityItem,
    MediaJobOut,
    PublishJobOut,
    VideoBrief,
    VideoParams,
    VideoProjectDetail,
    VideoProjectItem,
    VideoPublish,
    VideoScriptRunOut,
    VideoScriptStepOut,
    VideoShotOut,
    VideoStyle,
)
from app.services.agent_run import model_error_text, require_chat_model
from app.services.media import (
    BIZ_VIDEO_COVER,
    BIZ_VIDEO_SHOT,
    JOB_ACTIVE_STATES,
    JOB_FINAL_STATES,
    JOB_SUCCEEDED,
    KIND_IMAGE,
    KIND_VIDEO,
    MODE_FIRST_FRAME,
    MODE_FIRST_LAST_FRAME,
    MODE_REFERENCE,
    ROLE_FIRST_FRAME,
    ROLE_REFERENCE_IMAGE,
    ROLE_REFERENCE_VIDEO,
    VIDEO_GEN_MODES,
)
from app.services.media import jobs as media_jobs
from app.services.media import store
from app.services.materials import KIND_IMAGE as MATERIAL_IMAGE
from app.services.materials.service import MaterialService
from app.services.materials.store import store_material
from app.services.publish import (
    CHANNEL_MANUAL,
    PUBLISH_ACTIVE_STATES,
    PUBLISH_QUEUED,
)
from app.services.videos.normalize import shots_from_model, text_of
from app.services.workflows.builtin import finish_builtin_step, start_builtin
from app.services.workflows.defs import WorkflowService
from app.services.workflows.runs import (
    fail_run,
    latest_run,
    latest_runs,
    running_subjects,
    subject_state,
)
from app.services.workflows import STEP_SUCCEEDED, SUBJECT_UPLOADED, SUBJECT_VIDEO_PROJECT

logger = logging.getLogger("yangvis.videos")

settings = get_settings()

# 脚本那条内置流程，以及它两步的 key：产物分别落在这里的两个 JSON 列上。
WORKFLOW_SCRIPT = "video-script-generation"
STEP_BRIEF = "brief"
STEP_SCRIPT = "script"

# 镜头时长的落地区间，取自方舟 2.0 系的 duration 窗口。seedance 2.5 能到 30 秒，
# 按低的那一档约束总不会错：模型多给了就裁，用户在工作台里手动加宽就行。
SHOT_MIN_S = 4
SHOT_MAX_S = 15

# 平台只影响人设里的节奏与时长偏好，不写死任何平台规则；这里是给策划师的那句话。
PLATFORM_LABELS = {
    "douyin": "抖音（竖屏，前 3 秒定生死，节奏快）",
    "kuaishou": "快手（竖屏，老铁式直给，人物要亲切）",
    "other": "通用短视频（竖屏）",
}

# 「参考上一版」拼在运动提示词**最前面**：方舟的示例都显式引用素材编号，只挂素材不
# 引用，模型可能直接忽略它；放前面还能保证它不被 480 字上限截掉。
#
# 措辞刻意避开「修改/替换/去掉」与「延长/续写」：挂了参考视频后，2.0 系会按提示词意图
# 把任务判成参考生视频/视频编辑/视频延长三类之一，后两类要求 ratio=adaptive、
# duration=-1，与这里按镜传的 9:16 + 镜头秒数冲突（异步报错）。能显式锁定子任务的
# omni_reference_task_type 只有 2.5 支持，2.0 系传不得，所以只能靠措辞落在「参考」那类。
REFERENCE_PREVIOUS_HINT = "参考@视频1的主体形象、画面风格与运镜节奏，按以下描述重新生成一段视频："


def _clamp_prompt(text: str) -> str:
    """提示词按方舟的字数限制截断。

    在**写入时**截而不是发送时截：用户在工作台里看到的就该是实际喂给模型的那段，
    不然改了半天看不见变化，而模型收到的一直是前 480 字。
    """
    limit = settings.VIDEO_MAX_PROMPT_CHARS
    clean = (text or "").strip()
    return clean if len(clean) <= limit else clean[:limit]


def _shot_nums(orders: Sequence[int]) -> str:
    return "、".join(str(num) for num in orders)


def _ready_rel_path(job: Optional[MediaJob]) -> str:
    """产物真的可用时的 ``rel_path``，否则空串。

    认「succeeded 且有 rel_path」而不是只认状态：转存是作业的最后一步，一条报了成功
    却没落下文件的行喂给上游只会换来一次「参考图路径不合法」。
    """
    if job is None or job.status != JOB_SUCCEEDED or not job.rel_path:
        return ""
    return str(job.rel_path)


def _style_note(style: Dict[str, Any]) -> str:
    parts = [
        f"{label}：{value}"
        for label, value in (
            ("画面风格", (style or {}).get("visual_style")),
            ("人物设定", (style or {}).get("persona")),
            ("色调", (style or {}).get("palette")),
        )
        if str(value or "").strip()
    ]
    return f"全片风格（每一镜的提示词都要贴住它）：{'；'.join(parts)}\n\n" if parts else ""


def _script_inputs(project: VideoProject) -> Dict[str, str]:
    """把一行项目整理成那条流程声明的输入变量。

    「有风格才附风格」这类条件拼装留在代码里，模板只做替换（与幻灯片同一约定）；
    目标时长并进平台那一段，因为它们合起来就是「这条片子要发在哪、多长」。

    键名必须与流程声明的一字不差：多一个少一个都在 ``create_run`` 那里当场报错，
    这是有意的——拼错的变量名静默消失，喂给模型的就是一段缺料的分镜。
    """
    brief = project.brief or {}
    target = brief.get("duration_s") if isinstance(brief.get("duration_s"), int) else None
    duration_note = f"\n成片目标时长：{target} 秒" if target else ""
    return {
        "idea": project.idea or "",
        "platform_note": f"发布平台：{PLATFORM_LABELS.get(project.platform, project.platform)}"
        f"{duration_note}\n\n",
        "style_note": _style_note(project.style or {}),
        "requirement_note": (
            f"用户的补充要求：{project.requirement}\n\n" if project.requirement else ""
        ),
        "max_shots": str(settings.VIDEO_MAX_SHOTS),
    }


class VideoService:
    """针对单个用户的短视频项目做存取、脚本触发与首帧入队。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(VideoProject.owner_id == self._owner_id)

    def _materials(self) -> MaterialService:
        """素材库那层的入口：引用的校验、渲染时读哪条素材的什么类型，都在它那里。

        不复用同一个实例是故意的——``MaterialService`` 只是 ``(db, owner)`` 的一对绑定，
        构造没有成本，而共享实例会让「谁改了这批 session」变得说不清。
        """
        return MaterialService(self._db, self._owner_id)

    # -- 模型门槛 -------------------------------------------------------------

    def require_chat_config(self) -> ModelConfig:
        """脚本阶段的门槛：得先有一份验证过的默认对话模型（与简历、幻灯片同一约定）。"""
        return require_chat_model(self._db, self._owner_id)

    def require_image_config(self) -> ModelConfig:
        """渲染阶段的门槛：媒体配置要求**已经通过探测**，理由见 :func:`resolve_media_config`。"""
        return media_jobs.resolve_media_config(self._db, self._owner_id, KIND_IMAGE)

    def require_video_config(self) -> ModelConfig:
        """片段渲染的门槛：用途 ``video`` 的那一条，同样要求已通过探测。"""
        return media_jobs.resolve_media_config(self._db, self._owner_id, KIND_VIDEO)

    # -- 项目 -----------------------------------------------------------------

    def list_projects(self) -> List[VideoProject]:
        stmt = self._scope(select(VideoProject)).order_by(VideoProject.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_project(self, project_id: int) -> VideoProject:
        project = self._db.scalar(
            self._scope(select(VideoProject).where(VideoProject.id == project_id))
        )
        if project is None:
            raise LookupError("短视频项目不存在")
        return project

    def shots_of(self, project_id: int) -> List[VideoShot]:
        return list(
            self._db.scalars(
                select(VideoShot)
                .where(VideoShot.owner_id == self._owner_id, VideoShot.project_id == project_id)
                .order_by(VideoShot.sort_order, VideoShot.id)
            ).all()
        )

    def create_project(
        self,
        *,
        title: Optional[str],
        platform: str,
        idea: str,
        duration_s: Optional[int],
        requirement: Optional[str],
        style: Optional[Dict[str, Any]],
        params: Optional[Dict[str, Any]],
    ) -> VideoProject:
        """落一行项目并为它建一条脚本运行。

        ``title`` 留空时从想法里掐一段：列表页得说得出这是哪个片子，而用户在
        只写了一句话想法的时候并不想再起一个名字。
        """
        clean_idea = (idea or "").strip()
        if not clean_idea:
            raise ValueError("请先写下这条短视频的想法")

        project = VideoProject(
            owner_id=self._owner_id,
            title=(title or "").strip()[:128] or clean_idea[:24],
            platform=platform,
            idea=clean_idea,
            requirement=(requirement or "").strip()[:512] or None,
            brief=(
                VideoBrief(duration_s=duration_s).model_dump() if duration_s else {}
            ),
            script="",
            style=VideoStyle(**(style or {})).model_dump() if style else {},
            params=VideoParams(**(params or {})).model_dump(),
            publish={},
        )
        self._db.add(project)
        self._db.commit()
        self._db.refresh(project)
        self._start_script_run(project)
        logger.info(
            "video project created: id=%s owner=%s title=%r idea_chars=%s duration=%s",
            project.id,
            self._owner_id,
            project.title,
            len(clean_idea),
            duration_s,
        )
        return project

    def update_project(self, project_id: int, **fields: Any) -> VideoProject:
        """工作台的保存：``None`` 保持原样，给了的按 DTO 形状规范化后落库。"""
        project = self.get_project(project_id)

        if fields.get("title") is not None:
            clean = str(fields["title"]).strip()
            if not clean:
                raise ValueError("项目标题不能为空")
            project.title = clean[:128]
        if fields.get("platform") is not None:
            project.platform = str(fields["platform"])
        if fields.get("idea") is not None:
            clean_idea = str(fields["idea"]).strip()
            if not clean_idea:
                raise ValueError("想法不能为空")
            project.idea = clean_idea
        if fields.get("requirement") is not None:
            project.requirement = str(fields["requirement"]).strip()[:512] or None
        if fields.get("brief") is not None:
            project.brief = VideoBrief(**fields["brief"]).model_dump()
        if fields.get("script") is not None:
            project.script = str(fields["script"])
        if fields.get("style") is not None:
            project.style = VideoStyle(**fields["style"]).model_dump()
        if fields.get("params") is not None:
            project.params = VideoParams(**fields["params"]).model_dump()
        if fields.get("publish") is not None:
            project.publish = VideoPublish(**fields["publish"]).model_dump()

        self._db.commit()
        self._db.refresh(project)
        logger.info("video project saved: id=%s owner=%s", project_id, self._owner_id)
        return project

    def delete_project(self, project_id: int) -> int:
        """删项目与镜头，把项目 id 交回调用方去清整棵产物目录。

        两道闸门都是必须的：脚本在跑的时候删行，认领它的运行就指向一个不存在的
        对象；作业在跑的时候删，循环下一轮就为一个已经消失的镜头白烧一次额度。
        """
        project = self.get_project(project_id)
        if running_subjects(self._db, self._owner_id, SUBJECT_VIDEO_PROJECT, [project.id]):
            raise ValueError("脚本正在生成中，请稍后再删")

        # 作业按 (biz_type, biz_id) 寻址，biz_id 存的是字符串形态的业务 id；
        # 在这儿先把这个项目名下的 id 收齐再比，不去做「整型列 vs 字符串列」的隐式转换。
        biz_ids = [str(project.id)] + [str(row.id) for row in self.shots_of(project_id)]
        active = int(
            self._db.scalar(
                # 「还没结束」= 不是终态，含本地排队的 queued：那种作业一次上游都没发，
                # 删掉项目和镜头之后它照样会被循环捞起来渲染——为一个已经不存在的镜头
                # 烧掉一次额度，而且产物落进一棵已经没有主人的目录里。
                select(func.count(MediaJob.id)).where(
                    MediaJob.owner_id == self._owner_id,
                    MediaJob.biz_id.in_(biz_ids),
                    MediaJob.status.notin_(tuple(JOB_FINAL_STATES)),
                )
            )
            or 0
        )
        if active:
            raise ValueError(f"还有 {active} 个渲染没有结束，请先到工作台取消再删")

        # 合成是本机后台任务，跑到一半删项目会让 ffmpeg 往一棵已经没有主人的目录
        # 里写成片。终态作业不拦：产物本来就在项目目录里，跟着目录树一起清。
        publishing = int(
            self._db.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.owner_id == self._owner_id,
                    PublishJob.project_id == project_id,
                    PublishJob.status.in_(tuple(PUBLISH_ACTIVE_STATES)),
                )
            )
            or 0
        )
        if publishing:
            raise ValueError("成片还在合成中，等它结束再删项目")

        self._db.execute(
            delete(PublishJob).where(
                PublishJob.owner_id == self._owner_id,
                PublishJob.project_id == project_id,
            )
        )
        self._db.execute(
            delete(VideoShot).where(
                VideoShot.owner_id == self._owner_id, VideoShot.project_id == project_id
            )
        )
        self._db.delete(project)
        self._db.commit()
        logger.info(
            "video project deleted: id=%s owner=%s title=%r",
            project_id,
            self._owner_id,
            project.title,
        )
        return project_id

    # -- 脚本阶段 -------------------------------------------------------------

    def _start_script_run(self, project: VideoProject) -> WorkflowRun:
        return start_builtin(
            self._db,
            self._owner_id,
            WORKFLOW_SCRIPT,
            _script_inputs(project),
            subject=(SUBJECT_VIDEO_PROJECT, project.id),
            subject_label=project.title,
        )

    def start_script(
        self,
        project_id: int,
        *,
        requirement: Optional[str] = None,
        duration_s: Optional[int] = None,
    ) -> VideoProject:
        """重新发起一次脚本生成；给了 ``requirement`` 就顺手覆盖当时的补充要求。

        覆盖而不是追加：这条流程每次都是整包重跑，留下一个「上次要求还挂着」的
        状态会让用户看不出这次到底喂了什么。
        """
        project = self.get_project(project_id)
        if running_subjects(self._db, self._owner_id, SUBJECT_VIDEO_PROJECT, [project.id]):
            raise ValueError("该项目正在生成脚本，请勿重复触发")
        if requirement is not None:
            project.requirement = requirement.strip()[:512] or None
        if duration_s:
            merged = dict(project.brief or {})
            merged["duration_s"] = duration_s
            project.brief = merged
        self._db.commit()
        self._db.refresh(project)
        self._start_script_run(project)
        return project

    async def run_script(self, project_id: int) -> None:
        """后台任务体：跑发起时建好的那条运行，把两步产物落到项目与镜头行上。

        失败去处分两层：流程本身跑挂了原因已经在运行行上；镜头一行都没落下来这类
        **后处理**失败用 :func:`fail_run` 把已经成功的运行降级，否则界面上会出现
        一个「已完成」却没有分镜的项目。
        """
        started = time.monotonic()
        project = self.get_project(project_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_VIDEO_PROJECT, project.id)
        if run is None:
            logger.error("video project has no run to execute: id=%s", project_id)
            return
        logger.info(
            "video script generation started: id=%s run=%s owner=%s title=%r",
            project_id,
            run.id,
            self._owner_id,
            project.title,
        )
        try:
            step = await finish_builtin_step(
                self._db, self._owner_id, run.id, step_key=STEP_SCRIPT
            )
            script = step.parsed or {}
            shots = shots_from_model(script.get("shots"))
            if not shots:
                raise ValueError("模型没有产出任何合法镜头，请重新生成或换个说法")

            merged = dict(project.brief or {})
            merged.update(self._brief_of_run(run.id))
            project.brief = merged
            project.script = text_of(script.get("narration"), 20000)

            publish = dict(project.publish or {})
            title = text_of(script.get("title"), 128)
            publish["title_candidates"] = [title] if title else []
            publish["tags"] = [
                text_of(tag, 32) for tag in list(script.get("tags") or [])[:8] if tag
            ]
            publish["cover_prompt"] = text_of(script.get("cover_prompt"), 512)
            project.publish = publish

            # 镜头整包替换：脚本重跑出来的是另一套分镜，逐镜对齐既没有依据，
            # 也会让用户以为上一次的首帧还挂在新镜头上。
            self.save_shots(project_id, shots)
            logger.info(
                "video script generation finished: id=%s shots=%s elapsed=%.1fs",
                project_id,
                len(shots),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 失败写在运行上，不留到这里抛出
            logger.warning(
                "video script generation failed: id=%s elapsed=%.1fs error=%s",
                project_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

    def _brief_of_run(self, run_id: int) -> Dict[str, Any]:
        """读策划那一步的产物。

        只取这一层认识的那几个键（``VideoBrief`` 的字段）：模型多写的东西不进库，
        否则下一次保存时它会以「用户改过的字段」的身份回来。
        """
        row = next(
            (
                item
                for item in WorkflowService(self._db, self._owner_id).run_steps(run_id)
                if item.key == STEP_BRIEF and item.status == STEP_SUCCEEDED
            ),
            None,
        )
        if row is None or not isinstance(row.parsed, dict):
            return {}
        return VideoBrief(**{
            key: value
            for key, value in row.parsed.items()
            if key in VideoBrief.model_fields
        }).model_dump()

    # -- 镜头 -----------------------------------------------------------------

    def save_shots(self, project_id: int, shots: List[Dict[str, Any]]) -> VideoProject:
        """整批保存镜头：``id`` 命中就改，没有就建，不在列表里的删。

        ``sort_order`` 与 ``start_s`` 都由这里的数组顺序重算——时间轴上的位置是
        排队的结果，不是一个可以被前端独立维护的事实，两处各写一遍迟早对不上。
        """
        project = self.get_project(project_id)
        if len(shots) > settings.VIDEO_MAX_SHOTS:
            raise ValueError(f"一个项目最多 {settings.VIDEO_MAX_SHOTS} 个镜头")

        # 素材引用先在写入侧对一遍库：别人的 id、已经删掉的 id、模型编出来的 id 一律
        # 丢掉。留着它，报错要等到渲染那一刻才在「素材路径不合法」这种地方炸出来。
        known_ids = set(
            self._materials().materials_for(
                [
                    str(ref)
                    for item in shots
                    for ref in (item.get("material_ids") or [])
                ]
            )
        )
        existing = {row.id: row for row in self.shots_of(project_id)}
        wanted: List[VideoShot] = []

        for item in shots:
            shot_id = item.get("id")
            row = existing.get(int(shot_id)) if shot_id else None
            if shot_id and row is None:
                raise ValueError(f"镜头 {shot_id} 不属于这个项目")
            if row is None:
                row = VideoShot(project_id=project.id, owner_id=self._owner_id)
                self._db.add(row)
            row.duration_s = max(SHOT_MIN_S, min(SHOT_MAX_S, int(item.get("duration_s") or 5)))
            row.scene = text_of(item.get("scene"), 512) or None
            row.camera = text_of(item.get("camera"), 256) or None
            row.action = text_of(item.get("action"), 256) or None
            row.dialogue = text_of(item.get("dialogue"), 512) or None
            row.on_screen_text = text_of(item.get("on_screen_text"), 256) or None
            row.image_prompt = _clamp_prompt(str(item.get("image_prompt") or "")) or None
            row.video_prompt = _clamp_prompt(str(item.get("video_prompt") or "")) or None
            row.gen_mode = (
                item.get("gen_mode")
                if item.get("gen_mode") in VIDEO_GEN_MODES
                else MODE_FIRST_FRAME
            )
            row.material_ids = [
                str(ref) for ref in (item.get("material_ids") or []) if str(ref) in known_ids
            ]
            wanted.append(row)

        # flush 之后新建行才有 id，这时才谈得上「哪些还活着」。
        self._db.flush()
        cursor = 0
        for index, row in enumerate(wanted, start=1):
            row.sort_order = index
            row.start_s = cursor
            cursor += row.duration_s
        kept = {row.id for row in wanted}
        for stale_id, row in existing.items():
            if stale_id not in kept:
                self._db.delete(row)
        self._db.commit()
        logger.info(
            "video shots saved: project=%s owner=%s shots=%s duration=%ss",
            project_id,
            self._owner_id,
            len(wanted),
            cursor,
        )
        return project

    # -- 渲染阶段 -------------------------------------------------------------

    def render_frames(self, project_id: int, shot_ids: Sequence[int]) -> List[MediaJob]:
        """给这些镜头各入队一条首帧作业；``shot_ids`` 空表示全部镜头。

        整批里有一个镜头没写提示词就整条拒掉，而不是跳过它：用户在确认屏上看到
        「这次生成 N 张」，回来只有 N-1 张，那比一次拒绝难查得多。
        """
        project = self.get_project(project_id)
        config = self.require_image_config()
        rows = self.shots_of(project_id)
        wanted_ids = {int(item) for item in shot_ids or ()}
        picked = [row for row in rows if not wanted_ids or row.id in wanted_ids]
        if wanted_ids - {row.id for row in picked}:
            raise ValueError("选中了不属于该项目的镜头")
        if not picked:
            raise ValueError("还没有镜头可以渲染，先生成脚本")
        missing = [row.sort_order for row in picked if not (row.image_prompt or "").strip()]
        if missing:
            raise ValueError(
                f"第 {'、'.join(str(num) for num in missing)} 镜还没有首帧提示词，先补上再渲染"
            )

        params = project.params or {}
        materials = self._materials().materials_for(
            [str(ref) for row in picked for ref in (row.material_ids or [])]
        )
        created: List[MediaJob] = []
        for row in picked:
            # 只有图片类素材进得了请求体：参考视频要公网可达的地址，而默认那一档
            # 是把本地文件内联成 base64。视频素材在镜头上是一条「已记录、暂未使用」
            # 的引用（片段渲染的参考图档同一取舍）。
            refs = [
                materials[ref].rel_path
                for ref in [str(item) for item in (row.material_ids or [])]
                if ref in materials
                and materials[ref].kind == MATERIAL_IMAGE
                and materials[ref].rel_path
            ]
            job = media_jobs.enqueue(
                self._db,
                self._owner_id,
                kind=KIND_IMAGE,
                biz_type=BIZ_VIDEO_SHOT,
                biz_id=row.id,
                request={
                    "project_id": project.id,
                    "prompt": row.image_prompt,
                    "size": str(params.get("image_size") or "2K"),
                    "images": refs,
                    "watermark": bool(params.get("watermark")),
                },
                config=config,
            )
            row.image_job_id = job.id
            created.append(job)
        self._db.commit()
        logger.info(
            "video frames enqueued: project=%s owner=%s jobs=%s materials=%s",
            project.id,
            self._owner_id,
            len(created),
            len(materials),
        )
        return created

    def render_clips(
        self,
        project_id: int,
        shot_ids: Sequence[int],
        reference_previous: bool = False,
    ) -> List[MediaJob]:
        """给这些镜头各入队一条视频片段作业；``shot_ids`` 空表示全部镜头。

        与 :meth:`render_frames` 同一口径：整批里有一镜不合格就整条拒掉，不跳过它。
        四档不合格一次报全（缺运动提示词、首帧还没渲出来、选了没开的首尾帧、参考图
        档没挑图片素材），免得用户补一条、提交、再撞下一条；开了「参考上一版」再加
        一档：本镜上一版片段还没落盘。

        首帧档喂的是**这一镜已经渲好的首帧产物**，不再重新出图：状态与路径的唯一
        事实源是作业行，所以这里认的是指针指向那条作业真的落盘了没有。

        ``reference_previous`` 是重做时的「参考上一版」：把本镜 ``video_job_id`` 指着
        的那份成片再挂成 ``reference_video``，让新片沿用上一版的形象与运镜，而不是
        重掷一次骰子。默认关——开着就再也拿不到「完全重来一版」了。

        这一档**不再发首帧**，因此也不要求首帧已出图：方舟禁止首帧/尾帧与参考素材
        混用，而上一版成片已经承载了画面。任务类型随之变成纯「参考生视频」。
        """
        project = self.get_project(project_id)
        config = self.require_video_config()
        if reference_previous and not (settings.PUBLIC_MEDIA_BASE_URL or "").strip():
            raise ValueError(
                "「参考上一版」需要公网地址：方舟的视频参考只收公网 URL，"
                "请先在服务端配置 PUBLIC_MEDIA_BASE_URL（本站对外 origin）"
            )
        rows = self.shots_of(project_id)
        wanted_ids = {int(item) for item in shot_ids or ()}
        picked = [row for row in rows if not wanted_ids or row.id in wanted_ids]
        if wanted_ids - {row.id for row in picked}:
            raise ValueError("选中了不属于该项目的镜头")
        if not picked:
            raise ValueError("还没有镜头可以渲染，先生成脚本")

        frames = self._latest_jobs([row.image_job_id for row in picked])
        clips = (
            self._latest_jobs([row.video_job_id for row in picked])
            if reference_previous
            else {}
        )
        materials = self._materials().materials_for(
            [str(ref) for row in picked for ref in (row.material_ids or [])]
        )
        # 素材库里只有图片类进得了请求体：视频素材那一档还没接（它同样要公网地址，
        # 与下面「参考上一版」走的是同一条路），镜头上只留一条已记录、暂未使用的引用。
        refs = {
            row.id: [
                materials[ref].rel_path
                for ref in [str(item) for item in (row.material_ids or [])]
                if ref in materials
                and materials[ref].kind == MATERIAL_IMAGE
                and materials[ref].rel_path
            ]
            for row in picked
        }

        problems: List[str] = []
        unsupported = [row.sort_order for row in picked if row.gen_mode == MODE_FIRST_LAST_FRAME]
        if unsupported:
            problems.append(f"第 {_shot_nums(unsupported)} 镜选了首尾帧生视频，这一档还没开")
        no_prompt = [row.sort_order for row in picked if not (row.video_prompt or "").strip()]
        if no_prompt:
            problems.append(f"第 {_shot_nums(no_prompt)} 镜还没有运动提示词")
        no_frame = [
            row.sort_order
            for row in picked
            if row.gen_mode == MODE_FIRST_FRAME
            and not reference_previous
            and not _ready_rel_path(frames.get(row.image_job_id))
        ]
        if no_frame:
            problems.append(f"第 {_shot_nums(no_frame)} 镜的首帧还没渲出来")
        no_refs = [
            row.sort_order for row in picked if row.gen_mode == MODE_REFERENCE and not refs[row.id]
        ]
        if no_refs:
            problems.append(f"第 {_shot_nums(no_refs)} 镜选了参考图生视频，但没挑图片素材")
        if reference_previous:
            no_clip = [
                row.sort_order
                for row in picked
                if not _ready_rel_path(clips.get(row.video_job_id))
            ]
            if no_clip:
                problems.append(
                    f"第 {_shot_nums(no_clip)} 镜还没有可参考的上一版片段，先关掉「参考上一版」或把它们渲出来"
                )
        if problems:
            raise ValueError("；".join(problems) + "。先补齐再渲染")

        params = project.params or {}
        created: List[MediaJob] = []
        for row in picked:
            images: List[Dict[str, str]] = []
            # 挂了参考视频就**不能再给首帧**：方舟直接 400（InvalidParameter，原话
            # "first/last frame content cannot be mixed with reference media content"）。
            # 上一版成片本身承载了画面，首帧在这一档是冗余的，所以丢掉它而不是拒绝整批。
            # 参考图档不受影响：图片 + 视频的组合参考是允许的。
            if row.gen_mode == MODE_FIRST_FRAME and not reference_previous:
                images = [
                    {
                        "role": ROLE_FIRST_FRAME,
                        "rel_path": _ready_rel_path(frames.get(row.image_job_id)),
                    }
                ]
            elif row.gen_mode == MODE_REFERENCE:
                images = [{"role": ROLE_REFERENCE_IMAGE, "rel_path": path} for path in refs[row.id]]
            # 剩下的只有 t2v：一张图都不给，给了就把纯文生变成了图生视频。
            prompt = row.video_prompt
            if reference_previous:
                images.append(
                    {
                        "role": ROLE_REFERENCE_VIDEO,
                        "rel_path": _ready_rel_path(clips.get(row.video_job_id)),
                    }
                )
                prompt = _clamp_prompt(f"{REFERENCE_PREVIOUS_HINT}{prompt}")
            job = media_jobs.enqueue(
                self._db,
                self._owner_id,
                kind=KIND_VIDEO,
                biz_type=BIZ_VIDEO_SHOT,
                biz_id=row.id,
                request={
                    "project_id": project.id,
                    "prompt": prompt,
                    # 键名与方舟请求体同名，作业层原样透传（``jobs._video_params``
                    # 不兜默认值）。时长是**本镜**的，不是全片的：一镜一条任务。
                    "params": {
                        "ratio": str(params.get("ratio") or "9:16"),
                        "resolution": str(params.get("resolution") or "720p"),
                        "duration": int(row.duration_s),
                        "watermark": bool(params.get("watermark")),
                        "generate_audio": bool(params.get("generate_audio")),
                    },
                    "images": images,
                },
                config=config,
            )
            row.video_job_id = job.id
            created.append(job)
        self._db.commit()
        logger.info(
            "video clips enqueued: project=%s owner=%s jobs=%s materials=%s reference_previous=%s",
            project.id,
            self._owner_id,
            len(created),
            len(materials),
            reference_previous,
        )
        return created

    def render_cover(self, project_id: int) -> MediaJob:
        """单独出一条封面的图片作业。

        封面无镜头行可挂，靠 ``biz_type=video-project-cover`` + 项目 id 寻址；
        界面上要封面时查最新那条成功的，不在项目行上抄一份路径。
        """
        project = self.get_project(project_id)
        config = self.require_image_config()
        prompt = _clamp_prompt(
            str((project.publish or {}).get("cover_prompt") or "")
            or str((project.brief or {}).get("hook") or "")
        )
        if not prompt:
            raise ValueError("还没有封面描述，先生成脚本或在工作台里补一句")
        params = project.params or {}
        return media_jobs.enqueue(
            self._db,
            self._owner_id,
            kind=KIND_IMAGE,
            biz_type=BIZ_VIDEO_COVER,
            biz_id=project.id,
            request={
                "project_id": project.id,
                "prompt": prompt,
                "size": str(params.get("image_size") or "2K"),
                "images": [],
                "watermark": False,
            },
            config=config,
        )

    async def cancel_job(self, job_id: int) -> MediaJob:
        return await media_jobs.cancel(self._db, self._owner_id, job_id)

    def save_frame_to_material(self, project_id: int, shot_id: int) -> MaterialAsset:
        """把这一镜最近一次成功作业的产物**复制**一条进素材库。

        复制而不是移动，两处都是理由：产物路径记在 ``media_job.rel_path`` 上，移动会
        让这一镜的预览当场断掉；而目录归属本来就不同——产物按项目分（删项目 = 删一棵
        目录树），素材按账号分（跨项目复用，删项目不带走人物形象图）。

        名字由服务端按「项目 + 镜头号」生成，不叫调用方传：这一档动作九成是「这张后面
        还要用」，多一个填名框只会把它变成一次要斟酌的输入。镜头号取自镜头行，改名、
        重排之后存的是当时那一镜的号。
        """
        project = self.get_project(project_id)
        row = next((item for item in self.shots_of(project_id) if item.id == shot_id), None)
        if row is None:
            raise ValueError("这一镜不属于该项目")
        job = (
            self._latest_jobs([row.image_job_id]).get(row.image_job_id)
            if row.image_job_id
            else None
        )
        if job is None or job.status != JOB_SUCCEEDED or not job.rel_path:
            raise ValueError("这一镜还没有产物，先渲完再存")

        # 读产物、落素材都是同步 IO，由线程池里的那个 def 端点调用。
        stored = store_material(self._owner_id, store.read_blob(str(job.rel_path)))
        try:
            asset = self._materials().add(stored, f"{project.title}｜第 {row.sort_order} 镜首帧")
        except ValueError:
            # 条数闸门拦下时文件已经在盘上了，留着就是没有任何一行数据库指向它的孤儿。
            store.delete_blob(stored.rel_path)
            raise
        logger.info(
            "video frame copied to materials: owner=%s project=%s shot=%s job=%s asset=%s bytes=%d",
            self._owner_id,
            project.id,
            row.id,
            job.id,
            asset.id,
            asset.size,
        )
        return asset

    # -- 发布（成片合成 + 手动发布包） -----------------------------------------

    def compose_publish(self, project_id: int) -> PublishJob:
        """入队一条成片合成作业：把各镜最近一次成功片段按镜头顺序拼成一个 MP4。

        与渲染入队同一口径——整批里有一镜没有可用片段就整条拒掉，并一次报全缺的
        是哪几镜，而不是拼到一半才发现少一段。片段文件还要在盘上：作业行报成功但
        文件被手工清掉时，发 ffmpeg 只会换来看不懂的 No such file。
        """
        project = self.get_project(project_id)
        rows = self.shots_of(project_id)
        if not rows:
            raise ValueError("还没有镜头可以合成，先生成脚本")

        active = int(
            self._db.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.owner_id == self._owner_id,
                    PublishJob.project_id == project_id,
                    PublishJob.status.in_(tuple(PUBLISH_ACTIVE_STATES)),
                )
            )
            or 0
        )
        if active:
            raise ValueError("这个项目已有成片合成在进行，等它结束再发起")

        clip_jobs = self._latest_jobs([row.video_job_id for row in rows])
        missing: List[int] = []
        sources: List[str] = []
        for row in rows:
            rel_path = _ready_rel_path(clip_jobs.get(row.video_job_id))
            if not rel_path or not (settings.upload_media_path / rel_path).is_file():
                missing.append(row.sort_order)
            else:
                sources.append(rel_path)
        if missing:
            raise ValueError(
                f"第 {_shot_nums(missing)} 镜还没有可用的视频片段，先把片段渲完再合成"
            )

        cover = self._cover_jobs([project_id]).get(str(project_id))
        publish = project.publish or {}
        candidates = list(publish.get("title_candidates") or [])
        job = PublishJob(
            owner_id=self._owner_id,
            project_id=project_id,
            platform=project.platform,
            channel=CHANNEL_MANUAL,
            status=PUBLISH_QUEUED,
            # 快照而不是指针：之后重渲片段、改文案都不影响这次合成要拼什么、
            # 发布包里带哪版标题——与 media_job.request 同一个理由。
            request={
                "sources": sources,
                "title": str(candidates[0] if candidates else project.title),
                "tags": [str(tag) for tag in (publish.get("tags") or [])][:8],
                "cover": cover.rel_path if cover else "",
            },
        )
        self._db.add(job)
        self._db.commit()
        self._db.refresh(job)
        logger.info(
            "publish job enqueued: id=%s owner=%s project=%s clips=%s",
            job.id,
            self._owner_id,
            project_id,
            len(sources),
        )
        return job

    def latest_publish_job(self, project_id: int) -> Optional[PublishJob]:
        """项目最近一次发布作业（任何状态）；详情接口只带这一条，历史都在表里。"""
        return self._db.scalar(
            select(PublishJob)
            .where(
                PublishJob.owner_id == self._owner_id,
                PublishJob.project_id == project_id,
            )
            .order_by(PublishJob.id.desc())
            .limit(1)
        )

    def capacity(self) -> List[MediaCapacityItem]:
        """这个账号现在各类作业占着几个位子、还能塞几个——渲染前那屏确认要用它。"""
        occupied = {
            str(kind): int(count)
            for kind, count in self._db.execute(
                select(MediaJob.kind, func.count(MediaJob.id))
                .where(
                    MediaJob.owner_id == self._owner_id,
                    MediaJob.status.in_(tuple(JOB_ACTIVE_STATES)),
                )
                .group_by(MediaJob.kind)
            ).all()
        }
        limits = {
            KIND_IMAGE: settings.IMAGE_MAX_INFLIGHT,
            KIND_VIDEO: settings.VIDEO_MAX_INFLIGHT,
        }
        return [
            MediaCapacityItem(
                kind=kind, inflight=occupied.get(kind, 0), limit=limits.get(kind, 0)
            )
            for kind in (KIND_IMAGE, KIND_VIDEO)
        ]

    # -- 序列化 ---------------------------------------------------------------

    def _latest_jobs(self, job_ids: Sequence[Optional[int]]) -> Dict[int, MediaJob]:
        return media_jobs.jobs_by_ids(
            self._db, self._owner_id, [int(item) for item in job_ids if item]
        )

    def _cover_jobs(self, project_ids: Sequence[int]) -> Dict[str, MediaJob]:
        """每个项目的最新一条成功封面作业。

        按 id 正序扫一遍取最后一条，而不是在 SQL 里 ``max(id)`` 再回表：一次列表
        也就几十条作业，多写一遍子查询换不到什么，而这里的形状要跟
        :func:`latest_runs` 一样只查一次。
        """
        if not project_ids:
            return {}
        rows = self._db.scalars(
            select(MediaJob)
            .where(
                MediaJob.owner_id == self._owner_id,
                MediaJob.biz_type == BIZ_VIDEO_COVER,
                MediaJob.biz_id.in_([str(item) for item in project_ids]),
                MediaJob.status == JOB_SUCCEEDED,
                MediaJob.rel_path.is_not(None),
            )
            .order_by(MediaJob.id)
        ).all()
        return {str(job.biz_id): job for job in rows}

    def _shot_stats(self, project_ids: Sequence[int]) -> Dict[int, tuple[int, int]]:
        """一次查询取回每个项目的镜头数与总时长，列表页不是每行查两次。"""
        if not project_ids:
            return {}
        rows = self._db.execute(
            select(VideoShot.project_id, func.count(VideoShot.id), func.sum(VideoShot.duration_s))
            .where(VideoShot.owner_id == self._owner_id, VideoShot.project_id.in_(project_ids))
            .group_by(VideoShot.project_id)
        ).all()
        return {int(pid): (int(count), int(total or 0)) for pid, count, total in rows}

    def _item_of(
        self,
        project: VideoProject,
        run: Optional[WorkflowRun],
        stats: tuple[int, int],
        cover: Optional[MediaJob],
    ) -> VideoProjectItem:
        # idle 用 uploaded：项目一创建就带着一条脚本运行，走到「没有运行又没有镜头」
        # 的只剩运行记录被清掉那一种存量行。
        status, error_msg = subject_state(
            run, has_product=bool(stats[0]), idle=SUBJECT_UPLOADED
        )
        return VideoProjectItem(
            id=project.id,
            title=project.title,
            platform=project.platform,
            status=status,
            error_msg=error_msg,
            shot_count=stats[0],
            duration_s=stats[1],
            cover_url=store.resolve_url(cover.rel_path or "") if cover else "",
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    def to_item(self, project: VideoProject) -> VideoProjectItem:
        return self._item_of(
            project,
            latest_run(self._db, self._owner_id, SUBJECT_VIDEO_PROJECT, project.id),
            self._shot_stats([project.id]).get(project.id, (0, 0)),
            self._cover_jobs([project.id]).get(str(project.id)),
        )

    def to_items(self, projects: List[VideoProject]) -> List[VideoProjectItem]:
        ids = [project.id for project in projects]
        runs = latest_runs(self._db, self._owner_id, SUBJECT_VIDEO_PROJECT, ids)
        stats = self._shot_stats(ids)
        covers = self._cover_jobs(ids)
        return [
            self._item_of(
                project,
                runs.get(str(project.id)),
                stats.get(project.id, (0, 0)),
                covers.get(str(project.id)),
            )
            for project in projects
        ]

    def to_detail(self, project: VideoProject) -> VideoProjectDetail:
        rows = self.shots_of(project.id)
        jobs = self._latest_jobs(
            [row.image_job_id for row in rows] + [row.video_job_id for row in rows]
        )
        cover = self._cover_jobs([project.id]).get(str(project.id))
        run = latest_run(self._db, self._owner_id, SUBJECT_VIDEO_PROJECT, project.id)
        base = self._item_of(
            project,
            run,
            (len(rows), sum(row.duration_s for row in rows)),
            cover,
        )
        stored = {
            key: value
            for key, value in (project.brief or {}).items()
            if key in VideoBrief.model_fields
        }
        style = {
            key: value
            for key, value in (project.style or {}).items()
            if key in VideoStyle.model_fields
        }
        params = {
            key: value
            for key, value in (project.params or {}).items()
            if key in VideoParams.model_fields
        }
        publish = {
            key: value
            for key, value in (project.publish or {}).items()
            if key in VideoPublish.model_fields
        }
        return VideoProjectDetail(
            **base.model_dump(),
            idea=project.idea or "",
            requirement=project.requirement or "",
            brief=VideoBrief(**stored),
            script=project.script or "",
            style=VideoStyle(**style),
            params=VideoParams(**params),
            publish=VideoPublish(**publish),
            shots=[
                VideoShotOut(
                    id=row.id,
                    sort_order=row.sort_order,
                    start_s=row.start_s,
                    duration_s=row.duration_s,
                    scene=row.scene or "",
                    camera=row.camera or "",
                    action=row.action or "",
                    dialogue=row.dialogue or "",
                    on_screen_text=row.on_screen_text or "",
                    image_prompt=row.image_prompt or "",
                    video_prompt=row.video_prompt or "",
                    gen_mode=row.gen_mode,
                    material_ids=[str(item) for item in (row.material_ids or [])],
                    image_job=_job_out(jobs.get(row.image_job_id)) if row.image_job_id else None,
                    video_job=_job_out(jobs.get(row.video_job_id)) if row.video_job_id else None,
                )
                for row in rows
            ],
            script_run=self._script_run_out(run),
            publish_job=_publish_out(self.latest_publish_job(project.id)),
        )

    def _script_run_out(self, run: Optional[WorkflowRun]) -> Optional[VideoScriptRunOut]:
        """把脚本运行翻译成工作台要的两步现场：谁在跑、各自跑了多久。

        网关日志一次调用一行，这条流程有两行；只报其中一行会让人以为整个生成
        就是那几十秒。整条的墙上时间取运行行上的 ``elapsed_ms``，不是各步相加。
        """
        if run is None:
            return None
        rows = WorkflowService(self._db, self._owner_id).run_steps(run.id)
        return VideoScriptRunOut(
            id=run.id,
            status=run.status,
            elapsed_ms=run.elapsed_ms,
            started_at=run.started_at,
            steps=[
                VideoScriptStepOut(
                    key=row.key,
                    status=row.status,
                    elapsed_ms=row.elapsed_ms,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
                for row in rows
            ],
        )


def _job_out(job: Optional[MediaJob]) -> Optional[MediaJobOut]:
    """作业行的对外视图：产物只给本站地址，上游那条 24 小时链接不出门。"""
    if job is None:
        return None
    return MediaJobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        model_name=job.model_name,
        url=store.resolve_url(job.rel_path or "") if job.rel_path else "",
        mime=job.mime,
        file_size=job.file_size,
        error_msg=job.error_msg,
        elapsed_ms=job.elapsed_ms,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _publish_out(job: Optional[PublishJob]) -> Optional[PublishJobOut]:
    """发布作业的对外视图：成片地址同样只给站内的，rel_path 不出门。"""
    if job is None:
        return None
    return PublishJobOut(
        id=job.id,
        platform=job.platform,
        channel=job.channel,
        status=job.status,
        url=store.resolve_url(job.rel_path or "") if job.rel_path else "",
        file_size=job.file_size,
        error_msg=job.error_msg,
        elapsed_ms=job.elapsed_ms,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )
