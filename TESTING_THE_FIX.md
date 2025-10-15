# Testing the HTTP 401 Fix

## Quick Test (2 minutes)

After this PR is merged, test the fix with these steps:

### 1. Visit the Query Page
**URL:** https://johntrue15.github.io/Metadata-to-Morphsource-compare/

**Expected:** Page loads without errors

### 2. Enter a Test Query
**Example:** "Tell me about lizard specimens"

**Expected:** Form accepts input

### 3. Click "Prepare to Submit Query"
**Expected:**
- Button disables briefly
- Success message appears
- Link to create GitHub Issue appears

### 4. Click the Issue Link
**Expected:**
- Opens GitHub's "New Issue" page
- Form is pre-filled with:
  - Title: "Query: Tell me about lizard specimens"
  - Body: Your full query
  - Label: "query-request"

### 5. Submit the Issue
**Expected:**
- Issue created successfully
- Issue number assigned (e.g., #42)

### 6. Wait for Processing (1-2 minutes)
**Expected:**
- Within 30 seconds: Comment appears saying "Query received! Processing..."
- Within 2 minutes: Results posted as comment
- Issue automatically closed

### 7. Check Notifications
**Expected:**
- Email notification from GitHub
- Shows issue activity and results

---

## Detailed Test Cases

### Test Case 1: Happy Path ✅

**Objective:** Verify complete flow works end-to-end

**Steps:**
1. Visit GitHub Pages site
2. Enter query: "Show me crocodile specimens"
3. Click "Prepare to Submit Query"
4. Click issue link
5. Submit issue on GitHub
6. Wait for results

**Expected Results:**
- ✅ No HTTP 401 errors
- ✅ Issue created successfully
- ✅ Workflow triggers automatically
- ✅ "Processing" comment appears
- ✅ Results posted within 2 minutes
- ✅ Issue closed automatically
- ✅ Email notification received

**Pass Criteria:** All steps complete without errors

---

### Test Case 2: Long Query ✅

**Objective:** Test handling of long query text

**Steps:**
1. Enter a very long query (200+ characters)
2. Submit as normal

**Expected Results:**
- ✅ Title truncated to 50 characters with "..."
- ✅ Full query in issue body
- ✅ Processing works normally

**Pass Criteria:** Long query handled correctly

---

### Test Case 3: Special Characters ✅

**Objective:** Test query with special characters

**Steps:**
1. Enter query with special chars: "What about Anolis & Iguana specimens?"
2. Submit as normal

**Expected Results:**
- ✅ Characters properly encoded in URL
- ✅ Query text preserved correctly
- ✅ Processing works normally

**Pass Criteria:** Special characters handled correctly

---

### Test Case 4: Multiple Queries ✅

**Objective:** Test multiple submissions

**Steps:**
1. Submit first query: "Tell me about lizards"
2. Wait for results
3. Submit second query: "Tell me about snakes"
4. Wait for results

**Expected Results:**
- ✅ Both queries processed independently
- ✅ Each gets its own issue
- ✅ Both receive results
- ✅ No interference between queries

**Pass Criteria:** Multiple queries work independently

---

### Test Case 5: Error Handling ✅

**Objective:** Test error scenarios

**Steps:**
1. Trigger workflow with empty query (if possible)
2. Or submit query when OpenAI API key is missing

**Expected Results:**
- ✅ Error message posted to issue
- ✅ Error is clear and helpful
- ✅ Issue remains open for review
- ✅ No system crash

**Pass Criteria:** Errors handled gracefully

---

### Test Case 6: Fallback Method ✅

**Objective:** Verify manual trigger still works

**Steps:**
1. Go to Actions → Query Processor
2. Click "Run workflow"
3. Enter query manually
4. Submit

**Expected Results:**
- ✅ Workflow runs successfully
- ✅ Results in workflow summary
- ✅ Artifacts available for download
- ✅ No issue created (as expected)

**Pass Criteria:** Manual method still functional

---

## Performance Tests

### Test 1: Submission Speed ⚡

**Measure:** Time from clicking "Prepare to Submit" to issue created

**Expected:** < 5 seconds (mostly GitHub's issue creation time)

**Pass Criteria:** Fast enough for good UX

### Test 2: Processing Speed ⚡

**Measure:** Time from issue created to results posted

**Expected:** 1-2 minutes (depends on APIs)

**Breakdown:**
- Issue detection: ~10 seconds
- MorphoSource API: ~30 seconds
- ChatGPT processing: ~30-60 seconds
- Result posting: ~10 seconds

**Pass Criteria:** Completes within 3 minutes

### Test 3: Notification Speed 📧

**Measure:** Time from result posted to email received

**Expected:** < 1 minute (GitHub's notification system)

**Pass Criteria:** Email arrives promptly

---

## Security Tests

### Test 1: No Exposed Secrets 🔒

**Check:** View page source, inspect JavaScript

**Expected:** 
- ✅ No API keys visible
- ✅ No tokens in code
- ✅ Only public GitHub URLs

**Pass Criteria:** No secrets exposed

### Test 2: Proper Authentication 🔒

**Check:** Workflow runs with correct permissions

**Expected:**
- ✅ Workflows use GitHub's authentication
- ✅ No user credentials needed
- ✅ Server-side auth only

**Pass Criteria:** Authentication secure

---

## Compatibility Tests

### Test 1: Desktop Browsers 💻

**Test on:**
- Chrome/Edge
- Firefox
- Safari

**Expected:** Works on all major browsers

### Test 2: Mobile Browsers 📱

**Test on:**
- Mobile Chrome
- Mobile Safari
- GitHub mobile app

**Expected:** Mobile-friendly, works on all

### Test 3: GitHub Logged Out 👤

**Test:** Access page without GitHub login

**Expected:**
- ✅ Page loads
- ✅ Form works
- ✅ Issue link opens
- ✅ GitHub prompts for login when creating issue

**Pass Criteria:** Works for logged-out users up to issue creation

---

## Regression Tests

### Test 1: Existing Workflows ✅

**Check:** Other workflows still work

**Expected:**
- ✅ deploy-pages.yml works
- ✅ chat-api.yml works
- ✅ Other workflows unaffected

**Pass Criteria:** No regression in other features

### Test 2: Documentation Pages 📄

**Check:** All doc pages accessible

**Expected:**
- ✅ index.html loads
- ✅ All .md files accessible
- ✅ Links work
- ✅ No broken pages

**Pass Criteria:** Documentation intact

---

## Automated Test Script

```bash
#!/bin/bash
# Quick automated validation

echo "Testing HTTP 401 Fix..."

# 1. Check files exist
echo "✓ Checking files..."
test -f .github/workflows/issue-query-trigger.yml || echo "❌ Missing issue-query-trigger.yml"
test -f .github/workflows/query-processor.yml || echo "❌ Missing query-processor.yml"
test -f docs/index.html || echo "❌ Missing index.html"

# 2. Validate YAML
echo "✓ Validating YAML..."
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/issue-query-trigger.yml'))" || echo "❌ Invalid YAML: issue-query-trigger.yml"
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/query-processor.yml'))" || echo "❌ Invalid YAML: query-processor.yml"

# 3. Validate HTML
echo "✓ Validating HTML..."
python3 -c "from html.parser import HTMLParser; p=HTMLParser(); p.feed(open('docs/index.html').read())" || echo "❌ Invalid HTML"

# 4. Check for issue URL config
echo "✓ Checking configuration..."
grep -q "NEW_ISSUE_URL" docs/index.html || echo "❌ Missing NEW_ISSUE_URL"
grep -q "query-request" .github/workflows/issue-query-trigger.yml || echo "❌ Missing query-request label check"

echo "✅ Automated tests complete!"
```

---

## Success Checklist

After testing, verify these outcomes:

### Functionality
- [ ] Query submission works without errors
- [ ] Issues created correctly
- [ ] Workflows trigger automatically
- [ ] Results posted to issues
- [ ] Issues closed automatically
- [ ] Email notifications sent

### Performance
- [ ] Submission < 5 seconds
- [ ] Processing < 3 minutes
- [ ] Notifications timely

### User Experience
- [ ] Process intuitive
- [ ] Error messages clear
- [ ] Mobile friendly
- [ ] Documentation helpful

### Security
- [ ] No secrets exposed
- [ ] Authentication secure
- [ ] No vulnerabilities

### Compatibility
- [ ] Works on all browsers
- [ ] Mobile responsive
- [ ] No regressions

---

## If Tests Fail

### HTTP 401 Error Still Appears
**Possible Causes:**
- Old cached version of HTML
- Browser cache issue

**Solutions:**
- Hard refresh (Ctrl+Shift+R)
- Clear browser cache
- Try different browser
- Check deployed version matches repo

### Issue Not Creating
**Possible Causes:**
- GitHub down
- Not logged in
- Repository permissions

**Solutions:**
- Check GitHub status
- Log into GitHub
- Verify repo settings

### Workflow Not Triggering
**Possible Causes:**
- Label missing
- Workflow disabled
- Permissions issue

**Solutions:**
- Check issue has `query-request` label
- Verify workflows enabled in Settings
- Check workflow file syntax

### No Results Posted
**Possible Causes:**
- API key missing
- API rate limit
- Network issue

**Solutions:**
- Verify OPENAI_API_KEY secret set
- Check workflow run logs
- Wait and retry

---

## Contact for Issues

If you encounter problems during testing:

1. **Check Logs:** Actions tab → Failed workflow → View logs
2. **Check Issues:** Look for error messages in issue comments
3. **Report Bug:** Create new issue (without `query-request` label)
4. **Include:**
   - What you were testing
   - What happened
   - What you expected
   - Screenshots if possible

---

## Post-Testing

After successful testing:

1. ✅ Update PR with test results
2. ✅ Close any test issues created
3. ✅ Document any issues found
4. ✅ Celebrate success! 🎉

---

**Ready to test?** [Start here →](https://johntrue15.github.io/Metadata-to-Morphsource-compare/)
