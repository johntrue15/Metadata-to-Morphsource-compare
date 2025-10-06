# Architecture Overview

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    USER'S WEB BROWSER                       │
│                                                             │
│  ┌───────────────────────────────────────────────────┐    │
│  │         MorphoSource AI Assistant UI              │    │
│  │  (index.html - ChatGPT-style interface)           │    │
│  └───────────────────────────────────────────────────┘    │
│                          │                                  │
│                          ▼                                  │
│  ┌───────────────────────────────────────────────────┐    │
│  │       JavaScript Application Logic                │    │
│  │  - Message handling                               │    │
│  │  - Conversation history                           │    │
│  │  - Tool execution                                 │    │
│  └───────────────────────────────────────────────────┘    │
│         │                                    │              │
│         │                                    │              │
│         ▼                                    ▼              │
│  ┌──────────────┐                  ┌─────────────────┐    │
│  │ localStorage │                  │  Network Layer  │    │
│  │  (API Key)   │                  │  (fetch calls)  │    │
│  └──────────────┘                  └─────────────────┘    │
│                                            │                │
└────────────────────────────────────────────┼────────────────┘
                                             │
                    ┌────────────────────────┴─────────────────┐
                    │                                          │
                    ▼                                          ▼
         ┌──────────────────────┐                  ┌──────────────────────┐
         │   OpenAI API         │                  │  MorphoSource API    │
         │   (GPT-4)            │                  │  (Public API)        │
         │                      │                  │                      │
         │ - Chat completions   │                  │ - Specimen search    │
         │ - Function calling   │                  │ - Media details      │
         │ - Response generation│                  │ - Taxonomy data      │
         └──────────────────────┘                  └──────────────────────┘
```

## Request Flow

### 1. User Query Flow

```
User types query
    ↓
Message added to UI
    ↓
Conversation history updated
    ↓
"Thinking..." indicator shown
    ↓
Call OpenAI API with conversation history + tools
    ↓
OpenAI decides to use search_morphosource tool
    ↓
Tool call displayed in UI
    ↓
JavaScript executes MorphoSource API search
    ↓
Results returned
    ↓
Results added to conversation as tool response
    ↓
"Thinking..." indicator shown again
    ↓
Call OpenAI API with updated conversation (including tool results)
    ↓
OpenAI analyzes results and generates response
    ↓
Response displayed to user
```

### 2. Data Flow Diagram

```
┌──────────┐     1. Query      ┌─────────┐
│   User   │ ────────────────> │   UI    │
└──────────┘                   └────┬────┘
                                    │ 2. Process
                                    ▼
                            ┌──────────────┐
                            │  JavaScript  │
                            │   Handler    │
                            └───────┬──────┘
                                    │ 3. API Call
                        ┌───────────┴───────────┐
                        │                       │
                        ▼                       ▼
                ┌──────────────┐        ┌─────────────┐
                │  OpenAI API  │        │ MorphoSource│
                │   (GPT-4)    │        │     API     │
                └──────┬───────┘        └──────┬──────┘
                       │ 4. Tool Call          │
                       └───────────────────────┘
                                    │
                                    │ 5. Results
                                    ▼
                            ┌──────────────┐
                            │  JavaScript  │
                            │   Handler    │
                            └───────┬──────┘
                                    │ 6. Analysis Call
                                    ▼
                            ┌──────────────┐
                            │  OpenAI API  │
                            │   (GPT-4)    │
                            └───────┬──────┘
                                    │ 7. Response
                                    ▼
┌──────────┐   8. Display    ┌─────────┐
│   User   │ <────────────── │   UI    │
└──────────┘                 └─────────┘
```

## Component Responsibilities

### Frontend (index.html)

**UI Layer:**
- Message display (user & assistant)
- Input handling (text area, buttons)
- Visual feedback (thinking indicators)
- Example prompts
- Settings (API key management)

**Application Logic:**
- Conversation history management
- OpenAI API integration
- MorphoSource API integration
- Tool execution
- Error handling

**Storage:**
- localStorage for API key persistence
- In-memory conversation history

### OpenAI Integration

**Purpose:** Natural language understanding and response generation

**Features Used:**
- Chat Completions API
- Function/Tool calling
- GPT-4 model
- Conversation context maintenance

**Tools Defined:**
1. `search_morphosource` - Search for specimens
2. `get_morphosource_media` - Get media details

### MorphoSource Integration

**Purpose:** Retrieve specimen and media data

**Endpoints Used:**
- `/api/specimens` - Specimen search
- `/api/media` - Media details
- `/api/media/{id}` - Specific media item

**Authentication:**
- Public API (no key required for basic access)
- Optional API key for extended access

## Security Model

### Client-Side Security

```
┌────────────────────────────────────┐
│  User's Browser (Isolated)         │
│                                    │
│  ┌──────────────────────────┐    │
│  │   localStorage           │    │
│  │   - openai_api_key       │    │
│  │   (Browser-specific)     │    │
│  └──────────────────────────┘    │
│                                    │
│  Never sent to GitHub Pages       │
│  Only sent to OpenAI API           │
└────────────────────────────────────┘
```

### API Key Flow

1. User enters API key
2. Stored in browser localStorage
3. Retrieved for each OpenAI API call
4. Never transmitted to any server except OpenAI
5. Can be cleared via settings

## Deployment Architecture

```
┌─────────────────────────────────────────┐
│         GitHub Repository               │
│                                         │
│  ┌────────────────────────────────┐   │
│  │  docs/                         │   │
│  │  ├── index.html                │   │
│  │  ├── README.md                 │   │
│  │  └── ...                       │   │
│  └────────────────────────────────┘   │
│                                         │
│  ┌────────────────────────────────┐   │
│  │  .github/workflows/             │   │
│  │  └── deploy-pages.yml          │   │
│  └────────────────────────────────┘   │
└──────────────┬──────────────────────────┘
               │
               │ GitHub Actions
               │ (Automated Deployment)
               ▼
┌─────────────────────────────────────────┐
│      GitHub Pages CDN                   │
│                                         │
│  https://johntrue15.github.io/         │
│    Metadata-to-Morphsource-compare/    │
│                                         │
│  - Serves static files                  │
│  - Global CDN                          │
│  - HTTPS by default                    │
└─────────────────────────────────────────┘
```

## Technology Stack

### Frontend
- **HTML5** - Semantic markup
- **CSS3** - Modern styling (Flexbox, Grid)
- **Vanilla JavaScript** - No frameworks
- **ES6+** - Modern JavaScript features
  - async/await
  - fetch API
  - arrow functions
  - template literals
  - destructuring

### APIs
- **OpenAI API** - GPT-4 with function calling
- **MorphoSource API** - Specimen database

### Hosting
- **GitHub Pages** - Static site hosting
- **GitHub Actions** - CI/CD pipeline

## Performance Considerations

### Frontend
- Single HTML file (~19KB)
- No external dependencies
- Minimal JavaScript
- CSS in-page (no extra requests)

### API Calls
- OpenAI: ~2-5 seconds per query
- MorphoSource: ~1-2 seconds per search
- Total: ~3-7 seconds per conversation turn

### Optimization
- Messages load instantly (local)
- API calls are async (non-blocking)
- Thinking indicators prevent user confusion
- Conversation history maintained in memory

## Scalability

### Current Design
- **No server** - Fully client-side
- **No database** - State in browser only
- **No backend** - Direct API calls
- **Cost model** - User pays for their own API usage

### Future Enhancements (Optional)
- Backend API for centralized key management
- Caching layer for common queries
- Rate limiting per user
- Usage analytics
- Advanced error handling
- Conversation persistence
