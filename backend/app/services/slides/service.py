"""智能办公 · 幻灯片的存取与 AI 生成。

数据访问按单个用户隔离（与 :class:`ResumeService` 同一约定）：所有查询都经过
:meth:`SlideService._scope`，handler 不直接接触 session。

生成是后台任务：一次要读完全部材料再产出十几页结构，远超 SPA 的 30s axios
超时，所以接口只建一条认领这份 deck 的运行，真正的模型调用跑在
``BackgroundTasks`` 里，前端靠轮询状态感知进度（与简历分析同一形态）。

进度与失败原因只有 ``workflow_run`` 一份事实源：界面上的 ``status`` /
``error_msg`` 由 :func:`subject_state` 从最近一次运行翻译出来，deck 这一行只存
产物（slides / theme / html）。

事实源是 ``slides`` 这一列结构化页面；``html`` 是从它确定性渲染出来的派生物
（见 :mod:`app.services.slides.html`），每次写入和每次读取都会重渲一遍。用户改
内容只改 ``slides``，预览 / 放映 / 导出自然同步。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import ModelConfig, SlideDeck, WorkflowRun
from app.models.schemas import (
    SlideAsset,
    SlideDeckDetail,
    SlideDeckItem,
    SlideTheme,
)
from app.services.agent_run import (
    json_span,
    labeled_section,
    model_error_text,
    require_chat_model,
    strip_json_fence,
)
from app.services.slides.html import (
    MAX_DECK_PAGES,
    render_document,
    sanitize_pages,
    sanitize_theme,
)
from app.services.slides.store import StoredImage
from app.services.workflows.builtin import builtin_output, start_builtin
from app.services.workflows.runs import (
    fail_run,
    latest_run,
    latest_runs,
    running_subjects,
    subject_state,
)
from app.services.workflows import SUBJECT_ERROR, SUBJECT_SLIDE_DECK

logger = logging.getLogger("yangvis.slides")

settings = get_settings()

# 生成这一步的人设（slide-architect）与 Prompt 骨架都在内置工作流上，界面上能改；
# 这里只留「哪条流程」。
WORKFLOW_GENERATE = "slide-generation"

# 默认外观，deck 还没生成时也用这套。
DEFAULT_THEME: Dict[str, Any] = {"preset": "teal", "ratio": "16x9", "accent": None}


def _truncate(text: str) -> str:
    limit = settings.SLIDE_MAX_SOURCE_CHARS
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n……（材料过长，已截断）"


def _json_candidate(text: str) -> Optional[str]:
    """从模型输出里截出可能是 JSON 的那一段。

    模型并不总是守规矩：裹 ```json 栅栏、前后带寒暄都是常态。看**最先出现**的
    那个开括号——我们的契约是 ``{"theme":…, "slides":…}``，但只回一个页面数组
    的输出同样能用；先 ``{`` 就按对象截，先 ``[`` 就按数组截。反过来做会出事：
    数组形式 ``[{...}]`` 里也有花括号，按「对象优先」截会掐出第一个元素，
    解析出来是个没有 slides 的字典，整份结构白丢。
    """
    stripped = strip_json_fence(text)

    pairs = (("{", "}"), ("[", "]"))
    starts = {open_brace: stripped.find(open_brace) for open_brace, _ in pairs}
    object_first = starts["{"] != -1 and (starts["["] == -1 or starts["{"] < starts["["])
    first, second = pairs if object_first else (pairs[1], pairs[0])
    return json_span(stripped, *first) or json_span(stripped, *second)


def _parse_deck_output(raw_text: str) -> tuple[Dict[str, Any], List[Any]]:
    """把模型输出解析成 ``(theme, pages)``。解析不出来抛 ValueError 让调用方落状态。"""
    candidate = _json_candidate(raw_text)
    if candidate is None:
        raise ValueError("模型没有返回可解析的幻灯片结构")
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型返回的 JSON 无法解析：{exc}") from exc

    if isinstance(data, list):
        return {}, data
    if isinstance(data, dict):
        pages = data.get("slides") or data.get("pages") or []
        return dict(data.get("theme") or {}), list(pages)
    raise ValueError("模型返回的结构既不是对象也不是数组")


def _generate_inputs(
    *,
    title: str,
    description: Optional[str],
    requirement: Optional[str],
    source_text: str,
    asset_names: List[str],
) -> Dict[str, str]:
    """把一份 deck 整理成那条工作流声明的输入变量。

    「有材料才附材料、有配图才附配图」这类条件拼装留在代码里，模板只做替换；
    每一段自带结尾空行，拼起来就是原来那段 human 消息。
    """
    listing = "\n".join(f"- {name}" for name in asset_names)
    return {
        "title": title,
        "description_note": f"补充说明：{description}\n\n" if description else "",
        "requirement_note": (
            f"用户对这份幻灯片的要求：{requirement}\n\n" if requirement else ""
        ),
        "source_block": (
            f"{labeled_section('材料全文', _truncate(source_text))}\n\n" if source_text else ""
        ),
        # 模型看不到图片内容，只拿到清单；image 版式必须引用清单里的 id。
        "asset_block": (
            f"可用配图（id: 文件名，只有这些 id 能被 asset_id 引用）：\n{listing}\n\n"
            if asset_names
            else ""
        ),
        "max_pages": str(MAX_DECK_PAGES),
    }


class SlideService:
    """针对单个用户的幻灯片做增删改查、AI 生成与 HTML 渲染。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(SlideDeck.owner_id == self._owner_id)

    # -- 模型门槛（与简历同一份共用实现）---------------------------------------

    def require_chat_config(self) -> ModelConfig:
        """生成门槛：用户得先有一份验证过的默认对话模型（与简历同一约定）。"""
        return require_chat_model(self._db, self._owner_id)

    # -- CRUD ---------------------------------------------------------------

    def list_decks(self) -> List[SlideDeck]:
        stmt = self._scope(select(SlideDeck)).order_by(SlideDeck.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_deck(self, deck_id: int) -> SlideDeck:
        deck = self._db.scalar(self._scope(select(SlideDeck).where(SlideDeck.id == deck_id)))
        if deck is None:
            raise LookupError("幻灯片不存在")
        return deck

    def create_deck(
        self,
        *,
        title: str,
        description: Optional[str],
        requirement: Optional[str],
        source_text: str,
    ) -> SlideDeck:
        """落一条记录并为它建一条运行。材料与图片无关（图片是后加的素材）。

        ``source_text`` 是路由侧把上传文档解析出的文本与用户粘贴的文案合在一起的
        结果；三者全空时拒掉——没有任何材料，模型只能瞎编。
        """
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("请填写幻灯片主题")
        if not source_text.strip() and not (description or "").strip():
            raise ValueError("请至少提供一份材料：上传文档、粘贴文案，或填写内容说明")

        deck = SlideDeck(
            owner_id=self._owner_id,
            title=clean_title[:128],
            description=(description or "").strip()[:512] or None,
            requirement=(requirement or "").strip()[:512] or None,
            source_text=source_text,
            assets=[],
            theme=dict(DEFAULT_THEME),
            slides=[],
        )
        self._db.add(deck)
        self._db.commit()
        self._db.refresh(deck)
        self._refresh_html(deck)
        self._db.commit()
        self._start_generation_run(deck)
        logger.info(
            "slide deck created: id=%s owner=%s title=%r source_chars=%s",
            deck.id,
            self._owner_id,
            deck.title,
            len(source_text),
        )
        return deck

    def delete_deck(self, deck_id: int) -> List[str]:
        """删记录，并把图片文件的相对路径交回调用方去清理磁盘。

        磁盘删除放在路由里做（服务不该知道文件系统），这里只保证返回的路径都
        来自这一行自己的 assets。
        """
        deck = self.get_deck(deck_id)
        if running_subjects(self._db, self._owner_id, SUBJECT_SLIDE_DECK, [deck.id]):
            raise ValueError("幻灯片正在生成中，请稍后再删")
        rel_paths = [
            str(asset.get("rel_path") or "")
            for asset in (deck.assets or [])
            if isinstance(asset, dict)
        ]
        self._db.delete(deck)
        self._db.commit()
        logger.info("slide deck deleted: id=%s owner=%s title=%r", deck_id, self._owner_id, deck.title)
        return [path for path in rel_paths if path]

    # -- 配图素材 -----------------------------------------------------------

    def add_asset(self, deck_id: int, stored: StoredImage) -> SlideDeck:
        """把刚落盘的一张图片登记进 deck。超量时拒绝，文件由调用方删。"""
        deck = self.get_deck(deck_id)
        if len(deck.assets or []) >= settings.SLIDE_MAX_IMAGES_PER_DECK:
            raise ValueError(f"一份幻灯片最多 {settings.SLIDE_MAX_IMAGES_PER_DECK} 张配图")
        assets = list(deck.assets or [])
        assets.append(
            {
                "id": stored.id,
                "name": stored.name,
                "mime": stored.mime,
                "size": stored.size,
                "rel_path": stored.rel_path,
                "url": stored.url,
            }
        )
        deck.assets = assets
        # 新素材会改变「哪些 asset_id 有效」，重渲染一次让预览里的图能出现。
        self._refresh_html(deck)
        self._db.commit()
        return deck

    def remove_asset(self, deck_id: int, asset_id: str) -> tuple[SlideDeck, str]:
        """删一张配图，并解掉所有引用它的页面（image 版式由渲染器降级成 bullets）。

        返回 ``(deck, rel_path)``，rel_path 为空表示没有文件要清。
        """
        deck = self.get_deck(deck_id)
        assets = list(deck.assets or [])
        target = next((item for item in assets if isinstance(item, dict) and str(item.get("id")) == asset_id), None)
        if target is None:
            raise LookupError("配图不存在")
        deck.assets = [item for item in assets if item is not target]
        deck.slides = [
            {**page, "asset_id": ""} if page.get("asset_id") == asset_id else page
            for page in (deck.slides or [])
            if isinstance(page, dict)
        ]
        self._refresh_html(deck)
        self._db.commit()
        return deck, str(target.get("rel_path") or "")

    # -- AI 生成 ------------------------------------------------------------

    def _start_generation_run(self, deck: SlideDeck) -> WorkflowRun:
        """为这份 deck 建一条认领它的运行，输入取当时的材料快照。

        运行在请求里建、在后台任务里跑：接口一返回，界面上就是「生成中」，
        而这份状态只有 ``workflow_run`` 一处。
        """
        asset_names = [
            f"{asset.get('id')}: {asset.get('name')}"
            for asset in (deck.assets or [])
            if isinstance(asset, dict) and asset.get("id")
        ]
        return start_builtin(
            self._db,
            self._owner_id,
            WORKFLOW_GENERATE,
            _generate_inputs(
                title=deck.title,
                description=deck.description,
                requirement=deck.requirement,
                source_text=deck.source_text or "",
                asset_names=asset_names,
            ),
            subject=(SUBJECT_SLIDE_DECK, deck.id),
            subject_label=deck.title,
        )

    def start_generation(self, deck_id: int, requirement: Optional[str] = None) -> SlideDeck:
        """新建一条生成运行；``requirement`` 给了就覆盖当时的要求（重生成时补一句）。"""
        deck = self.get_deck(deck_id)
        if running_subjects(self._db, self._owner_id, SUBJECT_SLIDE_DECK, [deck.id]):
            raise ValueError("该幻灯片正在生成中，请勿重复触发")
        if requirement is not None:
            deck.requirement = requirement.strip()[:512] or None
        self._db.commit()
        self._db.refresh(deck)
        self._start_generation_run(deck)
        return deck

    async def run_generation(self, deck_id: int) -> None:
        """后台任务体：跑发起时建好的运行，清洗结构、重渲染 HTML。

        失败的去处是运行本身；清洗没留下任何合法页面这类**后处理**失败，用
        :func:`fail_run` 把已经成功的运行降级，别让它显示成「已完成但什么都没有」。
        """
        started = time.monotonic()
        deck = self.get_deck(deck_id)
        run = latest_run(self._db, self._owner_id, SUBJECT_SLIDE_DECK, deck.id)
        if run is None:
            logger.error("slide deck has no run to execute: id=%s", deck_id)
            return
        logger.info(
            "slide generation started: id=%s run=%s owner=%s title=%r source_chars=%s assets=%s",
            deck_id,
            run.id,
            self._owner_id,
            deck.title,
            len(deck.source_text or ""),
            len(deck.assets or []),
        )
        try:
            # 这一步在流程里声明的是 markdown：模型的产物既可能是对象（带 theme），
            # 也可能是整页数组，而引擎的 JSON 契约只收对象。所以这里照旧自己解析原文，
            # 不能改取 parsed。
            text = await builtin_output(self._db, self._owner_id, run.id)
            theme, pages = _parse_deck_output(text)
            # 模型给的外观要先过白名单再谈合并：非法 preset / 比例 / 颜色一律
            # 回落默认，不能让任意字符串进到 CSS 里。
            merged_theme = sanitize_theme({**DEFAULT_THEME, **sanitize_theme(theme)})
            slides = sanitize_pages(pages, [str(a.get("id")) for a in (deck.assets or []) if isinstance(a, dict)])
            if not slides:
                raise ValueError("模型返回的结构里没有任何合法页面")

            deck.slides = slides
            deck.theme = merged_theme
            deck.html = None
            self._refresh_html(deck)
            self._db.commit()
            logger.info(
                "slide generation finished: id=%s pages=%s elapsed=%.1fs",
                deck_id,
                len(slides),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 失败写在运行上，不留到这里抛出
            logger.warning(
                "slide generation failed: id=%s elapsed=%.1fs error=%s",
                deck_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            fail_run(self._db, self._owner_id, run.id, model_error_text(exc))

    # -- 编辑 ---------------------------------------------------------------

    def save_deck(
        self,
        deck_id: int,
        *,
        title: Optional[str],
        description: Optional[str],
        theme: Optional[Dict[str, Any]],
        slides: Optional[List[Any]],
    ) -> SlideDeck:
        """整包保存（工作台）。``None`` 字段保持原样。"""
        deck = self.get_deck(deck_id)
        if title is not None:
            clean = title.strip()
            if not clean:
                raise ValueError("主题不能为空")
            deck.title = clean[:128]
        if description is not None:
            deck.description = description.strip()[:512] or None

        asset_ids = [str(item.get("id")) for item in (deck.assets or []) if isinstance(item, dict)]
        if theme is not None:
            deck.theme = sanitize_theme(theme)
        if slides is not None:
            deck.slides = sanitize_pages(slides, asset_ids)

        self._refresh_html(deck)
        self._db.commit()
        self._db.refresh(deck)
        logger.info("slide deck saved: id=%s owner=%s pages=%s", deck_id, self._owner_id, len(deck.slides or []))
        return deck

    # -- 渲染 ---------------------------------------------------------------

    def render_html(self, deck: SlideDeck) -> str:
        """从 slides 确定性渲染整份 HTML 文档。"""
        return render_document(
            title=deck.title,
            theme=deck.theme or {},
            pages=deck.slides or [],
            assets=deck.assets or [],
        )

    def _refresh_html(self, deck: SlideDeck) -> None:
        """每次改 slides / theme / assets 之后重渲染派生的 HTML 列。"""
        deck.html = self.render_html(deck)

    def deck_html(self, deck: SlideDeck) -> str:
        """取预览 / 放映 / 导出用的文档。

        每次都从 ``slides`` 现渲一份并回写：``html`` 列只是派生物，读的时候不
        重渲的话，渲染器一改（比如主题声明漏了分号那类）所有历史行就会一直发
        老版本，用户怎么点都看不到变化。渲染是纯字符串拼装、上限 80 页，成本
        相对于一次 HTTP 请求可以忽略，换来的是「看到的一定是当前渲染结果」。
        """
        self._refresh_html(deck)
        self._db.commit()
        return deck.html or ""

    # -- 序列化 -------------------------------------------------------------

    def _item_of(self, deck: SlideDeck, run: Optional[WorkflowRun]) -> SlideDeckItem:
        # idle 用 error：deck 一创建就带着运行，「没有运行又没有页面」只剩改造之前
        # 那些被重启收尾成失败的历史行这一种解释。
        status, error_msg = subject_state(
            run, has_product=bool(deck.slides), idle=SUBJECT_ERROR
        )
        return SlideDeckItem(
            id=deck.id,
            title=deck.title,
            description=deck.description,
            status=status,
            error_msg=error_msg,
            page_count=len(deck.slides or []),
            asset_count=len(deck.assets or []),
            created_at=deck.created_at,
            updated_at=deck.updated_at,
        )

    def to_item(self, deck: SlideDeck) -> SlideDeckItem:
        return self._item_of(
            deck, latest_run(self._db, self._owner_id, SUBJECT_SLIDE_DECK, deck.id)
        )

    def to_items(self, decks: List[SlideDeck]) -> List[SlideDeckItem]:
        runs = latest_runs(
            self._db, self._owner_id, SUBJECT_SLIDE_DECK, [deck.id for deck in decks]
        )
        return [self._item_of(deck, runs.get(str(deck.id))) for deck in decks]

    def to_detail(self, deck: SlideDeck) -> SlideDeckDetail:
        # 页面与素材直接给清洗后的形状：前端编辑器拿到的 sid / layout /
        # asset_id 一定是合法值，不用自己再兜一遍脏数据。
        asset_ids = [str(item.get("id")) for item in (deck.assets or []) if isinstance(item, dict)]
        theme = sanitize_theme(deck.theme or {})
        pages = sanitize_pages(deck.slides or [], asset_ids)
        return SlideDeckDetail(
            **self.to_item(deck).model_dump(),
            requirement=deck.requirement,
            source_text=deck.source_text or "",
            theme=SlideTheme(**theme),
            slides=pages,
            assets=[SlideAsset(**{k: v for k, v in item.items() if k in SlideAsset.model_fields})
                    for item in (deck.assets or []) if isinstance(item, dict)],
        )
