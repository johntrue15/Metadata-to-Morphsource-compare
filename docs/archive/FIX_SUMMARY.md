# Fix Summary: Shell Quoting Issues in GitHub Actions Workflows

## Problem

The ChatGPT Query processing was failing with the following error:
```
/home/runner/work/_temp/xxxxx.sh: line 13: unexpected EOF while looking for matching `''
Error: Process completed with exit code 2.
```

## Root Cause

GitHub Actions variables were being interpolated directly into shell commands and Python scripts without proper escaping. When user queries or API results contained special characters (quotes, backticks, newlines, etc.), they would break the shell syntax and cause workflow failures.

### Vulnerable Patterns

**Before (Unsafe):**
```yaml
run: |
  echo "Query: ${{ steps.get-query.outputs.query }}" >> $GITHUB_STEP_SUMMARY
```

```yaml
run: |
  python - <<'EOF'
  query = "${{ steps.get-query.outputs.query }}"
  EOF
```

**After (Safe):**
```yaml
env:
  QUERY_TEXT: ${{ steps.get-query.outputs.query }}
run: |
  echo "Query: $QUERY_TEXT" >> $GITHUB_STEP_SUMMARY
```

```yaml
env:
  QUERY_TEXT: ${{ steps.get-query.outputs.query }}
run: |
  python - <<'EOF'
  import os
  query = os.environ.get('QUERY_TEXT', '')
  EOF
```

## Changes Made

### 1. `.github/workflows/query-processor.yml`

#### Summary Step (Lines 251-269)
- Added `env:` block with `QUERY_TEXT` and `MORPHOSOURCE_RESULTS`
- Changed echo commands to use environment variables instead of direct interpolation

#### Extract Query Steps (Lines 38-45 and 150-157)
- Added `env:` block with `DISPATCH_QUERY` and `PAYLOAD_QUERY`
- Changed echo commands writing to GITHUB_OUTPUT to use environment variables

#### Python Scripts (Lines 47-118 and 159-243)
- Added `QUERY_TEXT` to the `env:` block
- Changed from `query = "${{ ... }}"` to `query = os.environ.get('QUERY_TEXT', '')`

### 2. `.github/workflows/issue-query-trigger.yml`

#### Extract Query Step (Lines 18-33)
- Added `env:` block with `ISSUE_BODY`
- Removed direct assignment of `ISSUE_BODY='${{ ... }}'`

#### Trigger Workflow Step (Lines 46-63)
- Added `env:` block with `QUERY_TEXT`
- Changed JavaScript from template literal to `process.env.QUERY_TEXT`

### 3. `.github/workflows/parse_morphosource.yml`

#### Run Comparison Step (Lines 32-41)
- Added `env:` block with `CSV_FILENAME`
- Changed echo and heredoc to use environment variable

## Testing

All changes were validated with:

1. **YAML Syntax Validation**: Confirmed all workflow files have valid YAML syntax
2. **Shell Escaping Tests**: Verified environment variables properly handle:
   - Double quotes
   - Single quotes
   - Backticks
   - Newlines
   - Special characters ($, &, <, >, !)
3. **Python Environment Variable Tests**: Confirmed Python scripts correctly read from environment variables

## Benefits

✅ **Prevents Shell Injection**: User input cannot break shell syntax
✅ **Handles Special Characters**: Quotes, backticks, and other special chars work correctly
✅ **Maintains Functionality**: All workflows continue to work as designed
✅ **Consistent Pattern**: Uses the same safe pattern across all workflows
✅ **No Breaking Changes**: Existing functionality is preserved

## Example Queries That Now Work

These queries would have previously caused failures:

- `Tell me about "lizard" specimens`
- `Show me specimens with 'special' names`
- `Find specimens from the 1990's & 2000's`
- Queries with newlines or long text
- Queries containing backticks or dollar signs

## Related Documentation

- GitHub Actions: [Environment Variables](https://docs.github.com/en/actions/learn-github-actions/variables)
- GitHub Actions: [Workflow syntax for GitHub Actions](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions)
- Bash: [Quoting](https://www.gnu.org/software/bash/manual/html_node/Quoting.html)
