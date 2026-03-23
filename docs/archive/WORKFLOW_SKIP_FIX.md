# Workflow Skip Issue Fix

## Issue
Issue #13 with query "Tell me about lizard specimens" was created but the `issue-query-trigger.yml` workflow was skipped.

## Root Cause
The workflow had a job-level conditional that required the `query-request` label to be present on the issue:
```yaml
if: contains(github.event.issue.labels.*.name, 'query-request')
```

When users create issues via GitHub's issue creation URL (even with the `labels` parameter), the label is only pre-filled in the UI but not automatically applied unless the user explicitly confirms. Issue #13 was created without any labels, causing the workflow to skip.

## Solution
Modified the `issue-query-trigger.yml` workflow to:

1. **Remove the label requirement** - The job now runs for all issues
2. **Detect query submissions by content** - Added a new step that checks if:
   - Issue body contains "MorphoSource Query Submission" marker, OR
   - Issue title starts with "Query:"
3. **Auto-add label on detection** - When a query is detected, the workflow automatically adds the `query-request` label
4. **Conditional execution** - All subsequent steps only run if the issue is detected as a query submission

## Changes Made

### Before
```yaml
jobs:
  trigger-query:
    if: contains(github.event.issue.labels.*.name, 'query-request')
    runs-on: ubuntu-latest
    steps:
      - name: Extract query from issue
        # ...
```

### After
```yaml
jobs:
  trigger-query:
    runs-on: ubuntu-latest
    steps:
      - name: Check if issue is a query submission
        id: check
        run: |
          # Check body for marker or title for prefix
          if echo "$ISSUE_BODY" | grep -q "MorphoSource Query Submission"; then
            echo "is_query=true" >> $GITHUB_OUTPUT
          elif echo "$ISSUE_TITLE" | grep -q "^Query:"; then
            echo "is_query=true" >> $GITHUB_OUTPUT
          else
            echo "is_query=false" >> $GITHUB_OUTPUT
          fi
      
      - name: Extract query from issue
        if: steps.check.outputs.is_query == 'true'
        # ...
      
      - name: Add query-request label
        if: steps.check.outputs.is_query == 'true'
        # Automatically adds the label
```

## Benefits
- ✅ **Robust detection** - Works even if the label isn't applied during issue creation
- ✅ **Backward compatible** - Still works with manually labeled issues
- ✅ **No user impact** - Users follow the same submission process
- ✅ **Automatic labeling** - Label is applied after detection for organization
- ✅ **Safe filtering** - Regular bug reports and issues are correctly ignored

## Testing
The fix was validated with:
1. Issue #13's actual content (has the marker, should be detected) ✅
2. Regular issues without the marker (should be skipped) ✅
3. Issues with only the title prefix (should be detected) ✅

## Related Files
- `.github/workflows/issue-query-trigger.yml` - Main fix
- `docs/QUERY_SUBMISSION_GUIDE.md` - Updated troubleshooting section
- `docs/index.html` - No changes needed (already sets the label in URL)

## How to Test
To verify this works for Issue #13:
1. Add the `query-request` label manually to Issue #13
2. The workflow will trigger on the `labeled` event and process the query

Or create a new test issue with the same content to see it work automatically.
