"""
Tests for the research_agent.py script (AutoResearchClaw)
"""
import os
import sys
import json
import types
import pytest
from unittest.mock import MagicMock, patch

# Add scripts directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts'))

# Provide lightweight stubs when packages are unavailable
if 'openai' not in sys.modules:
    openai_stub = types.ModuleType('openai')

    class _StubOpenAI:
        def __init__(self, *_, **__):
            pass

    openai_stub.OpenAI = _StubOpenAI
    sys.modules['openai'] = openai_stub

if 'requests' not in sys.modules:
    requests_stub = types.ModuleType('requests')

    class _StubRequest:
        def __init__(self, method, url, params=None):
            self.method = method
            self.url = url
            self.params = params or {}

        def prepare(self):
            from urllib.parse import urlencode
            query = urlencode(self.params, doseq=True)
            prepared = types.SimpleNamespace()
            prepared.url = f"{self.url}?{query}" if query else self.url
            return prepared

    def _stub_get(*_, **__):
        raise NotImplementedError("requests.get should be patched in tests")

    requests_stub.Request = _StubRequest
    requests_stub.get = _stub_get
    sys.modules['requests'] = requests_stub

import research_agent


class TestDecomposeTopic:
    """Test research topic decomposition."""

    def test_decompose_without_api_key(self):
        """Returns heuristic sub-queries when no API key is set."""
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}, clear=True):
            result = research_agent.decompose_topic("lizard cranial morphology")

        assert isinstance(result, list)
        assert len(result) >= 1
        # Heuristic decompose extracts keywords; raw topic is NOT used as-is
        for item in result:
            assert "query" in item
            assert "rationale" in item

    @patch('research_agent.OpenAI')
    def test_decompose_success(self, mock_openai):
        """Parses a JSON array from the LLM response."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps([
            {"query": "CT scans of Anolis", "rationale": "Get cranial data."},
            {"query": "Anolis physical specimens", "rationale": "Get specimen metadata."},
        ])
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.decompose_topic("Anolis skull evolution")

        assert len(result) == 2
        assert result[0]["query"] == "CT scans of Anolis"

    @patch('research_agent.OpenAI')
    def test_decompose_handles_markdown_fenced_json(self, mock_openai):
        """Strips ```json fences from the LLM output."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            '```json\n[{"query": "snake venom", "rationale": "r"}]\n```'
        )
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.decompose_topic("snake venom evolution")

        assert len(result) == 1
        assert result[0]["query"] == "snake venom"

    @patch('research_agent.OpenAI')
    def test_decompose_limits_to_max_queries(self, mock_openai):
        """At most MAX_QUERIES sub-queries are returned."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        queries = [{"query": f"q{i}", "rationale": "r"} for i in range(10)]
        mock_response.choices[0].message.content = json.dumps(queries)
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.decompose_topic("big topic")

        assert len(result) == research_agent.MAX_QUERIES

    @patch('research_agent.OpenAI')
    def test_decompose_handles_exception(self, mock_openai):
        """Returns heuristic sub-queries on LLM failure."""
        mock_openai.side_effect = Exception("API down")

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.decompose_topic("anything")

        assert len(result) >= 1
        for item in result:
            assert "query" in item
            assert "rationale" in item


class TestHeuristicDecompose:
    """Test the heuristic fallback decomposition."""

    def test_extracts_ct_and_specimen_concepts(self):
        """Detects CT, specimen, and metadata from a complex topic."""
        topic = (
            "Analyze MorphoSource's specimen, CT/X-ray, and metadata ecosystem "
            "to identify the most promising research pathways."
        )
        result = research_agent._heuristic_decompose(topic)
        query_texts = [q["query"] for q in result]

        assert len(result) >= 2
        assert "specimen" in query_texts
        assert "CT scan" in query_texts

    def test_does_not_return_raw_verbose_topic(self):
        """Heuristic decompose should never return the entire verbose topic."""
        topic = (
            "Analyze MorphoSource's specimen, CT/X-ray, and metadata ecosystem "
            "to identify the most promising research pathways, AI opportunities, "
            "and commercialization wedges."
        )
        result = research_agent._heuristic_decompose(topic)
        for q in result:
            assert q["query"] != topic

    def test_respects_max_queries(self):
        """Should return at most MAX_QUERIES results."""
        topic = "specimen CT scan mesh 3D metadata x-ray micro-ct xray"
        result = research_agent._heuristic_decompose(topic)
        assert len(result) <= research_agent.MAX_QUERIES

    def test_fallback_to_keywords(self):
        """When no known concepts match, extract keywords."""
        topic = "unusual morphological variation in deep sea organisms"
        result = research_agent._heuristic_decompose(topic)
        assert len(result) >= 1
        for q in result:
            assert len(q["query"]) < len(topic)  # Shorter than raw topic
            # Verify meaningful words from the topic were extracted
            topic_words = set(topic.lower().split())
            query_words = set(q["query"].lower().split())
            assert query_words & topic_words, (
                f"Query '{q['query']}' should contain words from the topic"
            )


class TestExecuteSearches:
    """Test MorphoSource search execution."""

    @patch('research_agent.morphosource_api.search_morphosource')
    @patch('research_agent.query_formatter.format_query')
    def test_execute_searches_success(self, mock_format, mock_search):
        """Processes each query through the pipeline."""
        mock_format.return_value = {
            'formatted_query': 'Serpentes',
            'api_params': {'q': 'Serpentes', 'per_page': 10},
            'generated_url': 'https://www.morphosource.org/api/media?q=Serpentes',
            'api_endpoint': 'media',
        }
        mock_search.return_value = {
            'full_data': {'media': [{'id': '1'}]},
            'summary': {'status': 'success', 'count': 1},
        }

        queries = [
            {"query": "snake CT scans", "rationale": "Get CT data"},
        ]
        results = research_agent.execute_searches(queries)

        assert len(results) == 1
        assert results[0]["result_count"] == 1
        assert results[0]["result_status"] == "success"
        mock_format.assert_called_once_with("snake CT scans")

    @patch('research_agent.morphosource_api.search_morphosource')
    @patch('research_agent.query_formatter.format_query')
    def test_execute_searches_zero_results(self, mock_format, mock_search):
        """Handles queries that return zero results."""
        mock_format.return_value = {
            'formatted_query': 'Nonexistent',
            'api_params': {'q': 'Nonexistent'},
            'api_endpoint': 'media',
        }
        mock_search.return_value = {
            'full_data': {'media': []},
            'summary': {'status': 'success', 'count': 0},
        }

        queries = [{"query": "nonexistent taxon", "rationale": "test"}]
        results = research_agent.execute_searches(queries)

        assert results[0]["result_count"] == 0


class TestSynthesizeReport:
    """Test report synthesis."""

    def test_synthesize_fallback_without_api_key(self):
        """Builds a plain Markdown report when LLM is unavailable."""
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}, clear=True):
            result = research_agent.synthesize_report(
                "test topic",
                [
                    {
                        "query": "q1",
                        "rationale": "r1",
                        "formatted_query": "Q1",
                        "api_endpoint": "media",
                        "result_count": 5,
                        "result_status": "success",
                        "result_data": {},
                    }
                ],
            )

        assert result["status"] == "fallback"
        assert "test topic" in result["report"]
        assert "5 result(s)" in result["report"]

    @patch('research_agent.OpenAI')
    def test_synthesize_success(self, mock_openai):
        """Generates a report via the LLM."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "## Report\nAll good."
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.synthesize_report(
                "topic",
                [
                    {
                        "query": "q",
                        "rationale": "r",
                        "formatted_query": "Q",
                        "api_endpoint": "media",
                        "result_count": 3,
                        "result_status": "success",
                        "result_data": {"media": [{"id": "1"}]},
                    }
                ],
            )

        assert result["status"] == "success"
        assert "Report" in result["report"]

    @patch('research_agent.OpenAI')
    def test_synthesize_handles_exception(self, mock_openai):
        """Falls back on LLM errors."""
        mock_openai.side_effect = Exception("fail")

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.synthesize_report("t", [])

        assert result["status"] == "fallback"


class TestRunResearch:
    """Test the end-to-end orchestrator."""

    @patch('research_agent.synthesize_report')
    @patch('research_agent.execute_searches')
    @patch('research_agent.decompose_topic')
    def test_run_research_end_to_end(self, mock_decompose, mock_execute, mock_synth):
        """Chains decompose → search → synthesize."""
        mock_decompose.return_value = [
            {"query": "q1", "rationale": "r1"},
        ]
        mock_execute.return_value = [
            {
                "query": "q1",
                "rationale": "r1",
                "formatted_query": "Q1",
                "api_endpoint": "media",
                "result_count": 2,
                "result_status": "success",
                "result_data": {},
            }
        ]
        mock_synth.return_value = {"status": "success", "report": "# Done"}

        result = research_agent.run_research("my topic")

        assert result["topic"] == "my topic"
        assert len(result["queries"]) == 1
        assert len(result["search_results"]) == 1
        assert result["report"]["status"] == "success"
        mock_decompose.assert_called_once_with("my topic")
        mock_execute.assert_called_once()
        mock_synth.assert_called_once()

    @patch('research_agent.synthesize_report')
    @patch('research_agent.execute_searches')
    @patch('research_agent.decompose_topic')
    def test_run_research_strips_result_data(self, mock_decompose, mock_execute, mock_synth):
        """The final output should not include bulky result_data."""
        mock_decompose.return_value = [{"query": "q", "rationale": "r"}]
        mock_execute.return_value = [
            {
                "query": "q",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 1,
                "result_status": "success",
                "result_data": {"media": [{"id": "big-payload"}]},
            }
        ]
        mock_synth.return_value = {"status": "success", "report": "ok"}

        result = research_agent.run_research("topic")

        for sr in result["search_results"]:
            assert "result_data" not in sr


class TestScriptStructure:
    """Ensure the script has the expected public interface."""

    def test_has_main(self):
        assert hasattr(research_agent, 'main')
        assert callable(research_agent.main)

    def test_script_exists(self):
        path = os.path.join(
            os.path.dirname(__file__), '..', '.github', 'scripts', 'research_agent.py'
        )
        assert os.path.exists(path)

    def test_script_is_executable(self):
        path = os.path.join(
            os.path.dirname(__file__), '..', '.github', 'scripts', 'research_agent.py'
        )
        assert os.access(path, os.X_OK)
