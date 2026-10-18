Hand-written context for the Oct 2026 run (the numbers above are computed by `eval/score.py`; this section is not).

- **Run sessions.** Revised pipeline: Q01–Q12 and Q17–Q20 ran at commit `e6d7350`; the equity questions Q13–Q16 ran at
  `822a8cd`, after the Finnhub key was fixed. The commits in between changed only the validator and the eval scripts,
  not the prompts or data the agents see. The original pipeline ran all 20 questions in one session.
- **Validator changes after seeing outputs.** A first revised attempt was stopped after two questions because the
  validator missed gpt-oss's citation style (`【S1】`), misread non-breaking hyphens, and computed year-over-year changes
  by observation count (FRED has no CPI value for 2025-10, so "12 observations back" was 13 months). These were fixed
  before the runs reported here. Later fixes came from spot-checking these runs: a spread has two sources; a figure
  that is also in the cited web text is not mis-cited; `×` counts as a multiple; and when two fields round to the same
  number, the one named in the sentence wins (Q13: "earnings growth of 17.9%" first matched ROE 17.91 instead of EPS
  growth 17.88). Every fix applies equally to both pipelines.
- **Why the original scores low.** The original agents never saw the fetched values: the Writer got only table titles.
  It also fetched little data: FRED series for 5 of 20 questions (Q01, Q02, Q03, Q08, Q17; no credit series matched
  "high-yield", "investment-grade" or "IG vs HY") and Finnhub tables for the 4 equity questions. Of its 481 unverified
  numbers, 144 appear in a fetched web/news snippet; the rest could not be traced to anything fetched.
- **The two remaining no-data issues in the revised pipeline.** Both reports disclose the missing data, and in both the
  flagged figures are recommended allocation weights, which are advice rather than data; the rule cannot tell the two
  apart. Q11: the router added "rates" because of the word "bonds" (a routing miss), and "a 10–15% high-yield
  allocation" sits in a sentence that mentions bonds. Q20: no series is tagged "portfolio", and the report recommends
  weights such as 50–60%.
- **Uncited paragraphs went up (19 → 65).** The revised reports state far more numbers that trace to data, and many of
  them sit in bullets or tables without an `[S#]`. This is a real weakness of the revised prompts.
- **Not reproducible run to run.** The hosted Ollama API ignored the seed in the determinism check, so a re-run will
  give different reports and somewhat different numbers.
