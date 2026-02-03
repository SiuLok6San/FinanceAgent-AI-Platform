import streamlit as st
from agents import run_multiagent_pipeline

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

/* Section card */
.section-card {
    background-color: #f7f7f7;
    padding: 18px 20px;
    border-radius: 8px;
    border: 1px solid #e6e6e6;
    margin-bottom: 22px;
}
.section-title {
    font-size: 20px;
    font-weight: 650;
    margin-bottom: 6px;
}
</style>
""", unsafe_allow_html=True)

# -------------------------------------------------------
# Title
# -------------------------------------------------------
st.markdown('<div class="b17-title">B17 Finance Multi-Agent</div>', unsafe_allow_html=True)
st.markdown('<div class="b17-caption">Institutional-grade macro, credit, and portfolio intelligence — powered by local LLM + real data.</div>', unsafe_allow_html=True)
st.markdown('<div class="b17-divider"></div>', unsafe_allow_html=True)

# -------------------------------------------------------
# Layout: left input, right output
# -------------------------------------------------------
left, right = st.columns([1.15, 2], gap="large")

with left:
    st.subheader("Input")

    question = st.text_area(
        "Finance Question",
        placeholder="Example: How does higher-for-longer rates affect U.S. IG vs HY credit spreads?",
        height=160
    )

    ticker = st.text_input(
        "Ticker (optional)",
        placeholder="e.g., JPM"
    )

    run_button = st.button("Run Institutional Analysis")


with right:
    if not run_button:
        st.info("Enter a question and click **Run Institutional Analysis** to begin.")
    else:
        if not question.strip():
            st.error("Please enter a finance question.")
        else:
            with st.spinner("Generating institutional research…"):
                result = run_multiagent_pipeline(
                    question.strip(),
                    ticker.strip().upper() or None
                )

            # ---------------------------------------------------
            # Final Institutional Report
            # ---------------------------------------------------
            st.markdown('<div class="section-card">', unsafe_allow_html=True)
            st.markdown('<div class="section-title">Final Institutional Report</div>', unsafe_allow_html=True)
            st.markdown(result["report_text"])
            st.markdown('</div>', unsafe_allow_html=True)

            # ---------------------------------------------------
            # Key Data Tables
            # ---------------------------------------------------
            if result.get("key_data_tables"):
                st.markdown('<div class="section-card">', unsafe_allow_html=True)
                st.markdown('<div class="section-title">Key Data Tables</div>', unsafe_allow_html=True)

                for i, tbl in enumerate(result["key_data_tables"], start=1):
                    with st.expander(f"Table {i}: {tbl.get('title', 'Untitled')}"):
                        st.dataframe(tbl.get("data", {}))

                st.markdown('</div>', unsafe_allow_html=True)

            # ---------------------------------------------------
            # Charts
            # ---------------------------------------------------
            if result.get("chart_specs"):
                st.markdown('<div class="section-card">', unsafe_allow_html=True)
                st.markdown('<div class="section-title">Charts</div>', unsafe_allow_html=True)

                for i, chart in enumerate(result["chart_specs"], start=1):
                    with st.expander(f"Chart {i}: {chart.get('title','Untitled')}"):
                        st.line_chart(chart.get("data"))

                st.markdown('</div>', unsafe_allow_html=True)

            # ---------------------------------------------------
            # Sources
            # ---------------------------------------------------
            if result.get("sources"):
                st.markdown('<div class="section-card">', unsafe_allow_html=True)
                st.markdown('<div class="section-title">Sources</div>', unsafe_allow_html=True)

                for src in result["sources"]:
                    st.markdown(f"""
                        **[{src['id']}]** {src['title']}  
                        *{src['source']}*  
                        {src.get('snippet','')}  
                        <small>{src.get('url','')}</small>
                    """, unsafe_allow_html=True)

                st.markdown('</div>', unsafe_allow_html=True)
