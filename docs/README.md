# MorphoSource Query System

Welcome to the MorphoSource Query System! This web application allows you to submit natural language queries about specimens in the MorphoSource database.

## How to Use

1. **Enter Your Query**: Type your question in the text box on the main page
2. **Submit**: Click "Submit Query" to trigger the GitHub Actions workflow
3. **View Results**: Follow the link to the Actions tab to see your query results

## Example Queries

- "Tell me about lizard specimens"
- "How many snake specimens are available?"
- "Show me CT scans of crocodiles"
- "What Anolis specimens are in the database?"

## How It Works

This system uses GitHub Actions workflows to process your queries:

1. **Submit**: Your query triggers a GitHub Actions workflow
2. **Search**: The workflow searches the MorphoSource API for relevant data
3. **Analyze**: ChatGPT processes the results and generates a response
4. **Results**: View the workflow summary and download artifacts for detailed information

## Documentation

For detailed information, see the [Query System Guide](QUERY_SYSTEM_GUIDE.md).

## About MorphoSource

MorphoSource is a digital repository of 3D specimen data, including CT scans and other imaging data for biological specimens. Learn more at [morphosource.org](https://www.morphosource.org/).
