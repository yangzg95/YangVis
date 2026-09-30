"""面试记录的纯函数测试。

questions JSON 的读写全部收敛在 services/interview.py 顶部那几个纯函数里，而
参考答案的**进度**已经不在 JSON 上了：它由认领该 qid 的运行翻译出来
（``InterviewService._question_of``）。并发回写的正确性钉在这里，重启后的收尾
则在 ``test_workflow_flows.py`` 里跟着运行记录一起测。
"""
import pytest

from app.models.entities import InterviewRecord, WorkflowRun
from app.models.schemas import InterviewQuestionIn
from app.services.interview import (
    REF_NONE,
    InterviewService,
    _answer_inputs,
    _find_question,
    _merge_ref_answer,
    _new_question,
    _new_qid,
)
from app.services.workflows import (
    RUN_FAILED,
    RUN_PENDING,
    RUN_SUCCEEDED,
    SUBJECT_ANALYZING,
    SUBJECT_ERROR,
    SUBJECT_READY,
)


def _question(qid: str, **extra) -> dict:
    base = {
        "qid": qid,
        "question": f"问题 {qid}",
        "my_answer": None,
        "note": None,
        "ref_answer": None,
    }
    base.update(extra)
    return base


def _run(status: str, error_msg: str | None = None) -> WorkflowRun:
    """一条不用落库的运行：状态翻译只看这两个字段。"""
    return WorkflowRun(status=status, error_msg=error_msg)


# ---- _new_qid -----------------------------------------------------------------


def test_new_qid_is_unique():
    qids = {_new_qid() for _ in range(100)}
    assert len(qids) == 100


def test_new_question_stores_only_the_answer():
    """进度不进 JSON：一题存下来的只有产物，ref_status / ref_error 是序列化时算的。"""
    question = _new_question(InterviewQuestionIn(question=" 你的技术栈是什么？ "))
    assert set(question) == {"qid", "question", "my_answer", "note", "ref_answer"}
    assert question["question"] == "你的技术栈是什么？"


# ---- _merge_ref_answer ----------------------------------------------------------


def test_merge_ref_answer_only_touches_that_questions_answer():
    questions = [
        _question("aaa", my_answer="我的回答", note="备注"),
        _question("bbb"),
    ]
    merged = _merge_ref_answer(questions, "aaa", "参考答案")

    target = _find_question(merged, "aaa")
    assert target["ref_answer"] == "参考答案"
    # 用户手填的字段必须原样保留——这条写路径只动 ref_answer。
    assert target["my_answer"] == "我的回答"
    assert target["note"] == "备注"
    # 别的题不受影响。
    assert _find_question(merged, "bbb")["ref_answer"] is None
    assert questions[0]["ref_answer"] is None, "返回的是新列表，原列表没被就地改"


def test_merge_ref_answer_missing_qid_is_noop():
    questions = [_question("aaa")]
    assert _merge_ref_answer(questions, "zzz", "x") == questions


# ---- 状态翻译：运行 → ref_status ------------------------------------------------


@pytest.mark.parametrize(
    "run,expected",
    [
        (None, REF_NONE),
        (_run(RUN_PENDING), SUBJECT_ANALYZING),
        (_run(RUN_SUCCEEDED), SUBJECT_READY),
        (_run(RUN_FAILED, "上游 502"), SUBJECT_ERROR),
    ],
)
def test_question_of_translates_the_run_into_ref_status(run, expected):
    question = _question("aaa", ref_answer="参考答案" if expected == SUBJECT_READY else None)
    view = InterviewService._question_of(question, run)
    assert view.ref_status == expected
    assert view.ref_error == ("上游 502" if expected == SUBJECT_ERROR else None)


def test_question_of_reports_legacy_rows_as_ready():
    """改造前生成的题目没有运行，但答案在：别让历史数据看起来像没做过。"""
    view = InterviewService._question_of(_question("aaa", ref_answer="旧答案"), None)
    assert (view.ref_status, view.ref_error) == (SUBJECT_READY, None)


def test_question_of_keeps_the_users_fields_verbatim():
    view = InterviewService._question_of(
        _question("aaa", question="你的技术栈是什么？", my_answer="Python", note="待补"), None
    )
    assert (view.qid, view.question, view.my_answer, view.note) == (
        "aaa",
        "你的技术栈是什么？",
        "Python",
        "待补",
    )
    assert view.ref_answer is None


# ---- _answer_inputs --------------------------------------------------------------


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


def test_answer_inputs_carries_background_and_question():
    inputs = _answer_inputs(
        _record(round="二面"), _question("aaa", question="讲讲你做过的高并发优化")
    )

    assert inputs["company"] == "示例科技"
    assert inputs["position"] == "高级后端工程师"
    assert inputs["round_note"] == "\n面试轮次：二面"
    assert inputs["question"] == "讲讲你做过的高并发优化"
    # 没有「当时的回答」就不该出现那一段。
    assert inputs["answer_note"] == ""


def test_answer_inputs_wraps_my_answer_in_a_section():
    inputs = _answer_inputs(_record(), _question("aaa", my_answer="我当时答了缓存预热"))

    assert "求职者当时的回答" in inputs["answer_note"]
    assert "我当时答了缓存预热" in inputs["answer_note"]


def test_answer_inputs_leaves_the_round_line_out_when_empty():
    inputs = _answer_inputs(_record(round=None), _question("aaa"))
    assert inputs["round_note"] == ""
