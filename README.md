# Metadata-to-Morphosource Compare

A user-friendly tool for researchers to compare their specimen metadata with Morphosource database records and verify voxel spacing values.

## 🤖 NEW: AI-Powered MorphoSource Search

**Try our interactive AI assistant at: [GitHub Pages URL](https://johntrue15.github.io/Metadata-to-Morphsource-compare/)**

Ask natural language questions like:
- "Tell me about lizards on MorphoSource"
- "How many snake specimens are available?"
- "Show me CT scans of crocodiles"

The AI assistant uses ChatGPT to understand your query and automatically searches the MorphoSource database to provide relevant results.

### Setting Up the AI Assistant

1. Visit the [GitHub Pages site](https://johntrue15.github.io/Metadata-to-Morphsource-compare/)
2. Enter your OpenAI API key when prompted (get one at [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys))
3. Your API key is stored only in your browser - it's never sent to any server except OpenAI
4. Start asking questions about MorphoSource specimens!

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

# Place your CSV in the data/csv directory

# Run the comparison
python run_comparison.py --csv "Your CSV Filename.csv" --api-key "your-api-key-here"
```

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
3. Contact repository maintainers for assistance

## License

[Your license information here]