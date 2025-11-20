# Metadata-to-Morphosource Compare

[![Tests](https://github.com/johntrue15/Metadata-to-Morphsource-compare/actions/workflows/tests.yml/badge.svg)](https://github.com/johntrue15/Metadata-to-Morphsource-compare/actions/workflows/tests.yml)
[![Code Quality](https://github.com/johntrue15/Metadata-to-Morphsource-compare/actions/workflows/code-quality.yml/badge.svg)](https://github.com/johntrue15/Metadata-to-Morphsource-compare/actions/workflows/code-quality.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A user-friendly tool for researchers to compare their specimen metadata with Morphosource database records and verify voxel spacing values.

## 🤖 NEW: AI-Powered MorphoSource Query System

**Try our interactive query system at: [https://johntrue15.github.io/Metadata-to-Morphsource-compare/](https://johntrue15.github.io/Metadata-to-Morphsource-compare/)**

Ask natural language questions like:
- "Tell me about lizards on MorphoSource"
- "How many snake specimens are available?"
- "Show me CT scans of crocodiles"

The system uses GitHub Actions to process your queries through:
1. **ChatGPT Query Formatter** - Converts natural language to optimized API queries
2. **MorphoSource API** - Searches the database with formatted queries
3. **ChatGPT Response Processor** - Analyzes results and provides natural language responses

### How It Works

1. **Submit a Query**: Visit the GitHub Pages site and enter your question
2. **Create Issue**: Click to create a GitHub Issue (requires free GitHub account)
3. **Auto-Trigger**: The issue automatically triggers the query processor workflow
4. **Sequential Processing**: 
   - Job 1: ChatGPT formats your natural language query into an optimized API call
   - Job 2: MorphoSource API searches for relevant data using the formatted query
   - Job 3: ChatGPT processes the results and generates a natural language response
5. **Get Results**: Results are posted as a comment on your issue + you get notified

**Why Issues?** This approach eliminates HTTP 401 errors by using GitHub's native issue system instead of API authentication.

### Setting Up (For Repository Owner)

To enable the query system:

1. **Enable GitHub Pages:**
   - Go to repository **Settings** → **Pages**
   - Under **Source**, select **GitHub Actions**
   - The site will be automatically deployed

2. **Configure API Keys:**
   - Go to **Settings** → **Secrets and variables** → **Actions**
   - Add these secrets:
     - `OPENAI_API_KEY` - Your OpenAI API key
     - `MORPHOSOURCE_API_KEY` - Your MorphoSource API key (optional)

### Using the Query System

1. Visit the GitHub Pages site
2. Enter your question in the text box
3. Click "Prepare to Submit Query"
4. Click the link to create a GitHub Issue (requires GitHub account)
5. Submit the pre-filled issue
6. Wait for results to be posted as a comment on your issue (usually within 1-2 minutes)
7. Optionally download artifacts from the Actions tab for detailed JSON responses

### 💬 NEW: Conversation Continuation

**Ask follow-up questions!** You can now continue the conversation directly in the issue:

1. After receiving a response to your query, simply **add a comment** to the same issue with your follow-up question
2. The system automatically detects your follow-up and triggers the query processor with full conversation context
3. The bot will quote the previous response and answer your new question in context
4. You can keep asking follow-ups as many times as you like!

**Example:**
- Initial query: "How many snake specimens are available?"
- Bot responds: "There are 4 snake specimens..."
- You comment: "Can you list them?"
- Bot responds with the full list, maintaining context from the original conversation

This creates a natural conversational flow without having to create new issues for each question.

For more details, see [CONVERSATION_CONTINUATION.md](CONVERSATION_CONTINUATION.md)

### 🧪 Batch Query Testing & Grading

**NEW:** Test multiple queries at once and get automated quality assessments!

- **Batch Query Processor** - Process up to 25 queries from a CSV file simultaneously
- **Response Grader** - Automatically grade query responses from 0-100% using AI

For details, see [BATCH_TESTING.md](BATCH_TESTING.md)

---

## For Researchers: Quick Start Guide

**Step 1:** Upload your CSV file to the `data/csv/` folder in this repository
- Click on the `data/csv/` folder
- Click "Add file" → "Upload files"
- Drag your CSV file or click to browse your computer
- Add a commit message like "Add my specimen data CSV"
- Click "Commit changes"

**Step 2:** Run the comparison workflow
- Click on the "Actions" tab at the top of the repository
- Select "Morphosource Data Comparison and Verification" from the left sidebar
- Click the "Run workflow" button
- Enter your CSV filename (just the name, not the path)
- Click "Run workflow" to start the process

**Step 3:** Access your results
- When the workflow completes (usually within a few minutes), click on the completed run
- Scroll to the bottom to the "Artifacts" section
- Click on "morphosource-comparison-results" to download a zip file with the results

That's it! The system will match your specimen data against Morphosource records and verify the voxel spacing values.

## What This Tool Does

This repository helps researchers to:

1. **Compare** your local specimen metadata with Morphosource database records
2. **Match** specimens based on catalog numbers and taxonomic information
3. **Verify** voxel spacing values in CT scans between your records and Morphosource

## Understanding Your Results

You'll receive two CSV files with your results:

1. **matched.csv** - Shows which of your specimens were found in Morphosource:
   - Contains all your original data
   - Adds a `Match_Found` column (yes/no)
   - Adds `Morphosource_URL` links to matching records
   - Adds `Match_Score` showing the confidence of each match (higher is better)

2. **confirmed_matches.csv** - Verifies the voxel spacing values:
   - Includes a `voxel_spacing_verified` column showing if values match
   - Contains API voxel spacing values for comparison
   - Helps identify any discrepancies between your data and Morphosource

### What The Verification Status Means

In your results, the `voxel_spacing_verified` column will show:

- **Yes** - Voxel spacing values match between your data and Morphosource
- **No** - Voxel spacing values don't match (possible data quality issue)
- **API values used** - Your CSV didn't have voxel data, so Morphosource values were used
- **Incomplete API data** - Morphosource couldn't provide complete voxel data
- **Skipped** or **Invalid URL** - The record couldn't be processed

## Technical Details

### Required Files and Structure

The repository uses this file structure:

```
├── data/
│   ├── csv/          # Upload your CSV files here
│   ├── json/         # Contains the Morphosource database
│   └── output/       # Where results are saved (created automatically)
├── compare.py        # Comparison script
├── verify_pixel_spacing.py # Verification script
└── run_comparison.py # Helper script for local execution
```

### Required API Keys (For Repository Maintainers)

To use the verification feature and AI assistant backend, configure these API keys:

1. Go to repository Settings
2. Select "Secrets and variables" → "Actions"
3. Click "New repository secret"
4. Add the following secrets:
   - Name: `MORPHOSOURCE_API_KEY`
     Value: Your Morphosource API key
   - Name: `OPENAI_API_KEY`
     Value: Your OpenAI API key (for backend chat processing)
5. Click "Add secret" for each

**Note:** The OPENAI_API_KEY is optional. The AI assistant works client-side with users providing their own keys. The repository secret is only needed if you want to implement a backend API to handle API keys centrally.

### Running Locally (For Advanced Users)

If you prefer to run the tool locally rather than through GitHub Actions:

```bash
# Clone the repository
git clone https://github.com/yourusername/Metadata-to-Morphsource-compare.git
cd Metadata-to-Morphsource-compare

# Create and activate a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Optional: Set up environment variables
cp .env.example .env
# Edit .env and add your API keys

# Place your CSV in the data/csv directory

# Run the comparison
python run_comparison.py --csv "Your CSV Filename.csv" --api-key "your-api-key-here"
```

### Installation as a Package (Optional)

You can also install this tool as a Python package:

```bash
# Install in development mode
pip install -e .

# Or install with development dependencies
pip install -e ".[dev]"

# Use the morpho command-line tool
morpho --help
```

## For Developers

### Testing

This project includes a comprehensive test suite. To run tests locally:

```bash
# Install test dependencies
pip install -r requirements-test.txt

# Run all tests
pytest tests/

# Run tests with coverage
pytest tests/ --cov=. --cov-report=term
```

For detailed testing information, see [TESTING.md](TESTING.md).

### Continuous Integration

Tests automatically run on:
- Push to main or develop branches
- Pull requests
- Manual workflow dispatch

View test results in the **Actions** tab.

## Troubleshooting

### Common Issues

1. **"File not found" errors:**
   - Make sure your CSV file is correctly uploaded to the `data/csv/` folder
   - Check that you entered the exact filename in the workflow

2. **Column name warnings:**
   - The script looks for specific column names for voxel spacing data
   - If your CSV uses different column names, the system will still work but may not match all values
   - Columns with voxel spacing should ideally be named: `x_voxel_spacing_mm`, `y_voxel_spacing_mm`, `z_voxel_spacing_mm`

3. **No matches found:**
   - Check that your specimen identifiers (catalog numbers) match the format in Morphosource
   - The system uses both catalog numbers and taxonomic information for matching

### Getting Help

If you encounter issues:
1. Check the workflow run logs for error messages
2. Verify your CSV format matches expected columns
3. Open an issue on GitHub for assistance

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on:
- Setting up your development environment
- Code style and quality standards
- Testing requirements
- How to submit pull requests

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.