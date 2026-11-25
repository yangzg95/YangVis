"""人物库：形象图落盘（只收图片）、账号级存取、回收期软删除与引用反查、HTTP 信封。

三条线各管一段（与 ``test_materials.py`` 同一形状）：

* ``store_character_image`` 只碰磁盘，钉的是「魔数定类型、视频与不认识的内容不留半个文件」；
* ``CharacterService`` 只碰数据库，钉隔离、条数闸门、角色组解析与删除时的引用反查；
* HTTP 层钉 multipart 信封与答复文案。

产物目录一律指到 pytest 的临时目录，不碰真实部署的 uploads/。
"""
from __future__ import annotations

import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import bootstrap
from app.config import get_settings
from app.crypto import encrypt
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import ModelConfig, VideoCharacter, VideoProject
from app.models.schemas import CurrentUser, ModelPurpose
from app.services.characters import service as character_service
from app.services.characters import store as character_store
from app.services.characters.prompts import build_turnaround_prompt
from app.services.characters.service import CharacterService
from app.services.characters.store import (
    ensure_thumbnail,
    store_character_image,
    thumbnail_url,
)
from app.services.media import ark, store as media_store
from app.services.media.store import MediaFileError

settings = get_settings()
OWNER = 1
OTHER = 2

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32
NOT_MEDIA = b"<html><script>alert(1)</script></html>"


def _fake_settings(root, **overrides):
    """一份够用就行的 settings：三个模块共用，属性按各自用到的那几项给。"""
    values = {
        "upload_media_path": root,
        "UPLOAD_MEDIA_URL": "/uploads",
        "MATERIAL_MAX_IMAGE_BYTES": settings.MATERIAL_MAX_IMAGE_BYTES,
        "CHARACTER_MAX_PER_OWNER": settings.CHARACTER_MAX_PER_OWNER,
        "CHARACTER_TRASH_TTL_SECONDS": settings.CHARACTER_TRASH_TTL_SECONDS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def engine(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'characters.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    fake = _fake_settings(tmp_path)
    monkeypatch.setattr(media_store, "settings", fake)
    monkeypatch.setattr(character_store, "settings", fake)
    monkeypatch.setattr(character_service, "settings", fake)
    return tmp_path


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    monkeypatch.setattr(bootstrap, "SessionLocal", maker, raising=False)
    monkeypatch.setattr(bootstrap, "engine", engine, raising=False)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client_for(db):
    """工厂：按身份取 TestClient（与 test_materials 同一约定，用例里不要交错身份）。"""

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    def make_client(owner_id: int) -> TestClient:
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner_id, username=f"user{owner_id}"
        )
        return TestClient(app)

    try:
        yield make_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def client(client_for):
    return client_for(OWNER)


def _service(db, owner_id: int = OWNER) -> CharacterService:
    return CharacterService(db, owner_id)


def _real_png(width: int = 800, height: int = 1000) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (18, 52, 86)).save(buffer, "PNG")
    return buffer.getvalue()


def _character(db, owner_id: int = OWNER, name: str = "林小夏", **fields) -> VideoCharacter:
    stored = store_character_image(owner_id, _real_png())
    return _service(db, owner_id).create(
        name=name,
        persona=fields.get("persona", "28 岁互联网运营，语速快、爱用反问句"),
        outfit=fields.get("outfit", "米白色西装外套、牛仔裤"),
        features=fields.get("features", "齐肩短发、圆框眼镜、帆布包"),
        stored=stored,
    )


def _project(db, character_ids: list[str] | None = None) -> VideoProject:
    project = VideoProject(
        owner_id=OWNER,
        title="季度复盘三步",
        idea="教打工人做复盘",
        character_ids=character_ids or [],
    )
    db.add(project)
    db.commit()
    return project


# ---- 落盘 -------------------------------------------------------------------


def test_store_character_image_lands_under_account_tree(tmp_path):
    stored = store_character_image(OWNER, PNG)

    assert (stored.mime, stored.size) == ("image/png", len(PNG))
    # 按账号分目录的第三种树：媒体层路径守卫必须放行，读盘/拼 URL 才不会当非法路径。
    assert stored.rel_path.startswith(f"characters/{OWNER}/")
    assert stored.rel_path.endswith(".png")
    assert stored.url == f"/uploads/{stored.rel_path}"
    assert (tmp_path / stored.rel_path).read_bytes() == PNG
    assert media_store.is_safe_rel_path(stored.rel_path)
    assert media_store.resolve_url(stored.rel_path) == stored.url


def test_store_character_image_rejects_video_and_unknown_bytes(tmp_path):
    # 角色要的是稳定的人脸/穿搭参考，视频容器没有入口，魔数判出视频直接拒。
    with pytest.raises(MediaFileError, match="只支持"):
        store_character_image(OWNER, MP4)
    with pytest.raises(MediaFileError, match="只支持|空的"):
        store_character_image(OWNER, NOT_MEDIA)
    assert not (tmp_path / "characters").exists(), "拒绝必须发生在建目录之前"


def test_store_character_image_gates_size(tmp_path, monkeypatch):
    monkeypatch.setattr(
        character_store,
        "settings",
        _fake_settings(tmp_path, MATERIAL_MAX_IMAGE_BYTES=1024),
    )
    with pytest.raises(MediaFileError, match="超过"):
        store_character_image(OWNER, PNG + b"0" * 4096)


def test_character_thumbnail_is_generated_next_to_the_portrait(tmp_path):
    stored = store_character_image(OWNER, _real_png())

    thumb_url = thumbnail_url(stored.rel_path)
    assert thumb_url.endswith("_thumb.webp")
    assert (tmp_path / thumb_url.removeprefix("/uploads/")).is_file()
    # 已有缩略图时直接回 URL，不重复生成；删掉后 ensure 再补一张。
    assert ensure_thumbnail(stored.rel_path) == thumb_url


# ---- 服务层：隔离与闸门 ------------------------------------------------------


def test_character_crud_and_search(db):
    row = _character(db, name="林小夏")
    assert row.persona and row.outfit and row.features

    fetched = _service(db).get_character(row.id)
    assert fetched.name == "林小夏"

    rows, total = _service(db).list_characters()
    assert total == 1 and rows[0].id == row.id

    rows, total = _service(db).list_characters(keyword="林")
    assert total == 1 and rows[0].id == row.id
    assert _service(db).list_characters(keyword="不存在的人")[1] == 0

    updated = _service(db).update(
        row.id, name="林小夏（改）", persona="", outfit="黑色卫衣", features=""
    )
    assert updated.name == "林小夏（改）"
    assert updated.persona == "" and updated.features == "", "空串是显式清空"
    # None = 这个字段不动：重新取一行只改名字，其余档案保持原样。
    untouched = _service(db).update(row.id, name="林小夏", persona=None)
    assert untouched.outfit == "黑色卫衣" and untouched.features == ""


def test_character_isolation_between_accounts(db):
    row = _character(db, owner_id=OWNER)
    _character(db, owner_id=OTHER, name="别人的角色")

    with pytest.raises(LookupError):
        _service(db, OTHER).get_character(row.id)
    # 列表也按账号收敛：OTHER 只看得到自己那一个。
    assert _service(db, OTHER).list_characters()[1] == 1


def test_character_requires_a_name(db):
    stored = store_character_image(OWNER, PNG)
    with pytest.raises(ValueError, match="名称不能为空"):
        _service(db).create(name="   ", persona="", outfit="", features="", stored=stored)
    # 落库失败时调用方负责删孤儿文件，这里只保证没留下数据库行。
    assert db.scalar(select(func.count(VideoCharacter.id))) == 0


def test_character_quota_gate(db, monkeypatch):
    # 只改条数上限，落盘目录沿用 media_dir 夹具 monkeypatch 过的那份（tmp_path）。
    fake = _fake_settings(character_store.settings.upload_media_path, CHARACTER_MAX_PER_OWNER=1)
    monkeypatch.setattr(character_service, "settings", fake)
    _character(db, name="第一个")
    stored = store_character_image(OWNER, PNG)
    with pytest.raises(ValueError, match="最多建 1 个"):
        _service(db).create(name="第二个", persona="", outfit="", features="", stored=stored)


def test_update_rejects_blank_name(db):
    row = _character(db)
    with pytest.raises(ValueError, match="名称不能为空"):
        _service(db).update(row.id, name="  ")


def test_replace_portrait_swaps_file_and_deletes_the_old_one(db, tmp_path):
    row = _character(db)
    old_rel = row.rel_path
    assert (tmp_path / old_rel).is_file()

    stored = store_character_image(OWNER, PNG)
    updated = _service(db).replace_portrait(row.id, stored)

    assert updated.rel_path == stored.rel_path
    assert not (tmp_path / old_rel).exists(), "旧形象图（含缩略图）应 best-effort 清掉"
    assert (tmp_path / stored.rel_path).is_file()


def test_characters_for_drops_unknown_and_foreign_ids(db):
    row = _character(db, owner_id=OWNER)
    _character(db, owner_id=OTHER, name="别人的角色")

    found = _service(db).characters_for([str(row.id), "999999", "not-an-int"])
    assert set(found) == {str(row.id)}
    assert _service(db).characters_for([]) == {}


def _expire_character(db, *character_ids: int) -> None:
    """把删除时间调到撤销窗口之外，状态与「很久以前删的」一致。"""
    for character_id in character_ids:
        db.get(VideoCharacter, character_id).deleted_at = datetime(2000, 1, 1)
    db.commit()
    db.expire_all()


def test_remove_moves_to_trash_keeps_file_and_counts_references(db, tmp_path):
    row = _character(db)
    project = _project(db, character_ids=[str(row.id)])
    # 另一个不相关的项目不该被算进引用数。
    _project(db, character_ids=[])

    references = _service(db).remove(row.id)

    assert references == 1
    # 软删：正常读路径看不见，但行还在、文件还在、项目引用原样保留。
    with pytest.raises(LookupError):
        _service(db).get_character(row.id)
    assert (tmp_path / row.rel_path).is_file()
    assert _service(db).list_characters()[1] == 0
    assert _service(db).trash_count() == 1
    db.refresh(project)
    assert project.character_ids == [str(row.id)]
    # 撤销窗口内 characters_for 仍能解析（与素材 materials_for 同口径），撤销才无损。
    assert str(row.id) in _service(db).characters_for([str(row.id)])


def test_restore_brings_a_trashed_character_back(db):
    row = _character(db)
    _service(db).remove(row.id)
    assert _service(db).list_characters()[1] == 0

    assert _service(db).restore_many([row.id]) == 1
    assert _service(db).get_character(row.id).id == row.id
    assert _service(db).trash_count() == 0
    # 已经在列表里的再恢复是空操作。
    assert _service(db).restore_many([row.id]) == 0


def test_purge_after_window_deletes_row_and_file(db, tmp_path):
    row = _character(db)
    rel_path = row.rel_path
    _service(db).remove(row.id)
    assert (tmp_path / rel_path).is_file()

    _expire_character(db, row.id)
    # 下次列表读取顺手懒清理。
    assert _service(db).list_characters()[1] == 0
    db.expire_all()
    assert db.get(VideoCharacter, row.id) is None
    assert not (tmp_path / rel_path).exists()


def test_restore_respects_the_owner_cap(db, tmp_path, monkeypatch):
    """名额被占满后，撤销不能把人物库撑过上限，先恢复的那批才进得来。"""
    monkeypatch.setattr(
        character_service, "settings", _fake_settings(tmp_path, CHARACTER_MAX_PER_OWNER=1)
    )
    first = _character(db)
    _service(db).remove(first.id)
    _character(db, name="占满名额的新人")

    assert _service(db).restore_many([first.id]) == 0
    assert _service(db).list_characters()[1] == 1
    assert _service(db).trash_count() == 1


def test_usage_lists_referencing_projects(db):
    first = _character(db, name="林小夏")
    second = _character(db, name="老周")
    project = _project(db, character_ids=[str(first.id), str(second.id)])

    usage = _service(db).usage([first.id, second.id])
    by_id = {item.character_id: item for item in usage}
    assert by_id[first.id].projects[0].project_id == project.id
    assert by_id[first.id].projects[0].title == project.title
    assert by_id[second.id].projects[0].project_id == project.id


# ---- HTTP 层 ---------------------------------------------------------------


def test_api_create_character_envelope(client):
    with io.BytesIO(_real_png()) as handle:
        body = client.post(
            "/api/office/characters",
            files={"file": ("林小夏.png", handle, "image/png")},
            data={"name": "林小夏", "persona": "运营", "outfit": "西装", "features": "短发"},
        ).json()

    assert body["code"] == 0
    data = body["data"]
    assert data["name"] == "林小夏"
    assert data["persona"] == "运营"
    assert data["url"].startswith("/uploads/characters/")
    assert data["thumb_url"].endswith("_thumb.webp")


def test_api_rejects_non_image_and_blank_name(client):
    body = client.post(
        "/api/office/characters",
        files={"file": ("x.mp4", MP4, "video/mp4")},
        data={"name": "视频角色"},
    ).json()
    assert body["code"] == -1 and "只支持" in body["message"]

    body = client.post(
        "/api/office/characters",
        files={"file": ("x.png", PNG, "image/png")},
        data={"name": "   "},
    ).json()
    assert body["code"] == -1 and "名称不能为空" in body["message"]


def test_api_update_replace_and_delete(client):
    with io.BytesIO(PNG) as handle:
        created = client.post(
            "/api/office/characters",
            files={"file": ("a.png", handle, "image/png")},
            data={"name": "老周"},
        ).json()["data"]

    body = client.put(
        f"/api/office/characters/{created['id']}",
        json={"persona": "资深工程师", "outfit": "格纹衬衫"},
    ).json()
    assert body["data"]["persona"] == "资深工程师"

    with io.BytesIO(PNG) as handle:
        body = client.put(
            f"/api/office/characters/{created['id']}/portrait",
            files={"file": ("b.png", handle, "image/png")},
        ).json()
    assert body["code"] == 0 and body["data"]["url"] != created["url"]

    listed = client.get("/api/office/characters").json()["data"]
    assert listed["total"] == 1 and listed["max_per_owner"] == settings.CHARACTER_MAX_PER_OWNER
    # 列表每条带项目引用数，回收站角标初始为 0。
    assert listed["items"][0]["project_count"] == 0
    assert listed["trash_count"] == 0

    body = client.delete(f"/api/office/characters/{created['id']}").json()
    assert body["code"] == 0 and body["data"]["references"] == 0
    assert "回收站" in body["message"]
    assert client.get("/api/office/characters").json()["data"]["total"] == 0

    # 回收站里能看到它并撤销恢复。
    trash = client.get("/api/office/characters/trash").json()["data"]
    assert trash["total"] == 1 and trash["trash_ttl_seconds"] == 600
    item = trash["items"][0]
    assert item["id"] == created["id"] and 0 < item["expires_in_seconds"] <= 600
    restored = client.post(
        "/api/office/characters/batch-restore", json={"ids": [created["id"]]}
    ).json()
    assert restored["code"] == 0
    assert client.get("/api/office/characters").json()["data"]["total"] == 1
    assert client.get("/api/office/characters/trash").json()["data"]["total"] == 0


def test_api_unknown_character_is_404(client):
    response = client.put("/api/office/characters/999999", json={"name": "谁"})
    assert response.status_code == 404


def test_api_usage_endpoint(client):
    with io.BytesIO(PNG) as handle:
        created = client.post(
            "/api/office/characters",
            files={"file": ("a.png", handle, "image/png")},
            data={"name": "林小夏"},
        ).json()["data"]

    body = client.post(
        "/api/office/characters/usage", json={"ids": [created["id"]]}
    ).json()
    assert body["code"] == 0 and body["data"][0]["character_id"] == created["id"]


# ---- AI 三视图形象预览 -------------------------------------------------------


def _image_model(db, *, tested: bool = True) -> ModelConfig:
    config = ModelConfig(
        owner_id=OWNER,
        purpose=ModelPurpose.IMAGE.value,
        title="测试图片模型",
        model_name="image-unit-test",
        base_url="http://localhost:1/api/v3",
        api_key_enc=encrypt("sk-image"),
        is_default=True,
        last_test_ok=tested,
    )
    db.add(config)
    db.commit()
    return config


def test_turnaround_prompt_carries_three_views_and_description():
    prompt = build_turnaround_prompt("  28 岁女老师，爱穿风衣  ")
    # 三个视角与「同一人」约束是模板的立身之本，关键词丢了等于功能失效。
    for keyword in ("正面", "侧面", "背面", "全身", "turnaround"):
        assert keyword in prompt
    # 入口 strip 过的描述不该带首尾空白，且必须原样接在模板后面。
    assert "28 岁女老师，爱穿风衣" in prompt
    assert "  28 岁" not in prompt
    # 纯文字模式用文生图开场（硬性要求里的「角色参考图」字样两种模式都有，不作区分），
    # 描述挂在「角色设定」段。
    assert "请根据参考图" not in prompt and "角色设定：" in prompt


def test_turnaround_prompt_with_reference_pins_identity_and_allows_blank():
    prompt = build_turnaround_prompt("保持微笑", has_reference=True)
    # 参考图模式先钉死「与参考图同一个人」，描述降为补充要求。
    for keyword in ("参考图", "正面", "侧面", "背面", "同一个人"):
        assert keyword in prompt
    assert "补充要求：" in prompt and "保持微笑" in prompt
    # 描述可空：空描述时整段不渲染，也不许出现 Python 的 None 字样。
    blank = build_turnaround_prompt("   ", has_reference=True)
    assert "补充要求" not in blank and "None" not in blank


def _patch_ark(monkeypatch, *, artifact: bytes | None = None):
    """把方舟两件套换成记录调用的假实现，返回捕获 dict。"""
    captured: dict = {}
    png = artifact if artifact is not None else _real_png()

    async def fake_generate(config, *, prompt, size, images=(), **kwargs):
        captured.update(prompt=prompt, size=size, images=list(images))
        return ark.ImageResult(url="https://ark.example.com/sheet.png", size="2048x2048")

    async def fake_download(url, *, max_bytes, timeout=None):
        assert url == "https://ark.example.com/sheet.png"
        return png

    monkeypatch.setattr(ark, "generate_image", fake_generate)
    monkeypatch.setattr(ark, "download", fake_download)
    return captured


def test_portrait_preview_text_mode_returns_image_bytes(client, db, monkeypatch):
    _image_model(db)
    png = _real_png()
    captured = _patch_ark(monkeypatch, artifact=png)

    # 不带文件就是纯文生图：multipart 只有 prompt 字段。
    response = client.post(
        "/api/office/characters/portrait-preview",
        data={"prompt": "短发女教师，米色风衣"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    # no-store：预览图没有复用价值，别让浏览器/代理缓存占盘（与 TTS 试听同口径）。
    assert response.headers["cache-control"] == "no-store"
    assert response.content == png
    # 2K 档位与首帧渲染同档；纯文字模式不带任何参考图，描述原样进上游 prompt。
    assert captured["size"] == "2K"
    assert captured["images"] == []
    assert "请根据参考图" not in captured["prompt"]
    assert "短发女教师，米色风衣" in captured["prompt"]
    for keyword in ("正面", "侧面", "背面"):
        assert keyword in captured["prompt"]


def test_portrait_preview_sends_reference_as_data_url(client, db, monkeypatch):
    _image_model(db)
    captured = _patch_ark(monkeypatch)

    with io.BytesIO(_real_png()) as handle:
        response = client.post(
            "/api/office/characters/portrait-preview",
            files={"file": ("ref.png", handle, "image/png")},
            data={"prompt": " 保持圆脸与风衣配色 "},
        )
    assert response.status_code == 200
    # 参考图与首帧渲染同一条通道：读本地字节转 base64 data URL 内联进请求体。
    assert len(captured["images"]) == 1
    assert captured["images"][0].startswith("data:image/png;base64,")
    # 图生图模式的提示词必须带「参考图同一个人」约束，补充描述原样接在后面。
    assert "参考图" in captured["prompt"]
    assert "保持圆脸与风衣配色" in captured["prompt"]


def test_portrait_preview_accepts_reference_with_blank_prompt(client, db, monkeypatch):
    _image_model(db)
    captured = _patch_ark(monkeypatch)

    with io.BytesIO(_real_png()) as handle:
        response = client.post(
            "/api/office/characters/portrait-preview",
            files={"file": ("ref.png", handle, "image/png")},
            data={"prompt": "   "},
        )
    assert response.status_code == 200
    assert len(captured["images"]) == 1


def test_portrait_preview_requires_image_or_prompt(client, db):
    _image_model(db)
    # 两个依据都不给时在进上游之前拦下，省一次真计费调用。
    response = client.post(
        "/api/office/characters/portrait-preview", data={"prompt": "   "}
    )
    assert response.status_code == 400
    assert "参考图" in response.json()["message"]


def test_portrait_preview_requires_usable_image_config(client, db):
    # 没配图片模型：明确告诉去设置页，而不是把上游 401 抛给用户。
    response = client.post(
        "/api/office/characters/portrait-preview", data={"prompt": "一个角色"}
    )
    assert response.status_code == 400
    assert "图片" in response.json()["message"]

    _image_model(db, tested=False)
    response = client.post(
        "/api/office/characters/portrait-preview", data={"prompt": "一个角色"}
    )
    assert response.status_code == 400
    assert "连通性测试" in response.json()["message"]


def test_portrait_preview_rejects_non_image_reference(client, db):
    _image_model(db)
    # 视频容器没有入口，魔数判型在写任何上游请求之前拒绝。
    with io.BytesIO(MP4) as handle:
        response = client.post(
            "/api/office/characters/portrait-preview",
            files={"file": ("ref.mp4", handle, "video/mp4")},
            data={"prompt": ""},
        )
    assert response.status_code == 400
    assert "参考图" in response.json()["message"]

    # 识别不出的字节同样拒绝，不把 MediaFileError 抛成 500。
    with io.BytesIO(NOT_MEDIA) as handle:
        response = client.post(
            "/api/office/characters/portrait-preview",
            files={"file": ("ref.bin", handle, "application/octet-stream")},
            data={"prompt": ""},
        )
    assert response.status_code == 400
    assert "参考图" in response.json()["message"]


def test_portrait_preview_rejects_oversize_reference(client, db, monkeypatch):
    _image_model(db)
    # 把闸门压到 128 字节：真实 PNG 必然超限，read_upload_limited 返回 None。
    monkeypatch.setattr(
        "app.routers.characters.settings",
        SimpleNamespace(MATERIAL_MAX_IMAGE_BYTES=128, MEDIA_IMAGE_MAX_BYTES=128),
    )
    with io.BytesIO(_real_png()) as handle:
        response = client.post(
            "/api/office/characters/portrait-preview",
            files={"file": ("big.png", handle, "image/png")},
            data={"prompt": ""},
        )
    assert response.status_code == 400
    assert "上限" in response.json()["message"]


def test_portrait_preview_translates_ark_error(client, db, monkeypatch):
    _image_model(db)

    async def broken(config, *, prompt, size, images=(), **kwargs):
        raise ark.ArkError("提示词未通过内容安全审核（ContentRisk）", code="ContentRisk")

    monkeypatch.setattr(ark, "generate_image", broken)
    response = client.post(
        "/api/office/characters/portrait-preview", data={"prompt": "敏感描述"}
    )
    assert response.status_code == 400
    assert "ContentRisk" in response.json()["message"]


def test_portrait_preview_rejects_non_image_payload(client, db, monkeypatch):
    _image_model(db)
    _patch_ark(monkeypatch, artifact=NOT_MEDIA)

    response = client.post(
        "/api/office/characters/portrait-preview", data={"prompt": "一个角色"}
    )
    assert response.status_code == 400
    assert "不是图片" in response.json()["message"]
