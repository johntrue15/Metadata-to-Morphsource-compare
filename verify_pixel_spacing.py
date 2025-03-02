# Import required libraries
import pandas as pd
import requests
import re
import json
import time
import os
from urllib.parse import urlparse
import argparse

class MorphosourceVoxelVerifier:
    def __init__(self, input_csv, api_key=None):
        """
        Initialize the verifier with the matched CSV file from compare.py
        
        Args:
            input_csv (str): Path to the matched.csv file
            api_key (str, optional): MorphoSource API key for accessing private media
        """
        self.input_csv = input_csv
        self.api_key = api_key
        self.base_url = "https://www.morphosource.org/api/media"
        self.matches_data = None
        self.verified_data = None
        self.headers = {}
        
        # Configure API key if provided
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
    
    def load_data(self):
        """Load the matched.csv file from compare.py"""
        try:
            print(f"Loading data from {self.input_csv}...")
            self.matches_data = pd.read_csv(self.input_csv)
            
            # Check available columns for debugging
            print(f"Available columns in CSV: {list(self.matches_data.columns)}")
            
            # Check if required columns exist
            required_cols = ['Morphosource_URL', 'x_voxel_spacing_mm', 'y_voxel_spacing_mm', 'z_voxel_spacing_mm']
            missing_cols = [col for col in required_cols if col not in self.matches_data.columns]
            
            if missing_cols:
                # Try alternate column names
                column_mappings = {
                    'Morphosource_URL': ['url', 'URL', 'morphosource_url', 'Match_URL'],
                    'x_voxel_spacing_mm': ['voxel_x_spacing', 'x_spacing', 'x_pixel_spacing', 'Voxel_x_spacing', 'pixel_spacing_x'],
                    'y_voxel_spacing_mm': ['voxel_y_spacing', 'y_spacing', 'y_pixel_spacing', 'Voxel_y_spacing', 'pixel_spacing_y'],
                    'z_voxel_spacing_mm': ['voxel_z_spacing', 'z_spacing', 'z_pixel_spacing', 'Voxel_z_spacing', 'pixel_spacing_z']
                }
                
                for required_col in missing_cols:
                    for alt_col in column_mappings[required_col]:
                        if alt_col in self.matches_data.columns:
                            print(f"Mapping {alt_col} to {required_col}")
                            self.matches_data[required_col] = self.matches_data[alt_col]
                            break
            
            # Check again for required columns
            missing_cols = [col for col in required_cols if col not in self.matches_data.columns]
            if missing_cols:
                print(f"Error: Missing required columns: {missing_cols}")
                print(f"Available columns: {list(self.matches_data.columns)}")
                
                # Create empty columns with NaN values for missing columns
                print(f"Creating empty columns for missing fields: {missing_cols}")
                for col in missing_cols:
                    self.matches_data[col] = float('nan')
                
            print(f"Successfully loaded {len(self.matches_data)} records")
            return True
            
        except Exception as e:
            print(f"Error loading data: {str(e)}")
            return False
    
    def extract_media_id(self, url):
        """
        Extract the media ID from a MorphoSource URL
        
        Args:
            url (str): MorphoSource URL
            
        Returns:
            str: Media ID if found, None otherwise
        """
        if not url or pd.isna(url):
            return None
            
        # Extract media ID using regex patterns
        # Pattern for URLs like: https://www.morphosource.org/concern/media/000000000?locale=en
        pattern1 = r'morphosource\.org/concern/media/([a-zA-Z0-9]+)'
        # Alternate pattern for older URLs
        pattern2 = r'morphosource\.org/media/([a-zA-Z0-9]+)'
        
        match = re.search(pattern1, url)
        if not match:
            match = re.search(pattern2, url)
            
        if match:
            return match.group(1)
        
        # If no match found, try parsing the URL path
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.split('/')
        
        # Look for 'media' in the path
        if 'media' in path_parts:
            media_index = path_parts.index('media')
            if media_index + 1 < len(path_parts):
                return path_parts[media_index + 1]
                
        return None
    
    def get_media_details(self, media_id):
        """
        Get media details from MorphoSource API
        
        Args:
            media_id (str): Media ID
            
        Returns:
            dict: Media details if successful, None otherwise
        """
        if not media_id:
            return None
            
        url = f"{self.base_url}/{media_id}"
        
        try:
            print(f"Fetching details for media ID: {media_id}")
            response = requests.get(url, headers=self.headers)
            
            # Check if request was successful
            if response.status_code == 200:
                return response.json()
            else:
                print(f"API request failed with status code {response.status_code}: {response.text}")
                return None
                
        except Exception as e:
            print(f"Error fetching media details: {str(e)}")
            return None
    
    def extract_pixel_spacing(self, media_data):
        """
        Extract pixel spacing values from media data
        
        Args:
            media_data (dict): Media data from API
            
        Returns:
            tuple: (x_spacing, y_spacing, z_spacing) if found, (None, None, None) otherwise
        """
        if not media_data:
            return None, None, None
            
        try:
            # Check if there's a 'response' wrapper (seen in some API responses)
            if 'response' in media_data:
                media_data = media_data['response']
            
            # Check if the response has the 'media' field (new API structure)
            if 'media' in media_data:
                media = media_data['media']
                
                # Look for pixel spacing in this media object
                x_spacing = None
                y_spacing = None
                z_spacing = None
                
                if 'x_pixel_spacing' in media:
                    x_spacing = self._extract_first_value(media['x_pixel_spacing'])
                    print(f"Found x_pixel_spacing in media: {x_spacing}")
                    
                if 'y_pixel_spacing' in media:
                    y_spacing = self._extract_first_value(media['y_pixel_spacing'])
                    print(f"Found y_pixel_spacing in media: {y_spacing}")
                    
                if 'z_pixel_spacing' in media:
                    z_spacing = self._extract_first_value(media['z_pixel_spacing'])
                    print(f"Found z_pixel_spacing in media: {z_spacing}")
                    
                if x_spacing and y_spacing and z_spacing:
                    return x_spacing, y_spacing, z_spacing
            
            # If we didn't find values in the 'media' field or they're not complete,
            # try the old approach with 'data' field
            if 'data' in media_data:
                data = media_data['data']
                
                # Check for different possible field names in data
                x_spacing = None
                y_spacing = None
                z_spacing = None
                
                # Direct properties
                if 'x_pixel_spacing' in data:
                    x_spacing = self._extract_first_value(data['x_pixel_spacing'])
                if 'y_pixel_spacing' in data:
                    y_spacing = self._extract_first_value(data['y_pixel_spacing'])
                if 'z_pixel_spacing' in data:
                    z_spacing = self._extract_first_value(data['z_pixel_spacing'])
                
                # Try alternative paths in metadata
                if (x_spacing is None or y_spacing is None or z_spacing is None) and 'metadata' in data:
                    metadata = data['metadata']
                    
                    # Check for pixel spacing in metadata
                    if 'x_pixel_spacing' in metadata:
                        x_spacing = self._extract_first_value(metadata['x_pixel_spacing'])
                    if 'y_pixel_spacing' in metadata:
                        y_spacing = self._extract_first_value(metadata['y_pixel_spacing'])
                    if 'z_pixel_spacing' in metadata:
                        z_spacing = self._extract_first_value(metadata['z_pixel_spacing'])
                
                if x_spacing and y_spacing and z_spacing:
                    return x_spacing, y_spacing, z_spacing
            
            # If we still don't have complete values, check at the root level
            if 'x_pixel_spacing' in media_data:
                x_spacing = self._extract_first_value(media_data['x_pixel_spacing'])
                y_spacing = self._extract_first_value(media_data['y_pixel_spacing']) if 'y_pixel_spacing' in media_data else None
                z_spacing = self._extract_first_value(media_data['z_pixel_spacing']) if 'z_pixel_spacing' in media_data else None
                
                if x_spacing and y_spacing and z_spacing:
                    return x_spacing, y_spacing, z_spacing
            
            # If we still can't find the values, return None
            return None, None, None
            
        except Exception as e:
            print(f"Error extracting pixel spacing: {str(e)}")
            return None, None, None
    
    def _extract_first_value(self, value):
        """Helper method to extract the first value from a list or string"""
        if isinstance(value, list) and len(value) > 0:
            return value[0]
        return value
    
    def compare_pixel_spacing(self, csv_x, csv_y, csv_z, api_x, api_y, api_z, tolerance=0.0001):
        """
        Compare pixel spacing values between CSV and API
        
        Args:
            csv_x, csv_y, csv_z: Pixel spacing values from CSV
            api_x, api_y, api_z: Pixel spacing values from API
            tolerance: Floating point comparison tolerance
            
        Returns:
            bool: True if values match within tolerance, False otherwise
        """
        try:
            # Convert all values to float for comparison
            values = []
            for val in [csv_x, csv_y, csv_z, api_x, api_y, api_z]:
                if val is None:
                    return False
                    
                # Handle string values that might contain units
                if isinstance(val, str):
                    # Extract numeric part
                    numeric_val = re.search(r'([-+]?\d*\.\d+|\d+)', val)
                    if numeric_val:
                        values.append(float(numeric_val.group(1)))
                    else:
                        return False
                else:
                    values.append(float(val))
            
            # Compare values within tolerance
            if (abs(values[0] - values[3]) <= tolerance and
                abs(values[1] - values[4]) <= tolerance and
                abs(values[2] - values[5]) <= tolerance):
                return True
            return False
            
        except Exception as e:
            print(f"Error comparing pixel spacing: {str(e)}")
            return False
    
    def verify_matches(self, start_row=0, limit=None):
        """
        Verify pixel spacing matches between CSV and MorphoSource API
        
        This method:
        1. Iterates through each row in the matched.csv file
        2. Extracts the media ID from the MorphoSource URL
        3. Fetches the media details from the MorphoSource API
        4. Compares the pixel spacing values from the CSV with those from the API
        5. Updates the 'voxel_spacing_verified' column in the DataFrame
        
        Args:
            start_row (int): Index of the first row to process (0-indexed)
            limit (int): Maximum number of rows to process
            
        Returns:
            bool: True if verification completed successfully, False otherwise
        """
        if self.matches_data is None:
            print("No data loaded. Call load_data() first.")
            return False
        
        # Add verification column if it doesn't exist
        if 'voxel_spacing_verified' not in self.matches_data.columns:
            self.matches_data['voxel_spacing_verified'] = "Not checked"
        
        if 'api_voxel_spacing' not in self.matches_data.columns:
            self.matches_data['api_voxel_spacing'] = None
        
        # Track progress
        total_rows = min(len(self.matches_data) - start_row, limit if limit else float('inf'))
        processed = 0
        verified_count = 0
        mismatch_count = 0
        skipped_count = 0
        
        # Process each row
        for idx, row in self.matches_data.iterrows():
            if idx < start_row:
                continue
            
            if limit and processed >= limit:
                break
            
            processed += 1
            
            # Get the URL from Morphosource_URL or alternative column names
            url = None
            url_col_names = ['Morphosource_URL', 'url', 'URL', 'morphosource_url', 'Match_URL']
            for col_name in url_col_names:
                if col_name in row and pd.notna(row[col_name]) and row[col_name]:
                    url = row[col_name]
                    break
                    
            # Skip rows without URLs
            if not url:
                print(f"Skipping row {idx+1}: No URL")
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "Skipped"
                skipped_count += 1
                continue
            
            # Extract media ID from URL
            media_id = self.extract_media_id(url)
            if not media_id:
                print(f"Skipping row {idx+1}: Could not extract media ID from URL: {url}")
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "Invalid URL"
                skipped_count += 1
                continue
            
            # Fetch media details from API
            media_data = self.get_media_details(media_id)
            if not media_data:
                print(f"Skipping row {idx+1}: Could not fetch media details for ID: {media_id}")
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "API Error"
                skipped_count += 1
                continue
            
            # Extract voxel spacing from API response
            api_x, api_y, api_z = self.extract_pixel_spacing(media_data)
            
            # Store API values
            self.matches_data.at[idx, 'api_voxel_spacing'] = f"({api_x}, {api_y}, {api_z})"
            
            # Extract voxel spacing from CSV - try different column names if needed
            csv_x = row.get('x_voxel_spacing_mm', None)
            csv_y = row.get('y_voxel_spacing_mm', None)
            csv_z = row.get('z_voxel_spacing_mm', None)
            
            # Compare values
            if not api_x or not api_y or not api_z:
                print(f"Row {idx+1}: API returned incomplete voxel spacing data for ID: {media_id}")
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "Incomplete API data"
                skipped_count += 1
            elif not csv_x or not csv_y or not csv_z:
                print(f"Row {idx+1}: CSV has incomplete voxel spacing data")
                
                # Store the API values in the CSV for future use
                self.matches_data.at[idx, 'x_voxel_spacing_mm'] = api_x
                self.matches_data.at[idx, 'y_voxel_spacing_mm'] = api_y
                self.matches_data.at[idx, 'z_voxel_spacing_mm'] = api_z
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "API values used"
                verified_count += 1
            elif self.compare_pixel_spacing(csv_x, csv_y, csv_z, api_x, api_y, api_z):
                print(f"Row {idx+1}: Voxel spacing verified ✓")
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "Yes"
                verified_count += 1
            else:
                print(f"Row {idx+1}: Voxel spacing mismatch! CSV: ({csv_x}, {csv_y}, {csv_z}) API: ({api_x}, {api_y}, {api_z})")
                self.matches_data.at[idx, 'voxel_spacing_verified'] = "No"
                mismatch_count += 1
        
        # Print summary
        print(f"\nProcessed {processed} rows")
        print(f"Verified: {verified_count}")
        print(f"Mismatches: {mismatch_count}")
        print(f"Skipped: {skipped_count}")
        
        # Store the data for saving
        self.verified_data = self.matches_data
        
        return True
    
    def save_results(self, output_file="confirmed_matched.csv"):
        """
        Save verification results to CSV
        
        Args:
            output_file (str): Path to output CSV file
        """
        if self.verified_data is None:
            print("No verified data available. Run verify_matches() first.")
            return False
            
        try:
            self.verified_data.to_csv(output_file, index=False)
            print(f"Results saved to {output_file}")
            
            # Summary statistics
            verified_count = (self.verified_data['voxel_spacing_verified'] == "Yes").sum()
            total_count = len(self.verified_data)
            verified_pct = (verified_count / total_count) * 100 if total_count > 0 else 0
            
            print(f"\nVerification Summary:")
            print(f"Total records: {total_count}")
            print(f"Verified matches: {verified_count} ({verified_pct:.1f}%)")
            print(f"Unverified matches: {total_count - verified_count} ({100 - verified_pct:.1f}%)")
            
            return True
            
        except Exception as e:
            print(f"Error saving results: {str(e)}")
            return False

# Main execution block when script is run directly
if __name__ == "__main__":
    # Uncomment below for testing specific media IDs
    """
    # Improved function to examine API response in detail
    def examine_api_response_in_detail(media_id):
        # Create a basic verifier with no file dependency
        temp_verifier = MorphosourceVoxelVerifier("dummy.csv")
        
        # Fetch the API response
        print(f"Fetching API data for media ID: {media_id}")
        response = temp_verifier.get_media_details(media_id)
        
        if not response:
            print("Failed to get API response")
            return
        
        # Save the full response for examination
        with open(f"media_{media_id}_response.json", "w") as f:
            json.dump(response, f, indent=2)
        print(f"Saved full API response to media_{media_id}_response.json")
        
        # Recursive function to find all fields related to pixel spacing or resolution
        def find_all_related_fields(obj, path=""):
            results = []
            
            if isinstance(obj, dict):
                for key, value in obj.items():
                    new_path = f"{path}.{key}" if path else key
                    
                    # Check if key contains relevant terms
                    terms = ["pixel", "spacing", "voxel", "resolution", "res", "dimension", "scale"]
                    if any(term in key.lower() for term in terms):
                        results.append({
                            "path": new_path,
                            "key": key,
                            "value": value,
                            "type": type(value).__name__
                        })
                    
                    # Continue searching nested objects
                    results.extend(find_all_related_fields(value, new_path))
                    
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    new_path = f"{path}[{i}]"
                    results.extend(find_all_related_fields(item, new_path))
                    
            return results
        
        # Find all fields that might contain pixel spacing info
        fields = find_all_related_fields(response)
        
        if fields:
            print("\n=== Potential Pixel Spacing Fields ===")
            for field in fields:
                print(f"Path: {field['path']}")
                print(f"Key: {field['key']}")
                print(f"Value: {field['value']}")
                print(f"Type: {field['type']}")
                print("---")
        else:
            print("\nNo fields related to pixel spacing found in the API response.")
        
        # Check for specific structures in the API response
        if 'data' in response:
            data = response['data']
            
            # Common locations to check
            locations = [
                ('metadata.MicroCT Settings', 'MicroCT scanning settings'),
                ('metadata.Scanner Parameters', 'Scanner configuration'),
                ('metadata.Resolution', 'Resolution information'),
                ('media_metrics', 'Media metrics'),
                ('technical_metadata', 'Technical metadata'),
                ('derived_metrics', 'Derived metrics')
            ]
            
            print("\n=== Checking Common Locations ===")
            for path, description in locations:
                parts = path.split('.')
                current = data
                found = True
                
                for part in parts:
                    if part in current:
                        current = current[part]
                    else:
                        found = False
                        break
                
                if found:
                    print(f"Found {description} at data.{path}:")
                    if isinstance(current, dict):
                        for k, v in current.items():
                            print(f"  {k}: {v}")
                    else:
                        print(f"  Value: {current}")
        
        return response

    # Simple function to print key sections of the API response
    def print_key_sections(media_data):
        if not media_data:
            print("No media data to examine")
            return
        
        # Check if we have the data key
        data = media_data.get('data', media_data)
        
        print("\n=== Key Sections ===")
        
        # Print metadata keys 
        if 'metadata' in data:
            print("\nMetadata sections:")
            for key in data['metadata'].keys():
                print(f"  - {key}")
        
        # Check for common technical sections
        for section in ['media_metrics', 'technical_metadata', 'derived_metrics']:
            if section in data:
                print(f"\n{section}:")
                if isinstance(data[section], dict):
                    for key, value in data[section].items():
                        print(f"  - {key}: {value}")
                
        # Look for sections with voxel, resolution or pixel in the name
        for key, value in data.items():
            if any(term in key.lower() for term in ['voxel', 'resolution', 'pixel']):
                print(f"\n{key}:")
                print(f"  {value}")

    # Test the updated extraction function
    def test_updated_extraction(media_id):
        # Create a verifier
        verifier = MorphosourceVoxelVerifier("dummy.csv")
        
        # Get media details
        media_data = verifier.get_media_details(media_id)
        
        if not media_data:
            print("Failed to get media data")
            return
        
        # Extract pixel spacing using the updated method
        x, y, z = verifier.extract_pixel_spacing(media_data)
        
        print(f"\nExtracted spacing values:")
        print(f"X spacing: {x}")
        print(f"Y spacing: {y}")
        print(f"Z spacing: {z}")
        
        # Compare with expected values
        expected = "0.04134338"
        if x == expected and y == expected and z == expected:
            print("\n✅ Values match expected value of 0.04134338")
        else:
            print(f"\n❌ Values don't match expected value of 0.04134338")

    # Uncomment and run for testing specific media IDs
    # media_id = "000407755"
    # detailed_response = examine_api_response_in_detail(media_id)
    # if detailed_response:
    #     print_key_sections(detailed_response)
    # test_updated_extraction("000407755")
    """
    
    # Set up command line argument parser
    parser = argparse.ArgumentParser(description="Verify voxel spacing values in matched.csv against MorphoSource API")
    parser.add_argument("--csv", "-c", help="Path to the matched.csv file", default="matched.csv")
    parser.add_argument("--output", "-o", help="Path to output file", default="confirmed_matched.csv")
    parser.add_argument("--api-key", "-k", help="MorphoSource API key (optional)")
    parser.add_argument("--limit", "-l", type=int, help="Limit the number of rows to process", default=None)
    parser.add_argument("--start", "-s", type=int, help="Starting row number (1-indexed)", default=1)
    args = parser.parse_args()
    
    print("=== MorphoSource Voxel Spacing Verification ===")
    print(f"Input file: {args.csv}")
    print(f"Output file: {args.output}")
    if args.limit:
        print(f"Processing up to {args.limit} rows starting from row {args.start}")
    else:
        print(f"Processing all rows starting from row {args.start}")
    
    # Create verifier instance
    verifier = MorphosourceVoxelVerifier(args.csv, api_key=args.api_key)
    
    # Load data
    if verifier.load_data():
        # Verify all matches
        print("\nVerifying voxel spacing values...")
        verifier.verify_matches(start_row=args.start-1, limit=args.limit)  # Convert to 0-indexed
        
        # Save results
        verifier.save_results(args.output)
        print("\nVerification complete! Results saved to:", args.output)
    else:
        print("\nFailed to load data. Please check the input file.")
