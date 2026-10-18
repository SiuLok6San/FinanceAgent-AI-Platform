Hand-written context for the Oct 2026 runs (the numbers above are computed by `eval/score.py`; this section is not).

- **Run sessions.** The original pipeline ran all 20 questions in one session on 2026-10-18. The revised pipeline (with
  the validator feedback loop) ran at commit `7cd4818`: Q02–Q20 in one session, then Q01, Q02, Q03 and Q07 again,
  because the laptop had gone to sleep during their first attempts and their timings were meaningless. Earlier
  interrupted attempts were overwritten. An earlier revised run without the feedback loop (same day, commits
  `e6d7350`/`822a8cd`) is in the git history at commit `89fd2b2`; its RESULTS.md (scored with the validator of
  that time) reports 59% traceable numbers and 65 uncited paragraphs.
- **The two "Revised" columns come from the same run.** "First draft" scores the Writer's draft before validator
  feedback; "after feedback loop" scores the report that was kept. The time per question covers the whole pipeline,
  including the revision call, so it is the same in both columns.
- **Keep/reject decisions were made during the run** by the validator as it was at `7cd4818`. The counts above come
  from the current validator, which since then also reads ISO dates written with non-breaking hyphens
  (`2026‑10‑15`; before, the "10" and "15" inside them counted as unverified numbers) and citations in parentheses
  (`(S4, S8)`).
- **What the feedback loop changed.** Open issues fell from 169 to 69 and uncited paragraphs from 60 to 26, mostly
  because the revision added citations (219 → 335 markers) and labelled its own numbers as assumptions (96 → 124).
  The share of numbers traceable to fetched data moved only from 61% to 64%: the loop fixes attribution more than it
  adds data. 4 of 20 revisions were rejected (Q02, Q20: same number of open issues; Q08, Q11: more), and the first
  draft was kept.
- **Assumption labels are a policy choice.** A number the report marks "(assumption)" stays *unverified*, but it is
  not counted as an open issue or a no-data problem. A writer could over-label to avoid checks, so the count is
  reported separately.
- **Validator changes after seeing outputs.** Fixes made after reading real reports: gpt-oss's citation styles
  (`【S1】`, `(S4, S8)`), non-breaking hyphens in labels, units and dates, `×` as a multiple, year-over-year changes by calendar
  month (FRED has no CPI value for 2025-10, so "12 observations back" was 13 months), a spread having two sources, a
  figure that is also in the cited web text not counting as mis-cited, and preferring the field named in the sentence
  when two fields round to the same number. Every fix applies equally to all columns.
- **Why the original scores low.** The original agents never saw the fetched values: the Writer got only table titles.
  It also fetched little data: FRED series for 5 of 20 questions (Q01, Q02, Q03, Q08, Q17; no credit series matched
  "high-yield", "investment-grade" or "IG vs HY") and Finnhub tables for the 4 equity questions. Of its 481 unverified
  numbers, 144 appear in a fetched web/news snippet; the rest could not be traced to anything fetched.
- **The remaining no-data issue (Q20).** No series is tagged "portfolio". The report labels its recommended weights
  ("40–50% IG (assumption)") but repeats them once in the conclusion without the label.
- **Not reproducible run to run.** The hosted Ollama API ignored the seed in the determinism check, so a re-run will
  give different reports and somewhat different numbers.
