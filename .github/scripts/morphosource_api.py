#!/usr/bin/env python3
"""
MorphoSource API query handler.
Searches MorphoSource database using API parameters.
"""

import os
import json
import requests
import sys


def search_morphosource(api_params, formatted_query):
    """
    Search MorphoSource API with given parameters.
    
    Args:
        api_params (dict): API query parameters
        formatted_query (str): Formatted query string
        
    Returns:
        dict: Search results from MorphoSource API
    """
    api_key = os.environ.get('MORPHOSOURCE_API_KEY', '')
    
    print(f"Searching MorphoSource with formatted query: {formatted_query}")
    print(f"API parameters: {json.dumps(api_params)}")
    
    # MorphoSource API configuration
    MORPHOSOURCE_API_BASE = "https://www.morphosource.org/api"
    
    headers = {}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    try:
        # Search media
        search_url = f"{MORPHOSOURCE_API_BASE}/media"
        
        response = requests.get(search_url, params=api_params, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            print(f"✓ Found results from media endpoint")
            print(json.dumps(data, indent=2))
            
            # Prepare result summary
            results_summary = {
                "status": "success",
                "count": len(data.get('media', [])),
                "formatted_query": formatted_query
            }
            
            return {
                'full_data': data,
                'summary': results_summary
            }
        else:
            print(f"⚠ API returned status {response.status_code}")
            print(f"Response: {response.text[:500]}")
            
            error_data = {
                "status": "error",
                "code": response.status_code,
                "message": response.text[:200]
            }
            
            return {
                'full_data': error_data,
                'summary': error_data
            }
    
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        error_data = {"status": "error", "message": str(e)}
        
        return {
            'full_data': error_data,
            'summary': error_data
        }


def main():
    """Main entry point for MorphoSource API script."""
    if len(sys.argv) < 3:
        print("Usage: morphosource_api.py '<formatted_query>' '<api_params_json>'")
        sys.exit(1)
    
    formatted_query = sys.argv[1]
    api_params_str = sys.argv[2]
    
    try:
        api_params = json.loads(api_params_str)
    except Exception as e:
        print(f"Error parsing API params: {e}")
        api_params = {'q': formatted_query, 'per_page': 10}
    
    result = search_morphosource(api_params, formatted_query)
    
    # Save full data to output file for artifact
    with open('morphosource_results.json', 'w') as f:
        json.dump(result['full_data'], f, indent=2)
    
    # Set output for GitHub Actions (use summary to avoid size limits)
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        with open(github_output, 'a') as f:
            f.write(f"results={json.dumps(result['summary'])}\n")
    
    print(f"\n✓ MorphoSource search complete")
    
    # Exit with error if search failed
    if result['summary'].get('status') == 'error':
        sys.exit(1)


if __name__ == "__main__":
    main()
