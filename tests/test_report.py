"""Tests for report scoring and rendering."""
import pytest
from server.report.render import score_report, render_report, to_markdown
from server.search.orchestrator import build_review_question, requirement_text
from server.core.contracts import (
    CriterionCategory, CriterionResult, CriterionState, JobCriterion,
    ConditionNode, ResumeVersion, SourceSpan, ScopeRule, ReviewStatus,
    ScopeKind
)


def make_criterion(cid, category, predicate="test"):
    """Helper to create a test criterion."""
    return JobCriterion(
        criterion_id=cid,
        query_id="q-test",
        category=CriterionCategory(category),
        expression=ConditionNode(predicate=predicate, value=None),
        min_duration_months=None,
        scope_rule=ScopeRule.CANDIDATE_ANYWHERE,
        weight=1.0,
        input_spans=[(0, 10)],
        review_status=ReviewStatus.AUTO,
    )


def make_result(cid, state, reason="test"):
    """Helper to create a test result."""
    return CriterionResult(
        criterion_id=cid,
        candidate_id="test",
        state=CriterionState(state),
        source_span_ids=[],
        supporting_fact_ids=[],
        reason_code=reason,
        model_version="test",
    )


class TestScoreReport:
    """Test score_report function."""

    def test_all_supported(self):
        """Test scoring when all criteria are supported."""
        criteria = [
            make_criterion("c01", "required"),
            make_criterion("c02", "required"),
        ]
        results = [
            make_result("c01", "SUPPORTED"),
            make_result("c02", "SUPPORTED"),
        ]
        score = score_report(criteria, results)
        assert score["score"] == 100.0
        assert score["coverage_pct"] == 100.0
        assert score["pending_weight_pct"] == 0.0

    def test_partial_support(self):
        """Test scoring with partial support."""
        criteria = [
            make_criterion("c01", "required"),
            make_criterion("c02", "required"),
        ]
        results = [
            make_result("c01", "SUPPORTED"),
            make_result("c02", "PARTIAL"),
        ]
        score = score_report(criteria, results)
        assert score["score"] == 75.0  # 100% + 50% / 2
        assert score["coverage_pct"] == 100.0

    def test_not_evidenced(self):
        """Test scoring with not evidenced criteria."""
        criteria = [
            make_criterion("c01", "required"),
            make_criterion("c02", "required"),
        ]
        results = [
            make_result("c01", "SUPPORTED"),
            make_result("c02", "NOT_EVIDENCED"),
        ]
        score = score_report(criteria, results)
        assert score["score"] == 50.0
        assert score["coverage_pct"] == 100.0

    def test_needs_review_pending(self):
        """Test that NEEDS_REVIEW contributes to pending weight."""
        criteria = [
            make_criterion("c01", "required"),
            make_criterion("c02", "required"),
        ]
        results = [
            make_result("c01", "SUPPORTED"),
            make_result("c02", "NEEDS_REVIEW"),
        ]
        score = score_report(criteria, results)
        assert score["score"] == 50.0
        assert score["pending_weight_pct"] == 50.0
        assert score["coverage_pct"] == 50.0

    def test_preferred_not_in_score(self):
        """Test that preferred criteria don't participate in scoring."""
        criteria = [
            make_criterion("c01", "required"),
            make_criterion("c02", "preferred"),
        ]
        results = [
            make_result("c01", "SUPPORTED"),
            make_result("c02", "NOT_EVIDENCED"),
        ]
        score = score_report(criteria, results)
        assert score["score"] == 100.0  # only c01 counts
        assert len(score["preferred"]) == 1
        assert score["preferred"][0]["criterion_id"] == "c02"

    def test_conflicting_detected(self):
        """Test that conflicting criteria are detected."""
        criteria = [
            make_criterion("c01", "required"),
        ]
        results = [
            make_result("c01", "CONFLICTING"),
        ]
        score = score_report(criteria, results)
        assert "c01" in score["conflicting_criteria"]


class TestToMarkdown:
    """Test to_markdown function."""

    def test_basic_report(self):
        """Test basic markdown report generation."""
        report = {
            "candidate": {"filename": "test.pdf", "source_sha256": "abc123"},
            "score": {
                "score_name": "当前证据匹配分",
                "score": 75.0,
                "coverage_pct": 100.0,
                "pending_weight_pct": 0.0,
            },
            "items": [
                {
                    "criterion_id": "c01",
                    "category": "required",
                    "state": "SUPPORTED",
                    "input_fragments": ["Python经验"],
                    "reason": "scope_verified",
                    "citations": [
                        {"page": 1, "label": "原文", "quote": "5年Python开发经验"}
                    ],
                    "review_question": None,
                }
            ],
            "review_questions": [],
            "notes": [],
            "analysis": {
                "overall_assessment": "候选人整体匹配度良好",
                "strengths": [{"point": "Python经验丰富"}],
                "risks": [],
                "role_fit": {"fit_level": "强匹配", "explanation": ""},
                "communication_questions": [],
                "recommendations": {"next_step": "建议进入下一轮"},
            },
        }
        md = to_markdown(report)
        assert "# 候选人报告 test.pdf" in md
        assert "当前证据匹配分" in md
        assert "75.0" in md
        assert "顾问评估" in md
        assert "优势" in md
        assert "逐条对照" in md

    def test_markdown_chinese_labels(self):
        """Test that Chinese labels are used."""
        report = {
            "candidate": {"filename": "test.pdf", "source_sha256": "abc"},
            "score": {"score_name": "当前证据匹配分", "score": 50, "coverage_pct": 100, "pending_weight_pct": 0},
            "items": [
                {
                    "criterion_id": "c01",
                    "category": "required",
                    "state": "SUPPORTED",
                    "input_fragments": ["test"],
                    "reason": "",
                    "citations": [],
                    "review_question": None,
                }
            ],
            "review_questions": [],
            "notes": [],
        }
        md = to_markdown(report)
        assert "硬性要求" in md  # required → 硬性要求
        assert "满足" in md  # SUPPORTED → 满足


def test_review_question_names_the_jd_line():
    """待确认话术要点名这条岗位原文,不能吐内部条件标签。"""
    from server.search.orchestrator import build_review_question
    q = build_review_question(CriterionState.PARTIAL, "MySQL,有支付或风控系统开发经验优先")
    assert "MySQL,有支付或风控系统开发经验优先" in q
    assert "请确认候选人是否具备:" not in q
    assert build_review_question(CriterionState.SUPPORTED, "x") is None


def test_rendered_item_and_question_agree():
    """左栏那条"要求"和 ❓ 里引用的必须是同一句原话,否则 HR 看着像两码事。"""
    jd = "要求候选人有3年以上Java后端开发经验,熟悉Spring Boot和MySQL,有支付或风控系统开发经验优先"
    crit = make_criterion("c03", "preferred")
    crit.input_spans = [(36, 50)]
    crit.expression = ConditionNode(predicate="业务领域开发经验", value="系统开发经验 风控系统")
    res = make_result("c03", "PARTIAL")
    res.review_question = build_review_question(CriterionState.PARTIAL,
                                                requirement_text(crit, jd))
    ver = ResumeVersion(candidate_id="CV05", dataset_id="ds", document_id="doc",
                        source_sha256="sha", filename="CV05.pdf", ingested_at="",
                        parser_version="v1", status="DONE")
    rep = render_report(jd, "sha", [crit], [res], ver, {},
                        score_report([crit], [res]))
    item = rep["items"][0]
    assert item["input_fragments"] == ["MySQL,有支付或风控系统开发经验优先"]
    assert item["review_question"].startswith("「MySQL,有支付或风控系统开发经验优先」")
