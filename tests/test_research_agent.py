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
        """Falls back on LLM errors and includes error in conclusion."""
        mock_openai.side_effect = Exception("fail")

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.synthesize_report("t", [])

        assert result["status"] == "fallback"
        assert "fail" in result["report"]
        assert "AI-powered synthesis was attempted" in result["report"]

    @patch('research_agent.OpenAI')
    def test_synthesize_falls_back_on_empty_content(self, mock_openai):
        """Falls back when LLM returns empty content."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = ""
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
                        "result_count": 12,
                        "result_status": "success",
                        "result_data": {},
                    }
                ],
            )

        assert result["status"] == "fallback"
        assert "topic" in result["report"]
        assert "12 result(s)" in result["report"]
        assert "LLM returned empty response" in result["report"]

    @patch('research_agent.OpenAI')
    def test_synthesize_falls_back_on_none_content(self, mock_openai):
        """Falls back when LLM returns None content."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = research_agent.synthesize_report("topic", [])

        assert result["status"] == "fallback"
        assert "LLM returned empty response" in result["report"]

    def test_synthesize_no_api_key_suggests_configuration(self):
        """When no API key is set, conclusion suggests configuring it."""
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}, clear=True):
            result = research_agent.synthesize_report("topic", [])

        assert result["status"] == "fallback"
        assert "OPENAI_API_KEY" in result["report"]


class TestFallbackReport:
    """Test the fallback report generation."""

    def test_includes_sample_records(self):
        """Fallback report includes sample record names when available."""
        results = [
            {
                "query": "specimen",
                "rationale": "r",
                "formatted_query": "specimen",
                "api_endpoint": "physical-objects",
                "result_count": 3,
                "result_status": "success",
                "result_data": {
                    "physical_objects": [
                        {"name": "Alligator skull"},
                        {"name": "Felis catus mandible"},
                    ]
                },
            }
        ]
        result = research_agent._fallback_report("test topic", results)
        assert "Alligator skull" in result["report"]
        assert "Felis catus mandible" in result["report"]

    def test_handles_missing_result_data(self):
        """Fallback report works when result_data is absent."""
        results = [
            {
                "query": "q",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 0,
                "result_status": "success",
            }
        ]
        result = research_agent._fallback_report("topic", results)
        assert result["status"] == "fallback"
        assert "0 result(s)" in result["report"]

    def test_no_api_key_conclusion(self):
        """Conclusion suggests configuring the API key when reason is no_api_key."""
        result = research_agent._fallback_report("topic", [], reason="no_api_key")
        assert "OPENAI_API_KEY" in result["report"]
        assert "ensure" in result["report"].lower()

    def test_default_reason_matches_no_api_key(self):
        """Default (None) reason produces the same conclusion as no_api_key."""
        default = research_agent._fallback_report("topic", [])
        explicit = research_agent._fallback_report("topic", [], reason="no_api_key")
        assert default["report"] == explicit["report"]

    def test_error_reason_shown_in_conclusion(self):
        """When an error reason is provided, it appears in the conclusion."""
        result = research_agent._fallback_report(
            "topic", [], reason="Connection timed out"
        )
        assert "Connection timed out" in result["report"]
        assert "AI-powered synthesis was attempted" in result["report"]

    def test_empty_response_reason(self):
        """LLM empty response reason is surfaced in conclusion."""
        result = research_agent._fallback_report(
            "topic", [], reason="LLM returned empty response"
        )
        assert "LLM returned empty response" in result["report"]
        assert "OPENAI_API_KEY" not in result["report"]


class TestRefineZeroResultQueries:
    """Test the iterative refinement logic."""

    def test_generates_refined_queries_for_zero_results(self):
        """Produces simplified queries from zero-result searches."""
        search_results = [
            {
                "query": "rare deep-sea anglerfish CT",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 0,
                "result_status": "success",
                "result_data": {},
            },
        ]
        refined = research_agent._refine_zero_result_queries(search_results)
        assert len(refined) >= 1
        # Should contain simplified terms, not the full query
        for q in refined:
            assert len(q["query"]) < len(search_results[0]["query"])
            assert "rationale" in q

    def test_skips_queries_with_results(self):
        """Does not refine queries that already have results."""
        search_results = [
            {
                "query": "specimen",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 12,
                "result_status": "success",
                "result_data": {},
            },
        ]
        refined = research_agent._refine_zero_result_queries(search_results)
        assert refined == []

    def test_respects_max_queries_limit(self):
        """Refined queries are capped at MAX_QUERIES."""
        search_results = [
            {
                "query": f"word{i} extra{i} more{i} stuff{i}",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 0,
                "result_status": "success",
                "result_data": {},
            }
            for i in range(10)
        ]
        refined = research_agent._refine_zero_result_queries(search_results)
        assert len(refined) <= research_agent.MAX_QUERIES


class TestRefineSearches:
    """Test the refine_searches orchestration."""

    @patch('research_agent.execute_searches')
    def test_refine_searches_with_zero_results(self, mock_execute):
        """Calls execute_searches with refined queries."""
        mock_execute.return_value = [
            {
                "query": "anglerfish",
                "rationale": "Simplified retry",
                "formatted_query": "anglerfish",
                "api_endpoint": "media",
                "result_count": 5,
                "result_status": "success",
                "result_data": {},
            }
        ]
        search_results = [
            {
                "query": "rare deep-sea anglerfish CT",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 0,
                "result_status": "success",
                "result_data": {},
            }
        ]
        new_results, had_refinements = research_agent.refine_searches(search_results)

        assert had_refinements is True
        assert len(new_results) >= 1
        mock_execute.assert_called_once()

    def test_refine_searches_no_zero_results(self):
        """Returns empty when no queries had zero results."""
        search_results = [
            {
                "query": "specimen",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 12,
                "result_status": "success",
                "result_data": {},
            }
        ]
        new_results, had_refinements = research_agent.refine_searches(search_results)
        assert had_refinements is False
        assert new_results == []


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

    @patch('research_agent.synthesize_report')
    @patch('research_agent.refine_searches')
    @patch('research_agent.execute_searches')
    @patch('research_agent.decompose_topic')
    def test_run_research_iterates_on_zero_results(
        self, mock_decompose, mock_execute, mock_refine, mock_synth
    ):
        """Calls refine_searches when initial queries return zero results."""
        mock_decompose.return_value = [
            {"query": "obscure taxon CT", "rationale": "r"},
        ]
        mock_execute.return_value = [
            {
                "query": "obscure taxon CT",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 0,
                "result_status": "success",
                "result_data": {},
            }
        ]
        mock_refine.side_effect = [
            (
                [
                    {
                        "query": "taxon",
                        "rationale": "Simplified retry",
                        "formatted_query": "taxon",
                        "api_endpoint": "media",
                        "result_count": 3,
                        "result_status": "success",
                        "result_data": {},
                    }
                ],
                True,
            ),
            ([], False),
        ]
        mock_synth.return_value = {"status": "fallback", "report": "report text"}

        result = research_agent.run_research("obscure topic")

        # Should have called refine at least once
        mock_refine.assert_called()
        # Final results should include the refined query's results
        assert len(result["search_results"]) == 2
        assert result["search_results"][1]["result_count"] == 3

    @patch('research_agent.synthesize_report')
    @patch('research_agent.execute_searches')
    @patch('research_agent.decompose_topic')
    def test_run_research_skips_refinement_when_all_have_results(
        self, mock_decompose, mock_execute, mock_synth
    ):
        """Skips refinement when all queries already have results."""
        mock_decompose.return_value = [
            {"query": "specimen", "rationale": "r"},
        ]
        mock_execute.return_value = [
            {
                "query": "specimen",
                "rationale": "r",
                "formatted_query": "Q",
                "api_endpoint": "media",
                "result_count": 12,
                "result_status": "success",
                "result_data": {},
            }
        ]
        mock_synth.return_value = {"status": "success", "report": "ok"}

        result = research_agent.run_research("topic")

        # No refinement needed - only 1 result from the original search
        assert len(result["search_results"]) == 1


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
