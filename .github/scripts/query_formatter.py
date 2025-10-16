#!/usr/bin/env python3
"""
Query formatter for processing natural language queries with ChatGPT.
Converts user queries into MorphoSource API requests.
"""

import os
import json
import sys
from openai import OpenAI


def format_query(query):
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
            'formatted_query': query,
            'api_params': {'q': query, 'per_page': 10},
            'generated_url': None
        }
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Ask ChatGPT to format the query for MorphoSource API
        system_prompt = """Role: You are an API query planner that outputs only HTTP GET URLs for the MorphoSource REST API.
        
        Goal: Convert the user's natural-language request into one or more MorphoSource API requests that, when executed, return the correct records and an accurate total count via pagination metadata. Prefer taxonomic filters over raw keyword search to avoid missing records labeled by scientific names.
        
        Key rules
        
        Default entity type
        
        If the user asks about "specimens" → use Physical Objects with object_type=BiologicalSpecimen at /api/physical-objects.
        
        If they ask about "files", "scans", "media", "CTs", etc. → use Media at /api/media.
        
        Taxonomy strategy (critical for common names like "snake")
        
        Map common names to the GBIF taxon that covers the intended clade and pass it via taxonomy_gbif=<GBIF name>.
        
        Examples:
        
        snakes → Serpentes
        
        lizards → Lacertilia (or higher: Squamata if user implies "all lizards")
        
        crocodiles → Crocodylia
        
        frogs → Anura
        
        If the user specifies a genus/species, pass that literal value in taxonomy_gbif (e.g., taxonomy_gbif=Pantherophis guttatus).
        
        Counting efficiently
        
        Always include per_page=1&page=1 to minimize payload; read pages.total_count for the count.
        
        If the user asks for available or public downloads for media, add visibility=OPEN (Media endpoint only).
        
        Other optional filters (use only if the user asks)
        
        media_type= (e.g., Mesh, Image)
        
        media_tag= (anatomical tags, etc.)
        
        Date, institution, device, etc., only if explicitly requested.
        
        Output format
        
        Output only the final URL(s), one per line.
        
        Do not include prose, JSON wrappers, or explanations.
        
        Templates
        
        Specimens (Physical Objects):
        https://www.morphosource.org/api/physical-objects?object_type=BiologicalSpecimen&taxonomy_gbif=<GBIF taxon>&per_page=1&page=1
        
        Media:
        https://www.morphosource.org/api/media?taxonomy_gbif=<GBIF taxon>&per_page=1&page=1
        (Optionally add &visibility=OPEN when the user asks for downloadable/open media only.)
        
        Edge cases
        
        If the user's term is ambiguous (e.g., "viper" could be common name or family Viperidae), prefer the higher-level clade that matches the user's intent (e.g., Viperidae) and return a single URL.
        
        Only emit multiple URLs if the user explicitly asks for multiple taxa that cannot be covered by one higher-rank clade.
        
        Test: "How many snake specimens are available?"
        
        Expected output (specimen count across all snakes = Order Serpentes):
        
        https://www.morphosource.org/api/physical-objects?object_type=BiologicalSpecimen&taxonomy_gbif=Serpentes&per_page=1&page=1
        
        
        (Optional, if they instead wanted media count for snakes):
        
        https://www.morphosource.org/api/media?taxonomy_gbif=Serpentes&per_page=1&page=1"""

        messages = [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": query
            }
        ]
        
        response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            temperature=0.3,
            max_tokens=200
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
            else:
                # No URL found, fallback
                print("Warning: No URL found in response, using original query")
                formatted_query = query
                api_params = {'q': query, 'per_page': 10}
                api_url = None
        except Exception as parse_error:
            # Fallback if URL parsing fails
            print(f"Warning: Could not parse URL response: {parse_error}, using original query")
            formatted_query = query
            api_params = {'q': query, 'per_page': 10}
            api_url = None
        
        print(f"✓ Formatted query: {formatted_query}")
        print(f"✓ API params: {json.dumps(api_params)}")
        
        return {
            'original_query': query,
            'formatted_query': formatted_query,
            'api_params': api_params,
            'generated_url': api_url
        }
    
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        # Fallback to using raw query
        return {
            'formatted_query': query,
            'api_params': {'q': query, 'per_page': 10},
            'generated_url': None
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
