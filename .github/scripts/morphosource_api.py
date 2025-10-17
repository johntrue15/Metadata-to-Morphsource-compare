#!/usr/bin/env python3
"""
MorphoSource API query handler.
Searches MorphoSource database using API parameters.
"""

import os
import json
import requests
import sys
from copy import deepcopy
from urllib.parse import urlparse

from requests import Request

import query_formatter


def _extract_endpoint(query_info):
    """Determine the MorphoSource endpoint to use based on query info."""

    if not query_info:
        return 'media'

    endpoint = query_info.get('api_endpoint')
    if endpoint:
        return endpoint

    generated_url = query_info.get('generated_url')
    if generated_url:
        parsed = urlparse(generated_url)
        parts = [part for part in parsed.path.split('/') if part]
        if len(parts) >= 2 and parts[0] == 'api':
            return parts[1]

    return 'media'


def _extract_result_count(data):
    """Return the number of results from a MorphoSource API payload."""

    if not isinstance(data, dict):
        return 0

    for key in ('media', 'physical_objects', 'assets'):
        if isinstance(data.get(key), list):
            return len(data[key])

    pages = data.get('pages')
    if isinstance(pages, dict):
        total_count = pages.get('total_count')
        if isinstance(total_count, int):
            return total_count

    return 0


def _build_feedback(attempt_index, url, response_data):
    """Prepare feedback payload for query refinement."""

    try:
        response_excerpt = json.dumps(response_data, indent=2)[:1200]
    except Exception:
        response_excerpt = str(response_data)[:1200]

    return {
        'attempt': attempt_index,
        'failed_url': url,
        'response_excerpt': response_excerpt
    }


def search_morphosource(api_params, formatted_query, query_info=None, max_retries=2):
    """
    Search MorphoSource API with given parameters.
    
    Args:
        api_params (dict): API query parameters
        formatted_query (str): Formatted query string
        
    Returns:
        dict: Search results from MorphoSource API
    """
    api_key = os.environ.get('MORPHOSOURCE_API_KEY', '')
    
    query_info = deepcopy(query_info) if query_info else {}
    if 'formatted_query' not in query_info:
        query_info['formatted_query'] = formatted_query
    if 'api_params' not in query_info:
        query_info['api_params'] = deepcopy(api_params)
    if 'original_query' not in query_info:
        query_info['original_query'] = formatted_query

    print(f"Searching MorphoSource with formatted query: {query_info['formatted_query']}")
    print(f"API parameters: {json.dumps(api_params)}")

    # MorphoSource API configuration
    MORPHOSOURCE_API_BASE = "https://www.morphosource.org/api"

    headers = {}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    
    attempt_history = []
    current_params = deepcopy(api_params)
    current_formatted_query = query_info['formatted_query']
    current_query_info = deepcopy(query_info)

    try:
        for attempt in range(1, max_retries + 2):  # original attempt + retries
            endpoint = _extract_endpoint(current_query_info)
            search_url = f"{MORPHOSOURCE_API_BASE}/{endpoint}"

            prepared_request = Request('GET', search_url, params=current_params).prepare()
            request_url = prepared_request.url

            print(f"Attempt {attempt}: querying {request_url}")

            response = requests.get(search_url, params=current_params, headers=headers, timeout=30)

            attempt_entry = {
                'attempt': attempt,
                'endpoint': endpoint,
                'url': request_url,
                'status_code': response.status_code
            }

            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = {'status': 'error', 'message': 'Invalid JSON response'}

                result_count = _extract_result_count(data)
                attempt_entry['result_count'] = result_count
                attempt_history.append(attempt_entry)

                print(f"✓ Received response (attempt {attempt})")
                print(json.dumps(data, indent=2))

                if result_count > 0 or attempt == max_retries + 1:
                    results_summary = {
                        "status": "success",
                        "count": result_count,
                        "formatted_query": current_formatted_query,
                        "endpoint": endpoint,
                        "attempts": attempt_history
                    }

                    return {
                        'full_data': data,
                        'summary': results_summary,
                        'query_info': {
                            **current_query_info,
                            'formatted_query': current_formatted_query,
                            'api_params': current_params,
                            'api_endpoint': endpoint
                        }
                    }

                # Zero results and we can retry
                openai_available = bool(os.environ.get('OPENAI_API_KEY'))
                if not openai_available:
                    print("No OPENAI_API_KEY available for retry. Returning zero results.")
                    results_summary = {
                        "status": "success",
                        "count": result_count,
                        "formatted_query": current_formatted_query,
                        "endpoint": endpoint,
                        "attempts": attempt_history
                    }

                    return {
                        'full_data': data,
                        'summary': results_summary,
                        'query_info': {
                            **current_query_info,
                            'formatted_query': current_formatted_query,
                            'api_params': current_params,
                            'api_endpoint': endpoint
                        }
                    }

                feedback = _build_feedback(attempt, request_url, data)
                print("No results found. Requesting reformatted query from ChatGPT...")

                try:
                    refined = query_formatter.format_query(current_query_info['original_query'], feedback=feedback)
                except Exception as refine_error:
                    print(f"Retry formatting failed: {refine_error}")
                    refined = None

                if not refined:
                    print("No refined query returned; stopping retries.")
                    results_summary = {
                        "status": "success",
                        "count": result_count,
                        "formatted_query": current_formatted_query,
                        "endpoint": endpoint,
                        "attempts": attempt_history
                    }

                    return {
                        'full_data': data,
                        'summary': results_summary,
                        'query_info': {
                            **current_query_info,
                            'formatted_query': current_formatted_query,
                            'api_params': current_params,
                            'api_endpoint': endpoint
                        }
                    }

                new_url = refined.get('generated_url')
                new_params = refined.get('api_params') or current_params

                if not new_url and new_params == current_params:
                    print("Refined query did not change parameters; stopping retries.")
                    results_summary = {
                        "status": "success",
                        "count": result_count,
                        "formatted_query": current_formatted_query,
                        "endpoint": endpoint,
                        "attempts": attempt_history
                    }

                    return {
                        'full_data': data,
                        'summary': results_summary,
                        'query_info': {
                            **current_query_info,
                            'formatted_query': current_formatted_query,
                            'api_params': current_params,
                            'api_endpoint': endpoint
                        }
                    }

                current_params = deepcopy(new_params)
                current_formatted_query = refined.get('formatted_query', current_formatted_query)
                current_query_info = {
                    **refined,
                    'original_query': current_query_info['original_query']
                }
                continue

            # Non-200 status codes
            print(f"⚠ API returned status {response.status_code}")
            print(f"Response: {response.text[:500]}")
            attempt_history.append(attempt_entry)

            error_data = {
                "status": "error",
                "code": response.status_code,
                "message": response.text[:200],
                "attempts": attempt_history
            }

            return {
                'full_data': error_data,
                'summary': error_data
            }

        # Should not reach here, but handle as zero results
        results_summary = {
            "status": "success",
            "count": 0,
            "formatted_query": current_formatted_query,
            "attempts": attempt_history
        }

        return {
            'full_data': {},
            'summary': results_summary,
            'query_info': {
                **current_query_info,
                'formatted_query': current_formatted_query,
                'api_params': current_params
            }
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

    query_info = None
    try:
        with open('formatted_query.json', 'r') as f:
            query_info = json.load(f)
    except Exception as e:
        print(f"Warning: Could not load formatted_query.json: {e}")

    result = search_morphosource(api_params, formatted_query, query_info=query_info)

    # Save full data to output file for artifact
    with open('morphosource_results.json', 'w') as f:
        json.dump(result['full_data'], f, indent=2)

    # Persist final formatted query information for downstream steps
    final_query_info = result.get('query_info')
    if not final_query_info:
        fallback_info = query_info or {}
        final_query_info = {
            'formatted_query': fallback_info.get('formatted_query', formatted_query),
            'api_params': fallback_info.get('api_params', api_params),
            'api_endpoint': fallback_info.get('api_endpoint', _extract_endpoint(fallback_info)),
            'original_query': fallback_info.get('original_query', formatted_query)
        }

    with open('formatted_query_final.json', 'w') as f:
        json.dump(final_query_info, f, indent=2)

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
