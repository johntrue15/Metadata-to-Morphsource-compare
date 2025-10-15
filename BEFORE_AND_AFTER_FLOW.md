# Query Processing Flow: Before vs After

## BEFORE: 2-Job Pipeline

```
┌─────────────────────────────────────────────────┐
│           User submits query via issue          │
│     "Tell me about lizard specimens"            │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
     ┌───────────────────────────────────────┐
     │   Job 1: MorphoSource API Query       │
     ├───────────────────────────────────────┤
     │   Query: "Tell me about lizard        │
     │           specimens" (RAW)            │
     │   ├─ Search with raw query            │
     │   ├─ Include conversational words     │
     │   └─ Potentially less accurate        │
     └───────────────┬───────────────────────┘
                     │
                     ▼
     ┌───────────────────────────────────────┐
     │   Job 2: ChatGPT Processing           │
     ├───────────────────────────────────────┤
     │   ├─ Analyze MorphoSource results     │
     │   ├─ Generate response                │
     │   └─ Post to issue                    │
     └───────────────┬───────────────────────┘
                     │
                     ▼
            ┌────────────────┐
            │    Results     │
            └────────────────┘
```

### Issues with Old Approach:
- ❌ Raw queries sent to API (inefficient)
- ❌ Conversational words included in searches
- ❌ No query optimization
- ❌ Less accurate search results

---

## AFTER: 3-Job Pipeline with Query Formatting

```
┌─────────────────────────────────────────────────┐
│           User submits query via issue          │
│     "Tell me about lizard specimens"            │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
     ┌───────────────────────────────────────┐
     │   Job 1: ChatGPT Query Formatter      │
     │              ⭐ NEW!                   │
     ├───────────────────────────────────────┤
     │   Input: "Tell me about lizard        │
     │           specimens"                  │
     │                                       │
     │   ChatGPT Analysis:                   │
     │   ├─ Extract: "lizard"                │
     │   ├─ Remove: "tell me about"          │
     │   └─ Format: {"q": "lizard",          │
     │               "per_page": 10}         │
     │                                       │
     │   Output: formatted_query = "lizard"  │
     │           api_params = {...}          │
     └───────────────┬───────────────────────┘
                     │
                     ▼
     ┌───────────────────────────────────────┐
     │   Job 2: MorphoSource API Query       │
     ├───────────────────────────────────────┤
     │   Query: "lizard" (FORMATTED)         │
     │   ├─ Uses optimized query             │
     │   ├─ Clean search terms               │
     │   └─ More accurate results            │
     └───────────────┬───────────────────────┘
                     │
                     ▼
     ┌───────────────────────────────────────┐
     │   Job 3: ChatGPT Processing           │
     ├───────────────────────────────────────┤
     │   Context includes:                   │
     │   ├─ Original query                   │
     │   ├─ Formatted query                  │
     │   ├─ MorphoSource results             │
     │   └─ Generate comprehensive response  │
     └───────────────┬───────────────────────┘
                     │
                     ▼
            ┌────────────────┐
            │ Better Results │
            └────────────────┘
```

### Benefits of New Approach:
- ✅ Intelligent query formatting
- ✅ Removes conversational noise
- ✅ Optimized for MorphoSource API
- ✅ More accurate search results
- ✅ Better user experience
- ✅ Full transparency (users see formatting)

---

## Query Transformation Examples

### Example 1: Simple Query
```
User Input:    "Tell me about lizard specimens"
                      ↓
Formatted:     "lizard"
API Params:    {"q": "lizard", "per_page": 10}
```

### Example 2: Specific Species
```
User Input:    "What Anolis specimens are in the database?"
                      ↓
Formatted:     "Anolis"
API Params:    {"q": "Anolis", "per_page": 10}
```

### Example 3: Technical Query
```
User Input:    "Show me CT scans of crocodiles"
                      ↓
Formatted:     "crocodile CT"
API Params:    {"q": "crocodile CT", "per_page": 10}
```

### Example 4: Complex Query
```
User Input:    "Find specimens with micro-CT data"
                      ↓
Formatted:     "micro-CT"
API Params:    {"q": "micro-CT", "per_page": 10}
```

---

## Technical Comparison

| Aspect | Before | After |
|--------|--------|-------|
| Jobs | 2 | 3 |
| Query Processing | None | ChatGPT-based |
| API Query Quality | Raw/Unoptimized | Formatted/Optimized |
| Search Accuracy | Lower | Higher |
| User Transparency | Limited | Full |
| Processing Time | ~1-2 min | ~1-2 min |
| Artifacts | 2 | 3 |
| API Calls | 1 ChatGPT | 2 ChatGPT |

---

## Workflow Job Dependencies

### Before:
```
morphosource-api → chatgpt-processing
     (no dependencies)        ↑
```

### After:
```
query-formatter → morphosource-api → chatgpt-processing
(no dependencies)         ↑                    ↑
                          |____________________|
```

---

## Data Flow

### Before:
```
User Query
    ↓
MorphoSource API (raw query)
    ↓
ChatGPT (analyze results)
    ↓
Response to User
```

### After:
```
User Query
    ↓
ChatGPT (format query)
    ↓
MorphoSource API (formatted query)
    ↓
ChatGPT (analyze results with context)
    ↓
Enhanced Response to User
```

---

## Impact on User Experience

### Old Flow:
1. User submits: "Tell me about lizard specimens"
2. System searches MorphoSource for exact phrase
3. Results may miss relevant specimens
4. User gets incomplete information

### New Flow:
1. User submits: "Tell me about lizard specimens"
2. ChatGPT extracts: "lizard"
3. System searches MorphoSource for "lizard"
4. More relevant results found
5. User sees:
   - Original query
   - How it was formatted
   - Better search results
   - Comprehensive response

---

## Conclusion

The new 3-job pipeline with ChatGPT query formatting provides:
- **Better accuracy** through intelligent query optimization
- **Enhanced transparency** by showing query transformations
- **Improved results** with cleaner, more focused searches
- **Same simplicity** for users (no changes to submission process)
- **No breaking changes** to existing functionality
