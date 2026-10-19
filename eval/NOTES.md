Hand-written context for these runs (the numbers above are computed by `eval/score.py`; this section is not).

- **Versions.** *original* is tag `v1.0-course` (commit `0468eea`), run from a `git archive` checkout with today's
  LLM client injected (its own client cannot call the hosted API). *v2* is tag `v2.0-validation` (`faf19bc`),
  run from its own checkout. *v3* is this version. All three use `gpt-oss:20b` on the hosted Ollama API.
- **Question sets.**
  - *Development* (Q01–Q20): written in Phase 1 and used while building v2 and v3 (the rule-based router was
    also fixed after seeing them). Every validator and pipeline fix listed below was found on these outputs.
  - *Held-out* (H01–H10): committed (`95878d7`) before any v3 code. One slip: two of them (H05, H10) were pasted
    once into a manual smoke test of the Planner prompt during development. No code or prompt was changed
    because of their outputs, and no other held-out output was read before the final scoring.
  - *Fresh* (F01–F10): committed (`fca4f31`) after that slip, and not run on any version before the final runs.
    This is the cleanest comparison. The comparison and wording checks were added after it was frozen, but
    they were built and tuned on development outputs only.
- **Where the original and v2 results come from.** Development questions: the runs of 2026-10-18 (v2 at
  `7cd4818`, the feedback-loop version that became the `v2.0-validation` tag). Held-out and fresh questions:
  later runs from the tags; see Setup for dates and commits. All original and v2 outputs are re-scored with
  the current validator, so the new checks apply to every version.
- **How v3 got to its final run.** v3 was run on the development questions several times, and each round of
  fixes came from reading those outputs:
  1. spread facts between unrelated series (e.g. "10-year Treasury minus HY OAS"), a direction check that
     reached across commas, and "S&P 500" written with a non-breaking space read as the number 500
     (`64b74cb`, `93ee28f`); earlier, units, signs and citations the writer adds around a placeholder (Q09);
  2. the comparison check (`efd31bc`) and the history and trend wording checks (`9f223ba`), each tuned until
     it flagged only real contradictions on the development reports;
  3. revision calls that hit the 180 s limit on every attempt (Q01, Q05): the limit is now 300 s and failed
     attempts are logged (`1de5e0a`);
  4. a revision that cleared typed numbers by labelling them as assumptions ("New York Fed: 25.2%
     (assumption) [S4]", with 25.2% in S4's text; Q03): such labels now count as open issues (`e28852d`);
  5. a report (Q01) concluding "inflation is cooling modestly" while the 12-month rate had gone from +3.4%
     to +3.7% and the last three months ran at +4.6% annualized: the trend check skipped price indexes,
     because their level almost always rises. "Cooling" and "accelerating" are now checked against the
     rates, flagged only when both measures disagree; on the development reports of all versions the
     check flagged 15 statements, all of which contradicted the data;
  6. the feedback loop introducing errors: in placeholder reports, typed numbers were still matched to the
     data by value, and those coincidences went back to the writer. "Above the Fed's 2% target" became
     "above the Fed's {{FEDFUNDS.latest}} target" (3.75%) in Q04 after the feedback named an old policy-rate
     reading as the match; in Q03 "IG spreads likely to widen (10–20 bp)" became "narrow" after a direction
     flag from "20 bp" matching a −0.2 pp unemployment change. In placeholder reports only inserted facts are
     now direction- and citation-checked, hedged clauses ("could", "if") are skipped, and the feedback no
     longer names value matches;
  7. two scoring fixes found while checking item 4: a number counted as "in a source's text" on matches such
     as "0.8 pp" against the "1" in "## 1. Introduction" (now the source must show the same number at the
     precision written, `ddc5b56`), and percentile facts labelled with the configured window ("5y") whatever
     the data covered (`d74f5ea`). FRED serves only 3 years of the ICE credit spreads, so earlier v3 runs
     called a 3-year percentile "5y"; the original and v2 tables cover 1–2.5 years. History wording now
     needs at least 3 years, and "five-year" phrases five.

  All 40 v3 results in the tables come from one run of the final code; earlier v3 outputs are not used.
  The fixes in items 5–7 also change the original and v2 numbers, since every report is re-scored with
  the current validator.
- **One request at a time.** The hosted free tier allows one concurrent request (HTTP 429 beyond that). Early
  runs of several pipelines in parallel failed on a few questions; those were run again one at a time, and the
  final runs are sequential.
- **Laptop sleep.** Some earlier runs overlapped with the laptop sleeping (lid closed), which freezes the
  process and can time out LLM calls. Affected questions were run again with the machine awake; mean times can
  still include short pauses.
- **The Planner.** The hosted model returns JSON but ignores schema constraints, so the Planner's output is
  validated in code (topics from the allowed list, series from the catalog) and replaced by the rule-based router
  when unusable.
- **Not reproducible run to run.** The hosted Ollama API ignored the seed in the determinism check, so a re-run
  gives different reports and somewhat different numbers.
