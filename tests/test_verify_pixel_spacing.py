"""
Unit tests for verify_pixel_spacing.py
"""
import unittest
import pytest
pd = pytest.importorskip("pandas")
import os
import sys
import tempfile
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path to import module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from verify_pixel_spacing import MorphosourceVoxelVerifier


class TestMorphosourceVoxelVerifier(unittest.TestCase):
    """Test cases for MorphosourceVoxelVerifier class"""
    
    def setUp(self):
        """Set up test fixtures"""
        # Create a temporary CSV file for testing
        self.temp_csv = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False)
        self.temp_csv.write("Morphosource_URL,x_voxel_spacing_mm,y_voxel_spacing_mm,z_voxel_spacing_mm\n")
        self.temp_csv.write("https://www.morphosource.org/concern/media/000407755,0.04134338,0.04134338,0.04134338\n")
        self.temp_csv.write("https://www.morphosource.org/concern/media/000407756,0.05,0.05,0.05\n")
        self.temp_csv.close()
        
        self.verifier = MorphosourceVoxelVerifier(self.temp_csv.name)
    
    def tearDown(self):
        """Clean up test fixtures"""
        if os.path.exists(self.temp_csv.name):
            os.unlink(self.temp_csv.name)
    
    def test_initialization(self):
        """Test MorphosourceVoxelVerifier initialization"""
        verifier = MorphosourceVoxelVerifier("test.csv")
        self.assertEqual(verifier.input_csv, "test.csv")
        self.assertIsNone(verifier.api_key)
        self.assertEqual(verifier.base_url, "https://www.morphosource.org/api/media")
        self.assertIsNone(verifier.matches_data)
        self.assertIsNone(verifier.verified_data)
        self.assertEqual(verifier.headers, {})
    
    def test_initialization_with_api_key(self):
        """Test initialization with API key"""
        verifier = MorphosourceVoxelVerifier("test.csv", api_key="test_key_123")
        self.assertEqual(verifier.api_key, "test_key_123")
        self.assertEqual(verifier.headers["Authorization"], "Bearer test_key_123")
    
    def test_load_data_success(self):
        """Test successful data loading"""
        result = self.verifier.load_data()
        self.assertTrue(result)
        self.assertIsNotNone(self.verifier.matches_data)
        self.assertEqual(len(self.verifier.matches_data), 2)
        self.assertIn('Morphosource_URL', self.verifier.matches_data.columns)
        self.assertIn('x_voxel_spacing_mm', self.verifier.matches_data.columns)
    
    def test_load_data_file_not_found(self):
        """Test loading data with non-existent file"""
        verifier = MorphosourceVoxelVerifier("nonexistent_file.csv")
        result = verifier.load_data()
        self.assertFalse(result)
    
    def test_extract_media_id_standard_url(self):
        """Test media ID extraction from standard URL"""
        test_cases = [
            ("https://www.morphosource.org/concern/media/000407755?locale=en", "000407755"),
            ("https://www.morphosource.org/concern/media/000407756", "000407756"),
            ("https://www.morphosource.org/media/123456789", "123456789"),
        ]
        
        for url, expected_id in test_cases:
            with self.subTest(url=url):
                result = self.verifier.extract_media_id(url)
                self.assertEqual(result, expected_id)
    
    def test_extract_media_id_invalid_url(self):
        """Test media ID extraction with invalid URL"""
        test_cases = [None, "", "not_a_url", "https://example.com"]
        
        for url in test_cases:
            with self.subTest(url=url):
                result = self.verifier.extract_media_id(url)
                self.assertIsNone(result)
    
    def test_extract_first_value_from_list(self):
        """Test _extract_first_value with list input"""
        result = self.verifier._extract_first_value([1, 2, 3])
        self.assertEqual(result, 1)
        
        result = self.verifier._extract_first_value(["value1", "value2"])
        self.assertEqual(result, "value1")
    
    def test_extract_first_value_from_single(self):
        """Test _extract_first_value with non-list input"""
        result = self.verifier._extract_first_value("single_value")
        self.assertEqual(result, "single_value")
        
        result = self.verifier._extract_first_value(42)
        self.assertEqual(result, 42)
    
    def test_extract_first_value_empty_list(self):
        """Test _extract_first_value with empty list"""
        result = self.verifier._extract_first_value([])
        self.assertEqual(result, [])
    
    def test_compare_pixel_spacing_match(self):
        """Test pixel spacing comparison with matching values"""
        result = self.verifier.compare_pixel_spacing(
            "0.04134338", "0.04134338", "0.04134338",
            "0.04134338", "0.04134338", "0.04134338"
        )
        self.assertTrue(result)
    
    def test_compare_pixel_spacing_match_with_tolerance(self):
        """Test pixel spacing comparison with values within tolerance"""
        result = self.verifier.compare_pixel_spacing(
            "0.04134338", "0.04134338", "0.04134338",
            "0.04134339", "0.04134339", "0.04134339",
            tolerance=0.0001
        )
        self.assertTrue(result)
    
    def test_compare_pixel_spacing_mismatch(self):
        """Test pixel spacing comparison with mismatched values"""
        result = self.verifier.compare_pixel_spacing(
            "0.04134338", "0.04134338", "0.04134338",
            "0.05", "0.05", "0.05"
        )
        self.assertFalse(result)
    
    def test_compare_pixel_spacing_with_none(self):
        """Test pixel spacing comparison with None values"""
        result = self.verifier.compare_pixel_spacing(
            None, "0.04134338", "0.04134338",
            "0.04134338", "0.04134338", "0.04134338"
        )
        self.assertFalse(result)
    
    def test_compare_pixel_spacing_numeric_values(self):
        """Test pixel spacing comparison with numeric (float) values"""
        result = self.verifier.compare_pixel_spacing(
            0.04134338, 0.04134338, 0.04134338,
            0.04134338, 0.04134338, 0.04134338
        )
        self.assertTrue(result)
    
    @patch('verify_pixel_spacing.requests.get')
    def test_get_media_details_success(self, mock_get):
        """Test successful media details retrieval"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "000407755",
            "pixel_spacing_x": "0.04134338",
            "pixel_spacing_y": "0.04134338",
            "pixel_spacing_z": "0.04134338"
        }
        mock_get.return_value = mock_response
        
        result = self.verifier.get_media_details("000407755")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "000407755")
    
    @patch('verify_pixel_spacing.requests.get')
    def test_get_media_details_failure(self, mock_get):
        """Test media details retrieval failure"""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response
        
        result = self.verifier.get_media_details("invalid_id")
        self.assertIsNone(result)
    
    @patch('verify_pixel_spacing.requests.get')
    def test_get_media_details_with_api_key(self, mock_get):
        """Test media details retrieval with API key"""
        verifier = MorphosourceVoxelVerifier(self.temp_csv.name, api_key="test_key")
        
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "000407755"}
        mock_get.return_value = mock_response
        
        result = verifier.get_media_details("000407755")
        
        # Verify that the API key was sent in headers
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args[1]
        self.assertIn('headers', call_kwargs)
        self.assertEqual(call_kwargs['headers']['Authorization'], 'Bearer test_key')


class TestPixelSpacingExtraction(unittest.TestCase):
    """Dedicated test suite for pixel spacing extraction logic"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.temp_csv = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False)
        self.temp_csv.write("Morphosource_URL,x_voxel_spacing_mm,y_voxel_spacing_mm,z_voxel_spacing_mm\n")
        self.temp_csv.write("https://www.morphosource.org/concern/media/000407755,0.04,0.04,0.04\n")
        self.temp_csv.close()
        
        self.verifier = MorphosourceVoxelVerifier(self.temp_csv.name)
    
    def tearDown(self):
        """Clean up test fixtures"""
        if os.path.exists(self.temp_csv.name):
            os.unlink(self.temp_csv.name)
    
    def test_extract_pixel_spacing_standard_format(self):
        """Test pixel spacing extraction with standard format"""
        media_data = {
            "media": {
                "x_pixel_spacing": "0.04134338",
                "y_pixel_spacing": "0.04134338",
                "z_pixel_spacing": "0.04134338"
            }
        }
        
        x, y, z = self.verifier.extract_pixel_spacing(media_data)
        self.assertEqual(x, "0.04134338")
        self.assertEqual(y, "0.04134338")
        self.assertEqual(z, "0.04134338")
    
    def test_extract_pixel_spacing_with_list_values(self):
        """Test pixel spacing extraction when values are in lists"""
        media_data = {
            "media": {
                "x_pixel_spacing": ["0.04134338", "0.05"],
                "y_pixel_spacing": ["0.04134338", "0.05"],
                "z_pixel_spacing": ["0.04134338", "0.05"]
            }
        }
        
        x, y, z = self.verifier.extract_pixel_spacing(media_data)
        # Should extract first value from list
        self.assertEqual(x, "0.04134338")
        self.assertEqual(y, "0.04134338")
        self.assertEqual(z, "0.04134338")


if __name__ == '__main__':
    unittest.main()
