# Metadata-to-Morphosource Compare

A tool for comparing local metadata with Morphosource database records and verifying voxel spacing values.

## Overview

This repository contains tools to:

1. **Compare** metadata from local CSV files with Morphosource database records
2. **Match** specimens based on catalog numbers and taxonomic information
3. **Verify** voxel spacing values between matched records

## Prerequisites

Before running the workflow, you need:

1. **Morphosource API Key** - For accessing private media records (required for verification)
2. **Local CSV File** - Containing specimen metadata to compare
3. **Morphosource JSON Data** - Complete dataset from Morphosource

### Directory Structure

The repository expects the following directory structure:

```
├── data/
│   ├── csv/          # Your local CSV files for comparison
│   ├── json/         # The Morphosource JSON data
│   └── output/       # Output directory (created automatically)
├── compare.py        # Main comparison script
├── verify_pixel_spacing.py # Verification script
└── run_comparison.py # Helper script for local execution
```

Make sure your CSV file is in the `data/csv/` directory and the Morphosource JSON file (`morphosource_data_complete_compare.json`) is in the `data/json/` directory.

## Running the Workflow

### Option 1: Using GitHub Actions (Recommended)

1. **Go to the Actions tab** in your GitHub repository
2. **Select the "Morphosource Data Comparison and Verification" workflow** from the left sidebar
3. **Click "Run workflow"** button
4. **Enter the CSV filename** to compare against (just the filename, not the full path)
5. **Click "Run workflow"** to start the process

The workflow will:
- Install required dependencies
- Run the comparison script
- Run the verification script using your stored API key
- Save the results as downloadable artifacts

### Option 2: Running Locally

You can also run the workflow locally using the provided `run_comparison.py` script:

```bash
# Basic comparison without verification
python run_comparison.py --csv "Your CSV Filename.csv"

# Full comparison with verification (requires API key)
python run_comparison.py --csv "Your CSV Filename.csv" --api-key "your-api-key-here"
```

## Understanding the Results

The workflow produces two primary output files:

1. **matched.csv** - The initial matching results:
   - Contains all records from your input CSV
   - Adds `Match_Found` column indicating if a match was found
   - Adds `Morphosource_URL` column with links to matching records
   - Adds `Match_Score` column with numerical quality scores for matches

2. **confirmed_matches.csv** - The verified matching results:
   - Contains all the matches from the first stage
   - Adds `voxel_spacing_verified` column indicating verification status
   - Adds `api_voxel_spacing` column showing the values retrieved from the API

### Verification Status Values

The `voxel_spacing_verified` column can have these values:

- **Yes** - Voxel spacing values match between CSV and API
- **No** - Voxel spacing values don't match
- **API values used** - CSV had no voxel data, API values were stored
- **Incomplete API data** - API couldn't provide complete voxel data
- **Skipped** or **Invalid URL** - Record couldn't be processed

## Setting Up API Key

To use the verification feature, you need to set up a Morphosource API key:

1. **For GitHub Actions:**
   - Go to your repository Settings
   - Select "Secrets and variables" → "Actions"
   - Click "New repository secret"
   - Name: `MORPHOSOURCE_API_KEY`
   - Value: Your actual API key
   - Click "Add secret"

2. **For local execution:**
   - Pass the API key directly using the `--api-key` parameter

## Troubleshooting

### Common Issues

1. **"File not found" errors:**
   - Ensure your CSV file is in the `data/csv/` directory
   - Ensure the Morphosource JSON file is in `data/json/`

2. **"Missing required columns" warnings:**
   - The script looks for specific column names related to voxel spacing
   - If your CSV uses different column names, they will be created automatically with empty values
   - The API data will be used if available

3. **API errors:**
   - Check that your API key is correct and has the necessary permissions
   - Some records may be inaccessible even with an API key

### Getting Help

If you encounter issues:
1. Check the workflow run logs for detailed error messages
2. Make sure your data files match the expected format
3. Verify that your API key is valid

## License

[Your license information here]