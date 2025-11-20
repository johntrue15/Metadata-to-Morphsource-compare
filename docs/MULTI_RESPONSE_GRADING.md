# Multi-Response Grading

## Overview

The response grader workflow has been enhanced to handle multiple query responses within a single issue. This allows each response to be graded separately and tracked individually.

## Problem

Previously, when an issue contained multiple "Query Processing Complete" responses, only the first one was graded. This was due to a `break` statement in the extraction logic that stopped after finding the first response comment.

## Solution

The workflow now:
1. Extracts ALL response comments from an issue
2. Tracks which responses have already been graded using comment ID markers
3. Grades only ungraded responses
4. Posts a separate grade comment for each response
5. Numbers responses when multiple exist (e.g., "Response 1/2", "Response 2/2")

## How It Works

### 1. Response Extraction

The workflow scans all comments on an issue looking for comments containing "Query Processing Complete" or "ChatGPT Response". Each response is collected with:
- Comment ID (for tracking)
- Response text
- MorphoSource API results

### 2. Tracking Graded Responses

When a grade is posted, the comment includes a hidden marker:
```html
<!-- graded-comment-id: 123456 -->
```

This marker allows the workflow to identify which specific responses have already been graded. The workflow filters out already-graded responses before processing.

### 3. Grading Process

For each ungraded response:
1. The Python grading script is called with the query, response text, and API results
2. A grade (0-100) is calculated with a breakdown of criteria
3. The grade is stored with its associated comment ID

### 4. Posting Grades

Each response gets its own grade comment:
- If multiple responses exist, they are numbered: "Response 1/2", "Response 2/2", etc.
- Each comment includes the grade breakdown, strengths, weaknesses, and reasoning
- The hidden marker links the grade to the original response comment

### 5. Issue Labeling

After grading all responses:
- The issue is labeled with the **highest** grade among all responses
- The 'graded' label is applied to the issue
- Labels: `grade-excellent` (80+), `grade-good` (60-79), `grade-fair` (40-59), `grade-low` (<40)

## Example

For an issue with two responses:

**Response 1**: Returns 12 alligator specimens
- Grade: 85/100 (🌟 grade-excellent)

**Response 2**: Returns 0 results
- Grade: 40/100 (⚠️ grade-fair)

**Issue Labels**: `graded`, `grade-excellent` (based on highest grade)

## Benefits

1. **Fair evaluation**: Each response attempt is graded on its own merit
2. **Complete tracking**: All responses are accounted for, not just the first one
3. **Incremental grading**: New responses can be added and graded even after initial grading
4. **Clear feedback**: Users can see the grade for each specific response
5. **No duplicates**: Already-graded responses are not re-graded

## Testing

The multi-response grading functionality is tested through:
- Unit tests in `tests/test_response_grader.py` (10 tests)
- Integration test in `tests/test_integration_multi_response.py`
- Workflow syntax validation

All tests verify:
- Multiple responses are collected
- Graded responses are tracked and filtered
- Each response gets its own grade comment
- Response numbering is correct
- Highest grade label is applied

## Future Enhancements

Potential improvements:
- Add a summary comment showing all grades in a single table
- Track average grade across all responses
- Allow manual re-grading of specific responses
- Add filters to grade only certain types of responses
