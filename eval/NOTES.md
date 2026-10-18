Hand-written context for these runs (the numbers above are computed by `eval/score.py`; this section is not).

- **Versions.** *original* is tag `v1.0-course` (commit `0468eea`), run from a `git archive` checkout with today's
  LLM client injected (its own client cannot call the hosted API). *v2* is tag `v2.0-validation` (`faf19bc`),
  run from its own checkout. *v3* is this version. All three use `gpt-oss:20b` on the hosted Ollama API.
- **Question sets.** The 20 development questions were written in Phase 1 and used while building v2 and v3 (the
  rule-based router was also fixed after seeing them). The 10 held-out questions were committed (`95878d7`) before
  any v3 code. One slip: two held-out questions (H05, H10) were pasted once into a manual smoke test of the Planner
  prompt during development; no code or prompt was changed because of their outputs, and no other held-out
  output was read before the final scoring.
- **Where the dev results come from.** Original and v2 on the dev questions are the runs of 2026-10-18 (v2 at
  `7cd4818`, the feedback-loop version that became the `v2.0-validation` tag). Original and v2 on the held-out
  questions, and v3 on both sets, were run later; see Setup for dates and commits.
- **Fixes between the first and the final v3 run.** A first full v3 run showed, on development questions only:
  spread facts between unrelated series (e.g. "10-year Treasury minus HY OAS"), a direction check that reached
  across commas, and "S&P 500" written with a non-breaking space being read as the number 500. These were fixed
  (`64b74cb`, `93ee28f`) and all 30 v3 questions were run again; the tables report that second run. Earlier
  render fixes (units, signs and citations the writer adds around a placeholder) were also found on a dev
  question (Q09).
- **Laptop sleep.** Some runs overlapped with the laptop sleeping (lid closed), which freezes the process and can
  time out LLM calls. Questions whose runs were affected were run again with the machine awake; mean times can
  still include short sleeps.
- **The Planner.** The hosted model returns JSON but ignores schema constraints, so the Planner's output is
  validated in code (topics from the allowed list, series from the catalog) and replaced by the rule-based router
  when unusable.
- **Not reproducible run to run.** The hosted Ollama API ignored the seed in the determinism check, so a re-run
  gives different reports and somewhat different numbers.
