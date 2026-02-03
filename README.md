# FinanceAgent-AI-Platform
A multi-agent AI-driven financial intelligence and decision support platform designed for institutional-style market analysis, risk monitoring, and structured financial research.

This system integrates specialized AI reasoning agents with real-time macroeconomic and market data pipelines to generate explainable, reproducible, and high-quality financial insights.

A core design principle is **robustness**: the platform combines AI-assisted reasoning with deterministic analytics, ensuring stable outputs even when external data sources or local LLM inference are unavailable.

---

## ✨ What This System Does

- Accepts natural language financial questions
- Automatically routes queries to specialized analysis pipelines
- Collects real-world financial and macroeconomic data
- Performs multi-agent collaborative reasoning
- Generates:
  - Institutional-style research reports
  - Structured data tables
  - Analytical charts
  - Risk assessments and scenario analysis
  - Source tracking and audit trails

---

## 🚀 Key Features

### Multi-Agent Financial Reasoning Pipeline

- Question routing with tiered topic classification
- Specialized AI agents for:
  - Market research
  - Data collection
  - Quantitative analysis
  - Logical validation
  - Institutional-style report writing
- Collaborative reasoning flow coordinated by a central orchestrator

---

### Real-Time Financial Data Integration

- Macroeconomic indicators from FRED
- Equity and company data from Finnhub
- Optional web/news intelligence via Tavily
- Cleaned and standardized data pipelines for consistent downstream analysis

---

### Hybrid Intelligence Design

- AI-assisted reasoning powered by a local or remote LLM
- Deterministic analytics and structured data processing
- Graceful fallback when AI or external APIs are unavailable
- Fully explainable and reproducible outputs

---

### Institutional-Style Output Generation

- Executive summaries
- Deep analytical sections
- Scenario modeling
- Risk considerations
- Portfolio and market implications
- Structured citations and source tracking

---

## 🛠️ System Architecture

Streamlit UI (app.py)
        │
        ▼
Multi-Agent Orchestrator (agents.py)
        │
 ┌──────┴──────────────────────────┐
 ▼                                 ▼
AI Reasoning Agents                 Data & Analytics Layer
--------------------               -------------------------
- Researcher Agent                 - FRED API Client
- Data Agent                       - Finnhub API Client
- Analyst Agent                    - Data Engine
- Reasoner Agent                   - Chart Builder
- Writer Agent                     - Structured Tables
        │
        ▼
Final Report Builder (answer_builder.py)

Routing Layer:
- router.py (Tier-1 topic routing)
- router_tier2.py (fine-grained subtopics)

LLM Connector:
- llm.py (local LLM or OpenAI-compatible endpoints)

Utilities:
- tools.py
📁 Project Structure
text
Copy code
FinAgent-AI-System/
├── app.py                  # Streamlit UI (main entry)
├── agents.py               # Multi-agent orchestrator
├── answer_builder.py       # Institutional-style report generator
├── data_providers.py       # External financial data APIs
├── data_engine.py          # Data formatting and chart preparation
├── llm.py                  # LLM connector (local or remote)
├── router.py               # Tier-1 topic routing
├── router_tier2.py         # Tier-2 subtopic routing
├── tools.py                # Utility functions
├── requirements.txt        # Python dependencies
└── .env                    # Optional environment configuration
📦 Installation & Setup
Ensure Python 3.9 or higher is installed.

(Optional but recommended) Create and activate a virtual environment:
python -m venv venv
source venv/bin/activate        # macOS / Linux
# or
venv\Scripts\activate           # Windows
Install dependencies:
pip install -r requirements.txt

Optional: Local LLM Support

The system supports local LLM inference (e.g., via Ollama) for enhanced financial reasoning.

Install Ollama from:

https://ollama.com

Run a local model, for example:

ollama pull llama3
ollama run llama3
If no local LLM is running, the system automatically falls back to deterministic analytics.

Launch the Application
streamlit run app.py
