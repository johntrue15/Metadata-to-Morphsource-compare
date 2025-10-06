# Testing the MorphoSource AI Assistant

## Pre-deployment Testing

Before deploying to GitHub Pages, you can test the application locally:

1. **Start a local web server:**
   ```bash
   cd docs
   python -m http.server 8080
   ```

2. **Open in browser:**
   ```
   http://localhost:8080
   ```

3. **Verify the interface loads correctly**

## Post-deployment Testing

Once deployed to GitHub Pages, test the following:

### 1. Initial Load Test
- [ ] Page loads without errors
- [ ] API key prompt appears on first visit
- [ ] Example prompts are visible
- [ ] Interface is responsive

### 2. API Key Management
- [ ] Can enter API key
- [ ] Key is saved to localStorage
- [ ] Can clear key via settings button
- [ ] Page reloads after clearing key

### 3. Basic Chat Functionality
- [ ] Can type in text area
- [ ] Text area auto-resizes
- [ ] Send button enables/disables correctly
- [ ] Can send message with button
- [ ] Can send message with Enter key
- [ ] Shift+Enter creates new line

### 4. Message Display
- [ ] User messages appear with correct styling
- [ ] AI messages appear with correct styling
- [ ] Avatars display correctly
- [ ] Thinking indicator shows during processing
- [ ] Error messages display properly

### 5. OpenAI Integration
Test with your OpenAI API key:

**Query:** "Tell me about lizards on MorphoSource"

Expected behavior:
- [ ] Thinking indicator appears
- [ ] Tool call is displayed showing MorphoSource API call
- [ ] Second thinking indicator appears
- [ ] AI response with results appears

### 6. MorphoSource API Integration
Verify these queries work:

**Query:** "Tell me about lizards"
- [ ] Searches MorphoSource
- [ ] Returns results or appropriate error message

**Query:** "How many snake specimens are there?"
- [ ] Attempts to search for snakes
- [ ] Provides information or graceful error

### 7. Example Prompts
- [ ] Clicking example fills text area
- [ ] Clicking example sends message
- [ ] Examples disappear after first message

### 8. Conversation Context
Send multiple messages:
1. "Tell me about lizards"
2. "What about snakes?"

Expected:
- [ ] Context is maintained
- [ ] Follow-up questions work correctly

## Common Issues

### API Key Not Working
- Verify key is valid at https://platform.openai.com/api-keys
- Check browser console for errors
- Ensure API key has sufficient credits

### MorphoSource API Errors
- MorphoSource API might have rate limits
- Some endpoints may return errors
- This is expected - the AI should handle gracefully

### CORS Errors
- Should not occur with GitHub Pages
- If testing locally, may need CORS workaround

## Browser Compatibility

Test in:
- [ ] Chrome/Edge (Chromium)
- [ ] Firefox
- [ ] Safari
- [ ] Mobile browsers

## Performance Testing

- [ ] Page loads quickly
- [ ] API calls complete in reasonable time (< 10 seconds)
- [ ] No memory leaks during extended use
- [ ] Multiple conversations work correctly

## Security Testing

- [ ] API key not visible in network requests (except to OpenAI)
- [ ] API key stored only in localStorage
- [ ] No sensitive data logged to console (in production)

## Reporting Issues

If you find bugs or issues:
1. Note the exact steps to reproduce
2. Include browser/OS information
3. Copy any error messages from console
4. Check if issue persists after clearing localStorage
