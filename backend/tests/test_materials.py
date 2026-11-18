"""素材库：落盘判定、账号级存取、引用解绑，以及旧项目参考图列的退役。

三条线各管一段：

* ``store_material`` 只碰磁盘，钉的是「魔数定类型、超限与不认识的内容都不留半个文件」；
* ``MaterialService`` 只碰数据库，钉隔离、筛选、条数闸门与删素材时的引用反查；
* HTTP 层钉信封与答复文案，最后那段钉启动时「只改表形状、不搬数据」，与
  ``test_videos.py`` 里镜头侧的引用校验正好接上。

产物目录一律指到 pytest 的临时目录，不碰真实部署的 uploads/。
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import bootstrap
from app.config import get_settings
from app.database import Base, get_db
from app.deps import require_user
from app.main import app
from app.models.entities import MaterialAsset, VideoProject, VideoShot
from app.models.schemas import CurrentUser
from app.services.materials import KIND_IMAGE, KIND_VIDEO
from app.services.materials import service as material_service
from app.services.materials import store as material_store
from app.services.materials.service import MaterialService
from app.services.materials.store import store_material
from app.services.media import store as media_store
from app.services.media.store import MediaFileError

settings = get_settings()
OWNER = 1
OTHER = 2

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32
WEBM = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1f" + b"0" * 32
NOT_MEDIA = b"<html><script>alert(1)</script></html>"


def _fake_settings(root, **overrides):
    """一份够用就行的 settings：三个模块共用，属性按各自用到的那几项给。"""
    values = {
        "upload_media_path": root,
        "UPLOAD_MEDIA_URL": "/uploads",
        "MATERIAL_MAX_IMAGE_BYTES": settings.MATERIAL_MAX_IMAGE_BYTES,
        "MATERIAL_MAX_VIDEO_BYTES": settings.MATERIAL_MAX_VIDEO_BYTES,
        "MATERIAL_MAX_PER_OWNER": settings.MATERIAL_MAX_PER_OWNER,
        "MATERIAL_TRASH_TTL_SECONDS": settings.MATERIAL_TRASH_TTL_SECONDS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture()
def engine(tmp_path):
    # 文件库：退役那一段要在库上手工改列，再让 bootstrap 自己连上来改回去。
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'materials.db'}", connect_args={"check_same_thread": False}
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
    monkeypatch.setattr(material_store, "settings", fake)
    monkeypatch.setattr(material_service, "settings", fake)
    return tmp_path


@pytest.fixture()
def db(engine, monkeypatch):
    maker = sessionmaker(bind=engine, future=True)
    session = maker()
    # TestClient 里 BackgroundTasks 与启动迁移都会自开 session，指到同一个文件库才看得见彼此。
    monkeypatch.setattr("app.database.SessionLocal", maker)
    monkeypatch.setattr(bootstrap, "SessionLocal", maker)
    monkeypatch.setattr(bootstrap, "engine", engine, raising=False)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client_for(db):
    """工厂：按身份取 TestClient。依赖覆盖是 app 上的全局字典，所以取 client 时
    最后一次设定的身份对之后所有请求生效——用例里不要交错使用两个身份发请求。"""

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


def _service(db, owner_id: int = OWNER) -> MaterialService:
    return MaterialService(db, owner_id)


def _add(db, name: str = "主角形象.png", raw: bytes = PNG) -> MaterialAsset:
    return _service(db).add(store_material(OWNER, raw), name)


def _project(db) -> VideoProject:
    """一行够镜头挂上来的项目；这里不测项目本身，它只在 project_id 那一列上出现。"""
    project = VideoProject(owner_id=OWNER, title="季度复盘三步", idea="教打工人做复盘")
    db.add(project)
    db.commit()
    return project


def _shot(db, project_id: int, material_ids: list[str]) -> VideoShot:
    """一行镜头：只造到「引用了哪几条素材」够用就行，渲染逻辑在 test_videos.py 钉。"""
    row = VideoShot(
        project_id=project_id,
        owner_id=OWNER,
        sort_order=1,
        image_prompt="竖屏，年轻职员抬头看镜头",
        material_ids=material_ids,
    )
    db.add(row)
    db.commit()
    return row


def _expire(db, *asset_ids: int) -> None:
    """把删除时间调到撤销窗口之外：状态与「用户十小时前删的」一致。"""
    for asset_id in asset_ids:
        row = db.get(MaterialAsset, asset_id)
        row.deleted_at = datetime(2000, 1, 1)
    db.commit()
    db.expire_all()


# ---- 落盘 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,kind,mime,extension",
    [(PNG, KIND_IMAGE, "image/png", ".png"),
     (MP4, KIND_VIDEO, "video/mp4", ".mp4"),
     (WEBM, KIND_VIDEO, "video/webm", ".webm")],
)
def test_store_material_decides_kind_from_the_magic_bytes(raw, kind, mime, extension, tmp_path):
    stored = store_material(OWNER, raw)

    assert (stored.kind, stored.mime, stored.size) == (kind, mime, len(raw))
    # 按账号分目录，不是按项目：换选题还要能接着用这条素材。
    assert stored.rel_path.startswith(f"materials/{OWNER}/")
    assert stored.rel_path.endswith(extension)
    assert stored.url == f"/uploads/{stored.rel_path}"
    assert (tmp_path / stored.rel_path).read_bytes() == raw


def test_store_material_writes_nothing_it_cannot_classify(tmp_path):
    with pytest.raises(MediaFileError, match="只支持"):
        store_material(OWNER, NOT_MEDIA)
    assert not (tmp_path / "materials").exists(), "拒绝必须发生在建目录之前"


def test_store_material_gates_size_per_kind(tmp_path, monkeypatch):
    """图片那一档比视频紧：一段 3MB 的素材不该被当成一张图收下，反之亦然。"""
    monkeypatch.setattr(
        material_store, "settings", _fake_settings(tmp_path, MATERIAL_MAX_IMAGE_BYTES=1024)
    )

    with pytest.raises(MediaFileError, match="超过"):
        store_material(OWNER, PNG + b"0" * 4096)
    stored = store_material(OWNER, MP4 + b"0" * 4096)
    assert stored.kind == KIND_VIDEO


def test_media_guard_reads_material_paths(tmp_path):
    """素材能被媒体层读出来：首帧渲染要把选中的图内联成 base64 送给方舟。

    这条跨模块的约定不钉住的话，``render_frames`` 入队时一切正常，失败要到作业
    执行器读文件那一刻才冒出来。
    """
    stored = store_material(OWNER, PNG)

    assert media_store.is_safe_rel_path(stored.rel_path)
    assert media_store.resolve_url(stored.rel_path) == stored.url
    assert media_store.read_blob(stored.rel_path) == PNG


# ---- 缩略图 -----------------------------------------------------------------


def _real_png(width: int = 2048, height: int = 1152) -> bytes:
    """一张真的能被 Pillow 打开的图：首帧就是 2K PNG，缩略图要吃得住这个档。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (18, 52, 86)).save(buffer, "PNG")
    return buffer.getvalue()


def test_store_material_generates_a_webp_thumbnail_for_images(tmp_path):
    """图片落盘顺手生成 320px 的 WebP 缩略图；视频不生成。

    列表一屏几十张原图（每张好几 MB）同时进网络就是卡的根因；320px 的 WebP
    一张几十 KB，差出两个数量级。视频走 ``preload=metadata`` 本来就只取首帧
    元数据，没有全量字节的问题要解。
    """
    stored = store_material(OWNER, _real_png())

    thumb = material_store.thumbnail_url(stored.rel_path, KIND_IMAGE)
    assert thumb.startswith(f"/uploads/materials/{OWNER}/") and thumb.endswith("_thumb.webp")
    rel_thumb = thumb.removeprefix("/uploads/")
    with Image.open(tmp_path / rel_thumb) as image:
        assert image.format == "WEBP"
        assert image.size == (320, 180), "最长边压到 320，2048x1152 等比缩到 320x180"
    # 缩略图是读方（StaticFiles）认的白名单形状，不能靠猜后缀挡掉。
    assert media_store.is_safe_rel_path(rel_thumb)
    assert media_store.read_blob(rel_thumb)[:4] == b"RIFF"
    # 已在盘上时 ensure 只做一次 stat，不再动它。
    assert material_store.ensure_thumbnail(stored.rel_path, KIND_IMAGE) == thumb

    video = store_material(OWNER, MP4)
    assert material_store.thumbnail_url(video.rel_path, KIND_VIDEO) == ""
    assert material_store.ensure_thumbnail(video.rel_path, KIND_VIDEO) == ""


def test_thumbnail_generation_falls_back_safely(tmp_path):
    """魔数对但内容不是图的文件生成不了缩略图：退回空串让界面回退原图。

    缩略图是提速件，坏了不该把列表也带走；盘上也不留半个文件。
    """
    stored = store_material(OWNER, PNG)

    assert material_store.ensure_thumbnail(stored.rel_path, KIND_IMAGE) == ""
    assert material_store.thumbnail_url(stored.rel_path, KIND_IMAGE) == ""
    assert [p.name for p in (tmp_path / "materials" / str(OWNER)).iterdir()] == [
        stored.rel_path.rsplit("/", 1)[-1]
    ]


# ---- 服务层 -----------------------------------------------------------------


def test_add_uses_the_given_name_and_falls_back_to_the_file(db):
    named = _service(db).add(store_material(OWNER, PNG), "  主角形象  ")
    blank = _service(db).add(store_material(OWNER, MP4), "   ")

    assert named.name == "主角形象"
    # 没给名字就用文件名，界面至少说得出这是哪个文件。
    assert blank.name == blank.rel_path.rsplit("/", 1)[-1]
    assert (blank.kind, blank.size) == (KIND_VIDEO, len(MP4))


def test_add_refuses_once_the_account_is_full(db, tmp_path, monkeypatch):
    monkeypatch.setattr(material_service, "settings", _fake_settings(tmp_path, MATERIAL_MAX_PER_OWNER=1))
    _add(db)

    stored = store_material(OWNER, PNG)
    with pytest.raises(ValueError, match="最多存 1 条"):
        _service(db).add(stored, "第二条.png")
    # 闸门只管数据库；文件是调用方（路由）写下的，孤儿由它清理。
    assert (tmp_path / stored.rel_path).exists()


def test_list_filters_by_kind_and_searches_the_name_literally(db):
    first = _add(db, "主角形象.png")
    second = _add(db, "片头.mp4", MP4)
    third = _add(db, "完成度 100%.png")

    rows, total, bytes_total = _service(db).list_assets()
    assert [row.id for row in rows] == [third.id, second.id, first.id], "新的在前"
    assert total == 3
    assert bytes_total == sum(row.size for row in (first, second, third))

    assert [row.id for row in _service(db).list_assets(kind=KIND_VIDEO)[0]] == [second.id]
    assert [row.id for row in _service(db).list_assets(keyword="主角")[0]] == [first.id]
    # 不转义的话 '%' 匹配一切；这里按字面量找，所以只有那条带百分号的命中。
    assert [row.id for row in _service(db).list_assets(keyword="%")[0]] == [third.id]
    # 白名单外的 kind 按「全部」处理，不是报错。
    assert _service(db).list_assets(kind="audio")[1] == 3


def test_list_pages_without_losing_the_total(db):
    ids = [_add(db, f"素材{i}.png").id for i in range(3)]

    rows, total, _ = _service(db).list_assets(limit=2, offset=2)

    assert [row.id for row in rows] == [ids[0]]
    assert total == 3, "total 跟着筛选走、不跟分页走，前端才数得对"


def test_another_account_sees_nothing(db):
    asset = _add(db)

    stranger = _service(db, OTHER)
    assert stranger.list_assets()[1] == 0
    assert stranger.materials_for([str(asset.id)]) == {}
    with pytest.raises(LookupError, match="素材不存在"):
        stranger.get_asset(asset.id)
    with pytest.raises(LookupError):
        _service(db).get_asset(asset.id + 9999)


def test_materials_for_tolerates_garbage_ids(db):
    asset = _add(db)

    assert set(_service(db).materials_for([str(asset.id), "不是数字", ""])) == {str(asset.id)}


def test_remove_moves_the_row_to_trash_and_keeps_the_shots(db):
    """删除先进回收期：行、文件与镜头引用都原样留着，只是列表不再看它。

    反过来先解绑的话，「撤销」就得把镜头引用一条条补回来，还不知道期间用户有没有
    改过；把解绑推迟到过期清理那一刻，撤销就只剩一个字段。
    """
    project = _project(db)
    gone = _add(db)
    kept = _add(db, "片头.mp4", MP4)
    first = _shot(db, project.id, [str(gone.id), str(kept.id)])
    # 一条素材常被多镜复用（同一个人物形象贯穿全片），引用数要按镜头逐条算。
    second = _shot(db, project.id, [str(gone.id)])

    references = _service(db).remove(gone.id)

    assert references == 2
    db.expire_all()
    row = db.get(MaterialAsset, gone.id)
    assert row is not None and row.deleted_at is not None
    assert first.material_ids == [str(gone.id), str(kept.id)]
    assert second.material_ids == [str(gone.id)]
    # 列表、计数与占用都不再算它：删掉一条就等于立刻腾出一个名额。
    rows, total, bytes_total = _service(db).list_assets()
    assert [item.id for item in rows] == [kept.id]
    assert total == 1 and bytes_total == kept.size
    # 已经删过的再删一次与不存在的 id 同一种答案。
    with pytest.raises(LookupError):
        _service(db).remove(gone.id)


def test_restore_brings_a_trashed_asset_back(db):
    asset = _add(db)
    _service(db).remove(asset.id)
    assert _service(db).list_assets()[1] == 0

    assert _service(db).restore_many([asset.id]) == 1

    assert [row.id for row in _service(db).list_assets()[0]] == [asset.id]
    db.expire_all()
    assert db.get(MaterialAsset, asset.id).deleted_at is None
    # 已经在列表里的再恢复是空操作，不该报错，也不该重复计数。
    assert _service(db).restore_many([asset.id]) == 0


def test_purge_after_the_window_drops_the_row_file_and_references(db, tmp_path):
    """过了撤销窗口：删行、删文件（含缩略图）、解掉镜头引用，一次做齐。"""
    project = _project(db)
    asset = _service(db).add(store_material(OWNER, _real_png()), "主角形象.png")
    shot = _shot(db, project.id, [str(asset.id)])
    rel_thumb = material_store.thumbnail_url(asset.rel_path, asset.kind).removeprefix("/uploads/")
    assert (tmp_path / asset.rel_path).exists()
    assert (tmp_path / rel_thumb).exists()

    _service(db).remove(asset.id)
    # 窗口内文件还在：撤销要能恢复到「和删之前一模一样」。
    assert (tmp_path / asset.rel_path).exists()
    assert (tmp_path / rel_thumb).exists()

    _expire(db, asset.id)
    assert _service(db).list_assets()[1] == 0, "列表顺手做懒清理"

    db.expire_all()
    assert db.get(MaterialAsset, asset.id) is None
    assert db.get(VideoShot, shot.id).material_ids == []
    assert not (tmp_path / asset.rel_path).exists()
    assert not (tmp_path / rel_thumb).exists()


def test_trash_frees_a_quota_slot_and_restore_respects_the_cap(db, tmp_path, monkeypatch):
    """删掉就腾名额；名额被别人占了之后，撤销不能再把库撑过上限。"""
    monkeypatch.setattr(material_service, "settings", _fake_settings(tmp_path, MATERIAL_MAX_PER_OWNER=1))
    first = _add(db)
    _service(db).remove(first.id)
    _add(db, "新片头.png")

    # 名额已满，这条留在回收期等过期清理，而不是把账号撑到 2 条。
    assert _service(db).restore_many([first.id]) == 0
    assert _service(db).list_assets()[1] == 1


def test_tag_is_normalized_on_write_and_searchable(db):
    """打标分组：空白即摘掉、超长即截断，名字与标签两条检索路都得能命中。"""
    service = _service(db)
    asset = _add(db)
    assert asset.tag is None, "直接上传的素材没打标，列表按时间分组"

    assert service.set_tag(asset.id, "  季度复盘  ").tag == "季度复盘"
    # 空白标签归一回「没打标」：两种分组之间不留「空标签」这个第三态。
    assert service.set_tag(asset.id, "   ").tag is None
    assert service.set_tag(asset.id, "长" * 200).tag == "长" * 128

    second = _add(db, "片头.mp4", MP4)
    service.set_tag(second.id, "年度大片")
    # 搜分组标签命中打标的那条，搜名字照旧命中名字那条。
    assert [row.id for row in service.list_assets(keyword="年度大片")[0]] == [second.id]
    assert [row.id for row in service.list_assets(keyword="片头")[0]] == [second.id]
    # 越界的 id 读作不存在，与打错的 id 同一种答案。
    with pytest.raises(LookupError):
        service.set_tag(asset.id + 9999, "x")


# ---- HTTP 层 ----------------------------------------------------------------


def test_upload_then_list_http(client):
    uploaded = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()
    assert uploaded["message"] == "已存入素材库"
    assert uploaded["data"]["kind"] == KIND_IMAGE
    assert uploaded["data"]["url"].startswith("/uploads/materials/1/")

    body = client.get("/api/office/materials").json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["name"] == "主角形象.png"
    assert body["bytes_total"] == len(PNG)
    assert body["max_per_owner"] == settings.MATERIAL_MAX_PER_OWNER


def test_upload_rejects_an_html_file_dressed_as_png(client):
    body = client.post(
        "/api/office/materials", files={"file": ("x.png", NOT_MEDIA, "image/png")}
    ).json()
    assert body["code"] == -1 and "只支持" in body["message"]
    assert client.get("/api/office/materials").json()["data"]["total"] == 0


def test_upload_rejects_an_image_over_its_own_gate(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        material_store, "settings", _fake_settings(tmp_path, MATERIAL_MAX_IMAGE_BYTES=1024)
    )

    body = client.post(
        "/api/office/materials", files={"file": ("大图.png", PNG + b"0" * 4096, "image/png")}
    ).json()
    assert body["code"] == -1 and "超过" in body["message"]


def test_delete_http_reports_the_references_and_the_undo_window(client, db):
    project = _project(db)
    asset_id = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]
    shot = _shot(db, project.id, [str(asset_id)])

    body = client.delete(f"/api/office/materials/{asset_id}").json()
    assert body["message"] == "已删除，10 分钟内可撤销；1 个镜头还在引用"
    # 回收期里引用不动，撤销才能原样回来。
    db.expire_all()
    assert shot.material_ids == [str(asset_id)]
    assert client.get("/api/office/materials").json()["data"]["total"] == 0

    # 已删过的不再是删除目标。
    assert client.delete(f"/api/office/materials/{asset_id}").status_code == 404


def test_delete_http_keeps_the_file_during_the_undo_window(client, tmp_path):
    asset_id = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]
    # 删除按库里的 rel_path 走，URL 是现拼的，所以从这里反推回那个文件。
    rel_path = client.get("/api/office/materials").json()["data"]["items"][0]["url"].removeprefix(
        "/uploads/"
    )

    client.delete(f"/api/office/materials/{asset_id}")

    assert (tmp_path / rel_path).exists(), "撤销窗口内文件必须还在，否则撤销回来的是空壳"


def test_another_account_cannot_delete_or_see(client_for):
    asset_id = client_for(OWNER).post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]
    other = client_for(OTHER)

    assert other.get("/api/office/materials").json()["data"]["total"] == 0
    assert other.delete(f"/api/office/materials/{asset_id}").status_code == 404


def test_tag_endpoint_updates_and_clears(client, db):
    asset_id = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]

    body = client.put(f"/api/office/materials/{asset_id}/tag", json={"tag": "季度复盘"}).json()
    assert body["message"] == "已更新分组"
    assert body["data"]["tag"] == "季度复盘"
    db.expire_all()
    assert db.get(MaterialAsset, asset_id).tag == "季度复盘"

    # 空白 = 摘掉标签，归回「按时间分组」那一边。
    body = client.put(f"/api/office/materials/{asset_id}/tag", json={"tag": "  "}).json()
    assert body["data"]["tag"] is None

    assert client.put(
        f"/api/office/materials/{asset_id + 9999}/tag", json={"tag": "x"}
    ).status_code == 404


def test_list_serves_the_thumbnail_url(client):
    """列表出参带缩略图 URL：界面按它渲染，空串时自己回退原图。"""
    client.post(
        "/api/office/materials", files={"file": ("主角形象.png", _real_png(), "image/png")}
    )

    item = client.get("/api/office/materials").json()["data"]["items"][0]
    assert item["thumb_url"].startswith(f"/uploads/materials/{OWNER}/")
    assert item["thumb_url"].endswith("_thumb.webp")
    assert item["tag"] is None


def test_list_http_purges_expired_trash_and_its_thumbnail(client, db, tmp_path):
    """过了撤销窗口的素材由下一次列表顺手清掉：行、缩略图、原图一起消失。"""
    client.post(
        "/api/office/materials", files={"file": ("主角形象.png", _real_png(), "image/png")}
    )
    data = client.get("/api/office/materials").json()["data"]
    item = data["items"][0]
    rel_path = item["url"].removeprefix("/uploads/")
    rel_thumb = item["thumb_url"].removeprefix("/uploads/")
    assert (tmp_path / rel_thumb).exists()

    client.delete(f"/api/office/materials/{item['id']}")
    _expire(db, item["id"])
    assert client.get("/api/office/materials").json()["data"]["total"] == 0

    db.expire_all()
    assert db.get(MaterialAsset, item["id"]) is None
    assert not (tmp_path / rel_path).exists()
    assert not (tmp_path / rel_thumb).exists()


# ---- 改名、批量操作与删除影响 -------------------------------------------------


def test_rename_trims_and_rejects_blank_name(client, db):
    asset_id = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]

    body = client.put(
        f"/api/office/materials/{asset_id}/name", json={"name": "  主角三视图.png  "}
    ).json()
    assert body["message"] == "已重命名"
    assert body["data"]["name"] == "主角三视图.png"
    db.expire_all()
    assert db.get(MaterialAsset, asset_id).name == "主角三视图.png"

    # 空白名字拒绝：名字是列表里唯一认得出这条素材的东西。
    assert client.put(
        f"/api/office/materials/{asset_id}/name", json={"name": "   "}
    ).status_code == 400
    assert client.put(
        f"/api/office/materials/{asset_id + 9999}/name", json={"name": "x"}
    ).status_code == 404


def test_batch_tag_updates_in_one_request(client, db):
    ids = [
        client.post(
            "/api/office/materials", files={"file": (f"主角{i}.png", PNG, "image/png")}
        ).json()["data"]["id"]
        for i in range(2)
    ]

    body = client.post(
        "/api/office/materials/batch-tag", json={"ids": ids, "tag": "季度复盘"}
    ).json()
    assert body["message"] == "已更新 2 条"
    assert [item["tag"] for item in body["data"]["items"]] == ["季度复盘", "季度复盘"]
    db.expire_all()
    assert [db.get(MaterialAsset, asset_id).tag for asset_id in ids] == ["季度复盘", "季度复盘"]

    # 空白 = 摘掉标签，与单条改分组同一口径。
    body = client.post(
        "/api/office/materials/batch-tag", json={"ids": ids, "tag": " "}
    ).json()
    assert [item["tag"] for item in body["data"]["items"]] == [None, None]
    assert client.post(
        "/api/office/materials/batch-tag", json={"ids": [], "tag": "x"}
    ).status_code == 422


def test_usage_lists_the_projects_and_shots_before_delete(client, db):
    asset = _add(db)
    project = _project(db)
    _shot(db, project.id, [str(asset.id)])

    body = client.post(
        "/api/office/materials/usage", json={"ids": [asset.id]}
    ).json()["data"][0]

    assert body["asset_id"] == asset.id
    assert body["projects"][0]["title"] == "季度复盘三步"
    assert [shot["sort_order"] for shot in body["projects"][0]["shots"]] == [1]
    # 没被引用的素材返回空影响面，界面照常显示「没有镜头引用」。
    assert client.post(
        "/api/office/materials/usage", json={"ids": [asset.id + 9999]}
    ).json()["data"][0]["projects"] == []


def test_batch_delete_trashes_and_restore_undoes_it(client, db):
    assets = [
        client.post(
            "/api/office/materials", files={"file": (f"参考{i}.png", PNG, "image/png")}
        ).json()["data"]["id"]
        for i in range(2)
    ]
    project = _project(db)
    shot = _shot(db, project.id, [str(asset_id) for asset_id in assets])

    body = client.post(
        "/api/office/materials/batch-delete", json={"ids": assets}
    ).json()
    assert body["data"] == {"deleted": 2, "references": 1, "undo_seconds": 600}
    assert body["message"] == "已删除 2 条素材，10 分钟内可撤销；1 个镜头还在引用"
    # 回收期内：行还在（只是带删除时间），镜头引用原样保留。
    db.expire_all()
    assert all(db.get(MaterialAsset, asset_id).deleted_at is not None for asset_id in assets)
    assert db.get(VideoShot, shot.id).material_ids == [str(asset_id) for asset_id in assets]
    assert client.get("/api/office/materials").json()["data"]["total"] == 0

    restored = client.post(
        "/api/office/materials/batch-restore", json={"ids": assets}
    ).json()
    assert restored["data"] == {"restored": 2}
    assert restored["message"] == "已恢复 2 条素材"
    assert client.get("/api/office/materials").json()["data"]["total"] == 2

    # 再恢复一次没有可恢复的：列入列表的不算回收期内。
    assert client.post(
        "/api/office/materials/batch-restore", json={"ids": assets}
    ).json()["data"] == {"restored": 0}


def test_batch_delete_unbinds_only_after_the_window(client, db):
    assets = [
        client.post(
            "/api/office/materials", files={"file": (f"参考{i}.png", PNG, "image/png")}
        ).json()["data"]["id"]
        for i in range(2)
    ]
    project = _project(db)
    shot = _shot(db, project.id, [str(asset_id) for asset_id in assets])

    client.post("/api/office/materials/batch-delete", json={"ids": assets})
    _expire(db, *assets)
    assert client.get("/api/office/materials").json()["data"]["total"] == 0

    db.expire_all()
    assert db.get(MaterialAsset, assets[0]) is None
    assert db.get(VideoShot, shot.id).material_ids == []


# ---- 旧参考图列的退役 ---------------------------------------------------------


def test_migrate_backfills_the_tag_column(db, engine):
    """升级前的库没有打标分组列与回收期列：启动时补上，已有行读作 NULL。

    与下面那两条退役列同一个口径：create_all 看不见已存在的表的新列，幂等
    ALTER 是旧库唯一的升级通道。
    """
    _add(db)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE material_asset DROP COLUMN tag"))
        conn.execute(text("ALTER TABLE material_asset DROP COLUMN deleted_at"))

    bootstrap.migrate_schema()

    columns = {column["name"] for column in inspect(engine).get_columns("material_asset")}
    assert {"tag", "deleted_at"} <= columns
    db.expire_all()
    row = db.scalar(select(MaterialAsset))
    assert row.tag is None
    assert row.deleted_at is None, "存量行都在正常可见状态，不是待清理的回收期条目"
    # 幂等：列已经在了，第二次启动是空操作。
    bootstrap.migrate_schema()


def _make_legacy_video_shape(db, engine):
    """把两张表退回升级前的形状：项目里一份参考图 JSON，镜头上一列旧的 asset 引用。

    只在 create_all 建好的表上改列、不整表重建：真实部署的库就是「别的列都还在，
    缺 material_ids、多 ref_asset_ids」，重建一张缺列的表测的却是另一件事。
    """
    old_id = "aa11bb22cc33dd44"
    project = _project(db)
    db.execute(text("ALTER TABLE video_shot DROP COLUMN material_ids"))
    db.execute(text("ALTER TABLE video_shot ADD COLUMN ref_asset_ids JSON"))
    db.execute(text("ALTER TABLE video_project ADD COLUMN assets JSON"))
    # 镜头行按 raw SQL 插：旧引用那一列 ORM 根本不认。start_s 等几列在 create_all 建的
    # 表上是 NOT NULL 无 DB 默认值（ORM 的默认值只在 Python 侧），所以得自己补齐。
    db.execute(
        text(
            "INSERT INTO video_shot (project_id, owner_id, sort_order, start_s, duration_s, "
            "gen_mode, image_prompt, ref_asset_ids) "
            "VALUES (:project_id, :owner_id, 1, 0, 5, 'first_frame', '首帧提示词', :refs)"
        ),
        {"project_id": project.id, "owner_id": OWNER, "refs": json.dumps([old_id])},
    )
    db.execute(
        text("UPDATE video_project SET assets = :assets WHERE id = :project_id"),
        {
            "project_id": project.id,
            "assets": json.dumps(
                [
                    {
                        "id": old_id,
                        "name": "主角形象.png",
                        "mime": "image/png",
                        "size": len(PNG),
                        "rel_path": f"videos/{OWNER}/{old_id}.png",
                    }
                ]
            ),
        },
    )
    db.commit()
    return project


def test_migrate_retires_the_legacy_reference_image_columns(db, engine, tmp_path):
    """启动时只改形状：删两条旧列、给镜头补上 material_ids，半个文件都不搬。

    存量库里没有参考图，而旧 asset id 指向的正是那种「跟着项目一起删」的文件——换算进
    素材库只会在工作台上留一格点不开的引用，所以直接丢掉，旧引用不折算成任何东西。
    """
    project = _make_legacy_video_shape(db, engine)

    bootstrap.migrate_schema()

    shot_cols = {column["name"] for column in inspect(engine).get_columns("video_shot")}
    project_cols = {column["name"] for column in inspect(engine).get_columns("video_project")}
    assert "material_ids" in shot_cols and "ref_asset_ids" not in shot_cols
    assert "assets" not in project_cols
    # 存量行的新列是空的：服务层一律按「这一镜没引用素材」读，而不是读到一串死引用。
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT material_ids FROM video_shot WHERE project_id = :project_id"),
            {"project_id": project.id},
        ).scalar() is None
    assert db.scalar(select(func.count(MaterialAsset.id))) == 0
    assert not (tmp_path / "materials").exists()

    # 幂等：两条旧列都没了，第二次启动是空操作——新列不会被再补一次，也不会被删掉。
    bootstrap.migrate_schema()
    assert {column["name"] for column in inspect(engine).get_columns("video_shot")} == shot_cols
