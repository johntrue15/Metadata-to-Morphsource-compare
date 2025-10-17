"""
Tests for the query processor Python scripts
"""
import os
import sys
import json
import types
import pytest
from unittest.mock import Mock, patch, MagicMock

# Add scripts directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts'))

# Provide a lightweight OpenAI stub when the package is unavailable (e.g., in tests)
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

import query_formatter
import morphosource_api
import chatgpt_processor


class TestQueryFormatter:
    """Test query_formatter.py script"""
    
    def test_format_query_without_api_key(self):
        """Test format_query returns fallback when no API key"""
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}, clear=True):
            result = query_formatter.format_query("test query")

            assert result['formatted_query'] == "test query"
            assert result['api_params'] == {'q': 'test query', 'per_page': 10}
            assert result['generated_url'] is None
            assert result['api_endpoint'] is None
    
    @patch('query_formatter.OpenAI')
    def test_format_query_with_api_key(self, mock_openai):
        """Test format_query with API key"""
        # Mock OpenAI response
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "https://www.morphosource.org/api/media?locale=en&search_field=all_fields"
            "&q=Serpentes&per_page=1&page=1"
        )
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("How many snakes?")

            assert 'formatted_query' in result
            assert 'api_params' in result
            assert result['api_params'].get('q') == 'Serpentes'
            assert result['api_endpoint'] == 'media'
    
    @patch('query_formatter.OpenAI')
    def test_format_query_handles_exception(self, mock_openai):
        """Test format_query handles exceptions gracefully"""
        mock_openai.side_effect = Exception("API Error")
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("test query")

            # Should fallback to original query
            assert result['formatted_query'] == "test query"
            assert result['api_params'] == {'q': 'test query', 'per_page': 10}
            assert result['api_endpoint'] is None
    
    @patch('query_formatter.OpenAI')
    def test_format_query_with_encoded_params(self, mock_openai):
        """Test format_query with new URL format including encoded array-style parameters"""
        # Mock OpenAI response with new URL format (encoded array-style parameters)
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "https://www.morphosource.org/api/physical-objects?f%5Btaxonomy_gbif%5D%5B%5D=Serpentes&locale=en&per_page=1&page=1&taxonomy_gbif=Serpentes"
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("How many snake specimens are available?")

            assert 'formatted_query' in result
            assert 'api_params' in result
            # Check for both array-style and plain parameters
            assert result['api_params'].get('taxonomy_gbif') == 'Serpentes'
            assert result['api_params'].get('locale') == 'en'
            assert result['api_params'].get('per_page') == '1'
            assert result['api_params'].get('page') == '1'
            assert result['api_endpoint'] == 'physical-objects'
            assert 'object_type' not in result['api_params']
    
    @patch('query_formatter.OpenAI')
    def test_format_query_ct_scans_with_modality(self, mock_openai):
        """Test format_query with CT scan request including modality filter"""
        # Mock OpenAI response for CT scans with modality filter
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "https://www.morphosource.org/api/media?f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography"
            "&locale=en&search_field=all_fields&q=Reptilia"
        )
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("Show me CT scans of reptiles")

            assert 'formatted_query' in result
            assert 'api_params' in result
            # Check for modality parameter
            assert result['api_params'].get('modality') or 'f[modality][]' in result['api_params']
            assert result['api_params'].get('q') == 'Reptilia'
            assert result['api_params'].get('locale') == 'en'
            assert result['api_params'].get('search_field') == 'all_fields'
            assert result['api_endpoint'] == 'media'

    @patch('query_formatter.OpenAI')
    def test_format_query_extracts_url_with_leading_text(self, mock_openai):
        """Ensure URLs embedded in bullet lists are still parsed."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            "- https://www.morphosource.org/api/media?locale=en&search_field=all_fields"
            "&q=Squamata&per_page=12&page=1"
        )
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("Show me squamates")

            assert result['generated_url'] == (
                "https://www.morphosource.org/api/media?locale=en&search_field=all_fields"
                "&q=Squamata&per_page=12&page=1"
            )
            assert result['api_params'].get('q') == 'Squamata'

    @patch('query_formatter.OpenAI')
    def test_format_query_infers_taxonomy_when_missing_url(self, mock_openai):
        """If no URL is returned, infer taxonomy term from the user query."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "   "
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client

        query = (
            "Here’s a snapshot of MorphoSource records matching the current Squamata search. "
            "The 12 specimens on this page are all lizards in the genus Sceloporus."
        )

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query(query)

        assert result['api_endpoint'] == 'physical-objects'
        assert result['formatted_query'] == 'Sceloporus'
        assert result['api_params']['taxonomy_gbif'] == 'Sceloporus'
        assert result['generated_url']


class TestMorphosourceAPI:
    """Test morphosource_api.py script"""
    
    @patch('morphosource_api.requests.get')
    def test_search_morphosource_success(self, mock_get):
        """Test successful MorphoSource API search"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'media': [
                {'id': '1', 'name': 'Test Media'}
            ]
        }
        mock_get.return_value = mock_response

        api_params = {'taxonomy_gbif': 'Serpentes', 'per_page': 1}
        result = morphosource_api.search_morphosource(api_params, 'Serpentes')

        assert result['summary']['status'] == 'success'
        assert result['summary']['count'] == 1
        assert 'media' in result['full_data']

    @patch('morphosource_api.requests.get')
    def test_search_morphosource_success_nested_response(self, mock_get):
        """Ensure nested `response` payloads still report non-zero counts."""

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'response': {
                'physical_objects': [
                    {'id': '0001', 'title': 'Specimen'}
                ],
                'pages': {
                    'total_count': 1
                }
            }
        }
        mock_get.return_value = mock_response

        api_params = {'taxonomy_gbif': 'Squamata', 'per_page': 12}
        result = morphosource_api.search_morphosource(api_params, 'Squamata')

        assert result['summary']['status'] == 'success'
        assert result['summary']['count'] == 1
        assert result['summary']['endpoint'] == 'media'
    
    @patch('morphosource_api.requests.get')
    def test_search_morphosource_error(self, mock_get):
        """Test MorphoSource API error handling"""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_get.return_value = mock_response

        api_params = {'q': 'test'}
        result = morphosource_api.search_morphosource(api_params, 'test')

        assert result['summary']['status'] == 'error'
        assert result['summary']['code'] == 404

    @patch('morphosource_api.requests.get')
    def test_search_morphosource_exception(self, mock_get):
        """Test MorphoSource API exception handling"""
        mock_get.side_effect = Exception("Network error")

        api_params = {'q': 'test'}
        result = morphosource_api.search_morphosource(api_params, 'test')

        assert result['summary']['status'] == 'error'
        assert 'message' in result['summary']

    @patch('morphosource_api.query_formatter.format_query')
    @patch('morphosource_api.requests.get')
    def test_search_morphosource_retry_on_empty_results(self, mock_get, mock_format_query):
        """Test MorphoSource API retries when initial query returns no data"""

        empty_response = Mock()
        empty_response.status_code = 200
        empty_response.json.return_value = {
            'physical_objects': [],
            'pages': {
                'total_count': 0
            }
        }

        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {
            'media': [
                {'id': '1', 'name': 'Lizard Scan'}
            ]
        }

        mock_get.side_effect = [empty_response, success_response]

        mock_format_query.return_value = {
            'original_query': 'Tell me about lizard specimens',
            'formatted_query': 'Anolis',
            'api_params': {
                'q': 'Anolis',
                'per_page': '12',
                'page': '1',
                'locale': 'en',
                'search_field': 'all_fields'
            },
            'generated_url': (
                'https://www.morphosource.org/api/media?locale=en&search_field=all_fields'
                '&q=Anolis&per_page=12&page=1'
            ),
            'api_endpoint': 'media'
        }

        initial_params = {'taxonomy_gbif': 'Squamata', 'per_page': '12'}
        query_info = {
            'original_query': 'Tell me about lizard specimens',
            'formatted_query': 'Squamata',
            'api_params': initial_params,
            'generated_url': 'https://www.morphosource.org/api/physical-objects?taxonomy_gbif=Squamata&per_page=12&page=1&locale=en',
            'api_endpoint': 'physical-objects'
        }

        with patch.dict(os.environ, {'OPENAI_API_KEY': 'retry-key'}):
            result = morphosource_api.search_morphosource(dict(initial_params), 'Squamata', query_info=query_info)

        assert mock_get.call_count == 2
        assert mock_format_query.call_count == 1
        assert result['summary']['status'] == 'success'
        assert result['summary']['count'] == 1
        assert result['summary']['attempts'][0]['endpoint'] == 'physical-objects'
        assert result['summary']['attempts'][1]['endpoint'] == 'media'
        assert result['query_info']['formatted_query'] == 'Anolis'


class TestChatGPTProcessor:
    """Test chatgpt_processor.py script"""
    
    def test_process_without_api_key(self):
        """Test process_with_chatgpt without API key"""
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}, clear=True):
            result = chatgpt_processor.process_with_chatgpt(
                "test query",
                {},
                {}
            )
            
            assert result['status'] == 'error'
            assert 'OPENAI_API_KEY' in result['message']
    
    @patch('chatgpt_processor.OpenAI')
    def test_process_with_chatgpt_success(self, mock_openai):
        """Test successful ChatGPT processing"""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Found 5 snake specimens"
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client
        
        morphosource_data = {
            'status': 'success',
            'media': [{'id': '1'}]
        }
        formatted_query_info = {
            'formatted_query': 'Serpentes',
            'api_params': {'q': 'Serpentes'}
        }
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = chatgpt_processor.process_with_chatgpt(
                "How many snakes?",
                morphosource_data,
                formatted_query_info
            )
            
            assert result['status'] == 'success'
            assert result['query'] == "How many snakes?"
            assert 'response' in result
    
    @patch('chatgpt_processor.OpenAI')
    def test_process_with_chatgpt_exception(self, mock_openai):
        """Test ChatGPT processing exception handling"""
        mock_openai.side_effect = Exception("API Error")
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = chatgpt_processor.process_with_chatgpt(
                "test query",
                {},
                {}
            )
            
            assert result['status'] == 'error'
            assert 'message' in result


class TestScriptIntegration:
    """Test that scripts can be imported and have correct structure"""
    
    def test_query_formatter_has_main(self):
        """Test query_formatter has main function"""
        assert hasattr(query_formatter, 'main')
        assert callable(query_formatter.main)
    
    def test_morphosource_api_has_main(self):
        """Test morphosource_api has main function"""
        assert hasattr(morphosource_api, 'main')
        assert callable(morphosource_api.main)
    
    def test_chatgpt_processor_has_main(self):
        """Test chatgpt_processor has main function"""
        assert hasattr(chatgpt_processor, 'main')
        assert callable(chatgpt_processor.main)
    
    def test_scripts_exist(self):
        """Test that script files exist"""
        scripts_dir = os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts')
        
        assert os.path.exists(os.path.join(scripts_dir, 'query_formatter.py'))
        assert os.path.exists(os.path.join(scripts_dir, 'morphosource_api.py'))
        assert os.path.exists(os.path.join(scripts_dir, 'chatgpt_processor.py'))
    
    def test_scripts_are_executable(self):
        """Test that script files are executable"""
        scripts_dir = os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts')
        
        for script in ['query_formatter.py', 'morphosource_api.py', 'chatgpt_processor.py']:
            script_path = os.path.join(scripts_dir, script)
            # Check if file has execute permission
            assert os.access(script_path, os.X_OK), f"{script} should be executable"
