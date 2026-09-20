# semiconductor-intel

Semiconductor industry intelligence system built on the [NOOA agent framework](/Users/liyifan/Documents/labs-OO-Agents): it ingests news, filings, PDFs and other sources about the semiconductor industry, runs NOOA-driven extraction and knowledge pipelines whose proposals are committed only after Python-side validation, and serves the results over a FastAPI API backed by PostgreSQL (full-text + vector search). The `intel` package lives in `src/intel/`; NOOA is integrated as an editable path dependency pinned to the local checkout at commit `d4d46f7`.

Full design specification (16 docs + contracts): `/Users/liyifan/Documents/Codex/2026-09-19-agent/semiconductor-intel-design/`

## Development

```bash
uv sync          # create .venv and install dependencies (includes NOOA path dep)
uv run pytest    # run unit tests (integration/live markers excluded by default)
```
