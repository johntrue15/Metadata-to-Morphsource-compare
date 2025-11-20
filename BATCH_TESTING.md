# Batch Query Testing and Grading System

This repository includes two automated workflows for testing and evaluating MorphoSource queries at scale.

## Overview

1. **Batch Query Processor** - Processes up to 25 queries from a CSV file
2. **Response Grader** - Automatically grades query responses from 0-100%

## Workflow 1: Batch Query Processor

### Purpose
Automates testing of multiple queries simultaneously by creating individual issues for each query.

### How to Use

1. **Prepare your queries CSV file**
   - Format: Single column with header `query`
   - Each row contains one query
   - Up to 25 queries will be processed
   - Example: See `test_queries.csv` in the repository root

2. **Run the workflow**
   - Go to **Actions** → **Batch Query Processor**
   - Click **Run workflow**
   - (Optional) Specify a custom CSV file path
   - Click **Run workflow**

3. **What happens**
   - Creates individual issues for each query
   - Labels them as `query-request`, `batch-query`, `awaiting-response`
   - Creates a summary issue with links to all queries
   - Each issue automatically triggers the query processor workflow

### CSV Format

```csv
query
Tell me about lizards on MorphoSource
How many snake specimens are available?
Show me CT scans of crocodiles
```

## Workflow 2: Response Grader

### Purpose
Evaluates the quality of query responses using AI-powered analysis.

### How It Works

The workflow automatically triggers when:
- A bot posts a query response comment on an issue
- The issue has labels `query-request` or `batch-query`
- The issue hasn't been graded yet

You can also manually trigger it:
- Go to **Actions** → **Response Grader**
- Click **Run workflow**
- Enter the issue number to grade
- Click **Run workflow**

### Grading Criteria

Responses are evaluated on a 100-point scale:

| Score | Category | Description |
|-------|----------|-------------|
| 0-20 | Failed | No valid query or response generated |
| 21-40 | Poor | Query generated but no results or mostly incorrect |
| 41-60 | Fair | Some results but incomplete or contains errors |
| 61-80 | Good | Relevant information with minor issues |
| 81-100 | Excellent | Accurate, comprehensive, well-formatted response |

### Breakdown (25 points each)

1. **Query Formation** - Was a valid MorphoSource API query generated?
2. **Results Quality** - Did the query return relevant results?
3. **Response Accuracy** - Is the response factually accurate?
4. **Response Completeness** - Does it fully address the question?

### Grade Labels

Issues are automatically labeled:
- `graded` - Response has been evaluated
- `grade-excellent` - Score 80-100
- `grade-good` - Score 60-79
- `grade-fair` - Score 40-59
- `grade-low` - Score 0-39

## Example Grade Comment

```markdown
## 🌟 Response Grade: 85/100

### Breakdown

| Criterion | Score |
|-----------|-------|
| Query Formation | 22/25 |
| Results Quality | 23/25 |
| Response Accuracy | 20/25 |
| Response Completeness | 20/25 |
| **Total** | **85/100** |

### Evaluation

**Strengths:** Successfully generated a valid API query that returned relevant results. Response was well-formatted and accurately described the specimens found.

**Areas for Improvement:** Could have included more detail about specific specimen characteristics.

**Reasoning:** The system performed well overall, with a valid query returning good results and an accurate, helpful response.

---

**Results Found:** 15 specimens

*Graded by automated response evaluation system*
```

## Testing the System

### Quick Test

1. Run the batch processor with the default `test_queries.csv`
2. Wait for issues to be created (1-2 minutes)
3. Wait for query responses to be posted (2-5 minutes per query)
4. Watch as responses are automatically graded (1-2 minutes per response)

### Custom Test

1. Create your own CSV file with test queries
2. Add it to the repository (e.g., in a `test-data/` folder)
3. Run the batch processor workflow with your CSV path
4. Monitor the results

## Workflow Files

- `.github/workflows/batch-query-processor.yml` - Batch query processor
- `.github/workflows/response-grader.yml` - Automatic grading system
- `.github/scripts/grade_response.py` - Python grading script
- `test_queries.csv` - Sample queries for testing

## Requirements

- **OpenAI API Key** - Required for grading (set as `OPENAI_API_KEY` secret)
- **MorphoSource API Key** - Optional, for querying (set as `MORPHOSOURCE_API_KEY` secret)
- **GitHub Actions** - Enabled in repository settings

## Monitoring

### Track Batch Query Progress

1. Check the summary issue created by the batch processor
2. View individual issue statuses
3. Monitor the Actions tab for workflow runs

### View Grades

1. Go to **Issues** tab
2. Filter by label: `graded`
3. Further filter by grade: `grade-excellent`, `grade-good`, etc.
4. Read detailed evaluation in issue comments

## Troubleshooting

### Batch Processor Issues

**Problem:** CSV file not found
- **Solution:** Ensure the CSV file path is correct relative to repo root

**Problem:** No issues created
- **Solution:** Check workflow logs for errors, verify CSV format

### Grader Issues

**Problem:** Response not graded
- **Solution:** Verify the response comment was posted by a bot and contains "Query Processing Complete"

**Problem:** Grading failed
- **Solution:** Check that `OPENAI_API_KEY` secret is configured correctly

## Best Practices

1. **Start Small** - Test with 5-10 queries first
2. **Monitor Costs** - Each grade uses OpenAI API credits
3. **Review Results** - Use grades to improve query processing
4. **Iterate** - Refine your test queries based on results

## Future Enhancements

Potential improvements:
- Aggregate grading statistics across batches
- Compare grades between different query formulations
- Generate reports on common failure patterns
- Auto-retry low-scoring queries with refined prompts
