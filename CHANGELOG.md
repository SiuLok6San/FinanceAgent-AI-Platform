# Changelog

## Oct 2026 — public demo

- Public site on Streamlit Community Cloud: <https://financeagent-ai-platform.streamlit.app>. It opens on the 20
  saved evaluation runs (re-checked by the current validator, no API calls). Live analysis needs an LLM endpoint
  in the deployment's secrets and, when `APP_PASSWORD` is set, the password; the public site has none configured.

## Oct 2026 — validator feedback loop (`validation-feedback-loop` branch)

Added
- The validator's findings go back to the Writer for one revision (replace, remove or label as
  "(assumption)" figures not in the data; cite the web source a figure came from; add missing citations;
  disclose topics with no data). The revision is kept only if the validator counts strictly fewer open
  issues; the first draft, its validation and the feedback are stored with the report and shown in the UI.
- Numbers the report labels "(assumption)" are counted separately (still unverified).
- GitHub Actions runs the test suite on every push.
- Evaluation re-run with the loop: open issues 169 → 69 and uncited paragraphs 60 → 26 between first drafts
  and kept reports; numbers traceable to fetched data 61% → 64% (see `eval/RESULTS.md`).

Fixed
- An LLM call now has a hard total time limit (the request runs in a watchdog thread). Long eval runs looked
  stuck for hours; the actual cause was the laptop sleeping mid-run, and the limit is kept as a safeguard.
- The validator reads citations written as `(S4, S8)` and ISO dates written with non-breaking hyphens.

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
