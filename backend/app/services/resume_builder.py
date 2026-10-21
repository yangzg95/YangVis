"""智能办公 · 在线简历编辑器的存取与流式 AI 帮写。

与 :mod:`app.services.resume`（上传 PDF/Word 做分析的那条线）是两个域：这条线
的简历由用户在页面上逐模块搭建，整份结构是一个 JSON 文档（``ResumeDoc.doc``）。

- 结构契约（模块键、条目字段、月份格式、白名单值）以 :func:`default_doc` 与
  :func:`normalize_doc` 为准——保存是唯一收口，旧版本缺键少字段打开也不崩。
- 富文本是 contenteditable 产出的 HTML 片段，落库前在这里过白名单消毒，
  前端预览/打印前还会再消毒一次（防御纵深）。
- AI 帮写是交互式的短平快调用，直接 SSE 流式返回，不走工作流/运行记录那套
  （那套是后台长任务 + 进度翻译，这里用户就在编辑器前等着出字）。
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import secrets
from html.parser import HTMLParser
from typing import AsyncIterator, Callable, List, Optional

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.errors import BusinessError
from app.models.entities import ResumeDoc
from app.models.schemas import ResumeDocAiRequest
from app.services.agent_run import MODEL_TASK_TIMEOUT, require_chat_model
from app.services.agents import AgentService
from app.services.providers import build_chat_model, resolve_temperature

logger = logging.getLogger("yangvis.resume_builder")

# ---- 结构契约 ---------------------------------------------------------------

MODULES = ("profile", "companies", "projects", "educations", "skills")
DEFAULT_MODULE_ORDER = list(MODULES)

# 版式模板与主题配色正交：渲染分支在前端 resumeTemplate.ts，加模板/配色两边一起改。
TEMPLATES = ("classic", "sidebar", "minimal")
DEFAULT_TEMPLATE = "classic"

THEMES = ("teal", "navy", "crimson", "forest", "violet", "amber", "slate")
DEFAULT_THEME = "teal"

DEGREES = ("高中", "大专", "本科", "硕士", "博士")
STUDY_MODES = ("全日制", "非全日制")

SHORT_LIMIT = 128
MONTH_LIMIT = 7
HTML_LIMIT = 20_000

# 喂给模型的整份简历上下文上限（与简历分析那条线的取值理由相同）。
CONTEXT_LIMIT = 12_000

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

MODULE_LABELS = {
    "profile": "个人信息",
    "companies": "工作经历",
    "projects": "项目经历",
    "educations": "教育经历",
    "skills": "专业技能",
}

# action → 使用的内置人设（agents.py 里 seed）。
AGENT_BY_ACTION = {
    "write": "resume-writer",
    "polish": "resume-polisher",
    "check": "resume-checker",
}


# ---- 富文本消毒 -------------------------------------------------------------

# 白名单只放开排版所需的最小标签集合：段落/换行/容器、粗斜下划线、两种列表、
# 链接、无语义 span。style/class 一律不保留——打印版面由我们自己的 CSS 决定，
# 模型或粘贴内容带进来的样式既不可控也可能藏点击劫持。
_ALLOWED_TAGS = {"p", "br", "div", "span", "strong", "b", "em", "i", "u", "ul", "ol", "li", "a"}
_VOID_TAGS = {"br"}
_RAW_TEXT_TAGS = {"script", "style", "iframe", "object", "embed"}
_SAFE_HREF = re.compile(r"^\s*(?:https?|mailto|tel):", re.IGNORECASE)


class _HtmlSanitizer(HTMLParser):
    """把任意 HTML 重建成白名单内、标签平衡的片段。

    不在白名单的标签整对丢弃但保留其文本（``<strong>`` 保留、``<marquee>`` 不
    保留）；``script/style`` 这类原文本标签连内容一起丢；``<a>`` 只放行
    http(s)/mailto/tel 协议，javascript: 一律变成纯文本。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._stack: List[str] = []
        # >0 时正在跳过 script/style 等标签的原文内容。
        self._raw_skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag in _RAW_TEXT_TAGS:
            self._raw_skip += 1
            return
        if self._raw_skip or tag not in _ALLOWED_TAGS:
            return
        if tag in _VOID_TAGS:
            self._parts.append("<br>")
            return
        if tag == "a":
            href = html_lib.unescape(dict(attrs).get("href") or "").strip()
            if not _SAFE_HREF.match(href):
                # 危险链接：丢掉标签本身，闭合时按栈处理（不会留下孤立 </a>）。
                return
            self._parts.append(f'<a href="{html_lib.escape(href, quote=True)}">')
        else:
            self._parts.append(f"<{tag}>")
        self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag in _VOID_TAGS and not self._raw_skip:
            self._parts.append("<br>")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag = tag.lower()
        if tag in _RAW_TEXT_TAGS:
            self._raw_skip = max(0, self._raw_skip - 1)
            return
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        # 模型/浏览器可能漏闭合：从栈顶一路关到匹配的那个，保证标签平衡。
        if tag in self._stack:
            while self._stack:
                opened = self._stack.pop()
                self._parts.append(f"</{opened}>")
                if opened == tag:
                    break

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._raw_skip:
            return
        self._parts.append(html_lib.escape(data, quote=False))

    def result(self) -> str:
        while self._stack:
            self._parts.append(f"</{self._stack.pop()}>")
        return "".join(self._parts)


def sanitize_html(raw: object, limit: int = HTML_LIMIT) -> str:
    """把富文本输入消毒成白名单 HTML 片段；非字符串或全危险内容得到空串。"""
    if not isinstance(raw, str):
        return ""
    parser = _HtmlSanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - 解析器遇到畸形 HTML 不该让保存失败
        logger.debug("html sanitize hit malformed input", exc_info=True)
    return parser.result()[:limit]


# ---- 小工具 -----------------------------------------------------------------


def _text(value: object, limit: int = SHORT_LIMIT) -> str:
    """字符串字段的统一收口：非字符串丢弃、首尾空白与换行收掉、截断。"""
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", value.strip())[:limit]


def _month(value: object) -> str:
    """月份只接受 YYYY-MM，别的形态（完整日期、「2020.1」）一律置空。"""
    text = value if isinstance(value, str) else ""
    return text if _MONTH_RE.match(text or "") else ""


def _bool(value: object) -> bool:
    return value is True


def _entry_id(value: object) -> str:
    """条目 id 是整文档保存下的前端本地键，只在文档内部寻址，认一个安全形状；
    不认（旧数据/手改库）就现场补发一个。"""
    return value if isinstance(value, str) and _ID_RE.match(value) else secrets.token_hex(6)


def _whitelist(value: object, choices: tuple) -> str:
    return value if isinstance(value, str) and value in choices else ""


def _html(value: object) -> str:
    return sanitize_html(value)


# ---- 默认文档与归一化 ---------------------------------------------------------


def default_doc() -> dict:
    """新建简历的骨架。字段集与 normalize_doc 输出严格一致。"""
    return {
        "template": DEFAULT_TEMPLATE,
        "theme": DEFAULT_THEME,
        "module_order": list(DEFAULT_MODULE_ORDER),
        "profile": {
            "name": "",
            "gender": "",
            "job_target": "",
            "phone": "",
            "email": "",
            "city": "",
            "birthday": "",
            "summary_html": "",
        },
        "companies": [],
        "projects": [],
        "educations": [],
        "skills_html": "",
    }


def _normalize_profile(raw: object) -> dict:
    data = raw if isinstance(raw, dict) else {}
    return {
        "name": _text(data.get("name")),
        "gender": _text(data.get("gender"), 32),
        "job_target": _text(data.get("job_target")),
        "phone": _text(data.get("phone"), 32),
        "email": _text(data.get("email")),
        "city": _text(data.get("city"), 64),
        "birthday": _text(data.get("birthday"), 32),
        "summary_html": _html(data.get("summary_html")),
    }


def _normalize_entry(raw: object, spec: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    # id 字段按各自的键名落回（cid/pid/eid）。
    entry = {spec["id_field"]: _entry_id(raw.get(spec["id_field"]))}
    for field in spec["text_fields"]:
        entry[field] = _text(raw.get(field))
    if spec.get("whitelist"):
        for field, choices in spec["whitelist"].items():
            entry[field] = _whitelist(raw.get(field), choices)
    entry["start"] = _month(raw.get("start"))
    entry["end"] = _month(raw.get("end"))
    entry["current"] = _bool(raw.get("current"))
    entry[spec["html_field"]] = _html(raw.get(spec["html_field"]))
    return entry


_ENTRY_SPECS = {
    "companies": {
        "id_field": "cid",
        "text_fields": ["company", "position", "department"],
        "html_field": "description_html",
    },
    "projects": {
        "id_field": "pid",
        "text_fields": ["name", "role", "department"],
        "html_field": "description_html",
    },
    "educations": {
        "id_field": "eid",
        "text_fields": ["school", "major", "college"],
        "whitelist": {"degree": DEGREES, "study_mode": STUDY_MODES},
        "html_field": "description_html",
    },
}


def _normalize_entries(raw: object, module: str) -> list:
    if not isinstance(raw, list):
        return []
    entries = []
    for item in raw:
        entry = _normalize_entry(item, _ENTRY_SPECS[module])
        if entry is not None:
            entries.append(entry)
    return entries


def normalize_doc(raw: object) -> dict:
    """保存时的唯一收口：任意来源（前端/旧版本/手改库）进来，出去都是完整契约。

    模块顺序必须恰好是五个键的一个排列——缺了补回默认尾部、多了/乱了都退回默认，
    免得渲染层处理「神秘第六模块」或漏模块。
    """
    data = raw if isinstance(raw, dict) else {}
    base = default_doc()

    order = data.get("module_order")
    if (
        not isinstance(order, list)
        or len(order) != len(MODULES)
        or set(order) != set(MODULES)
    ):
        order = list(DEFAULT_MODULE_ORDER)

    base["template"] = (
        data["template"] if data.get("template") in TEMPLATES else DEFAULT_TEMPLATE
    )
    base["theme"] = data["theme"] if data.get("theme") in THEMES else DEFAULT_THEME
    base["module_order"] = order
    base["profile"] = _normalize_profile(data.get("profile"))
    base["companies"] = _normalize_entries(data.get("companies"), "companies")
    base["projects"] = _normalize_entries(data.get("projects"), "projects")
    base["educations"] = _normalize_entries(data.get("educations"), "educations")
    base["skills_html"] = _html(data.get("skills_html"))
    return base


# ---- AI prompt 拼装 ----------------------------------------------------------


def html_to_text(fragment: str) -> str:
    """把消毒过的 HTML 片段拍成给模型看的纯文本。"""
    text = _TAG_RE.sub("", fragment)
    text = html_lib.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _month_range(start: str, end: str, current: bool) -> str:
    if current:
        return f"{start or '?'} 至今"
    if start or end:
        return f"{start or '?'} - {end or '?'}"
    return ""


def _entry_context(module: str, entry: dict) -> List[str]:
    if module == "companies":
        rows = [
            ("公司名称", entry.get("company")),
            ("职位名称", entry.get("position")),
            ("所在部门", entry.get("department")),
        ]
    elif module == "projects":
        rows = [
            ("项目名称", entry.get("name")),
            ("我的角色", entry.get("role")),
            ("所在部门", entry.get("department")),
        ]
    else:
        rows = [
            ("学校名称", entry.get("school")),
            ("专业", entry.get("major")),
            ("学院", entry.get("college")),
            ("学历", entry.get("degree")),
            ("就读形式", entry.get("study_mode")),
        ]
    period = _month_range(entry.get("start", ""), entry.get("end", ""), entry.get("current", False))
    if period:
        rows.append(("时间", period))
    return [f"{label}：{value}" for label, value in rows if value]


def _find_entry(doc: dict, module: str, item_id: Optional[str]) -> Optional[dict]:
    if module == "profile" or module == "skills":
        return None
    for entry in doc.get(module, []):
        id_field = _ENTRY_SPECS[module]["id_field"]
        if entry.get(id_field) == item_id:
            return entry
    return None


def _full_resume_text(doc: dict) -> str:
    """把整份简历拍成带标签的纯文本，帮写与体检都拿它当事实边界。"""
    lines = []
    profile = doc.get("profile", {})
    head = " / ".join(
        value
        for value in (profile.get("name"), profile.get("job_target"), profile.get("city"))
        if value
    )
    if head:
        lines.append(f"个人信息：{head}")
    for key in ("phone", "email", "gender", "birthday"):
        if profile.get(key):
            lines.append(f"- {key}：{profile[key]}")
    if profile.get("summary_html"):
        lines.append(f"个人优势：{html_to_text(profile['summary_html'])}")

    secondary_fields = {"companies": "position", "projects": "role", "educations": "major"}
    for module, title_field in (
        ("companies", "company"),
        ("projects", "name"),
        ("educations", "school"),
    ):
        for index, entry in enumerate(doc.get(module, []), start=1):
            label = MODULE_LABELS[module]
            heading = " / ".join(
                value
                for value in (entry.get(title_field), entry.get(secondary_fields[module]))
                if value
            )
            period = _month_range(entry.get("start", ""), entry.get("end", ""), entry.get("current", False))
            lines.append(f"\n{label} {index}：{heading} {period}".rstrip())
            for row in _entry_context(module, entry):
                lines.append(f"- {row}")
            desc = html_to_text(entry.get("description_html", ""))
            if desc:
                lines.append(f"- 已有描述：{desc}")

    skills = html_to_text(doc.get("skills_html", ""))
    if skills:
        lines.append(f"\n专业技能：{skills}")
    return "\n".join(lines).strip()


def _truncate(text: str, limit: int = CONTEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + "\n……（内容过长，已截断）"


_HTML_FORMAT_RULE = (
    "只输出一段 HTML 片段：允许的标签只有 <p>、<ul>、<li>、<strong>；"
    "不要输出 <html>/<body>、不要 markdown 代码栅栏、不要标题寒暄或任何解释。"
)


def build_assist_prompt(doc: dict, payload: ResumeDocAiRequest) -> str:
    """帮写/润色的用户提示：整份简历给事实边界，当前模块给聚焦点。"""
    module = payload.module
    label = MODULE_LABELS[module]
    full = _truncate(_full_resume_text(doc))
    selection = (payload.selection or "").strip()

    # 这个模块里富文本承载的内容叫什么（profile 是个人优势，技能就是技能本身，
    # 三类经历条目统一叫描述）。
    content_name = {
        "profile": "个人优势/自我评价",
        "skills": "专业技能",
        "companies": "工作描述",
        "projects": "项目描述",
        "educations": "在校经历",
    }[module]

    focused: List[str] = []
    if module in ("companies", "projects", "educations"):
        entry = _find_entry(doc, module, payload.item_id) if payload.item_id else None
        if entry is None and payload.item_id:
            raise ValueError("找不到对应的经历条目，可能已被删除")
        if entry is not None:
            focused = _entry_context(module, entry)
            current_desc = html_to_text(entry.get("description_html", ""))
        else:
            current_desc = ""
    elif module == "profile":
        current_desc = html_to_text(doc.get("profile", {}).get("summary_html", ""))
    else:
        current_desc = html_to_text(doc.get("skills_html", ""))

    parts = [f"模块：{label}"]
    if focused:
        parts.append("当前条目的已有信息：\n" + "\n".join(focused))
    if full:
        parts.append("整份简历（事实边界，只能基于这里真实存在的信息）：\n" + full)

    if payload.action == "polish":
        target = selection or current_desc
        if not target:
            raise ValueError("还没有可润色的内容，请先填写或选中一段文字")
        parts.append("需要润色的原文：\n" + target)
        parts.append(
            "请在不改变任何事实的前提下润色上面这段"
            f"「{content_name}」：用强有力的行动动词开头、"
            "语言精炼专业、保留可量化的数字；信息不足处不要编造。" + _HTML_FORMAT_RULE
        )
    else:
        if current_desc:
            parts.append("现有的描述（可在其基础上扩写，不要简单复述空话）：\n" + current_desc)
        if selection:
            parts.append("用户选中的片段：\n" + selection)
        if module == "skills":
            parts.append(
                "请根据整份简历生成「专业技能」：用 <ul><li> 列出 4-8 条技能，"
                "每条「技能点：具体掌握程度/应用场景」，只写简历里有依据的技能。" + _HTML_FORMAT_RULE
            )
        elif module == "profile":
            parts.append(
                "请生成「个人优势/自我评价」：用 <ul><li> 列出 3-5 条，"
                "提炼与求职意向相关的经验年限、核心能力与成果，不要空洞的形容词堆砌。"
                + _HTML_FORMAT_RULE
            )
        else:
            parts.append(
                f"请生成「{label}」的{content_name}：简短交代背景后用 <ul><li> 分条罗列，"
                "每条以行动动词开头并尽量给出可量化的成果（提升 X%、节省 X 小时、支撑 X 万用户）；"
                "简历里没有的公司、数字与经历一律不要编造，需要用户自己补充的数字用「X」占位并在该条末尾用括号提示。"
                + _HTML_FORMAT_RULE
            )
    return "\n\n".join(parts)


def build_check_prompt(doc: dict) -> str:
    """整份体检的用户提示，输出 markdown。"""
    full = _truncate(_full_resume_text(doc), limit=20_000)
    if not full:
        raise ValueError("简历还是空的，先填写一些内容再做体检")
    return (
        "下面是用户的整份简历内容：\n"
        '"""\n' + full + '\n"""\n\n'
        "请做一次简历体检，用 markdown 输出：\n"
        "1. 先给 0-100 的评分与一句话总评；\n"
        "2. 再按模块（个人优势/工作经历/项目经历/教育经历/专业技能/联系方式）"
        "列出具体问题，每条包含「问题」与「怎么改」，要指出原文哪里、给出可直接替换的范例；\n"
        "3. 最后列出最该优先处理的 3 条。\n"
        "全部使用简体中文。"
    )


# ---- Service ----------------------------------------------------------------


class ResumeBuilderService:
    """针对单个用户的在线简历做增删改查与流式 AI 调用。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def _scope(self, stmt: Select) -> Select:
        return stmt.where(ResumeDoc.owner_id == self._owner_id)

    # -- CRUD ---------------------------------------------------------------

    def list_docs(self) -> List[ResumeDoc]:
        stmt = self._scope(select(ResumeDoc)).order_by(ResumeDoc.updated_at.desc(), ResumeDoc.id.desc())
        return list(self._db.scalars(stmt).all())

    def get_doc(self, doc_id: int) -> ResumeDoc:
        row = self._db.scalar(self._scope(select(ResumeDoc).where(ResumeDoc.id == doc_id)))
        if row is None:
            raise LookupError("简历不存在")
        return row

    def create_doc(self, title: str) -> ResumeDoc:
        row = ResumeDoc(
            owner_id=self._owner_id,
            title=title.strip()[:128] or "未命名简历",
            doc=default_doc(),
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info("owner %s created resume doc %s", self._owner_id, row.id)
        return row

    def update_doc(
        self, doc_id: int, *, title: Optional[str], doc: Optional[dict]
    ) -> ResumeDoc:
        row = self.get_doc(doc_id)
        if title is not None:
            row.title = title.strip()[:128] or row.title
        if doc is not None:
            row.doc = normalize_doc(doc)
        self._db.commit()
        self._db.refresh(row)
        return row

    def delete_doc(self, doc_id: int) -> None:
        row = self.get_doc(doc_id)
        self._db.delete(row)
        self._db.commit()

    # -- 序列化 -------------------------------------------------------------

    @staticmethod
    def to_item(row: ResumeDoc) -> dict:
        return {
            "id": row.id,
            "title": row.title,
            "updated_at": row.updated_at,
            "created_at": row.created_at,
        }

    def to_detail(self, row: ResumeDoc) -> dict:
        # 出库也过一遍归一化：老数据/历史脏数据读出来即补齐契约，前端不用兼容。
        return {**self.to_item(row), "doc": normalize_doc(row.doc)}

    # -- AI -----------------------------------------------------------------

    def _load_doc_for_ai(self, doc_id: Optional[int]) -> dict:
        if doc_id is None:
            return default_doc()
        return normalize_doc(self.get_doc(doc_id).doc)

    async def ai_stream(
        self,
        payload: ResumeDocAiRequest,
        *,
        is_disconnected: Optional[Callable[[], object]] = None,
    ) -> AsyncIterator[str]:
        """流式产出帮写/润色/体检的文本增量。

        人设决定输出形态（HTML 片段或 markdown）；这里只搬运 token，错误外抛给
        路由翻译成 SSE error 事件。
        """
        doc = self._load_doc_for_ai(payload.doc_id)
        agent = AgentService(self._db, self._owner_id).require_by_slug(
            AGENT_BY_ACTION[payload.action]
        )
        # 未配置可用对话模型时抛 BusinessError(CODE_CHAT_NOT_READY)，由路由落成
        # error 事件，前端按既有「去配置」引导处理。
        config = require_chat_model(self._db, self._owner_id)

        if payload.action == "check":
            prompt = build_check_prompt(doc)
        else:
            prompt = build_assist_prompt(doc, payload)

        from langchain_core.messages import HumanMessage, SystemMessage

        # 与 chat 流同一取法：streaming=True 的模型按 chunk 吐 content 增量。
        model = build_chat_model(
            config,
            temperature=resolve_temperature(config, agent.temperature),
            streaming=True,
            timeout=MODEL_TASK_TIMEOUT,
        )
        messages = [
            SystemMessage(content=agent.system_prompt),
            HumanMessage(content=prompt),
        ]
        async for chunk in model.astream(messages):
            if is_disconnected is not None and await is_disconnected():
                logger.info(
                    "resume ai stream aborted by client: owner=%s doc=%s action=%s",
                    self._owner_id,
                    payload.doc_id,
                    payload.action,
                )
                break
            text = getattr(chunk, "content", "") or ""
            if isinstance(text, list):
                # 部分 provider 以结构化内容块流式返回，同 chat 服务的取法。
                text = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in text
                )
            if text:
                yield text
