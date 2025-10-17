#!/usr/bin/env python3
"""
Query formatter for processing natural language queries with ChatGPT.
Converts user queries into MorphoSource API requests.
"""

import os
import json
import sys
import importlib.util

if 'openai' in sys.modules:
    OpenAI = getattr(sys.modules['openai'], 'OpenAI', None)  # type: ignore
else:
    _openai_spec = importlib.util.find_spec("openai")
    if _openai_spec:
        from openai import OpenAI  # type: ignore
    else:
        OpenAI = None  # type: ignore


def _build_user_prompt(query, feedback):
    """Construct the user prompt optionally including retry feedback."""

    if not feedback:
        return query

    previous_url = feedback.get('failed_url', 'Unknown URL')
    response_excerpt = feedback.get('response_excerpt', '')
    attempt = feedback.get('attempt', 1)

    return (
        "The previous MorphoSource API request returned no results. "
        "Adjust the API request so it still answers the user's question but has a better chance of returning data. "
        "You may switch endpoints (media vs physical-objects), broaden or narrow taxonomy filters, or remove filters as needed.\n\n"
        f"Original user request:\n{query}\n\n"
        f"Previous attempt #{attempt} URL:\n{previous_url}\n\n"
        "API JSON response excerpt (may be truncated):\n"
        f"{response_excerpt}"
    )


def format_query(query, feedback=None):
    """
    Format a natural language query using ChatGPT to generate MorphoSource API parameters.
    
    Args:
        query (str): Natural language query from user
        
    Returns:
        dict: Contains formatted_query, api_params, and generated_url
    """
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        print("✗ OPENAI_API_KEY not configured")
        # Fallback to using raw query
        return {
            'original_query': query,
            'formatted_query': query,
            'api_params': {'q': query, 'per_page': 10},
            'generated_url': None,
            'api_endpoint': None
        }

    if OpenAI is None:
        print("✗ OpenAI package is not installed")
        # Fallback to using raw query
        return {
            'original_query': query,
            'formatted_query': query,
            'api_params': {'q': query, 'per_page': 10},
            'generated_url': None,
            'api_endpoint': None
        }
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Ask ChatGPT to format the query for MorphoSource API
        system_prompt = """Role: You are an API query planner that outputs only HTTP GET URLs for the MorphoSource REST API. Output one or more URLs—one per line, no prose.

Goal: Convert the user's natural-language request into MorphoSource API requests that retrieve the intended records. Prefer taxonomic filters over plain keywords so common names map to the right clades.

Key rules

Endpoint (decide from user wording)

Specimens → Physical Objects:
https://www.morphosource.org/api/physical-objects?...
Include: f[object_type][]=BiologicalSpecimen (and mirror as object_type=BiologicalSpecimen).

Media / scans / CT / images / meshes / volumes / files → Media:
https://www.morphosource.org/api/media?...

Taxonomy (critical for common names)

Map common names to the GBIF taxon and pass it as array-style and mirrored plain:
f[taxonomy_gbif][]=<GBIF name> and taxonomy_gbif=<GBIF name>.

Examples: Serpentes (snakes), Crocodylia (crocodiles), Reptilia (reptiles), Anura (frogs), Squamata (lizards & snakes).

If the user gives a genus/species, pass that literal value in both places.

Modality / media narrowing (Media endpoint only, if implied)

CT scans (microCT/µCT/computed tomography):
f[modality][]=MicroNanoXRayComputedTomography and modality=MicroNanoXRayComputedTomography.

Availability (Media only, if requested)

Open/public/downloadable:
f[visibility][]=Open (no plain mirror needed unless you've observed it helps).

Pagination & locale

Always include locale=en.

For media endpoint, include search_field=all_fields.

Counts: per_page=1&page=1 (read pages.total_count).

Browse: per_page=12&page=1 (or as requested).

Output format

Output only the URL(s), one per line—no commentary, no JSON.

URL templates

Media (CT scans, browse)

https://www.morphosource.org/api/media?f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography&f%5Btaxonomy_gbif%5D%5B%5D=<GBIF taxon>&locale=en&search_field=all_fields


Media (CT scans, open only)

https://www.morphosource.org/api/media?f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography&f%5Bvisibility%5D%5B%5D=Open&f%5Btaxonomy_gbif%5D%5B%5D=<GBIF taxon>&locale=en&search_field=all_fields


Specimens (count)

https://www.morphosource.org/api/physical-objects?f%5Bobject_type%5D%5B%5D=BiologicalSpecimen&f%5Btaxonomy_gbif%5D%5B%5D=<GBIF taxon>&locale=en&object_type=BiologicalSpecimen&per_page=1&page=1&taxonomy_gbif=<GBIF taxon>


Specimens (browse)

https://www.morphosource.org/api/physical-objects?f%5Bobject_type%5D%5B%5D=BiologicalSpecimen&f%5Btaxonomy_gbif%5D%5B%5D=<GBIF taxon>&locale=en&object_type=BiologicalSpecimen&per_page=12&page=1&taxonomy_gbif=<GBIF taxon>

Tests (exact outputs)

"Show me CT scans of reptiles"

https://www.morphosource.org/api/media?f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography&f%5Btaxonomy_gbif%5D%5B%5D=Reptilia&locale=en&search_field=all_fields


"Show me CT scans of crocodiles"

https://www.morphosource.org/api/media?f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography&f%5Btaxonomy_gbif%5D%5B%5D=Crocodylia&locale=en&search_field=all_fields


"How many snake specimens are available?" (count)

https://www.morphosource.org/api/physical-objects?f%5Bobject_type%5D%5B%5D=BiologicalSpecimen&f%5Btaxonomy_gbif%5D%5B%5D=Serpentes&locale=en&object_type=BiologicalSpecimen&per_page=1&page=1&taxonomy_gbif=Serpentes"""

        retry_instruction = (
            "If you receive context about a previous API request that returned no results, "
            "produce a revised URL (or URLs) that is materially different and more likely to return matching records."
        )

        system_prompt = f"{system_prompt}\n\n{retry_instruction}"

        messages = [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": _build_user_prompt(query, feedback)
            }
        ]
        
        response = client.chat.completions.create(
            model="gpt-5",
            messages=messages,
            max_completion_tokens=2000
        )
        
        result_text = response.choices[0].message.content.strip()
        print(f"ChatGPT response: {result_text}")
        
        # Parse the URL response - the new prompt outputs URLs, not JSON
        try:
            # Split by lines and get the first URL
            urls = [line.strip() for line in result_text.split('\n') if line.strip().startswith('http')]
            
            if urls:
                api_url = urls[0]
                print(f"Extracted API URL: {api_url}")
                
                # Parse the URL to extract query parameters
                from urllib.parse import urlparse, parse_qs
                parsed_url = urlparse(api_url)
                query_params = parse_qs(parsed_url.query)

                # Convert from lists to single values
                api_params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}

                # Extract a formatted query from the URL - use taxonomy_gbif if present
                formatted_query = api_params.get('taxonomy_gbif', api_params.get('q', query))

                # Determine endpoint from the parsed URL
                path_parts = [part for part in parsed_url.path.split('/') if part]
                api_endpoint = None
                if len(path_parts) >= 2 and path_parts[0] == 'api':
                    api_endpoint = path_parts[1]
            else:
                # No URL found, fallback
                print("Warning: No URL found in response, using original query")
                formatted_query = query
                api_params = {'q': query, 'per_page': 10}
                api_url = None
                api_endpoint = None
        except Exception as parse_error:
            # Fallback if URL parsing fails
            print(f"Warning: Could not parse URL response: {parse_error}, using original query")
            formatted_query = query
            api_params = {'q': query, 'per_page': 10}
            api_url = None
            api_endpoint = None

        print(f"✓ Formatted query: {formatted_query}")
        print(f"✓ API params: {json.dumps(api_params)}")

        return {
            'original_query': query,
            'formatted_query': formatted_query,
            'api_params': api_params,
            'generated_url': api_url,
            'api_endpoint': api_endpoint
        }

    except Exception as e:
        print(f"✗ Error: {str(e)}")
        # Fallback to using raw query
        return {
            'original_query': query,
            'formatted_query': query,
            'api_params': {'q': query, 'per_page': 10},
            'generated_url': None,
            'api_endpoint': None
        }


def main():
    """Main entry point for query formatter script."""
    if len(sys.argv) < 2:
        print("Usage: query_formatter.py '<query>'")
        sys.exit(1)
    
    query = sys.argv[1]
    print(f"Formatting query with ChatGPT: {query}")
    
    result = format_query(query)
    
    # Save to output file for artifact
    with open('formatted_query.json', 'w') as f:
        json.dump(result, f, indent=2)
    
    # Set output for GitHub Actions
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        with open(github_output, 'a') as f:
            f.write(f"formatted_query={result['formatted_query']}\n")
            f.write(f"api_params={json.dumps(result['api_params'])}\n")
    
    print(f"\n✓ Query formatting complete")
    print(f"Formatted query: {result['formatted_query']}")
    print(f"API params: {json.dumps(result['api_params'])}")


if __name__ == "__main__":
    main()
