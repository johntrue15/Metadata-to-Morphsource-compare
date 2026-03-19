# AutoResearchClaw

A fork of [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) adapted for autonomous MorphoSource research via GitHub Actions.

## What It Does

This repository uses a GitHub Actions workflow to run an autonomous research agent that:

1. **Decomposes** a research topic into targeted MorphoSource queries using ChatGPT
2. **Searches** the [MorphoSource](https://www.morphosource.org/) database for relevant specimen data
3. **Evaluates** findings and decides what to explore next
4. **Repeats** for multiple cycles, building memory across iterations
5. **Reports** findings as GitHub Issues with full research reports

## Usage

1. Go to the **Actions** tab
2. Select the **AutoResearchClaw Agent** workflow
3. Click **Run workflow**
4. Enter a research topic (e.g., "CT scans of snake skulls on MorphoSource")
5. Optionally adjust research depth and number of output issues
6. Results are posted as GitHub Issues with detailed reports

### Required Secrets

Configure these in **Settings → Secrets and variables → Actions**:

- `OPENAI_API_KEY` — OpenAI API key for ChatGPT-powered query decomposition and synthesis
- `MORPHOSOURCE_API_KEY` — MorphoSource API key (optional)

## References

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — The original autoresearch project by Andrej Karpathy, demonstrating AI agents running autonomous research experiments
- [MorphoSource](https://www.morphosource.org/) — 3D data repository for biological specimens

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.