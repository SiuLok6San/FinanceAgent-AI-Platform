# Changelog

## Oct 2026 — revision (`revision-2026` branch)

Added
- Deterministic validation layer (`validators.py`, `facts.py`) run on every report: numeric grounding
  (verified / derived / unverified), citation integrity, data freshness and no-data honesty. A summary line
  goes on top of each report and a Validation panel in the UI highlights flagged figures without removing them.
- Evaluation set of 20 investor questions with expected routing tags (`eval/questions.csv`), scripts to run
  the original and the revised pipeline on it, and the results in `eval/RESULTS.md`.
- `examples/` with real reports from the revised pipeline and UI screenshots.
- Tests (`pytest`): number extraction and matching, citations, freshness, no-data checks, routing,
  data layer with HTTP mocked, pipeline smoke test with the LLM mocked, headless UI test.
- Support for the hosted Ollama API (`OLLAMA_API_KEY`) besides a local Ollama server; fixed seed and
  run settings stored with every report.

Changed
- The Analyst, Reasoner and Writer now receive the fetched data values with their source ids (before, no
  agent saw the numbers that the Researcher fetched).
- Routing rules, FRED series, thresholds and temperatures moved from code to `config/*.toml`.
- Rule-based router matches whole words (the substring matcher tagged "corporate" as rates and "signal" as credit).
- Data layer: one HTTP client per API, broader FRED coverage (Treasury yields, curve, mortgage rate,
  Sahm rule, S&P 500, VIX, WTI; credit spreads now found for "high-yield" / "investment-grade" / "IG vs HY").

Fixed
- Pipeline failures showed the Python traceback as the answer; now a short message with a reference id,
  and the traceback goes to `logs/pipeline.log`.
- `.env` was never loaded although the README said it was.
- Charts were always empty; the Finnhub financials table was always empty; `$` rendered as LaTeX in the UI.

Removed
- Unused dependencies (`openai`, `altair`, `matplotlib`, `finnhub-python`, `tavily-python`, `numpy` as a
  direct dependency); remaining versions pinned.
- `data_engine.py`, which nothing imported.

## Dec 2025 — original course version

Multi-agent assistant (Planner → Researcher → Analyst → Reasoner → Writer) built for the Columbia
industry-sponsored course project with Boosted.ai, using FRED, Finnhub and Tavily data and a local
Llama 3 model through Ollama. Uploaded to GitHub in Feb 2026 (commit `0468eea`).
