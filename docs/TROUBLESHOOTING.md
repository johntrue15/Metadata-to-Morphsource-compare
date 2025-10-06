# Troubleshooting Guide

## Common Issues and Solutions

### Issue: API Key Prompt Keeps Appearing

**Symptoms:**
- API key prompt appears every time you visit the page
- Key doesn't seem to save

**Solutions:**
1. Check if your browser allows localStorage
2. Disable private/incognito mode
3. Check browser settings for site permissions
4. Try a different browser

### Issue: "OpenAI API Error" Messages

**Symptoms:**
- Error: "OpenAI API key is not set"
- Error: "API request failed with status 401"
- Error: "API request failed with status 429"

**Solutions:**

**For 401 (Unauthorized):**
- Verify your API key is correct
- Check if API key is active at https://platform.openai.com/api-keys
- Try regenerating the API key

**For 429 (Rate Limit):**
- You've exceeded OpenAI's rate limits
- Wait a few minutes before trying again
- Check your API usage at OpenAI dashboard
- Consider upgrading your OpenAI plan

**For 403 (Forbidden):**
- Your API key might not have access to GPT-4
- Try using GPT-3.5 instead (requires code modification)
- Check your OpenAI account status

### Issue: "Insufficient Credits" Error

**Symptoms:**
- Error messages about billing or credits

**Solutions:**
1. Add payment method to your OpenAI account
2. Add credits/funding to your account
3. Check usage limits at OpenAI dashboard

### Issue: MorphoSource API Returns No Results

**Symptoms:**
- AI says "I couldn't find any results"
- "MorphoSource API error" messages

**Solutions:**
1. This may be expected - not all queries have results
2. Try more general search terms
3. Check if MorphoSource API is accessible: https://www.morphosource.org/api
4. Try different query phrasings

### Issue: "Thinking..." Never Completes

**Symptoms:**
- Message gets stuck on "Thinking..."
- No response appears

**Solutions:**
1. Check browser console for errors (F12)
2. Check your internet connection
3. OpenAI API might be slow - wait 30-60 seconds
4. Refresh the page and try again
5. Verify OpenAI API status: https://status.openai.com/

### Issue: Interface Doesn't Load

**Symptoms:**
- Blank page
- Only header shows
- Console errors

**Solutions:**
1. Clear browser cache
2. Hard refresh (Ctrl+Shift+R or Cmd+Shift+R)
3. Check browser console for JavaScript errors
4. Try a different browser
5. Ensure JavaScript is enabled

### Issue: Can't Type in Text Area

**Symptoms:**
- Text area is disabled
- Can't focus on input

**Solutions:**
1. Check if page finished loading
2. Refresh the page
3. Clear localStorage: `localStorage.clear()` in console
4. Restart browser

### Issue: Messages Display Incorrectly

**Symptoms:**
- Formatting is broken
- Avatars don't show
- Colors are wrong

**Solutions:**
1. Clear browser cache
2. Check browser compatibility (use modern browser)
3. Disable browser extensions that might interfere
4. Check if CSS loaded correctly (view page source)

## Developer Tools

To debug issues:

1. **Open Browser Console:**
   - Chrome/Edge: F12 or Ctrl+Shift+J
   - Firefox: F12 or Ctrl+Shift+K
   - Safari: Cmd+Option+C

2. **Check localStorage:**
   ```javascript
   console.log(localStorage.getItem('openai_api_key'));
   ```

3. **Clear localStorage:**
   ```javascript
   localStorage.clear();
   location.reload();
   ```

4. **Check conversation history:**
   ```javascript
   console.log(conversationHistory);
   ```

5. **Test MorphoSource API directly:**
   ```javascript
   fetch('https://www.morphosource.org/api/specimens?q=lizards')
     .then(r => r.json())
     .then(data => console.log(data));
   ```

## Getting Help

If you still have issues:

1. Check the browser console for errors
2. Note the exact error message
3. Document steps to reproduce
4. Check GitHub issues for similar problems
5. Create a new issue with:
   - Browser and version
   - Operating system
   - Steps to reproduce
   - Error messages
   - Screenshots (without API keys visible)

## Security Notes

- Never share your API key in screenshots or issues
- API keys in localStorage are visible in browser dev tools
- Consider using different API keys for testing vs production
- Regularly rotate your API keys for security

## Known Limitations

1. **Client-side only:** All processing happens in your browser
2. **API costs:** You pay for OpenAI API usage
3. **MorphoSource API:** May have rate limits or availability issues
4. **Browser compatibility:** Requires modern browser with ES6+ support
5. **Mobile:** May have layout quirks on very small screens
