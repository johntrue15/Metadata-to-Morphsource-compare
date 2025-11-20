# Conversation Continuation Feature

## Overview

The conversation continuation feature allows users to ask follow-up questions in the same GitHub issue without creating new issues. This creates a natural conversational flow when querying the MorphoSource database.

## How It Works

### Workflow Trigger

The `issue-comment-reply.yml` workflow is triggered when:
- A comment is created on any GitHub issue (`issue_comment.created` event)

### Detection Logic

The workflow determines whether to process a comment by checking:

1. **Is it from a user?** Bot comments are skipped to avoid infinite loops
2. **Is it on a query issue?** Must have `query-request` or `batch-query` label
3. **Is it a user question?** System comments (grading, processing status) are skipped

### Context Building

When a valid follow-up question is detected, the workflow:

1. **Extracts the original query** from the issue body
2. **Finds the most recent bot response** containing ChatGPT's answer
3. **Builds conversation context** combining:
   - Original question
   - Previous response
   - New follow-up question

### Query Processing

The conversation context is sent to the query processor as a single, context-aware query:

```
CONVERSATION CONTEXT:
Original Question: How many snake specimens are available?

Previous Response: There are 4 snake (Serpentes) specimens available on MorphoSource. Would you like me to list them?

Follow-up Question: Yes, please list them.

===

Please answer the follow-up question above in the context of the previous conversation. Be concise and direct.
```

### Response

The query processor:
1. Formats the contextual query for the MorphoSource API
2. Retrieves relevant data
3. Generates a response using ChatGPT that takes into account the conversation history
4. Posts the response as a new comment on the issue

## Usage Examples

### Example 1: Simple Follow-up

**Initial Query:**
```
How many lizard specimens are on MorphoSource?
```

**Bot Response:**
```
There are 127 lizard specimens available.
```

**User Comment:**
```
Can you show me ones from the genus Anolis?
```

**Bot Response:**
```
From the 127 lizard specimens, here are the ones from genus Anolis: ...
```

### Example 2: Multiple Follow-ups

Users can continue asking questions as many times as needed:

1. "Tell me about crocodile CT scans"
2. "How many have voxel spacing data?"
3. "Show me the ones from Australia"
4. "What's the average resolution?"

Each response maintains context from the entire conversation.

## Technical Details

### Workflow File

Location: `.github/workflows/issue-comment-reply.yml`

### Key Components

1. **Check Step**: Validates the comment should be processed
2. **Extract Context Step**: Builds conversation history
3. **Acknowledgment Step**: Posts immediate feedback to user
4. **Trigger Step**: Dispatches the query processor workflow

### Environment Variables

The workflow uses:
- `FOLLOW_UP_QUESTION`: The user's new question
- `CONVERSATION_CONTEXT`: Complete conversation history

### Permissions

Required GitHub permissions:
- `issues: write` - Post comments and update labels
- `actions: write` - Trigger the query processor workflow
- `contents: read` - Access repository files

## Testing

Comprehensive tests are in `tests/test_issue_comment_reply.py`:

- Workflow structure validation (YAML syntax, triggers, permissions)
- Step presence and configuration
- Bot detection logic
- Query issue identification
- Conversation context building
- Integration with query processor

Run tests with:
```bash
pytest tests/test_issue_comment_reply.py -v
```

## Benefits

1. **Natural Conversation Flow**: Users don't need to create multiple issues
2. **Context Awareness**: Bot remembers previous Q&A pairs
3. **Better User Experience**: Immediate acknowledgment of follow-up questions
4. **Clean Issue Tracking**: All related queries stay in one issue

## Limitations

- Context is built from comments in the same issue only
- Only the most recent bot response is included in context (to keep context size manageable)
- Users must have a GitHub account to comment on issues

## Future Enhancements

Potential improvements:
- Support for multi-turn context (beyond just the last response)
- Ability to reference specific previous responses
- Thread-based context tracking
- Conversation summarization for long threads
