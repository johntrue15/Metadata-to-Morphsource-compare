# Interface Guide

## Visual Layout

The MorphoSource AI Assistant features a ChatGPT-inspired interface with the following elements:

### Header
- **Title**: "MorphoSource AI Assistant" (centered)
- **Settings Button**: ⚙️ icon in top-right corner for clearing API key
- Dark background (#202123)

### Chat Container
- **Initial State**: Shows 3 example prompt cards:
  - 🦎 Search Lizards - "Tell me about lizards on MorphoSource"
  - 🐍 Count Snakes - "How many snake specimens are available?"
  - 🐊 CT Scans - "Show me CT scans of crocodiles"

- **During Conversation**: Displays messages with avatars
  - User messages: Purple avatar (U), lighter background
  - AI messages: Green avatar (AI), darker background
  - Tool calls shown as code blocks with green border

### Input Area
- Text area with placeholder: "Ask about MorphoSource specimens..."
- "Send" button (green #10a37f when enabled, gray when disabled)
- Auto-resizes as you type
- Press Enter to send (Shift+Enter for new line)

## Color Scheme

The interface uses a dark theme matching ChatGPT:
- **Background**: #343541 (dark gray)
- **Text**: #ececf1 (light gray)
- **Header/Cards**: #202123 (darker gray)
- **Messages**: #444654 (medium gray)
- **Accent**: #10a37f (green for AI/buttons)
- **User**: #5436da (purple)

## Interaction Flow

1. **First Visit**: API key prompt appears
2. **User Types**: Question in text area
3. **Click Send**: Message appears in chat
4. **Thinking**: "Thinking..." indicator with animated dots
5. **Tool Call**: If MorphoSource API called, shows function name and parameters
6. **Response**: AI's analyzed response appears
7. **Continue**: Can ask follow-up questions

## Example Conversation

```
User: Tell me about lizards on MorphoSource

AI: Let me search MorphoSource for that information...
    [Tool Call: search_morphosource]
    {"query": "lizards"}

AI: I found information from MorphoSource. There are 10 specimens 
    in the results. Here's what I discovered:
    
    1. Anolis carolinensis
       Carolina anole specimen with CT scan data
    
    2. Sceloporus occidentalis
       Western fence lizard, high-resolution imaging
    
    ...and 8 more specimens.
```

## Mobile Responsive

The interface adapts to different screen sizes:
- Max width of 900px for chat content
- Flexible layout for mobile devices
- Touch-friendly buttons and inputs

## Accessibility

- Semantic HTML structure
- Keyboard navigation support
- Clear visual feedback for interactions
- Readable color contrast ratios
