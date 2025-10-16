"""
Tests for the query processor Python scripts
"""
import os
import sys
import json
import pytest
from unittest.mock import Mock, patch, MagicMock

# Add scripts directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts'))

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
    
    @patch('query_formatter.OpenAI')
    def test_format_query_with_api_key(self, mock_openai):
        """Test format_query with API key"""
        # Mock OpenAI response
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "https://www.morphosource.org/api/media?taxonomy_gbif=Serpentes&per_page=1&page=1"
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai.return_value = mock_client
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("How many snakes?")
            
            assert 'formatted_query' in result
            assert 'api_params' in result
            assert result['api_params'].get('taxonomy_gbif') == 'Serpentes'
    
    @patch('query_formatter.OpenAI')
    def test_format_query_handles_exception(self, mock_openai):
        """Test format_query handles exceptions gracefully"""
        mock_openai.side_effect = Exception("API Error")
        
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
            result = query_formatter.format_query("test query")
            
            # Should fallback to original query
            assert result['formatted_query'] == "test query"
            assert result['api_params'] == {'q': 'test query', 'per_page': 10}


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
            'api_params': {'taxonomy_gbif': 'Serpentes'}
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
