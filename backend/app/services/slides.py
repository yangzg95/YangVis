"""智能办公 · 幻灯片的存取与 AI 生成。

数据访问按单个用户隔离（与 :class:`ResumeService` 同一约定）：所有查询都经过
:meth:`SlideService._scope`，handler 不直接接触 session。

生成是后台任务：一次要读完全部材料再产出十几页结构，远超 SPA 的 30s axios
超时，所以接口只落一行 ``analyzing`` 记录，真正的模型调用跑在
``BackgroundTasks`` 里，前端靠轮询状态感知进度（与简历分析同一形态）。

事实源是 ``slides`` 这一列结构化页面；``html`` 是从它确定性渲染出来的派生物
（见 :mod:`app.services.slides_html`），每次写入和每次读取都会重渲一遍。用户改
内容只改 ``slides``，预览 / 放映 / 导出自然同步。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import ModelConfig, SlideDeck
from app.models.schemas import (
    ModelPurpose,
    SlideAsset,
    SlideDeckDetail,
    SlideDeckItem,
    SlideTheme,
)
from app.services.agents import AgentService
from app.services.providers import build_chat_model
from app.services.slides_html import (
    MAX_DECK_PAGES,
    render_document,
    sanitize_pages,
    sanitize_theme,
)
from app.services.slides_store import StoredImage

logger = logging.getLogger("yangvis.slides")

settings = get_settings()

# 状态值。不用 Enum：实体里存的是裸字符串，schema 也是裸字符串。
STATUS_ANALYZING = "analyzing"
STATUS_READY = "ready"
STATUS_ERROR = "error"

AGENT_SLUG_GENERATE = "slide-architect"

# 与简历/对比同一理由：非流式调用要等整个 completion 落地才返回，十几页结构
# 动辄上千 token，全局 30s 的 MODEL_HTTP_TIMEOUT 几乎必然超时。
MODEL_TASK_TIMEOUT = 300.0

# 默认外观，deck 还没生成时也用这套。
DEFAULT_THEME: Dict[str, Any] = {"preset": "teal", "ratio": "16x9", "accent": None}


def _labeled_section(label: str, text: str) -> str:
    return f'{label}：\n"""\n{text.strip()}\n"""'


def _truncate(text: str) -> str:
    limit = settings.SLIDE_MAX_SOURCE_CHARS
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n……（材料过长，已截断）"


def _task_error_text(exc: Exception) -> str:
    """把后台任务里的模型异常转成用户能看懂的失败原因。"""
    text = str(exc)
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "模型响应超时：幻灯片一次要读完全部材料并产出整份结构，"
            "可重新生成试试；若反复出现，建议换用响应更快的模型。"
        )
    return text[:512]


def _json_candidate(text: str) -> Optional[str]:
    """从模型输出里截出可能是 JSON 的那一段。

    模型并不总是守规矩：裹 ```json 栅栏、前后带寒暄都是常态。看**最先出现**的
    那个开括号——我们的契约是 ``{"theme":…, "slides":…}``，但只回一个页面数组
    的输出同样能用；先 ``{`` 就按对象截，先 ``[`` 就按数组截。反过来做会出事：
    数组形式 ``[{...}]`` 里也有花括号，按「对象优先」截会掐出第一个元素，
    解析出来是个没有 slides 的字典，整份结构白丢。
    """
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)

    pairs = (("{", "}"), ("[", "]"))

    def span(open_brace: str, close_brace: str) -> Optional[str]:
        start, end = stripped.find(open_brace), stripped.rfind(close_brace)
        return stripped[start : end + 1] if 0 <= start < end else None

    starts = {open_brace: stripped.find(open_brace) for open_brace, _ in pairs}
    object_first = starts["{"] != -1 and (starts["["] == -1 or starts["{"] < starts["["])
    first, second = pairs if object_first else (pairs[1], pairs[0])
    return span(*first) or span(*second)


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


def _generate_prompt(
    *,
    title: str,
    description: Optional[str],
    requirement: Optional[str],
    source_text: str,
    asset_names: List[str],
) -> str:
    """组装喂模型的 human 消息。人设与输出格式在智能体上。"""
    parts = [f"主题：{title}"]
    if description:
        parts.append(f"补充说明：{description}")
    if requirement:
        parts.append(f"用户对这份幻灯片的要求：{requirement}")
    if source_text:
        parts.append(_labeled_section("材料全文", _truncate(source_text)))
    if asset_names:
        # 模型看不到图片内容，只拿到清单；image 版式必须引用清单里的 id。
        listing = "\n".join(f"- {name}" for name in asset_names)
        parts.append(
            "可用配图（id: 文件名，只有这些 id 能被 asset_id 引用）：\n"
            f"{listing}"
        )
    parts.append(
        "请依据以上材料产出整份幻灯片的 JSON 结构。"
        f"页数不超过 {MAX_DECK_PAGES} 页。"
    )
    return "\n\n".join(parts)


class SlideService:
    """针对单个用户的幻灯片做增删改查、AI 生成与 HTML 渲染。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._agents = AgentService(db, owner_id)

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(SlideDeck.owner_id == self._owner_id)

    # -- 模型配置 -----------------------------------------------------------

    def require_chat_config(self) -> ModelConfig:
        """生成门槛：用户得先有一份验证过的默认对话模型（与简历同一约定）。"""
        config = self._db.scalar(
            select(ModelConfig).where(
                ModelConfig.owner_id == self._owner_id,
                ModelConfig.purpose == ModelPurpose.CHAT.value,
                ModelConfig.is_default.is_(True),
            )
        )
        if config is None or not config.last_test_ok:
            raise BusinessError(
                CODE_CHAT_NOT_READY,
                "尚未配置可用的对话模型。请先到「系统设置 → 模型管理」添加并通过连通性测试。",
            )
        return config

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
        """落一条 ``analyzing`` 记录。材料与图片无关（图片是后加的素材）。

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
            status=STATUS_ANALYZING,
        )
        self._db.add(deck)
        self._db.commit()
        self._db.refresh(deck)
        self._refresh_html(deck)
        self._db.commit()
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
        if deck.status == STATUS_ANALYZING:
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

    async def _ask(self, slug: str, user_prompt: str) -> str:
        """按 slug 取内置智能体的人设发起一轮问答，返回纯文本输出。

        与简历同一形态：人设（含输出格式要求）在 agent.system_prompt 上，模型
        配置与温度也跟着智能体走；材料全文这类变量内容放 human 消息。
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        agent = self._agents.require_by_slug(slug)
        config = self.require_chat_config()
        logger.info(
            "calling chat model: agent=%s model=%s base_url=%s prompt_chars=%s owner=%s",
            slug,
            config.model_name,
            config.base_url,
            len(user_prompt),
            self._owner_id,
        )
        model = build_chat_model(
            config, temperature=agent.temperature / 100, timeout=MODEL_TASK_TIMEOUT
        )
        result = await model.ainvoke(
            [SystemMessage(content=agent.system_prompt), HumanMessage(content=user_prompt)]
        )
        text = result.content if hasattr(result, "content") else str(result)
        if isinstance(text, list):  # 部分 provider 返回 content block 列表
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in text
            )
        return str(text)

    def start_generation(self, deck_id: int, requirement: Optional[str] = None) -> SlideDeck:
        """置为生成中；``requirement`` 给了就覆盖当时的要求（重生成时补一句）。"""
        deck = self.get_deck(deck_id)
        if deck.status == STATUS_ANALYZING:
            raise ValueError("该幻灯片正在生成中，请勿重复触发")
        if requirement is not None:
            deck.requirement = requirement.strip()[:512] or None
        deck.status = STATUS_ANALYZING
        deck.error_msg = None
        self._db.commit()
        self._db.refresh(deck)
        return deck

    async def run_generation(self, deck_id: int) -> None:
        """后台任务体：调模型、清洗结构、重渲染 HTML。异常一律落状态而不抛出。"""
        started = time.monotonic()
        deck = self.get_deck(deck_id)
        logger.info(
            "slide generation started: id=%s owner=%s title=%r source_chars=%s assets=%s",
            deck_id,
            self._owner_id,
            deck.title,
            len(deck.source_text or ""),
            len(deck.assets or []),
        )
        try:
            asset_names = [
                f"{asset.get('id')}: {asset.get('name')}"
                for asset in (deck.assets or [])
                if isinstance(asset, dict) and asset.get("id")
            ]
            prompt = _generate_prompt(
                title=deck.title,
                description=deck.description,
                requirement=deck.requirement,
                source_text=deck.source_text or "",
                asset_names=asset_names,
            )
            text = await self._ask(AGENT_SLUG_GENERATE, prompt)
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
            deck.status = STATUS_READY
            deck.error_msg = None
            logger.info(
                "slide generation finished: id=%s pages=%s elapsed=%.1fs",
                deck_id,
                len(slides),
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - 状态字段就是这类错误的归宿
            logger.warning(
                "slide generation failed: id=%s elapsed=%.1fs error=%s",
                deck_id,
                time.monotonic() - started,
                exc,
                exc_info=True,
            )
            deck.status = STATUS_ERROR
            deck.error_msg = _task_error_text(exc)
        self._db.commit()

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

    @staticmethod
    def to_item(deck: SlideDeck) -> SlideDeckItem:
        return SlideDeckItem(
            id=deck.id,
            title=deck.title,
            description=deck.description,
            status=deck.status,
            error_msg=deck.error_msg,
            page_count=len(deck.slides or []),
            asset_count=len(deck.assets or []),
            created_at=deck.created_at,
            updated_at=deck.updated_at,
        )

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


def reset_stale_slides(db: Session) -> None:
    """把上一个进程遗留的「生成中」标记为失败。

    后台任务活在 Gunicorn worker 里，进程一重启它们就没了；不收拾的话这些行
    会永远停在 analyzing，用户既删不了也等不到结果（与简历同一处理）。
    """
    stale = db.execute(
        update(SlideDeck)
        .where(SlideDeck.status == STATUS_ANALYZING)
        .values(status=STATUS_ERROR, error_msg="服务重启导致生成中断，请重新生成")
    ).rowcount
    db.commit()
    if stale:
        logger.warning("reset stale slide decks after restart: %s", stale)
