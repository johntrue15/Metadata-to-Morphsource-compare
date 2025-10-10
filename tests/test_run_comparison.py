"""
Unit tests for run_comparison.py
"""
import unittest
import os
import sys
import tempfile
import shutil
from unittest.mock import Mock, patch, MagicMock, call

# Add parent directory to path to import module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from run_comparison import ensure_dir


class TestRunComparison(unittest.TestCase):
    """Test cases for run_comparison.py functions"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.test_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up test fixtures"""
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_ensure_dir_creates_directory(self):
        """Test that ensure_dir creates a directory that doesn't exist"""
        new_dir = os.path.join(self.test_dir, "new_directory")
        self.assertFalse(os.path.exists(new_dir))
        
        ensure_dir(new_dir)
        
        self.assertTrue(os.path.exists(new_dir))
        self.assertTrue(os.path.isdir(new_dir))
    
    def test_ensure_dir_with_existing_directory(self):
        """Test that ensure_dir doesn't fail if directory already exists"""
        existing_dir = os.path.join(self.test_dir, "existing_directory")
        os.makedirs(existing_dir)
        self.assertTrue(os.path.exists(existing_dir))
        
        # Should not raise an error
        ensure_dir(existing_dir)
        
        self.assertTrue(os.path.exists(existing_dir))
    
    def test_ensure_dir_with_nested_path(self):
        """Test that ensure_dir creates nested directories"""
        nested_dir = os.path.join(self.test_dir, "level1", "level2", "level3")
        self.assertFalse(os.path.exists(nested_dir))
        
        ensure_dir(nested_dir)
        
        self.assertTrue(os.path.exists(nested_dir))
        self.assertTrue(os.path.isdir(nested_dir))


class TestMainFunction(unittest.TestCase):
    """Test cases for the main() function"""
    
    @patch('run_comparison.subprocess.run')
    @patch('run_comparison.subprocess.Popen')
    @patch('run_comparison.ensure_dir')
    @patch('os.path.exists')
    @patch('os.listdir')
    @patch('os.path.getsize')
    def test_main_without_api_key(self, mock_getsize, mock_listdir, mock_exists, 
                                   mock_ensure_dir, mock_popen, mock_run):
        """Test main function without API key (skips verification)"""
        # Mock the Popen process for compare.py
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        
        # Mock file existence checks
        def exists_side_effect(path):
            return 'matched.csv' in path
        mock_exists.side_effect = exists_side_effect
        
        # Mock listdir for output files
        mock_listdir.return_value = ['matched.csv']
        mock_getsize.return_value = 1000
        
        # Import and run main with test arguments
        with patch('sys.argv', ['run_comparison.py', '--csv', 'test.csv']):
            from run_comparison import main
            main()
        
        # Verify ensure_dir was called
        mock_ensure_dir.assert_called_once_with('data/output')
        
        # Verify compare.py was called
        mock_popen.assert_called_once()
        
        # Verify verification was not called (no API key)
        mock_run.assert_not_called()
    
    @patch('run_comparison.subprocess.run')
    @patch('run_comparison.subprocess.Popen')
    @patch('run_comparison.ensure_dir')
    @patch('os.path.exists')
    @patch('os.listdir')
    @patch('os.path.getsize')
    def test_main_with_api_key(self, mock_getsize, mock_listdir, mock_exists,
                                mock_ensure_dir, mock_popen, mock_run):
        """Test main function with API key (runs verification)"""
        # Mock the Popen process for compare.py
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        
        # Mock file existence checks
        def exists_side_effect(path):
            if 'matched.csv' in path:
                return True
            if 'confirmed_matches.csv' in path:
                return True
            return False
        mock_exists.side_effect = exists_side_effect
        
        # Mock listdir for output files
        mock_listdir.return_value = ['matched.csv', 'confirmed_matches.csv']
        mock_getsize.return_value = 1000
        
        # Import and run main with test arguments including API key
        with patch('sys.argv', ['run_comparison.py', '--csv', 'test.csv', '--api-key', 'test_key']):
            from run_comparison import main
            main()
        
        # Verify ensure_dir was called
        mock_ensure_dir.assert_called_once_with('data/output')
        
        # Verify compare.py was called
        mock_popen.assert_called_once()
        
        # Verify verification was called with API key
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        self.assertIn('verify_pixel_spacing.py', call_args[0][0])


if __name__ == '__main__':
    unittest.main()
