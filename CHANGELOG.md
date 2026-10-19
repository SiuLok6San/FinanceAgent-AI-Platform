# Changelog

## Oct 2026 — v3: grounded by construction (`v3-grounded` branch)

Changed
- The LLM agents no longer type numbers. Every fetched or derived value gets a fact ID (`HY_OAS.latest`,
  `HY_OAS.chg_1m`, `HY_OAS.pctile_5y`, `HY_OAS_minus_IG_OAS`, `JPM.pe_ttm`, ...); the Analyst, Reasoner and
  Writer write `{{ID}}`, `{{S#: quoted number}}` or `{{assume: ...}}`, and `render.py` inserts the exact value
  and its citation. The free-text Writer and the KEY DATA prompt block are gone.
- The Planner returns JSON (topics + FRED series from a catalog), checked in code, with the rule-based router as
  fallback.
- 5–10 years of history per series, so reports can say where today's value sits in its range.

Added
- Direction check: a change described with the wrong word ("narrowed" next to a widening) is flagged and sent
  back to the Writer. It also runs on free-text reports, so all versions can be compared.
- Quoted web numbers must appear in the text of the source they name.
- Comparison check: "A is above B" between two facts is checked against their values, and a comparison
  between different units (a P/E multiple against an index level) is flagged.
- Wording checks: history words ("near five-year lows", "tight relative to history") are checked against the
  fact's percentile in the fetched window, and trend words ("widening", "rising") against its recent change.
  Like the direction and comparison checks, their findings go back to the Writer in the feedback loop.
- Growth-rate wording: "inflation is cooling" or "growth is slowing" is checked against the rate, not the
  index level: the 12-month rate against a period earlier and the recent annualized pace (new facts
  `CPI.pct_1y_prev`, `CPI.ann_3m`, `GDP.ann_growth_1q_prev`). It is flagged only when both disagree with the
  word. "Inflation fell from {{CPI.pct_1y_prev}} to {{CPI.pct_1y}}" is judged by the two rates.
- A number labelled "(assumption)" that is written the same way in a source the line cites counts as an open
  issue (it is that source's figure). A revision could otherwise clear typed numbers by relabelling them; the
  feedback now says to quote such numbers as `{{S#: …}}` and names the fact a typed number matches.
- UI: hover any number to see the fact it came from; colours mark data, quotes, assumptions and typed numbers;
  lines with no checked figure are shown in italics as interpretation (the model's reading, not checked); the
  Planner's choices and the full fact table are shown.
- Evaluation of original, v2 and v3 on 20 development questions, 10 held-out questions (frozen before v3 was
  built) and 10 fresh questions (frozen later, never run before the final runs).
- Tags `v1.0-course` and `v2.0-validation` mark the earlier versions.

Fixed
- The feedback loop could introduce errors: a typed number matched to the data by coincidence was sent back
  as a direction error or with a suggested fact ID ("above the Fed's 2% target" became "above the Fed's
  3.75% target"). In placeholder reports only inserted facts are direction- and citation-checked, hedged
  clauses are skipped, and a typed number gets "use its ID if it is data, {{assume: …}} if it is a target or
  estimate".
- A number counts as being in a web source's text only if the source shows it at the precision the report
  writes it ("0.8 pp" no longer matches the "1" of a section heading). This applies to quotes and to scoring.
- Percentile, min, max and average facts are labelled by the history actually fetched: FRED serves only 3
  years of the ICE credit spreads, which were labelled "5y". History wording needs at least 3 years of data,
  and a phrase naming a horizon ("five-year lows") needs that horizon.
- The hosted Ollama free tier allows one request at a time: an HTTP 429 now waits for the server's
  `Retry-After`, and evaluation runs go one question at a time.
- An LLM call may take up to 300 s (was 180 s): long revision calls timed out on every attempt. Each failed
  attempt is logged.

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
