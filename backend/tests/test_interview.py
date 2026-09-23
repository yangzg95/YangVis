"""面试记录的纯函数测试。

questions JSON 的读写全部收敛在 services/interview.py 顶部那几个纯函数里：
并发回写、重启收尾的正确性都钉在这，不依赖数据库 fixture。
"""
from app.services.interview import (
    REF_ANALYZING,
    REF_ERROR,
    REF_NONE,
    REF_READY,
    STALE_ANSWER_ERROR,
    _answer_prompt,
    _find_question,
    _merge_ref_fields,
    _new_qid,
    _reset_stale_questions,
)
from app.models.entities import InterviewRecord


def _question(qid: str, ref_status: str = REF_NONE, **extra) -> dict:
    base = {
        "qid": qid,
        "question": f"问题 {qid}",
        "my_answer": None,
        "note": None,
        "ref_answer": None,
        "ref_status": ref_status,
        "ref_error": None,
    }
    base.update(extra)
    return base


# ---- _new_qid -----------------------------------------------------------------


def test_new_qid_is_unique():
    qids = {_new_qid() for _ in range(100)}
    assert len(qids) == 100


# ---- _merge_ref_fields ----------------------------------------------------------


def test_merge_ref_fields_only_touches_ref_keys():
    questions = [
        _question("aaa", my_answer="我的回答", note="备注"),
        _question("bbb"),
    ]
    merged = _merge_ref_fields(questions, "aaa", status=REF_READY, answer="参考答案", error=None)

    target = _find_question(merged, "aaa")
    assert target["ref_status"] == REF_READY
    assert target["ref_answer"] == "参考答案"
    assert target["ref_error"] is None
    # 用户手填的字段必须原样保留——这条写路径只负责 ref_*。
    assert target["my_answer"] == "我的回答"
    assert target["note"] == "备注"
    # 别的题不受影响。
    assert _find_question(merged, "bbb")["ref_status"] == REF_NONE


def test_merge_ref_fields_missing_qid_is_noop():
    questions = [_question("aaa")]
    merged = _merge_ref_fields(questions, "zzz", status=REF_READY, answer="x", error=None)
    assert merged == questions


# ---- _reset_stale_questions ------------------------------------------------------


def test_reset_stale_questions_marks_analyzing_as_error():
    questions = [
        _question("aaa", ref_status=REF_ANALYZING),
        _question("bbb", ref_status=REF_READY, ref_answer="已生成"),
        _question("ccc"),
    ]
    fixed, count = _reset_stale_questions(questions)

    assert count == 1
    assert _find_question(fixed, "aaa")["ref_status"] == REF_ERROR
    assert _find_question(fixed, "aaa")["ref_error"] == STALE_ANSWER_ERROR
    # 已完成与未生成的题目不动。
    assert _find_question(fixed, "bbb")["ref_status"] == REF_READY
    assert _find_question(fixed, "bbb")["ref_answer"] == "已生成"
    assert _find_question(fixed, "ccc")["ref_status"] == REF_NONE


def test_reset_stale_questions_without_analyzing_is_noop():
    questions = [_question("aaa", ref_status=REF_READY)]
    fixed, count = _reset_stale_questions(questions)
    assert count == 0
    assert fixed == questions


# ---- _answer_prompt --------------------------------------------------------------


def _record(**overrides) -> InterviewRecord:
    record = InterviewRecord(
        owner_id=1,
        company="示例科技",
        position="高级后端工程师",
        result="pending",
        questions=[],
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def test_answer_prompt_includes_background_and_question():
    prompt = _answer_prompt(_record(round="二面"), _question("aaa", question="讲讲你做过的高并发优化"))

    assert "应聘公司：示例科技" in prompt
    assert "面试岗位：高级后端工程师" in prompt
    assert "面试轮次：二面" in prompt
    assert "面试问题" in prompt
    assert "讲讲你做过的高并发优化" in prompt
    # 没有「当时的回答」就不该出现这一段。
    assert "求职者当时的回答" not in prompt


def test_answer_prompt_includes_my_answer_when_present():
    prompt = _answer_prompt(_record(), _question("aaa", my_answer="我当时答了缓存预热"))

    assert "求职者当时的回答" in prompt
    assert "我当时答了缓存预热" in prompt


def test_answer_prompt_omits_round_when_empty():
    prompt = _answer_prompt(_record(round=None), _question("aaa"))
    assert "面试轮次" not in prompt
