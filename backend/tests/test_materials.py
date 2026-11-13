"""素材库：落盘判定、账号级存取、引用解绑，以及旧项目参考图列的退役。

三条线各管一段：

* ``store_material`` 只碰磁盘，钉的是「魔数定类型、超限与不认识的内容都不留半个文件」；
* ``MaterialService`` 只碰数据库，钉隔离、筛选、条数闸门与删素材时的引用反查；
* HTTP 层钉信封与答复文案，最后那段钉启动时「只改表形状、不搬数据」，与
  ``test_videos.py`` 里镜头侧的引用校验正好接上。

产物目录一律指到 pytest 的临时目录，不碰真实部署的 uploads/。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
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


def test_remove_deletes_the_row_and_unbinds_the_shots(db):
    """删素材得跟着解掉镜头引用：留着就是指向一个已经不存在的文件。"""
    project = _project(db)
    gone = _add(db)
    kept = _add(db, "片头.mp4", MP4)
    first = _shot(db, project.id, [str(gone.id), str(kept.id)])
    # 一条素材常被多镜复用（同一个人物形象贯穿全片），解绑要按引用逐条来。
    second = _shot(db, project.id, [str(gone.id)])

    rel_path, unbound = _service(db).remove(gone.id)

    assert (rel_path, unbound) == (gone.rel_path, 2)
    assert db.get(MaterialAsset, gone.id) is None
    db.expire_all()
    assert first.material_ids == [str(kept.id)]
    assert second.material_ids == []
    with pytest.raises(LookupError):
        _service(db).remove(gone.id)


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


def test_delete_http_reports_the_unbinding(client, db):
    project = _project(db)
    asset_id = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]
    shot = _shot(db, project.id, [str(asset_id)])

    body = client.delete(f"/api/office/materials/{asset_id}").json()
    assert body["message"] == "已删除，1 个镜头去掉这条引用"
    db.expire_all()
    assert shot.material_ids == []

    assert client.delete(f"/api/office/materials/{asset_id}").status_code == 404


def test_delete_http_removes_the_file_too(client, tmp_path):
    asset_id = client.post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]
    # 删除按库里的 rel_path 走，URL 是现拼的，所以从这里反推回那个文件。
    rel_path = client.get("/api/office/materials").json()["data"]["items"][0]["url"].removeprefix(
        "/uploads/"
    )

    client.delete(f"/api/office/materials/{asset_id}")

    assert not (tmp_path / rel_path).exists()


def test_another_account_cannot_delete_or_see(client_for):
    asset_id = client_for(OWNER).post(
        "/api/office/materials", files={"file": ("主角形象.png", PNG, "image/png")}
    ).json()["data"]["id"]
    other = client_for(OTHER)

    assert other.get("/api/office/materials").json()["data"]["total"] == 0
    assert other.delete(f"/api/office/materials/{asset_id}").status_code == 404


# ---- 旧参考图列的退役 ---------------------------------------------------------


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
