# Batch Processor Automation Fix

## Problem

The batch processor was successfully creating issues for queries, but the automation to create responses (via the query-processor workflow) did not run for any of the issues.

## Root Cause

GitHub Actions has a security feature that prevents workflows triggered by the `GITHUB_TOKEN` from triggering other workflows. This is designed to prevent recursive or accidental workflow executions.

When the batch-query-processor workflow creates issues using `actions/github-script@v8` with the default `GITHUB_TOKEN`, those issue creation events do not trigger the `issue-query-trigger.yml` workflow, even though the workflow is configured to listen for `issues: [opened, labeled]` events.

## Solution

Instead of relying on the `issue-query-trigger.yml` workflow to automatically detect and process batch-created issues, the batch processor now explicitly triggers the `query-processor.yml` workflow for each issue using the `workflow_dispatch` event.

### Changes Made

1. **Modified `.github/workflows/batch-query-processor.yml`**:
   - Added `id: create-issues` to the issue creation step to allow passing data to subsequent steps
   - Added two outputs from the issue creation step:
     - `issue_numbers`: JSON array of created issue numbers
     - `queries_for_processing`: JSON array of query texts
   - Added new step "Trigger query processing for each issue" that:
     - Iterates through each created issue
     - Calls `github.rest.actions.createWorkflowDispatch()` to trigger the query-processor workflow
     - Passes the query text and issue number as workflow inputs
     - Posts a comment to each issue indicating processing has started
     - Includes error handling for failed workflow triggers
     - Implements rate limiting (2 second delay between triggers)

2. **Added tests in `tests/test_workflow_structure.py`**:
   - `test_batch_processor_workflow_syntax`: Validates YAML syntax
   - `test_batch_processor_has_workflow_dispatch_trigger`: Ensures manual triggering is possible
   - `test_batch_processor_triggers_query_processor`: Verifies the workflow triggers query-processor
   - `test_batch_processor_creates_issues_with_id`: Validates the issue creation step has proper ID

## How It Works Now

1. **Batch Query Processor runs**:
   - Reads queries from CSV file (up to 25 queries)
   - Creates individual issues for each query
   - Labels issues as `query-request`, `batch-query`, `awaiting-response`
   - Creates a summary issue

2. **Query Processor is explicitly triggered**:
   - For each created issue, the batch processor calls `createWorkflowDispatch()`
   - Passes the query text and issue number as inputs
   - Posts a "processing started" comment to the issue

3. **Query Processor executes**:
   - Formats the query using ChatGPT
   - Searches MorphoSource API
   - Processes results with ChatGPT
   - Posts results as a comment on the issue
   - Closes the issue with `completed` label

4. **Response Grader automatically triggers**:
   - Detects the bot comment containing "Query Processing Complete"
   - Grades the response using AI evaluation
   - Posts grade as a comment on the issue
   - Adds appropriate grade labels

## Benefits of This Approach

1. **Reliable Automation**: No longer dependent on GitHub Actions event propagation limitations
2. **Explicit Control**: Clear workflow triggering with proper error handling
3. **Better Debugging**: Each workflow dispatch is logged and can be traced
4. **Maintains Compatibility**: The `issue-query-trigger.yml` workflow still works for manually created issues
5. **Rate Limiting**: Built-in delays prevent API rate limiting issues

## Testing

All existing tests pass, plus 4 new tests specifically for the batch processor workflow:

```bash
pytest tests/test_workflow_structure.py -v
```

```
✓ test_batch_processor_workflow_syntax
✓ test_batch_processor_has_workflow_dispatch_trigger
✓ test_batch_processor_triggers_query_processor
✓ test_batch_processor_creates_issues_with_id
```

## Future Considerations

- Monitor workflow execution logs to ensure all dispatches succeed
- Consider adding retry logic if workflow dispatch fails
- May want to add batch processing status tracking in the summary issue
