"""Tests for JD parser module."""
import pytest
from server.jd.parser import (parse_conditions, validate_offsets, _build_node,
                                snap_fragment, requirement_fragments)
from server.core.contracts import (ConditionNode, CriterionCategory, JobCriterion,
                                  ReviewStatus)


def test_validate_offsets_valid():
    """Test offset validation with valid spans."""
    text = "熟悉 Python 和 Java"
    assert validate_offsets([[0, 5]], text) is True
    assert validate_offsets([[0, 5], [8, 12]], text) is True


def test_validate_offsets_invalid():
    """Test offset validation with invalid spans."""
    text = "熟悉 Python"
    assert validate_offsets([[0, 100]], text) is False  # out of bounds
    assert validate_offsets([[5, 3]], text) is False  # start > end


def test_build_node_leaf():
    """Test building a leaf node."""
    d = {"op": "LEAF", "predicate": "Python", "value": None, "examples": []}
    node = _build_node(d)
    assert node is not None
    assert node.op is None  # LEAF becomes None
    assert node.predicate == "Python"


def test_build_node_or():
    """Test building an OR node."""
    d = {
        "op": "OR",
        "children": [
            {"op": "LEAF", "predicate": "Python", "value": None},
            {"op": "LEAF", "predicate": "Java", "value": None},
        ],
    }
    node = _build_node(d)
    assert node is not None
    assert node.op == "OR"
    assert len(node.children) == 2


def test_build_node_invalid():
    """Test building node with invalid structure."""
    assert _build_node({"op": "INVALID"}) is None
    assert _build_node({"op": "AND", "children": []}) is None  # empty children


class TestParseConditions:
    """Test parse_conditions with mock LLM."""

    def test_parse_simple_jd(self):
        """Test parsing a simple JD with mock LLM."""
        def mock_llm(system, user):
            return '''{"criteria": [
                {
                    "category": "required",
                    "expression": {"op": "LEAF", "predicate": "Python开发经验", "value": null, "examples": []},
                    "min_duration_months": null,
                    "scope_rule": "CANDIDATE_ANYWHERE",
                    "weight": 1.0,
                    "input_spans": [[0, 10]]
                }
            ]}'''

        criteria, qsha, warnings = parse_conditions("要求Python开发经验", mock_llm)
        assert len(criteria) == 1
        assert criteria[0].category == CriterionCategory.REQUIRED
        assert criteria[0].review_status == ReviewStatus.AUTO

    def test_parse_with_examples(self):
        """Test that examples are correctly extracted."""
        def mock_llm(system, user):
            return '''{"criteria": [
                {
                    "category": "required",
                    "expression": {
                        "op": "LEAF",
                        "predicate": "流批处理技术",
                        "value": null,
                        "examples": ["Flink", "Spark"]
                    },
                    "min_duration_months": null,
                    "scope_rule": "CANDIDATE_ANYWHERE",
                    "weight": 1.0,
                    "input_spans": [[0, 15]]
                }
            ]}'''

        criteria, _, _ = parse_conditions("熟悉流批处理技术,如Flink、Spark", mock_llm)
        assert len(criteria) == 1
        assert criteria[0].expression.examples == ["Flink", "Spark"]

    def test_parse_invalid_json(self):
        """Test handling of invalid JSON from LLM."""
        def mock_llm(system, user):
            return "not valid json"

        with pytest.raises(ValueError, match="条件解析模型输出非法"):
            parse_conditions("some text", mock_llm)

    def test_parse_structure_drift(self):
        """Test retry on structure drift."""
        call_count = [0]
        def mock_llm(system, user):
            call_count[0] += 1
            if call_count[0] < 2:
                # First attempt: bad structure
                return '''{"criteria": [
                    {"category": "required", "expression": {"op": "INVALID"}}
                ]}'''
            else:
                # Second attempt: good structure
                return '''{"criteria": [
                    {
                        "category": "required",
                        "expression": {"op": "LEAF", "predicate": "test", "value": null},
                        "min_duration_months": null,
                        "scope_rule": "CANDIDATE_ANYWHERE",
                        "weight": 1.0,
                        "input_spans": [[0, 5]]
                    }
                ]}'''

        criteria, _, _ = parse_conditions("test", mock_llm)
        assert call_count[0] == 2  # retried once
        assert len(criteria) == 1


JD_CV05 = ("要求候选人有3年以上Java后端开发经验,熟悉Spring Boot和MySQL,"
           "有支付或风控系统开发经验优先")


class TestRequirementFragments:
    """展示给 HR 的"要求"必须是岗位原文里完整的一句。"""

    def test_snap_restores_cut_words(self):
        # 模型给 [36,50) 切在 MySQL 中间、句尾断在"开发"上
        assert snap_fragment(JD_CV05, 36, 50) == "MySQL,有支付或风控系统开发经验优先"

    def test_snap_leaves_clean_boundaries_alone(self):
        assert snap_fragment(JD_CV05, 21, 34) == "熟悉Spring Boot"

    def test_snap_does_not_split_technology_names(self):
        text = "1) 精通Java或C/C++\n2) 具备扎实的系统设计与编码能力"
        s = text.find("Java")
        assert snap_fragment(text, s, s + 8) == "Java或C/C++"

    def test_snap_prefers_majority_line_when_span_crosses_two(self):
        text = "1) 精通Java或C/C++\n2) 具备扎实的系统设计与编码能力"
        got = snap_fragment(text, text.find("编码") - 6, text.find("编码"))
        assert got == "具备扎实的系统设计与编码能力"

    def test_fragments_fall_back_to_leaf_text_without_offsets(self):
        crit = JobCriterion(
            criterion_id="c03", query_id="q", category=CriterionCategory.REQUIRED,
            expression=ConditionNode(predicate="后端开发经验", value="5年以上"),
            input_spans=[], review_status=ReviewStatus.AUTO)
        jd = "应用架构师(风控平台方向): 1) 有反欺诈系统开发经验 2) 5年以上后端开发经验"
        assert requirement_fragments(crit, jd) == ["5年以上后端开发经验"]

    def test_fragments_keep_the_offset_slice_when_it_is_cleaner(self):
        crit = JobCriterion(
            criterion_id="c03", query_id="q", category=CriterionCategory.REQUIRED,
            expression=ConditionNode(predicate="技术栈", value="MySQL"),
            input_spans=[(36, 50)], review_status=ReviewStatus.AUTO)
        assert requirement_fragments(crit, JD_CV05) == [
            "MySQL,有支付或风控系统开发经验优先"]

    def test_snap_does_not_invent_text(self):
        """原文里找不到条件值时不硬凑,返回空让上层自己兜。"""
        crit = JobCriterion(
            criterion_id="c01", query_id="q", category=CriterionCategory.REQUIRED,
            expression=ConditionNode(predicate="Kubernetes", value=None),
            input_spans=[], review_status=ReviewStatus.AUTO)
        assert requirement_fragments(crit, JD_CV05) == []
