# MorphoSource Query System Guide

## Overview

The MorphoSource Query System allows you to ask natural language questions about specimens in the MorphoSource database. The system processes your queries through GitHub Actions workflows, combining MorphoSource API data with ChatGPT's analysis.

## How to Use

### Step 1: Submit a Query

1. Visit the GitHub Pages site: https://johntrue15.github.io/Metadata-to-Morphsource-compare/
2. Enter your question in the text box
3. Click "Submit Query"

### Step 2: View Results

1. Click the workflow link shown after submission
2. Navigate to the **Actions** tab in the GitHub repository
3. Find your workflow run (named "Query Processor")
4. View the workflow summary for quick results
5. Download artifacts for detailed JSON responses

## Example Queries

- "Tell me about lizard specimens"
- "How many snake specimens are available?"
- "Show me CT scans of crocodiles"
- "What Anolis specimens are in the database?"
- "Find specimens with micro-CT data"

## Understanding Results

### Job 1: MorphoSource API Query

This job searches the MorphoSource database and returns:
- Specimen metadata
- Taxonomy information
- Available media types
- Direct links to specimens

**Result Location:** Download `morphosource-results` artifact

### Job 2: ChatGPT Processing

This job analyzes the MorphoSource data and provides:
- Natural language summary
- Insights about the specimens found
- Answers to your specific questions
- Formatted, easy-to-read responses

**Result Location:** 
- Workflow summary (visible in Actions tab)
- Download `chatgpt-response` artifact

## Workflow Details

The query workflow runs two sequential jobs:

```
User Query
    ↓
Trigger Workflow (repository_dispatch)
    ↓
Job 1: MorphoSource API Search
    ├─ Search database
    ├─ Save results
    └─ Output summary
    ↓
Job 2: ChatGPT Processing
    ├─ Load MorphoSource results
    ├─ Send to ChatGPT with context
    ├─ Generate response
    └─ Create summary
    ↓
Results Available
```

## Manual Workflow Trigger

You can also trigger the workflow manually:

1. Go to the **Actions** tab
2. Select "Query Processor" from the left sidebar
3. Click "Run workflow"
4. Enter your query text
5. Click "Run workflow" button

This method gives you more control and doesn't require API authentication from the browser.

## Troubleshooting

### "Repository dispatch failed"

If the browser-based submission fails, use the manual workflow trigger method instead:
- Go to Actions → Query Processor → Run workflow

### "Workflow not showing up"

Wait 10-30 seconds and refresh the Actions page. GitHub may take a moment to process the dispatch event.

### "No results found"

The MorphoSource API may not have data matching your query. Try:
- Different search terms
- Broader queries (e.g., "reptiles" instead of specific species)
- Checking if the specimen exists at morphosource.org

### "OpenAI API error"

The repository owner needs to configure the `OPENAI_API_KEY` secret in repository settings.

## For Repository Owners

### Required Secrets

Configure these in **Settings → Secrets and variables → Actions**:

1. **OPENAI_API_KEY** (Required)
   - Get from: https://platform.openai.com/api-keys
   - Used for: ChatGPT processing in Job 2

2. **MORPHOSOURCE_API_KEY** (Optional)
   - Get from: MorphoSource.org
   - Used for: Enhanced API access (higher rate limits)

### Monitoring Usage

- Check Actions tab for workflow runs
- Review artifacts to see query/response patterns
- Monitor API costs (OpenAI charges per token)

### Customization

Edit `.github/workflows/query-processor.yml` to:
- Change GPT model (default: gpt-4)
- Adjust response length (max_tokens)
- Modify search parameters
- Add additional processing steps

## Technical Architecture

### Frontend
- Static HTML/CSS/JavaScript
- No backend server required
- Hosted on GitHub Pages
- Triggers workflows via GitHub API

### Backend
- GitHub Actions workflows
- Python scripts for API calls
- Artifact storage for results
- Job summaries for quick viewing

### APIs Used
1. **MorphoSource API**: Specimen database queries
2. **OpenAI API**: Natural language processing
3. **GitHub API**: Workflow triggering (repository_dispatch)

## Privacy & Security

- No user data is stored permanently
- API keys stored as encrypted GitHub secrets
- Workflow logs are retained per GitHub's policy
- Artifacts expire after 90 days (default)

## Cost Considerations

**GitHub Actions:**
- Free for public repositories
- 2000 minutes/month for free accounts

**OpenAI API:**
- Pay-per-use (typically $0.01-0.05 per query)
- Repository owner pays for usage
- Costs depend on query complexity

**MorphoSource API:**
- Free for basic access
- Optional API key for higher limits

## Support

For issues or questions:
1. Check existing workflow runs for error messages
2. Review this guide and README.md
3. Open an issue in the GitHub repository
4. Contact repository maintainers

## Future Enhancements

Potential improvements:
- Real-time result display (webhook-based)
- Result caching for common queries
- Additional data sources
- Advanced filtering options
- Export formats (CSV, PDF)
