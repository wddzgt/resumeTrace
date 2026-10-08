"""Tests for core contracts and invariant validation."""
import pytest
from server.core.contracts import (
    CriterionState, CriterionCategory, CriterionResult, JobCriterion,
    ConditionNode, ResumeVersion, SourceSpan, ResumeFact, ScopeRule,
    ReviewStatus, ScopeKind, AssertionType, validate_invariants,
    InvariantError
)


class TestCriterionState:
    """Test CriterionState enum."""

    def test_states_exist(self):
        """Test all expected states exist."""
        assert CriterionState.SUPPORTED
        assert CriterionState.PARTIAL
        assert CriterionState.NOT_EVIDENCED
        assert CriterionState.CONFLICTING
        assert CriterionState.NEEDS_REVIEW


class TestCriterionCategory:
    """Test CriterionCategory enum."""

    def test_categories_exist(self):
        """Test all expected categories exist."""
        assert CriterionCategory.REQUIRED
        assert CriterionCategory.PREFERRED
        assert CriterionCategory.SEARCH_INTENT
        assert CriterionCategory.CLARIFY
        assert CriterionCategory.RESPONSIBILITY


class TestValidateInvariants:
    """Test validate_invariants function."""

    def test_valid_invariants(self):
        """Test that valid data passes validation."""
        versions = [
            ResumeVersion(
                candidate_id="test",
                dataset_id="ds1",
                document_id="doc1",
                source_sha256="abc123",
                filename="test.pdf",
                ingested_at="2024-01-01",
                parser_version="v1",
                status="DONE",
            )
        ]
        spans = {
            "sp1": SourceSpan(
                source_sha256="abc123",
                page_index=0,
                line_start=0,
                line_end=0,
                quote="test quote",
                quote_hash="hash",
                rects=[],
                extraction_mode="OCR",
                status="unlocated",
            )
        }
        facts = [
            ResumeFact(
                fact_id="f1",
                candidate_id="test",
                scope_id="s1",
                predicate="test",
                value="value",
                assertion_type=AssertionType.SEMANTIC,
                source_span_ids=["sp1"],
            )
        ]
        results = [
            CriterionResult(
                criterion_id="c01",
                candidate_id="test",
                state=CriterionState.SUPPORTED,
                source_span_ids=["sp1"],
                supporting_fact_ids=["f1"],
                reason_code="test",
                model_version="v1",
            )
        ]
        # Should not raise
        validate_invariants(versions, spans, facts, results)

    def test_invalid_span_reference(self):
        """Test that invalid span references are caught."""
        versions = [
            ResumeVersion(
                candidate_id="test",
                dataset_id="ds1",
                document_id="doc1",
                source_sha256="abc123",
                filename="test.pdf",
                ingested_at="2024-01-01",
                parser_version="v1",
                status="DONE",
            )
        ]
        spans = {}  # empty spans
        facts = [
            ResumeFact(
                fact_id="f1",
                candidate_id="test",
                scope_id="s1",
                predicate="test",
                value="value",
                assertion_type=AssertionType.SEMANTIC,
                source_span_ids=["sp_nonexistent"],  # reference to non-existent span
            )
        ]
        results = []
        with pytest.raises(InvariantError, match="span"):
            validate_invariants(versions, spans, facts, results)

    def test_unsupported_without_evidence(self):
        """Test that SUPPORTED state requires evidence."""
        versions = [
            ResumeVersion(
                candidate_id="test",
                dataset_id="ds1",
                document_id="doc1",
                source_sha256="abc123",
                filename="test.pdf",
                ingested_at="2024-01-01",
                parser_version="v1",
                status="DONE",
            )
        ]
        spans = {}
        facts = []
        results = [
            CriterionResult(
                criterion_id="c01",
                candidate_id="test",
                state=CriterionState.SUPPORTED,
                source_span_ids=[],  # no evidence
                supporting_fact_ids=[],
                reason_code="test",
                model_version="v1",
            )
        ]
        with pytest.raises(InvariantError, match="SUPPORTED"):
            validate_invariants(versions, spans, facts, results)
