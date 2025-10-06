# MorphoSource AI Assistant

This is a web-based AI assistant that helps you search and explore the MorphoSource database using natural language queries.

## How It Works

1. The interface uses ChatGPT (via OpenAI API) to understand your natural language queries
2. ChatGPT automatically determines when to search MorphoSource and what parameters to use
3. The system calls the MorphoSource API to retrieve relevant data
4. ChatGPT analyzes the results and provides a human-readable response

## Features

- **Natural Language Queries**: Ask questions in plain English
- **Automatic Tool Selection**: ChatGPT decides when and how to query MorphoSource
- **Real-time Search**: Get immediate results from the MorphoSource database
- **Conversational Interface**: Follow-up questions maintain context
- **Secure**: Your API key is stored only in your browser's localStorage

## Setup

1. Visit the page
2. Enter your OpenAI API key when prompted
3. Start asking questions!

## Example Queries

- "Tell me about lizards on MorphoSource"
- "How many snake specimens are available?"
- "Show me CT scans of crocodiles"
- "Find specimens from the genus Anolis"

## Privacy & Security

- Your OpenAI API key is stored only in your browser (localStorage)
- API keys are never sent to any server except OpenAI's official API
- All API calls are made directly from your browser
- No data is collected or stored by this application

## Technical Details

This is a static web application built with:
- Pure HTML/CSS/JavaScript (no frameworks required)
- OpenAI GPT-4 API with function calling
- MorphoSource public API
- Client-side only architecture for maximum security

## Cost

Using this assistant requires an OpenAI API key. API usage costs are billed by OpenAI based on:
- GPT-4 model pricing
- Number of tokens used per conversation

Typical queries cost a few cents each.
