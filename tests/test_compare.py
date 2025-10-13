"""
Unit tests for compare.py
"""
import unittest
import pandas as pd
import json
import os
import sys
import tempfile
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path to import compare module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from compare import MorphosourceMatcher


class TestMorphosourceMatcher(unittest.TestCase):
    """Test cases for MorphosourceMatcher class"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.matcher = MorphosourceMatcher()
        
    def test_initialization(self):
        """Test MorphosourceMatcher initialization"""
        matcher = MorphosourceMatcher()
        self.assertIsNone(matcher.morphosource_data)
        self.assertIsNone(matcher.comparison_data)
        self.assertEqual(matcher.matches, [])
        self.assertEqual(matcher.match_scores, [])
        self.assertEqual(matcher.threshold, 80)
        
    def test_initialization_with_data(self):
        """Test initialization with data"""
        mock_ms_data = pd.DataFrame({'id': [1, 2, 3]})
        mock_comp_data = pd.DataFrame({'name': ['a', 'b', 'c']})
        matcher = MorphosourceMatcher(morphosource_data=mock_ms_data, 
                                     comparison_data=mock_comp_data)
        self.assertIsNotNone(matcher.morphosource_data)
        self.assertIsNotNone(matcher.comparison_data)
    
    def test_normalize_catalog_number_standard_format(self):
        """Test catalog number normalization with standard format"""
        test_cases = [
            ("UF:Herp:14628-1", ("UF:14628", "UF", "14628")),
            ("MCZ:Herp:4291", ("MCZ:4291", "MCZ", "4291")),
            ("AMNH:1234", ("AMNH:1234", "AMNH", "1234")),
        ]
        
        for input_val, expected in test_cases:
            with self.subTest(input=input_val):
                result = self.matcher.normalize_catalog_number(input_val)
                self.assertEqual(result, expected)
    
    def test_normalize_catalog_number_with_extensions(self):
        """Test catalog number normalization with file extensions"""
        test_cases = [
            ("UF90369.pca", ("UF:90369", "UF", "90369")),
            ("UF-herps-68567-body.pca", ("UF:68567", "UF", "68567")),
            ("UF-H-165490-head.pca", ("UF:165490", "UF", "165490")),
        ]
        
        for input_val, expected in test_cases:
            with self.subTest(input=input_val):
                result = self.matcher.normalize_catalog_number(input_val)
                self.assertEqual(result, expected)
    
    def test_normalize_catalog_number_empty_input(self):
        """Test catalog number normalization with empty input"""
        result = self.matcher.normalize_catalog_number("")
        self.assertEqual(result, ("", "", ""))
        
        result = self.matcher.normalize_catalog_number(None)
        self.assertEqual(result, ("", "", ""))
    
    def test_load_morphosource_data_json(self):
        """Test loading Morphosource data from JSON file"""
        # Create temporary JSON file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            test_data = [
                {
                    "id": "000001",
                    "metadata": {
                        "Taxonomy": "Reptilia",
                        "Object": "UF:12345",
                        "Element or Part": "skull"
                    }
                },
                {
                    "id": "000002",
                    "metadata": {
                        "Taxonomy": "Mammalia",
                        "Object": "MCZ:67890",
                        "Element or Part": "femur"
                    }
                }
            ]
            json.dump(test_data, f)
            temp_file = f.name
        
        try:
            # Test loading
            result = self.matcher.load_morphosource_data(temp_file)
            self.assertTrue(result)
            self.assertIsNotNone(self.matcher.morphosource_data)
            self.assertEqual(len(self.matcher.morphosource_data), 2)
            self.assertIn('taxonomy', self.matcher.morphosource_data.columns)
            self.assertIn('object_id', self.matcher.morphosource_data.columns)
        finally:
            os.unlink(temp_file)
    
    def test_load_comparison_data_csv(self):
        """Test loading comparison data from CSV file"""
        # Create temporary CSV file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("catalog_number,genus,species\n")
            f.write("UF:12345,Anolis,carolinensis\n")
            f.write("MCZ:67890,Mus,musculus\n")
            temp_file = f.name
        
        try:
            # Test loading
            result = self.matcher.load_comparison_data(temp_file)
            self.assertTrue(result)
            self.assertIsNotNone(self.matcher.comparison_data)
            self.assertEqual(len(self.matcher.comparison_data), 2)
            self.assertIn('catalog_number', self.matcher.comparison_data.columns)
        finally:
            os.unlink(temp_file)
    
    def test_load_morphosource_data_file_not_found(self):
        """Test loading Morphosource data with non-existent file"""
        result = self.matcher.load_morphosource_data("nonexistent_file.json")
        self.assertFalse(result)
    
    def test_load_comparison_data_file_not_found(self):
        """Test loading comparison data with non-existent file"""
        result = self.matcher.load_comparison_data("nonexistent_file.csv")
        self.assertFalse(result)


class TestCatalogNormalization(unittest.TestCase):
    """Dedicated test suite for catalog number normalization"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.matcher = MorphosourceMatcher()
    
    def test_case_insensitivity(self):
        """Test that normalization is case insensitive"""
        lower = self.matcher.normalize_catalog_number("uf:herp:14628")
        upper = self.matcher.normalize_catalog_number("UF:HERP:14628")
        mixed = self.matcher.normalize_catalog_number("Uf:Herp:14628")
        
        self.assertEqual(lower[0], upper[0])
        self.assertEqual(upper[0], mixed[0])
    
    def test_various_separators(self):
        """Test normalization with various separator styles"""
        colon = self.matcher.normalize_catalog_number("UF:12345")
        dash = self.matcher.normalize_catalog_number("UF-12345")
        underscore = self.matcher.normalize_catalog_number("UF_12345")
        
        # All should produce the same normalized number
        self.assertEqual(colon[2], "12345")
        self.assertEqual(dash[2], "12345")
        self.assertEqual(underscore[2], "12345")
    
    def test_suffix_removal(self):
        """Test removal of common suffixes"""
        suffixes = ["-head", "-body", "-skull", "-skeleton"]
        base = "UF-12345"
        
        expected_number = "12345"
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                result = self.matcher.normalize_catalog_number(f"{base}{suffix}.pca")
                self.assertEqual(result[2], expected_number)


if __name__ == '__main__':
    unittest.main()
