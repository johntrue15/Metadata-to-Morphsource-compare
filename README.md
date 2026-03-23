# AutoResearchClaw

An autonomous MorphoSource research agent inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch). Runs on a self-hosted Mac mini, searches MorphoSource for 3D specimen data, downloads and analyzes specimens with 3D Slicer + SlicerMorph, and produces structured research reports as GitHub Issues.

## Architecture

```
Research Topic + Seed Media ID/List
        |
        v
  ┌─────────────────────────────────┐
  │  AutoResearchClaw Agent         │
  │  (research_agent.py)            │
  │                                 │
  │  Inner loop (research_depth):   │
  │    decompose → search →         │
  │    evaluate → build memory      │
  │                                 │
  │  Outer loop (github_issues):    │
  │    aggregate → create issue     │
  │                                 │
  │  Tool calls:                    │
  │    - MorphoSource API search    │
  │    - Specimen download          │
  │    - 3D Slicer analysis         │
  │    - Literature search          │
  │    - Knowledge graph            │
  │    - UBERON ontology lookup     │
  └─────────────────────────────────┘
        |
        v
  GitHub Issues + Dashboard + Knowledge Graph
```

## Quick Start

1. Go to **Actions** > **AutoResearchClaw Agent** > **Run workflow**
2. Enter your research topic
3. Set **research depth** (internal cycles, default 10) and **GitHub issues** (reports to create, default 3)
4. Optionally provide a **seed media ID** or **media list ID** from MorphoSource
5. Results post as GitHub Issues; detailed logs available as artifacts

## Workflow Dispatch Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `research_topic` | (required) | Research goal or question |
| `research_depth` | 10 | Number of internal research cycles |
| `github_issues` | 3 | Number of GitHub issues to create with findings |
| `media_id` | | MorphoSource media ID to seed research (e.g. `000769445`) |
| `media_list_id` | | MorphoSource media list ID for batch seeding (e.g. `000656244`) |
| `openai_model` | gpt-5.4 | OpenAI model to use |

## Required Secrets

Configure in **Settings > Secrets and variables > Actions**:

- `OPENAI_API_KEY` -- OpenAI API key for query decomposition, evaluation, and synthesis
- `MORPHOSOURCE_API_KEY` -- MorphoSource API key for specimen downloads

## Features

### Two-Loop Research Engine
- **Inner loop** runs `research_depth` fast cycles: decompose topic, search MorphoSource API, evaluate results, build memory
- **Outer loop** creates `github_issues` reports at regular intervals aggregating findings
- Memory accumulates across cycles: queries tried, discoveries, dead ends, next directions

### 3D Slicer + SlicerMorph Integration
- Downloads open-access mesh specimens from MorphoSource
- Runs headless 3D Slicer morphometric analysis (landmarks, curvature, volume, connectivity)
- Produces publication-quality screenshots from multiple viewpoints
- Specimen cache at `~/.autoresearchclaw/specimens/` avoids re-downloading

### 3-Layer Analysis Pipeline
- **Layer 1**: Automated morphometrics (dimensions, surface area, volume, curvature, PCA, landmarks)
- **Layer 2**: Literature-guided analysis (PubMed + Google Scholar search, LLM-designed measurement protocols)
- **Layer 3**: Multi-specimen comparison (Procrustes alignment, PCA, distance matrices)

### Knowledge Graph
- Builds connections between media, specimens, papers, institutions, and taxa
- Exports as JSON (Neo4j/Cytoscape compatible) and Mermaid diagrams
- Tracks parent-child media relationships, DOI citations, taxonomic hierarchy

### UBERON Ontology Search (DRAGON-AI)
- Expands anatomy terms using the UBERON cross-species anatomy ontology
- Maps terms like "skull" to synonyms: cranium, neurocranium, calvaria, braincase
- Improves MorphoSource search coverage

### Local Dashboard
- Flask app at `http://localhost:5001` on the Mac mini
- Live-updating event log with cycle-by-cycle progress
- Score trends, query history, knowledge graph stats

## Self-Hosted Runner

The research pipeline runs on a self-hosted Mac mini runner with:
- 3D Slicer 5.10.0 + SlicerMorph
- Anaconda Python 3.12
- Persistent specimen cache

Runner setup:
```bash
cd ~/actions-runner-morphosource
./config.sh --url https://github.com/USER/REPO --token TOKEN
./run.sh
```

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Edit .env with your API keys

# Run locally
cd .github/scripts
python research_agent.py "Your research topic" \
  --research-depth 10 \
  --github-issues 1 \
  --media-list 000656244

# Start dashboard
python dashboard.py
# Open http://localhost:5001
```

## Project Structure

```
.github/
  scripts/
    _helpers.py              # Shared utilities (dotenv, LLM, constants)
    research_agent.py        # Main autonomous research agent
    slicer_tool.py           # 3D Slicer download + analysis tool
    morphosource_api_download.py  # MorphoSource file downloader
    knowledge_graph.py       # Media-specimen-paper graph builder
    ontology_search.py       # UBERON ontology term expansion
    citation_extractor.py    # DOI/paper extraction from records
    literature_search.py     # PubMed + Google Scholar search
    slicer_layer2.py         # Literature-guided deep analysis
    slicer_layer3.py         # Multi-specimen comparison
    dashboard.py             # Local Flask monitoring dashboard
    program.md               # Research strategy (Karpathy-style)
    query_formatter.py       # Natural language to API URL converter
    morphosource_api.py      # MorphoSource API search handler
  workflows/
    autoresearchclaw.yml     # Main research workflow
    tests.yml                # CI tests
    code-quality.yml         # Linting and formatting
    parse_morphosource.yml   # CSV comparison workflow
```

## References

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) -- Autonomous AI research experiments
- [MorphoSource](https://www.morphosource.org/) -- 3D data repository for biological specimens
- [3D Slicer](https://www.slicer.org/) -- Open-source medical image computing
- [SlicerMorph](https://slicermorph.github.io/) -- 3D morphometrics for Slicer

## License

MIT License -- see [LICENSE](LICENSE).
