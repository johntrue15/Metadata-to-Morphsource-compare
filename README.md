# Metadata-to-Morphosource Compare

A user-friendly tool for researchers to compare their specimen metadata with Morphosource database records and verify voxel spacing values.

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

### Required API Key (For Repository Maintainers)

To use the verification feature, a Morphosource API key must be configured:

1. Go to repository Settings
2. Select "Secrets and variables" → "Actions"
3. Click "New repository secret"
4. Name: `MORPHOSOURCE_API_KEY`
5. Value: Your Morphosource API key
6. Click "Add secret"

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