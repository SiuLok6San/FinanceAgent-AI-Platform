# Revision Spec: FinanceAgent-AI-Platform

Put this file at `docs/FINANCEAGENT_FIX_SPEC.md` in the repo. It is the single source of truth for this revision. Read it fully before doing anything.

---

## 0. Context and how we work

- This repo is a **Columbia industry-sponsored course project with Boosted.ai (Oct–Dec 2025)**: a multi-agent assistant (Planner → Researcher → Analyst → Reasoner → Writer) that answers investor questions using FRED, Finnhub, and Tavily data and a local Llama 3 model through Ollama. I built it early, when I was still learning AI coding tools. I am revising it because it is linked from my resume.
- I am an Operations Research M.S. student, **not a software engineer**. You write the code; I make the design decisions.
- **Language:** talk to me in Simplified Chinese (简体中文) in chat. Write all code, comments, and docs in English.
- **Workflow:** create a branch `revision-2026`. Work phase by phase (Section 6). At the end of each phase, STOP, summarize in Chinese, and wait for my approval. Merge to `main` only when I say so. **Never rewrite or force-push git history.**
- **Never fabricate** results, metrics, or example outputs. Every example and every metric must come from an actual run on my machine. If an API key or Ollama is missing, ask me.

---

## 1. Problems found in the current code

### P1: "quality checks" are only a prompt
`agents.py → run_reasoner()` asks the LLM to "remove weak claims and sharpen logic." There is no deterministic check that the numbers or citations in the final report are real. My resume claims *"source-cited reports"* and *"quality checks"*, so this needs a real, programmatic validation layer.

### P2: README problems
- Text copied from a chat interface ("text", "Copy code") appears in the body
- The architecture diagram and the project structure are not inside code blocks
- No screenshots, no example outputs, no context (course, sponsor, team, my role), and no limitations section

### P3: dependencies
`requirements.txt` lists `openai`, `altair`, `matplotlib`, `finnhub-python`, and `tavily-python`, but none of them appear to be imported (the APIs may be called through `requests`). Verify each with grep, remove the unused ones, and pin the versions that you test.

### P4: robustness and UX
- On failure, `run_multiagent_pipeline()` returns the full Python traceback as the answer text. The UI should show a friendly message and write the traceback to a log instead.
- Routing (`router.py`, `router_tier2.py`) is keyword-based. Keep it, but describe it honestly as rule-based, and add tests for it.

---

## 2. New: deterministic validation layer (`validators.py`)

Run the following checks on every final report **after** generation. Show the results in the UI as a "Validation" panel, and store them with the report.

1. **Numeric grounding.** Extract every number in the report (including percentages, basis points, $ amounts, and dates used as values). Try to match each one to a value in the data tables the Researcher actually fetched, allowing for rounding and unit conversion (for example, 0.0425 = 4.25%). Classify each number as `verified` / `derived` (a simple arithmetic result from verified values, such as a change or spread) / `unverified`. Ignore years used as labels, list numbering, and numbers that come from the question itself.
2. **Citation integrity.** Every `[S#]` marker must refer to a source that exists in the fetched source list. Flag dangling markers. Flag factual paragraphs that contain verified numbers but no citation. Reuse and extend the existing `extract_citations_used()` in `answer_builder.py`.
3. **Data freshness.** Show the last observation date for each data series. Warn if a series is older than its expected frequency allows (for example, a monthly series more than 60 days old).
4. **No-data honesty.** If no data was fetched for a topic, the report must say so. Flag any report that contains `unverified` numbers on a topic with no data.

**Policy:** do not silently delete flagged content. Highlight it in the UI and add a validation summary at the top of the report, for example *"18 of 20 figures verified against source data; 2 flagged."*

---

## 3. Evaluation (produces honest metrics for my resume)

- Create `eval/questions.csv` with **20 realistic investor questions**: 4 each for macro, rates, credit, equity, and portfolio. Draft them, and I will approve the list.
- Run the full pipeline on all 20 and save the outputs to `eval/results/`.
- Report:
  - **Routing accuracy:** compare the router's tags with the expected tags that I approve
  - **Validation metrics:** share of figures verified / derived / unverified, number of dangling citations, and number of reports with no-data honesty issues
  - **Before vs. after:** run the validation checks on outputs from the original pipeline as well (validation turned on, nothing else changed), so we can see what the new layer catches
- Write the results to `eval/RESULTS.md` with the exact run date, model name, and settings. **Report the numbers exactly as they come out, even if they look bad.**

---

## 4. Reproducibility and examples
- Pass a fixed `seed` in the Ollama request options and keep temperatures low (the current 0.1–0.15 is fine). Log the model name and settings with every report.
- Add `examples/` with **3 real reports** (macro, credit, and equity questions) generated by the revised pipeline, each with its validation summary, plus 2–3 UI screenshots. Viewers should understand the project without installing Ollama or getting API keys.
- Add `.env.example` with `FRED_API_KEY`, `FINNHUB_API_KEY`, `TAVILY_API_KEY`, `LLM_API_BASE`, and `LLM_MODEL_NAME` (no real values), and confirm that `.env` is gitignored.

---

## 5. README rewrite

Structure:
1. **Title and one-sentence pitch**
2. **Context:** a Columbia industry-sponsored course project with Boosted.ai, Oct–Dec 2025. Team project. **My role:** `[TO BE FILLED BY ME]`. Revised in Oct 2026 to add a deterministic validation layer and an evaluation (link to CHANGELOG).
3. **How it works:** the agent pipeline diagram (inside a code block), rule-based routing, the data sources, the validation layer
4. **Results:** a summary of the evaluation metrics from `eval/RESULTS.md`, plus links to the examples
5. **Screenshots**
6. **How to run:** correct code blocks, including Ollama setup (`ollama pull llama3`) and the API keys
7. **Limitations:** the LLM can still misinterpret data; keyword routing can misclassify unusual questions; free API rate limits; this is not investment advice
8. **Project structure**

Also add `CHANGELOG.md`: "Dec 2025: original course version. Oct 2026: added the deterministic validation layer, evaluation set, examples, and dependency cleanup; improved error handling."

---

## 6. Phases and checkpoints

| Phase | Work | Checkpoint |
|---|---|---|
| 1 | Create the branch, clean up dependencies, fix error handling, confirm the app runs. Draft `eval/questions.csv` with expected tags. | STOP; I approve the questions |
| 2 | Run a baseline evaluation with the original pipeline and save the outputs | STOP |
| 3 | Build `validators.py`, unit tests, and the UI Validation panel | STOP |
| 4 | Re-run the evaluation, write `eval/RESULTS.md`, generate the examples | STOP |
| 5 | README, CHANGELOG, screenshots | STOP |
| 6 | Final review; merge to `main` only when I approve | Done |

**Required tests (`pytest`):** number extraction and matching (including % and unit conversion), dangling-citation detection, freshness warnings, routing on the approved question set, and a smoke test of the pipeline with the LLM mocked.

At each checkpoint, also give me (in Chinese) **3 likely interview questions** about what changed, with short English answers.
