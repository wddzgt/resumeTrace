"""Tests for JD parser module."""
import pytest
from server.jd.parser import parse_conditions, validate_offsets, _build_node
from server.core.contracts import CriterionCategory, ReviewStatus


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
