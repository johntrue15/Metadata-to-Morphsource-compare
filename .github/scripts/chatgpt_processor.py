#!/usr/bin/env python3
"""
ChatGPT query processor.
Processes MorphoSource API results with ChatGPT to generate human-readable responses.
"""

import os
import json
import sys
from openai import OpenAI


def process_with_chatgpt(query, morphosource_data, formatted_query_info):
    """
    Process query and MorphoSource results with ChatGPT.
    
    Args:
        query (str): Original user query
        morphosource_data (dict): Results from MorphoSource API
        formatted_query_info (dict): Information about the formatted query
        
    Returns:
        dict: Contains status, response, and summary
    """
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        print("✗ OPENAI_API_KEY not configured")
        return {
            "status": "error",
            "message": "OPENAI_API_KEY not configured"
        }
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Build context with MorphoSource data
        context = f"User query: {query}\n\n"
        
        if formatted_query_info:
            context += f"Formatted search query: {formatted_query_info.get('formatted_query', 'N/A')}\n"
            context += f"API parameters used: {json.dumps(formatted_query_info.get('api_params', {}))}\n\n"
        
        context += "MorphoSource API Results:\n"
        context += json.dumps(morphosource_data, indent=2)
        
        messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant that answers questions about MorphoSource data. The user's natural language query has been automatically formatted into a MorphoSource API search query. Use the provided API results to give accurate, informative responses about the specimens found."
            },
            {
                "role": "user",
                "content": context
            }
        ]
        
        response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            max_tokens=1000
        )
        
        answer = response.choices[0].message.content
        print(f"✓ ChatGPT Response:\n{answer}")
        
        result = {
            "status": "success",
            "query": query,
            "response": answer,
            "morphosource_summary": morphosource_data.get('status', 'unknown')
        }
        
        print("\n" + "="*60)
        print("FINAL RESPONSE")
        print("="*60)
        print(answer)
        print("="*60)
        
        return result
    
    except Exception as e:
        print(f"✗ Error: {str(e)}")
        return {
            "status": "error",
            "message": str(e)
        }


def main():
    """Main entry point for ChatGPT processor script."""
    if len(sys.argv) < 2:
        print("Usage: chatgpt_processor.py '<query>'")
        sys.exit(1)
    
    query = sys.argv[1]
    print(f"Processing query with ChatGPT: {query}")
    
    # Load MorphoSource results
    try:
        with open('morphosource_results.json', 'r') as f:
            morphosource_data = json.load(f)
    except Exception as e:
        print(f"Warning: Could not load MorphoSource results: {e}")
        morphosource_data = {}
    
    # Load formatted query information
    formatted_query_info = {}
    try:
        with open('formatted_query.json', 'r') as f:
            formatted_query_info = json.load(f)
    except Exception as e:
        print(f"Warning: Could not load formatted query: {e}")
    
    result = process_with_chatgpt(query, morphosource_data, formatted_query_info)
    
    # Save result to output file for artifact
    with open('chatgpt_response.json', 'w') as f:
        json.dump(result, f, indent=2)
    
    print(f"\n✓ ChatGPT processing complete")


if __name__ == "__main__":
    main()
