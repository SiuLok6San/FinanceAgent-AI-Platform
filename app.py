import hmac

import pandas as pd
import streamlit as st

from agents import run_multiagent_pipeline
from demo import demo_runs, load_demo_result
from llm import llm_settings
from settings import env

# Set APP_PASSWORD (env var or Streamlit secret) on a public deployment so that
# only people with the password can start live runs, which spend API quota.
APP_PASSWORD = env("APP_PASSWORD")
# Live runs need an LLM endpoint configured explicitly (.env locally, secrets on a deployment);
# a deployment without one only shows the example runs.
LIVE_CONFIGURED = bool(env("LLM_API_BASE") or env("OLLAMA_API_KEY"))
EXAMPLE_MODE = "Example runs (saved, no API calls)"
LIVE_MODE = "Live analysis"

st.set_page_config(
    page_title="B17 Finance Multi-Agent",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# -------------------------------------------------------
# Global CSS — clean, modern, institutional style
# -------------------------------------------------------
st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: "Inter", sans-serif;
}
.block-container {
    padding-top: 1.5rem !important;
}

/* Main Title */
.b17-title {
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: -4px;
}
.b17-caption {
    font-size: 15px;
    color: #555;
    margin-bottom: 18px;
}
.b17-divider {
    border-bottom: 2px solid #0059b3;
    margin-top: 8px;
    margin-bottom: 25px;
}

/* Clean input styles */
textarea, input {
    border-radius: 6px !important;
    border: 1px solid #DDD !important;
}

/* Run button */
.stButton>button {
    background-color: #0059b3;
    color: white;
    padding: 10px 20px;
    border-radius: 6px;
    font-weight: 600;
    border: none;
}
.stButton>button:hover {
    background-color: #004185;
}


</style>
""", unsafe_allow_html=True)

# -------------------------------------------------------
# Title
# -------------------------------------------------------
st.markdown('<div class="b17-title">B17 Finance Multi-Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="b17-caption">Institutional-grade macro, credit, and portfolio intelligence — '
    'multi-agent LLM pipeline (Ollama) + live FRED, Finnhub and Tavily data, '
    'with deterministic validation of every report.</div>',
    unsafe_allow_html=True,
)
st.markdown('<div class="b17-divider"></div>', unsafe_allow_html=True)

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------
def escape_md(text: str) -> str:
    """Stop "$" from being rendered as LaTeX by st.markdown."""
    return (text or "").replace("$", "\\$")


def highlight_flagged(report: str, figures: list) -> str:
    """Highlight unverified figures (orange: found nowhere; blue: only in a web/news snippet). Nothing is removed."""
    flagged = sorted((f for f in figures if f["status"] == "unverified"), key=lambda f: f["start"], reverse=True)
    for f in flagged:
        tag = "\u2063HL_SNIPPET" if f.get("in_source_text") else "\u2063HL_NONE"
        report = report[:f["start"]] + tag + report[f["start"]:f["end"]] + "\u2063HL_END" + report[f["end"]:]
    report = escape_md(report)
    return (report.replace("\u2063HL_NONE", ":orange-background[")
                  .replace("\u2063HL_SNIPPET", ":blue-background[")
                  .replace("\u2063HL_END", "]"))


def render_repair_note(result: dict) -> None:
    """One line on the validator feedback loop, plus the first draft for comparison."""
    rounds = (result.get("repair") or {}).get("rounds") or []
    if not rounds:
        return
    last = rounds[-1]
    if last.get("error"):
        st.caption("A revision pass was attempted but failed; showing the first draft.")
    elif any(r["accepted"] for r in rounds):
        st.caption(f"Revised after validation feedback: open issues {rounds[0]['open_issues_before']} → "
                   f"{last['open_issues_after'] if last['accepted'] else last['open_issues_before']}.")
    else:
        st.caption(f"A revision was tried but not kept (open issues {last['open_issues_before']} → "
                   f"{last['open_issues_after']}); showing the first draft.")
    with st.expander("First draft and the feedback it received"):
        st.markdown(f"**Validation of the first draft:** {escape_md(result['draft_validation']['summary'])}")
        st.markdown(highlight_flagged(result["draft_report_text"], result["draft_validation"]["figures"]))
        st.markdown("**Feedback sent to the Writer:**")
        st.code(rounds[0]["feedback"], language=None)


def render_validation_panel(validation: dict, result: dict) -> None:
    counts = validation["counts"]
    cit = validation["citations"]
    box = st.container(border=True)
    box.subheader("Validation")

    with box:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Verified", counts["verified"])
        c2.metric("Derived", counts["derived"])
        c3.metric("Unverified", counts["unverified"])
        c4.metric("Open issues", validation.get("open_issues", 0))
        c5.metric("Dangling citations", len(cit["dangling"]))

        if validation["figures"]:
            with st.expander("Figures in the report", expanded=counts["unverified"] > 0):
                rows = [{
                    "figure": f["text"],
                    "status": f["status"],
                    "matched data": f["matched"] or "",
                    "source": f["source_id"] or (f"in snippet {f['in_source_text']}" if f["in_source_text"] else ""),
                "assumption": "yes" if f.get("assumption") else "",
                    "same topic": "" if f["topic_match"] is None else ("yes" if f["topic_match"] else "no"),
                    "context": f["context"],
                } for f in validation["figures"]]
                st.dataframe(pd.DataFrame(rows), hide_index=True)

        with st.expander("Citation integrity", expanded=bool(cit["dangling"] or cit["uncited_paragraphs"])):
            st.write(f"{cit['markers']} citation marker(s); dangling: {', '.join(cit['dangling']) or 'none'}.")
            for u in cit["uncited_paragraphs"]:
                st.warning(f"Figures {', '.join(u['figures'])} have no citation: {escape_md(u['text'])}")
            for m in cit["source_mismatches"]:
                st.info(f"Figures {', '.join(m['figures'])} come from a source other than the cited {', '.join(m['cited'])}: "
                        f"{escape_md(m['text'])}")

        with st.expander("Data freshness"):
            if validation["freshness"]:
                st.dataframe(pd.DataFrame(validation["freshness"]), hide_index=True)
            else:
                st.write("No dated data series were fetched.")

        nd = validation["no_data"]["topics_without_data"]
        with st.expander("No-data topics", expanded=validation["no_data"]["issue"]):
            st.write(f"Routed topics (rule-based): {', '.join(result.get('tags', []))}")
            if not nd:
                st.write("Every routed topic has fetched data.")
            for t in nd:
                msg = (f"**{t['topic']}**: no data fetched; "
                       f"{'the report says so' if t['disclosed'] else 'the report does not say so'}")
                if t["unverified_figures"]:
                    msg += f"; unverified figures: {', '.join(t['unverified_figures'])}"
                (st.warning if (not t["disclosed"] or t["unverified_figures"]) else st.write)(escape_md(msg))

        if validation["data_status"]:
            with st.expander("Data source problems"):
                for s in validation["data_status"]:
                    st.write(f"{s['provider']} {s['item']}: {s['detail']}")


def render_result(result: dict) -> None:
    """Report, validation panel, data tables, charts and sources for one pipeline result."""
    validation = result.get("validation") or {}
    figures = validation.get("figures", [])

    # ---------------------------------------------------
    # Final Institutional Report (flagged figures highlighted)
    # ---------------------------------------------------
    with st.container(border=True):
        st.subheader("Final Institutional Report")
        if validation:
            flagged = validation["counts"]["unverified"] or validation["no_data"]["issue"] or \
                validation["citations"]["dangling"] or validation["citations"]["uncited_paragraphs"]
            (st.warning if flagged else st.success)(f"**Validation:** {escape_md(validation['summary'])}")
        render_repair_note(result)
        st.markdown(highlight_flagged(result.get("report_text_raw", result["report_text"]), figures))
        st.caption(
            "Highlighted figures could not be matched to the fetched data tables "
            "(blue: the number appears in a fetched web/news snippet; orange: found nowhere). "
            f"Model: {result['run_meta']['model']} · seed {result['run_meta']['seed']} · "
            f"run {result['run_meta']['run_id']}"
        )

    # ---------------------------------------------------
    # Validation panel
    # ---------------------------------------------------
    if validation:
        render_validation_panel(validation, result)

    # ---------------------------------------------------
    # Key Data Tables
    # ---------------------------------------------------
    if result.get("key_data_tables"):
        box = st.container(border=True)
        box.subheader("Key Data Tables")

        for i, tbl in enumerate(result["key_data_tables"], start=1):
            with box.expander(f"Table {i}: {tbl.get('title', 'Untitled')}"):
                st.dataframe(tbl.get("data"), hide_index=True)

    # ---------------------------------------------------
    # Charts
    # ---------------------------------------------------
    if result.get("chart_specs"):
        box = st.container(border=True)
        box.subheader("Charts")

        for i, chart in enumerate(result["chart_specs"], start=1):
            with box.expander(f"Chart {i}: {chart.get('title','Untitled')}"):
                series = pd.Series(chart.get("y", []), index=pd.to_datetime(chart.get("x", [])),
                                   name=chart.get("y_label") or "value")
                st.line_chart(series)

    # ---------------------------------------------------
    # Sources
    # ---------------------------------------------------
    if result.get("sources"):
        box = st.container(border=True)
        box.subheader("Sources")

        for src in result["sources"]:
            title = escape_md(src["title"])
            link = f"[{title}]({src['url']})" if src.get("url") else title
            box.markdown(f"**[{src['id']}]** {link}  \n*{escape_md(src['source'])}*  \n{escape_md(src.get('snippet', ''))}")



# -------------------------------------------------------
# Layout: left input, right output
# -------------------------------------------------------
left, right = st.columns([1.15, 2], gap="large")

with left:
    st.subheader("Input")
    mode = st.radio("Mode", [EXAMPLE_MODE, LIVE_MODE], label_visibility="collapsed")

    if mode == EXAMPLE_MODE:
        runs = demo_runs()
        labels = {r["id"]: f"{r['id']} · {r['category']} · {r['question']}" + (f" ({r['ticker']})" if r["ticker"] else "")
                  for r in runs}
        chosen = st.selectbox("Saved evaluation run", list(labels), format_func=labels.get)
        st.caption("Real outputs of the revised pipeline on the 20 evaluation questions, saved in "
                   "`eval/results/` and re-checked by the current validator. Nothing is fetched or generated now.")
        run_button = False
    else:
        question = st.text_area(
            "Finance Question",
            placeholder="Example: How does higher-for-longer rates affect U.S. IG vs HY credit spreads?",
            height=160
        )

        ticker = st.text_input(
            "Ticker (optional)",
            placeholder="e.g., JPM"
        )

        unlocked = LIVE_CONFIGURED
        if not LIVE_CONFIGURED:
            st.warning("Live analysis is not configured on this deployment (no LLM endpoint or API keys). "
                       "The example runs show real outputs; to run it yourself, see *How to run* in the README.")
        elif APP_PASSWORD:
            entered = st.text_input("Password for live runs", type="password",
                                    help="Live runs call the LLM and data APIs, which have usage limits.")
            unlocked = hmac.compare_digest(entered.encode(), APP_PASSWORD.encode())
            if entered and not unlocked:
                st.error("Wrong password.")

        run_button = st.button("Run Institutional Analysis", disabled=not unlocked)
        if LIVE_CONFIGURED:
            _llm = llm_settings()
            st.caption(f"Model: {_llm['model']} via {_llm['api_base']}")
        if LIVE_CONFIGURED and not unlocked:
            st.caption("Live analysis is password-protected on the public site. The example runs show real outputs.")


with right:
    if mode == EXAMPLE_MODE:
        result = load_demo_result(chosen)
        meta = result["run_meta"]
        st.info(f"**Saved run** of {result['run_utc'][:10]} with `{meta['model']}` — data and model output from that "
                "day; live results would differ.")
        render_result(result)
    elif not run_button:
        st.info("Enter a question and click **Run Institutional Analysis** to begin.")
    elif not question.strip():
        st.error("Please enter a finance question.")
    else:
        with st.spinner("Generating institutional research…"):
            result = run_multiagent_pipeline(
                question.strip(),
                ticker.strip().upper() or None
            )

        if result.get("error"):
            st.error(result["report_text"])
            st.stop()

        render_result(result)
