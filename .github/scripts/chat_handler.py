#!/usr/bin/env python3
"""
Chat handler for processing OpenAI API requests with MorphoSource integration
"""

import os
import sys
import json
import requests
from openai import OpenAI

# MorphoSource API configuration
MORPHOSOURCE_API_BASE = "https://www.morphosource.org/api"

def search_morphosource(query):
    """
    Search for specimens in MorphoSource database
    
    Args:
        query (str): Search query
        
    Returns:
        dict: Search results
    """
    try:
        api_key = os.environ.get('MORPHOSOURCE_API_KEY')
        headers = {}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        
        # Try different API endpoints
        search_url = f"{MORPHOSOURCE_API_BASE}/specimens"
        params = {'q': query, 'per_page': 10}
        
        response = requests.get(search_url, params=params, headers=headers, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        else:
            # If specimens endpoint doesn't work, try media endpoint
            media_url = f"{MORPHOSOURCE_API_BASE}/media"
            params = {'q': query, 'per_page': 10}
            response = requests.get(media_url, params=params, headers=headers, timeout=30)
            
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "error": f"API returned status {response.status_code}",
                    "message": response.text[:200]
                }
    
    except Exception as e:
        return {"error": str(e)}


def get_morphosource_media(media_id):
    """
    Get details for a specific media item
    
    Args:
        media_id (str): Media ID
        
    Returns:
        dict: Media details
    """
    try:
        api_key = os.environ.get('MORPHOSOURCE_API_KEY')
        headers = {}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        
        media_url = f"{MORPHOSOURCE_API_BASE}/media/{media_id}"
        response = requests.get(media_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        else:
            return {
                "error": f"API returned status {response.status_code}",
                "message": response.text[:200]
            }
    
    except Exception as e:
        return {"error": str(e)}


# Define tools for OpenAI
tools = [
    {
        "type": "function",
        "function": {
            "name": "search_morphosource",
            "description": "Search for specimens in the MorphoSource database by taxonomy, specimen name, or other criteria. Returns information about specimens including taxonomy, descriptions, and media URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'lizards', 'Anolis', 'crocodiles', 'CT scans')"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_morphosource_media",
            "description": "Get detailed information about a specific media item from MorphoSource including voxel spacing, file formats, and specimen details",
            "parameters": {
                "type": "object",
                "properties": {
                    "media_id": {
                        "type": "string",
                        "description": "The MorphoSource media ID (e.g., '000407755')"
                    }
                },
                "required": ["media_id"]
            }
        }
    }
]


def process_chat(messages):
    """
    Process chat messages using OpenAI API with MorphoSource tools
    
    Args:
        messages (list): Conversation history
        
    Returns:
        dict: Response from OpenAI
    """
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return {"error": "OPENAI_API_KEY not configured"}
    
    client = OpenAI(api_key=api_key)
    
    try:
        # Initial API call
        response = client.chat.completions.create(
            model="gpt-4",
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        
        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls
        
        # If no tool calls, return the response
        if not tool_calls:
            return {
                "role": "assistant",
                "content": response_message.content
            }
        
        # Process tool calls
        messages.append(response_message)
        
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)
            
            # Execute the function
            if function_name == "search_morphosource":
                function_response = search_morphosource(function_args["query"])
            elif function_name == "get_morphosource_media":
                function_response = get_morphosource_media(function_args["media_id"])
            else:
                function_response = {"error": f"Unknown function: {function_name}"}
            
            # Add function response to messages
            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": function_name,
                "content": json.dumps(function_response)
            })
        
        # Get final response from OpenAI
        second_response = client.chat.completions.create(
            model="gpt-4",
            messages=messages
        )
        
        return {
            "role": "assistant",
            "content": second_response.choices[0].message.content,
            "tool_calls": [
                {
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments)
                }
                for tc in tool_calls
            ]
        }
    
    except Exception as e:
        return {"error": str(e)}


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: chat_handler.py '<json_payload>'")
        sys.exit(1)
    
    try:
        # Parse the payload
        payload = json.loads(sys.argv[1])
        messages = payload.get('messages', [])
        
        if not messages:
            print(json.dumps({"error": "No messages provided"}))
            sys.exit(1)
        
        # Process the chat
        result = process_chat(messages)
        
        # Output the result
        print(json.dumps(result, indent=2))
        
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
