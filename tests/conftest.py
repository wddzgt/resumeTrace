"""Pytest configuration and fixtures."""
import pytest
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def mock_llm_call():
    """Mock LLM call function for testing."""
    def _call(system, user):
        return '{"state": "SUPPORTED", "quote": "test", "role_action": "", "conflict_quote": ""}'
    return _call


@pytest.fixture
def sample_jd_text():
    """Sample JD text for testing."""
    return """
    岗位要求:
    1. 5年以上Python开发经验
    2. 熟悉流批处理技术,如Flink、Spark
    3. 有风控平台架构经验优先
    4. 本科及以上学历
    """
