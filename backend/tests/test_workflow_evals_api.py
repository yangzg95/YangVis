"""/workflows 评分细则与双运行对比评测：HTTP 层 + 关键服务语义。

只钉这一层容易回退的东西：路由没被 ``/{workflow_id}`` 吞掉、统一信封、
快照冻结、四元组去重、归属隔离、以及被评测引用的运行在 prune 时硬豁免。
打分下标对齐等纯校验在服务里实现，这里走真实请求验返回码。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import require_user
from app.models.entities import Workflow, WorkflowEvaluation, WorkflowRun
from app.models.schemas import CurrentUser
from app.routers import workflows as workflows_router
from app.services.workflows.runs import prune_runs
from app.timeutil import db_now

OWNER = 1
OTHER = 2


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'evals.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.database.SessionLocal", Session)

    def make_client(owner: int) -> TestClient:
        app = FastAPI()
        app.include_router(workflows_router.router, prefix="/api")

        def override_get_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_user] = lambda: CurrentUser(
            user_id=owner, username=f"u{owner}"
        )
        return TestClient(app)

    return make_client(OWNER), Session, make_client


def _rubric_body(name="对比尺子", criteria=None):
    return {
        "name": name,
        "description": None,
        "criteria": criteria
        if criteria is not None
        else [
            {"name": "完整度", "description": "要点有没有齐"},
            {"name": "可执行性", "description": None},
        ],
    }


def _scores(a, b):
    return [
        {"index": 0, "score_a": a[0], "score_b": b[0]},
        {"index": 1, "score_a": a[1], "score_b": b[1]},
    ]


def _make_workflow(db, slug="flow-a", owner=OWNER) -> int:
    """返回 id：实例出了 session 就 detached，调用方只拿得到 id 才不会踩过期属性。"""
    row = Workflow(owner_id=owner, slug=slug, name=slug)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _make_run(db, workflow_id, slug, *, owner=OWNER, status="succeeded", label=None, elapsed=1200, old_days=None) -> int:
    row = WorkflowRun(
        owner_id=owner,
        workflow_id=workflow_id,
        workflow_slug=slug,
        inputs={},
        status=status,
        subject_label=label,
        elapsed_ms=elapsed,
    )
    db.add(row)
    db.commit()
    if old_days is not None:
        # 显式改 created_at：prune 按它判年龄，server_default 的 func.now() 没法插老时间。
        row.created_at = db_now() - timedelta(days=old_days)
        db.commit()
    db.refresh(row)
    return row.id


# ---- 评分细则 ---------------------------------------------------------------


def test_rubric_crud_and_duplicate_name(env):
    client, _Session, _ = env
    created = client.post("/api/workflows/rubrics", json=_rubric_body()).json()
    assert created["code"] == 0
    rubric_id = created["data"]["id"]
    assert [c["name"] for c in created["data"]["criteria"]] == ["完整度", "可执行性"]
    # 纯空白描述归一为 None，不存一串空格。
    assert created["data"]["criteria"][1]["description"] is None

    listing = client.get("/api/workflows/rubrics").json()["data"]["items"]
    assert [r["id"] for r in listing] == [rubric_id]

    updated = client.put(
        f"/api/workflows/rubrics/{rubric_id}",
        json=_rubric_body("对比尺子", [{"name": "完整度"}, {"name": "准确"}, {"name": "可执行性"}]),
    ).json()
    assert len(updated["data"]["criteria"]) == 3

    # 同名再来一把：业务拒绝走信封，不是 500。
    dup = client.post("/api/workflows/rubrics", json=_rubric_body()).json()
    assert dup["code"] == -1

    assert client.put("/api/workflows/rubrics/9999", json=_rubric_body()).status_code == 404

    deleted = client.delete(f"/api/workflows/rubrics/{rubric_id}").json()
    assert deleted["code"] == 0
    assert client.get("/api/workflows/rubrics").json()["data"]["items"] == []
    assert client.delete(f"/api/workflows/rubrics/{rubric_id}").status_code == 404


def test_rubric_criteria_count_validated(env):
    client, _Session, _ = env
    one = client.post(
        "/api/workflows/rubrics", json=_rubric_body(criteria=[{"name": "只有一条"}])
    )
    assert one.status_code == 422
    seven = client.post(
        "/api/workflows/rubrics",
        json=_rubric_body(criteria=[{"name": f"标准{i}"} for i in range(7)]),
    )
    assert seven.status_code == 422


# ---- 评测保存 / 去重 / 快照 --------------------------------------------------


def test_save_evaluation_dedup_and_reverse_pair(env):
    client, Session, _ = env
    with Session() as db:
        wf = _make_workflow(db)
        run_a = _make_run(db, wf, "flow-a", label="A 版")
        run_b = _make_run(db, wf, "flow-a", label="B 版")

    rubric_id = client.post("/api/workflows/rubrics", json=_rubric_body()).json()["data"]["id"]
    payload = {
        "run_a_id": run_a,
        "run_b_id": run_b,
        "rubric_id": rubric_id,
        "scores": _scores([5, 3], [5, 5]),
        "note": "B 更稳",
    }
    saved = client.post("/api/workflows/evaluations", json=payload).json()
    assert saved["code"] == 0
    assert saved["message"] == "评测已保存"
    item = saved["data"]
    assert item["avg_a"] == 4.0
    assert item["avg_b"] == 5.0
    assert item["diff"] == 1.0
    assert item["run_a"]["subject_label"] == "A 版"
    assert item["rubric_name"] == "对比尺子"

    # 同对运行 + 同一把尺子再交：按更新，不新建；消息也要换。
    payload["scores"] = _scores([4, 4], [5, 3])
    updated = client.post("/api/workflows/evaluations", json=payload).json()
    assert updated["message"] == "评测已更新"
    assert updated["data"]["avg_a"] == 4.0
    assert updated["data"]["avg_b"] == 4.0
    listing = client.get(
        "/api/workflows/evaluations", params={"run_id": run_a, "with_run_id": run_b}
    ).json()["data"]["items"]
    assert len(listing) == 1
    assert listing[0]["id"] == item["id"]

    # A/B 反选是另一次评测：比较方向反过来，diff 也反号。
    reverse = client.post(
        "/api/workflows/evaluations",
        json={
            "run_a_id": run_b,
            "run_b_id": run_a,
            "rubric_id": rubric_id,
            "scores": _scores([5, 3], [4, 4]),
        },
    ).json()
    assert reverse["message"] == "评测已保存"
    assert reverse["data"]["diff"] == 0.0 or reverse["data"]["diff"] == -0.0

    both = client.get("/api/workflows/evaluations", params={"run_id": run_a}).json()["data"]["items"]
    assert len(both) == 2
    # with_run_id 收敛到这一对之间：反选也是这一对的评测，两条都在，新的在前。
    one_pair = client.get(
        "/api/workflows/evaluations", params={"run_id": run_a, "with_run_id": run_b}
    ).json()["data"]["items"]
    assert [row["id"] for row in one_pair] == [reverse["data"]["id"], item["id"]]

    # 跟第三个运行的评测不被这一对的查询带出来。
    with Session() as db:
        run_c = _make_run(db, wf, "flow-a")
    client.post(
        "/api/workflows/evaluations",
        json={
            "run_a_id": run_a,
            "run_b_id": run_c,
            "rubric_id": rubric_id,
            "scores": _scores([5, 3], [2, 2]),
        },
    )
    pair_again = client.get(
        "/api/workflows/evaluations", params={"run_id": run_a, "with_run_id": run_b}
    ).json()["data"]["items"]
    assert {row["id"] for row in pair_again} == {item["id"], reverse["data"]["id"]}


def test_evaluation_rejects_bad_pairs_and_scores(env):
    client, Session, _ = env
    with Session() as db:
        wf1 = _make_workflow(db, slug="flow-a")
        wf2 = _make_workflow(db, slug="flow-b")
        run_a = _make_run(db, wf1, "flow-a")
        run_b = _make_run(db, wf1, "flow-a")
        run_other_flow = _make_run(db, wf2, "flow-b")
    rubric_id = client.post("/api/workflows/rubrics", json=_rubric_body()).json()["data"]["id"]

    def post(**overrides):
        body = {
            "run_a_id": run_a,
            "run_b_id": run_b,
            "rubric_id": rubric_id,
            "scores": _scores([5, 3], [5, 5]),
        }
        body.update(overrides)
        return client.post("/api/workflows/evaluations", json=body)

    assert post(run_a_id=run_a, run_b_id=run_a).json()["code"] == -1
    assert post(run_b_id=run_other_flow).json()["code"] == -1
    # 下标重复 / 与快照不等长都拒收，避免存下对不上尺子的分数。
    assert post(scores=[{"index": 0, "score_a": 5, "score_b": 5}]).json()["code"] == -1
    assert post(
        scores=[
            {"index": 0, "score_a": 5, "score_b": 5},
            {"index": 0, "score_a": 3, "score_b": 5},
        ]
    ).json()["code"] == -1
    assert post(rubric_id=9999).status_code == 404
    assert post(run_b_id=9999).status_code == 404
    assert client.get("/api/workflows/evaluations", params={"run_id": 9999}).status_code == 404


def test_snapshot_frozen_when_rubric_changes_or_deleted(env):
    client, Session, _ = env
    with Session() as db:
        wf = _make_workflow(db)
        run_a = _make_run(db, wf, "flow-a")
        run_b = _make_run(db, wf, "flow-a")
    rubric_id = client.post("/api/workflows/rubrics", json=_rubric_body()).json()["data"]["id"]
    saved = client.post(
        "/api/workflows/evaluations",
        json={
            "run_a_id": run_a,
            "run_b_id": run_b,
            "rubric_id": rubric_id,
            "scores": _scores([5, 3], [5, 5]),
        },
    ).json()["data"]
    assert [c["name"] for c in saved["rubric_snapshot"]] == ["完整度", "可执行性"]

    # 尺子改名 + 加到三条：旧评测的快照不动，名字活取新值。
    client.put(
        f"/api/workflows/rubrics/{rubric_id}",
        json=_rubric_body("新尺子名", [{"name": "完整度"}, {"name": "准确"}, {"name": "可执行性"}]),
    )
    listing = client.get(
        "/api/workflows/evaluations", params={"run_id": run_a}
    ).json()["data"]["items"]
    assert listing[0]["rubric_name"] == "新尺子名"
    assert [c["name"] for c in listing[0]["rubric_snapshot"]] == ["完整度", "可执行性"]

    # 删尺子不删评测；名字落空，快照仍然自足。
    client.delete(f"/api/workflows/rubrics/{rubric_id}")
    listing = client.get(
        "/api/workflows/evaluations", params={"run_id": run_a}
    ).json()["data"]["items"]
    assert len(listing) == 1
    assert listing[0]["rubric_name"] is None
    assert len(listing[0]["rubric_snapshot"]) == 2


# ---- 归属隔离 ---------------------------------------------------------------


def test_rubrics_and_runs_are_owner_scoped(env):
    client, Session, make_client = env
    other = make_client(OTHER)
    with Session() as db:
        wf = _make_workflow(db)
        run_a = _make_run(db, wf, "flow-a")
        run_b = _make_run(db, wf, "flow-a")
        other_wf = _make_workflow(db, slug="other-flow", owner=OTHER)
        other_a = _make_run(db, other_wf, "other-flow", owner=OTHER)
        other_b = _make_run(db, other_wf, "other-flow", owner=OTHER)

    rubric_id = client.post("/api/workflows/rubrics", json=_rubric_body()).json()["data"]["id"]
    other_rubric = other.post("/api/workflows/rubrics", json=_rubric_body("别人的尺子")).json()[
        "data"
    ]["id"]

    # 列表互不可见。
    assert client.get("/api/workflows/rubrics").json()["data"]["total"] == 1
    assert other.get("/api/workflows/rubrics").json()["data"]["items"][0]["id"] == other_rubric

    def evaluate(c, rubric, a, b):
        return c.post(
            "/api/workflows/evaluations",
            json={"run_a_id": a, "run_b_id": b, "rubric_id": rubric, "scores": _scores([5, 3], [4, 4])},
        )

    # 拿别人的尺子给自家运行打分 / 拿别人的运行配对，都是 404 而不是读到数据。
    assert evaluate(client, other_rubric, run_a, run_b).status_code == 404
    assert evaluate(client, rubric_id, run_a, other_a).status_code == 404
    assert evaluate(other, rubric_id, other_a, other_b).status_code == 404

    # 各自存一条，谁也列不到谁的。
    assert evaluate(other, other_rubric, other_a, other_b).json()["code"] == 0
    mine = client.get("/api/workflows/evaluations", params={"run_id": run_a}).json()
    assert mine["data"]["total"] == 0


# ---- prune 豁免 -------------------------------------------------------------


def test_prune_keeps_referenced_runs_until_evaluation_deleted(env):
    client, Session, _ = env
    with Session() as db:
        wf = _make_workflow(db)
        # 三次都是无业务对象的手动运行：过窗口本来都在删除名单上。
        run_a = _make_run(db, wf, "flow-a", old_days=60)
        run_b = _make_run(db, wf, "flow-a", old_days=60)
        run_c = _make_run(db, wf, "flow-a", old_days=60)
    rubric_id = client.post("/api/workflows/rubrics", json=_rubric_body()).json()["data"]["id"]
    client.post(
        "/api/workflows/evaluations",
        json={
            "run_a_id": run_a,
            "run_b_id": run_b,
            "rubric_id": rubric_id,
            "scores": _scores([5, 3], [5, 5]),
        },
    )

    with Session() as db:
        deleted_runs, _deleted_steps = prune_runs(db, keep_days=30)
        surviving = set(db.execute(select(WorkflowRun.id)).scalars())
    assert deleted_runs == 1
    assert surviving == {run_a, run_b}
    assert run_c not in surviving

    # 引用解除后，豁免随之解除：下一轮清理连步骤现场一起删。
    with Session() as db:
        [evaluation_id] = db.execute(select(WorkflowEvaluation.id)).scalars()
    client.delete(f"/api/workflows/evaluations/{evaluation_id}")
    with Session() as db:
        deleted_runs, _ = prune_runs(db, keep_days=30)
        assert deleted_runs == 2
        assert db.execute(select(WorkflowRun.id)).scalars().all() == []
