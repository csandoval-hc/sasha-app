import pandas as pd
import json
import os
import sys
import re
import math
import hashlib
import traceback
import warnings
import requests # Added for Chart-to-Image conversion
from datetime import datetime
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from openai import OpenAI
from dotenv import load_dotenv
from flask_mail import Mail, Message # Added for Gmail

warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# ==============================================================================
# 1. FILE SYSTEM & AGENTIC DATA LOADER
# ==============================================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, 'knowledge_base.json')

excel_match = next((f for f in os.listdir(BASE_DIR) if 'diccionario' in f.lower() and f.endswith('.xlsx')), None)
if not excel_match:
    print("🚨 FATAL ERROR: Cannot find the DICCIONARIO Excel file.")
    sys.exit(1)

try:
    variables_df = pd.read_excel(os.path.join(BASE_DIR, excel_match), sheet_name='variables')
except Exception as e:
    print(f"🚨 EXCEL ENGINE ERROR: {e}")
    sys.exit(1)

dfs = {}
for f in os.listdir(BASE_DIR):
    if f.endswith('.csv') and 'diccionario' not in f.lower():
        table_name = f.replace('.csv', '')
        file_path = os.path.join(BASE_DIR, f)
        try:
            dfs[table_name] = pd.read_csv(file_path, encoding='utf-8', sep=None, engine='python')
        except UnicodeDecodeError:
            dfs[table_name] = pd.read_csv(file_path, encoding='latin1', sep=None, engine='python')

if not dfs:
    print("🚨 FATAL ERROR: No CSV data tables found.")
    sys.exit(1)

# ==============================================================================
# 2. CORE AI CONFIGURATION
# ==============================================================================
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_KEY)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "sasha_agent_secret_key_123")

# GMAIL CONFIGURATION
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
app.config['MAIL_DEFAULT_SENDER'] = os.getenv("MAIL_USERNAME")
mail = Mail(app)

if not os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, 'w') as f:
        json.dump({"_init": "Memory initialized."}, f)

# ==============================================================================
# 2.5 PERMISSION ROLES, MEMORY & RESULT CACHE
# ==============================================================================
USERS = {
    os.getenv("USER_1_USERNAME", "Carlos"): {
        "pwd": os.getenv("USER_1_PASSWORD", "HCTBDTCS.1"),
        "role": os.getenv("USER_1_ROLE", "Admin")
    },
    os.getenv("USER_2_USERNAME", "team1"): {
        "pwd": os.getenv("USER_2_PASSWORD", "change_this_password_3"),
        "role": os.getenv("USER_2_ROLE", "Collaborator")
    },
    os.getenv("USER_3_USERNAME", "viewer2"): {
        "pwd": os.getenv("USER_3_PASSWORD", "change_this_password_6"),
        "role": os.getenv("USER_3_ROLE", "Viewer")
    }
}

# In-memory result cache: hash(question + df_fingerprint) -> result
_result_cache = {}

def _df_fingerprint():
    """Quick fingerprint of loaded data so cache invalidates if data changes."""
    parts = []
    for name, df in sorted(dfs.items()):
        parts.append(f"{name}:{len(df)}:{list(df.columns)}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:8]

def cache_get(question):
    key = hashlib.md5(f"{question.strip().lower()}:{_df_fingerprint()}".encode()).hexdigest()
    return _result_cache.get(key)

def cache_set(question, result):
    key = hashlib.md5(f"{question.strip().lower()}:{_df_fingerprint()}".encode()).hexdigest()
    _result_cache[key] = result
    # Keep cache bounded
    if len(_result_cache) > 200:
        oldest = list(_result_cache.keys())[0]
        del _result_cache[oldest]

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def save_to_memory(new_rule):
    try:
        with open(MEMORY_FILE, 'r') as f:
            memory = json.load(f)
        memory[datetime.now().strftime("%Y-%m-%d %H:%M:%S")] = new_rule
        with open(MEMORY_FILE, 'w') as f:
            json.dump(memory, f, indent=4)
        return True
    except:
        return False

# ==============================================================================
# 3. UTILITY FUNCTIONS
# ==============================================================================

def normalize_text(text):
    return re.sub(r'[^a-z0-9]+', ' ', str(text).lower()).strip()

def safe_float(x):
    try:
        if pd.isna(x):
            return None
        if isinstance(x, str):
            x = x.replace(',', '').strip()
        return float(x)
    except:
        return None

def is_numeric_series(series):
    try:
        converted = pd.to_numeric(series, errors='coerce')
        return converted.notna().mean() > 0.7
    except:
        return False

def serialize_for_prompt(obj, max_rows=20):
    try:
        if isinstance(obj, pd.DataFrame):
            if len(obj) > max_rows:
                obj = obj.head(max_rows)
            return obj.to_dict(orient='records')
        if isinstance(obj, pd.Series):
            return obj.head(max_rows).to_dict()
        if isinstance(obj, (list, dict, str, int, float, bool)) or obj is None:
            return obj
        return str(obj)
    except:
        return str(obj)

def get_df_preview(df, n=3):
    try:
        return df.head(n).to_dict(orient='records')
    except:
        return []

def build_schema_info():
    schema_info = ""
    for name, df in dfs.items():
        schema_info += f"DataFrame: dfs['{name}'] | Columns: {', '.join(df.columns)} | Rows: {len(df)} | Preview: {get_df_preview(df, 2)}\n"
    return schema_info

def read_long_term_memory():
    try:
        with open(MEMORY_FILE, 'r') as f:
            return json.dumps(json.load(f))
    except:
        return "No custom rules saved yet."

# ==============================================================================
# FIX 2: SEMANTIC MEMORY — Pull only RELEVANT rules instead of dumping all
# ==============================================================================
def read_relevant_memory(question, top_k=5):
    """
    Instead of dumping every rule into the prompt (Junk Drawer problem),
    we score each memory entry by keyword overlap with the question and
    return only the top_k most relevant ones. This keeps the LLM's
    'thinking space' sharp and prevents attention decay on 40+ rules.
    """
    try:
        with open(MEMORY_FILE, 'r') as f:
            memory = json.load(f)

        if not memory or list(memory.keys()) == ['_init']:
            return "No custom rules saved yet."

        q_words = set(normalize_text(question).split())
        scored = []
        for timestamp, rule in memory.items():
            if timestamp == '_init':
                continue
            rule_words = set(normalize_text(str(rule)).split())
            overlap = len(q_words & rule_words)
            scored.append((overlap, timestamp, rule))

        # Sort by overlap descending; take top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        top_rules = scored[:top_k]

        if not top_rules:
            return "No relevant rules found."

        result = {}
        for _, ts, rule in top_rules:
            result[ts] = rule
        return json.dumps(result)
    except:
        return read_long_term_memory()

def find_columns_by_terms(df, include_terms, exclude_terms=None, prefer_numeric=None):
    exclude_terms = exclude_terms or []
    scored = []
    for col in df.columns:
        col_n = normalize_text(col)
        score = 0
        for term in include_terms:
            if term in col_n:
                score += 3
        for term in exclude_terms:
            if term in col_n:
                score -= 4
        if prefer_numeric is True and is_numeric_series(df[col]):
            score += 2
        if prefer_numeric is False and not is_numeric_series(df[col]):
            score += 1
        if score > 0:
            scored.append((score, col))
    scored.sort(reverse=True)
    return [c for _, c in scored]

def pick_first_existing(df, term_groups, prefer_numeric=None):
    for group in term_groups:
        matches = find_columns_by_terms(df, group, prefer_numeric=prefer_numeric)
        if matches:
            return matches[0]
    return None

def choose_best_dataframe(required_groups=None, optional_groups=None):
    required_groups = required_groups or []
    optional_groups = optional_groups or []
    best_score = -1
    best_name = None
    for name, df in dfs.items():
        score = 0
        valid = True
        for group in required_groups:
            matches = find_columns_by_terms(df, group)
            if matches:
                score += 10
            else:
                valid = False
                break
        if not valid:
            continue
        for group in optional_groups:
            matches = find_columns_by_terms(df, group)
            if matches:
                score += 2
        score += min(len(df.columns), 20) * 0.05
        if score > best_score:
            best_score = score
            best_name = name
    return best_name

def coerce_numeric(df, cols):
    df = df.copy()
    for col in cols:
        if col and col in df.columns:
            selected = df.loc[:, col]
            if isinstance(selected, pd.DataFrame):
                first_series = selected.iloc[:, 0]
                df[col] = pd.to_numeric(first_series, errors='coerce')
            else:
                df[col] = pd.to_numeric(selected, errors='coerce')
    return df

def aggregate_clients_frame(df):
    client_id_col = pick_first_existing(
        df,
        [['client_id'], ['client id'], ['customer_id'], ['customer id'], ['id_cliente'], ['cliente id']],
        prefer_numeric=True
    )
    business_name_col = pick_first_existing(
        df,
        [['business_name'], ['business name'], ['client_name'], ['client name'], ['customer_name'],
         ['customer name'], ['razon social'], ['nombre negocio'], ['nombre cliente']],
        prefer_numeric=False
    )
    total_paid_col = pick_first_existing(
        df,
        [['total_paid'], ['paid'], ['payment'], ['payments'], ['pagado'], ['cobrado'], ['recovered']],
        prefer_numeric=True
    )

    if not client_id_col or not total_paid_col:
        return None

    use_cols = []
    for c in [client_id_col, total_paid_col, business_name_col]:
        if c and c not in use_cols:
            use_cols.append(c)

    work = df.loc[:, use_cols].copy()
    work = coerce_numeric(work, [total_paid_col])

    rename_map = {client_id_col: 'client_id', total_paid_col: 'total_paid'}
    if business_name_col:
        rename_map[business_name_col] = 'business_name'

    work = work.rename(columns=rename_map)
    work = work.loc[:, ~work.columns.duplicated()]
    work = work.dropna(subset=['total_paid'])

    if 'business_name' in work.columns:
        agg = (
            work.groupby(['client_id', 'business_name'], as_index=False, dropna=False)['total_paid']
            .sum().copy()
        )
    else:
        agg = (
            work.groupby(['client_id'], as_index=False, dropna=False)['total_paid']
            .sum().copy()
        )
        agg['business_name'] = agg['client_id'].apply(lambda x: f"Client {x}")

    return agg[['client_id', 'business_name', 'total_paid']]

# ==============================================================================
# 4. TOOL LIBRARY — Added Professional Email Tooling
# ==============================================================================

def generate_static_chart_url(chart_type, chart_data):
    """Generates an image URL via QuickChart for professional emails."""
    if not chart_data or chart_type == 'none' or len(chart_data) < 2:
        return None
    labels = [str(d['label']) for d in chart_data]
    values = [float(d['value']) for d in chart_data]
    config = {
        "type": chart_type if chart_type in ['bar', 'line', 'pie', 'doughnut'] else 'bar',
        "data": {
            "labels": labels,
            "datasets": [{"label": "Data", "data": values, "backgroundColor": "#6366f1"}]
        },
        "options": {"title": {"display": True, "text": "Sasha Analytics Summary"}}
    }
    return f"https://quickchart.io/chart?c={json.dumps(config)}&width=600&height=300"

def tool_groupby(df_name, groupby_col, metric_col, agg_func='sum', sort_ascending=False, limit=20, label_col=None):
    """Generic group-by aggregation tool."""
    if df_name not in dfs:
        return None
    df = dfs[df_name].copy()
    if groupby_col not in df.columns or metric_col not in df.columns:
        return None
    df = coerce_numeric(df, [metric_col])
    df = df.dropna(subset=[metric_col])

    agg_map = {'sum': 'sum', 'mean': 'mean', 'count': 'count', 'max': 'max', 'min': 'min'}
    agg_fn = agg_map.get(agg_func, 'sum')

    result = df.groupby(groupby_col, as_index=False)[metric_col].agg(agg_fn)
    result = result.sort_values(by=metric_col, ascending=sort_ascending).head(limit)
    result.columns = [groupby_col, metric_col]

    label = label_col or groupby_col
    chart_data = [
        {"label": str(row[groupby_col]), "value": float(row[metric_col])}
        for _, row in result.iterrows()
        if pd.notnull(row[groupby_col]) and pd.notnull(row[metric_col])
    ]

    return {
        "tool": "groupby",
        "df_name": df_name,
        "final_df": result,
        "chart_data": chart_data,
        "metrics": {
            "rows": len(result),
            "total": safe_float(result[metric_col].sum()),
            "top_label": str(result.iloc[0][groupby_col]) if len(result) else None,
            "top_value": safe_float(result.iloc[0][metric_col]) if len(result) else None,
        }
    }

def tool_filter_rank(df_name, filter_col, operator, threshold, rank_col=None, limit=50, label_col=None):
    """Filter rows by condition, optionally rank by another column."""
    if df_name not in dfs:
        return None
    df = dfs[df_name].copy()
    if filter_col not in df.columns:
        return None
    df = coerce_numeric(df, [filter_col])
    df = df.dropna(subset=[filter_col])

    ops = {'>': df[filter_col] > threshold, '>=': df[filter_col] >= threshold,
           '<': df[filter_col] < threshold, '<=': df[filter_col] <= threshold,
           '==': df[filter_col] == threshold}
    mask = ops.get(operator)
    if mask is None:
        return None
    filtered = df[mask].copy()

    if rank_col and rank_col in filtered.columns:
        filtered = coerce_numeric(filtered, [rank_col])
        filtered = filtered.sort_values(by=rank_col, ascending=False)
    else:
        filtered = filtered.sort_values(by=filter_col, ascending=False)

    filtered = filtered.head(limit)

    chart_col = rank_col or filter_col
    chart_label = label_col or (filtered.columns[0] if len(filtered.columns) > 0 else 'index')
    chart_data = []
    if chart_label in filtered.columns and chart_col in filtered.columns:
        chart_data = [
            {"label": str(row[chart_label]), "value": float(row[chart_col])}
            for _, row in filtered.head(15).iterrows()
            if pd.notnull(row.get(chart_label)) and pd.notnull(row.get(chart_col))
        ]

    return {
        "tool": "filter_rank",
        "df_name": df_name,
        "final_df": filtered,
        "chart_data": chart_data,
        "metrics": {
            "matched_count": int(len(filtered)),
            "filter_col": filter_col,
            "threshold": threshold,
            "operator": operator,
        }
    }

def tool_top_bottom(df_name, metric_col, n=10, bottom=False, label_col=None):
    """Return top or bottom N rows by a metric column."""
    if df_name not in dfs:
        return None
    df = dfs[df_name].copy()
    if metric_col not in df.columns:
        return None
    df = coerce_numeric(df, [metric_col])
    df = df.dropna(subset=[metric_col])
    df = df.sort_values(by=metric_col, ascending=bottom).head(n)

    lbl = label_col or df.columns[0]
    chart_data = []
    if lbl in df.columns:
        chart_data = [
            {"label": str(row[lbl]), "value": float(row[metric_col])}
            for _, row in df.iterrows()
            if pd.notnull(row.get(lbl)) and pd.notnull(row.get(metric_col))
        ]

    return {
        "tool": "top_bottom",
        "df_name": df_name,
        "final_df": df,
        "chart_data": chart_data,
        "metrics": {
            "selected_count": len(df),
            "selected_total": safe_float(df[metric_col].sum()),
            "direction": "bottom" if bottom else "top",
        }
    }

def tool_compare_segments(df_name, segment_col, metric_col, segments=None, agg_func='sum'):
    """Compare metric across segments/categories."""
    if df_name not in dfs:
        return None
    df = dfs[df_name].copy()
    if segment_col not in df.columns or metric_col not in df.columns:
        return None
    df = coerce_numeric(df, [metric_col])
    df = df.dropna(subset=[metric_col])

    if segments:
        df = df[df[segment_col].isin(segments)]

    agg_map = {'sum': 'sum', 'mean': 'mean', 'count': 'count'}
    agg_fn = agg_map.get(agg_func, 'sum')
    result = df.groupby(segment_col, as_index=False)[metric_col].agg(agg_fn)
    result = result.sort_values(by=metric_col, ascending=False)

    chart_data = [
        {"label": str(row[segment_col]), "value": float(row[metric_col])}
        for _, row in result.iterrows()
        if pd.notnull(row[segment_col]) and pd.notnull(row[metric_col])
    ]

    return {
        "tool": "compare_segments",
        "df_name": df_name,
        "final_df": result,
        "chart_data": chart_data,
        "metrics": {
            "segment_count": len(result),
            "top_segment": str(result.iloc[0][segment_col]) if len(result) else None,
            "top_value": safe_float(result.iloc[0][metric_col]) if len(result) else None,
            "total": safe_float(result[metric_col].sum()),
        }
    }

# ==============================================================================
# 5. PLANNER — FIX 4: Strategist not Classifier + FIX 1: Email State Awareness
# ==============================================================================

def detect_user_requirements(question):
    q = (question or "").lower()
    wants_chart = any(term in q for term in ['chart', 'graph', 'bar', 'line', 'pie', 'doughnut', 'plot', 'visual', 'grafica', 'grafico'])
    wants_table = any(term in q for term in ['table', 'breakdown', 'compare', 'comparison', 'list', 'ranking', 'rank', 'top', 'worst', 'best', 'tabla', 'desglose'])
    wants_explanation = any(term in q for term in [
        'explain', 'analysis', 'analisis', 'reasoning', 'detail', 'detailed', 'context',
        'summary', 'interpret', 'interpretation', 'insight', 'insights', 'why', 'how',
        'report', 'presentation', 'professional summary', 'professional', 'executive',
        'boardroom', 'code explanation', 'explain code', 'resumen', 'explica', 'detalle'
    ])
    requested_chart_type = 'none'
    if 'doughnut' in q or 'donut' in q or 'pie' in q:
        requested_chart_type = 'doughnut'
    elif 'line' in q or 'trend' in q or 'tendencia' in q:
        requested_chart_type = 'line'
    elif 'bar' in q or 'graph' in q or 'chart' in q or 'grafica' in q or 'grafico' in q:
        requested_chart_type = 'bar'
    return {
        "wants_chart": wants_chart,
        "wants_table": wants_table,
        "wants_explanation": wants_explanation,
        "requested_chart_type": requested_chart_type,
        "question_lower": q
    }

def build_rich_history_context(history):
    if not history:
        return "No prior conversation."
    lines = []
    for msg in history[-12:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"USER: {content}")
        elif role == "assistant":
            if isinstance(content, dict):
                lines.append(f"SASHA RESULT: analysis_type={content.get('analysis_type','?')} | "
                             f"headline={content.get('headline','?')} | "
                             f"metrics={json.dumps(content.get('metrics',{}))}")
            else:
                lines.append(f"SASHA: {content}")
    return "\n".join(lines)

def heuristic_plan(question, requirements):
    q = requirements["question_lower"]
    analysis_type = "generic_python"

    if 'portfolio' in q and ('health' in q or 'review' in q or 'salud' in q):
        analysis_type = "portfolio_health_review"
    elif ('best' in q and 'worst' in q) or ('top 10' in q and 'worst' in q) or ('mejor' in q and 'peor' in q):
        analysis_type = "best_vs_worst_clients"
    elif 'risky client' in q or 'risky clients' in q or ('risk' in q and 'client' in q) or ('riesgo' in q and 'client' in q):
        analysis_type = "risky_clients"
    elif 'top client' in q or ('best client' in q and 'worst' not in q):
        analysis_type = "top_clients"
    elif 'worst client' in q or 'bottom client' in q or 'peor cliente' in q:
        analysis_type = "bottom_clients"
    elif 'explain code' in q or 'code explanation' in q:
        analysis_type = "code_explanation"
    elif any(x in q for x in ['by region', 'by product', 'by category', 'by month', 'by year',
                                'por region', 'por producto', 'por categoria', 'por mes', 'por año',
                                'group by', 'agrupar', 'breakdown by', 'desglose por']):
        analysis_type = "generic_groupby"
    elif any(x in q for x in ['top ', 'bottom ', 'top 5', 'top 10', 'worst 5', 'best 5']):
        analysis_type = "generic_top_bottom"
    elif any(x in q for x in ['above', 'below', 'greater than', 'less than', 'over', 'under',
                                'mayor que', 'menor que', 'encima de', 'debajo de']):
        analysis_type = "generic_filter"

    deterministic_types = [
        "risky_clients", "top_clients", "bottom_clients", "best_vs_worst_clients",
        "portfolio_health_review", "generic_groupby", "generic_top_bottom", "generic_filter"
    ]

    return {
        "analysis_type": analysis_type,
        "wants_chart": requirements["wants_chart"],
        "wants_table": requirements["wants_table"],
        "wants_explanation": requirements["wants_explanation"],
        "requested_chart_type": requirements["requested_chart_type"],
        "boardroom_style": any(x in q for x in ['executive', 'boardroom', 'professional', 'portfolio health review', 'presentation', 'ejecutivo', 'directivo']),
        "deterministic_first": analysis_type in deterministic_types,
        "user_intent_summary": question,
        "tool_params": {},
        # FIX 4: Strategist adds an explicit ordered action plan
        "action_plan": [{"step": 1, "action": "analyze", "description": "Execute analysis and return result"}]
    }

def planner_map_intent(question, history, role="Viewer"):
    """
    FIX 4: The Planner is now a Strategist, not just a Classifier.
    It produces an explicit ordered action_plan so Sasha knows WHAT to do
    and IN WHAT ORDER — not just which route to take.

    FIX 1 (Email awareness): The planner receives the active_workspace_state
    summary from the session so it can resolve "email that" without re-asking.
    """
    requirements = detect_user_requirements(question)
    now = datetime.now().strftime("%A, %B %d, %Y - %H:%M:%S")
    # FIX 2: Only load RELEVANT memory, not the full junk drawer
    relevant_memory = read_relevant_memory(question)
    schema_info = build_schema_info()
    rich_history = build_rich_history_context(history)

    system_prompt = f"""
You are Sasha's planning layer. You are a STRATEGIST, not a classifier.
Your job is to produce an ordered action plan — not just pick a route.

TIME: {now}
USER ROLE: {role}
DATA SCHEMA: {schema_info}
CONVERSATION CONTEXT: {rich_history}
RELEVANT RULES (only the most relevant from memory): {relevant_memory}

RULES:
- If the user says "email this", "send that", "share these results", or refers to the previous analysis,
  set analysis_type to "send_email" and extract recipient_email if visible in the message.
- For ANY multi-step request (e.g. "find risky clients and email them to Carlos"), produce a multi-step action_plan.
- action_plan steps are executed IN ORDER by the orchestrator.

Return STRICT JSON with exactly these keys:
{{
  "analysis_type": "send_email | risky_clients | top_clients | bottom_clients | best_vs_worst_clients | portfolio_health_review | generic_groupby | generic_top_bottom | generic_filter | code_explanation | generic_python",
  "wants_chart": true,
  "wants_table": true,
  "wants_explanation": true,
  "requested_chart_type": "bar | line | doughnut | none",
  "boardroom_style": false,
  "deterministic_first": true,
  "user_intent_summary": "...",
  "action_plan": [
    {{"step": 1, "action": "analyze", "description": "Calculate top risky clients above threshold 70"}},
    {{"step": 2, "action": "verify_email", "description": "Confirm recipient email address"}},
    {{"step": 3, "action": "send_email", "description": "Package and send results to recipient"}}
  ],
  "tool_params": {{
    "recipient_email": "...",
    "df_name": "...",
    "groupby_col": "...",
    "metric_col": "...",
    "agg_func": "sum | mean | count | max | min",
    "filter_col": "...",
    "operator": "> | >= | < | <= | ==",
    "threshold": 0,
    "limit": 10,
    "sort_ascending": false,
    "label_col": "...",
    "bottom": false
  }}
}}
"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-8:]:
        messages.append({"role": msg["role"], "content": msg["content"] if isinstance(msg["content"], str) else json.dumps(msg["content"])})
    messages.append({"role": "user", "content": question})

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0
        )
        plan = json.loads(response.choices[0].message.content)
        fallback = heuristic_plan(question, requirements)
        for k, v in fallback.items():
            plan.setdefault(k, v)
        if "tool_params" not in plan:
            plan["tool_params"] = {}
        if "action_plan" not in plan:
            plan["action_plan"] = [{"step": 1, "action": "analyze", "description": plan.get("user_intent_summary", question)}]
        return plan
    except:
        return heuristic_plan(question, requirements)

# ==============================================================================
# 6. DETERMINISTIC ENGINE — routes to tool library + legacy business analyses
# ==============================================================================

def compute_risky_clients(plan, question):
    df_name = choose_best_dataframe(
        required_groups=[['risk']],
        optional_groups=[['client'], ['business'], ['paid'], ['credit'], ['capital'], ['amount']]
    )
    if not df_name:
        return None

    df = dfs[df_name].copy()
    risk_col = pick_first_existing(df, [['risk_score'], ['risk'], ['score']], prefer_numeric=True)
    client_id_col = pick_first_existing(df, [['client_id'], ['client id'], ['customer_id'], ['customer id'], ['id_cliente']], prefer_numeric=True)
    business_name_col = pick_first_existing(df, [['business_name'], ['business name'], ['client_name'], ['client name'], ['customer_name'], ['customer name'], ['razon social'], ['nombre negocio']], prefer_numeric=False)
    total_paid_col = pick_first_existing(df, [['total_paid'], ['paid'], ['payment'], ['payments'], ['pagado'], ['cobrado']], prefer_numeric=True)
    credit_id_col = pick_first_existing(df, [['credit_id'], ['credit id'], ['loan_id'], ['loan id'], ['credito_id'], ['credito id']], prefer_numeric=True)
    exposure_col = pick_first_existing(df, [['capital'], ['principal'], ['amount'], ['credit_amount'], ['loan_amount'], ['monto']], prefer_numeric=True)

    if not risk_col:
        return None

    use_cols = [c for c in [client_id_col, business_name_col, risk_col, total_paid_col, credit_id_col, exposure_col] if c]
    work = df[use_cols].copy()
    num_cols = [c for c in [risk_col, total_paid_col, exposure_col] if c]
    work = coerce_numeric(work, num_cols)
    work = work.dropna(subset=[risk_col])

    q = (question or "").lower()
    threshold = 70.0
    if work[risk_col].dropna().max() > 100:
        threshold = float(work[risk_col].quantile(0.75))

    explicit = re.search(r'(above|over|greater than|>=|>|threshold|encima|mayor)\s*(\d+)', q)
    if explicit:
        try:
            threshold = float(explicit.group(2))
        except:
            pass

    risky = work[work[risk_col] >= threshold].copy()
    risky = risky.sort_values(by=risk_col, ascending=False)

    rename_map = {risk_col: 'risk_score'}
    if client_id_col: rename_map[client_id_col] = 'client_id'
    if business_name_col: rename_map[business_name_col] = 'business_name'
    if total_paid_col: rename_map[total_paid_col] = 'total_paid'
    if credit_id_col: rename_map[credit_id_col] = 'credit_id'
    if exposure_col: rename_map[exposure_col] = 'capital_exposure'

    risky = risky.rename(columns=rename_map)
    if 'client_id' not in risky.columns: risky['client_id'] = range(1, len(risky) + 1)
    if 'business_name' not in risky.columns: risky['business_name'] = risky['client_id'].apply(lambda x: f"Client {x}")
    if 'total_paid' not in risky.columns: risky['total_paid'] = 0.0

    table_cols = [c for c in ['client_id', 'business_name', 'risk_score', 'total_paid', 'credit_id', 'capital_exposure'] if c in risky.columns]
    final_df = risky[table_cols].head(50).copy()

    chart_source = final_df[['business_name', 'risk_score']].head(15).copy()

    return {
        "engine": "deterministic",
        "analysis_type": "risky_clients",
        "df_name": df_name,
        "metrics": {"threshold": threshold, "risky_count": int(len(risky)), "max_risk_score": safe_float(risky['risk_score'].max()) if len(risky) else None},
        "final_df": final_df,
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": [{"label": str(r['business_name']), "value": float(r['risk_score'])} for _, r in chart_source.iterrows()],
        "business_payload": {"summary_title": "Risky clients review", "threshold_used": threshold, "risky_count": int(len(risky)), "top_rows": serialize_for_prompt(final_df.head(10))}
    }

def compute_top_clients(plan, question, bottom=False):
    df_name = choose_best_dataframe(required_groups=[['client'], ['paid']], optional_groups=[['business'], ['customer']])
    if not df_name: return None
    agg = aggregate_clients_frame(dfs[df_name])
    if agg is None: return None

    ranked = agg.sort_values(by='total_paid', ascending=bottom).head(10).copy()
    ranked['segment'] = 'Worst Clients' if bottom else 'Best Clients'

    return {
        "engine": "deterministic",
        "analysis_type": "bottom_clients" if bottom else "top_clients",
        "df_name": df_name,
        "metrics": {"client_count": int(len(agg)), "selected_total": safe_float(ranked['total_paid'].sum()) if len(ranked) else 0.0},
        "final_df": ranked[['segment', 'client_id', 'business_name', 'total_paid']],
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": [{"label": str(r['business_name']), "value": float(r['total_paid'])} for _, r in ranked.iterrows()],
        "business_payload": {"summary_title": "Top clients review" if not bottom else "Worst clients review", "top_rows": serialize_for_prompt(ranked)}
    }

def compute_best_vs_worst_clients(plan, question):
    df_name = choose_best_dataframe(required_groups=[['client'], ['paid']], optional_groups=[['business'], ['customer']])
    if not df_name: return None
    agg = aggregate_clients_frame(dfs[df_name])
    if agg is None: return None

    best = agg.sort_values(by='total_paid', ascending=False).head(10).copy()
    worst = agg.sort_values(by='total_paid', ascending=True).head(10).copy()
    best['segment'] = 'Best Clients'
    worst['segment'] = 'Worst Clients'

    combined = pd.concat([best, worst], ignore_index=True)
    combined['label'] = combined.apply(lambda row: f"{'Best' if row['segment'] == 'Best Clients' else 'Worst'} - {row['business_name']}", axis=1)
    combined['value'] = combined['total_paid'].astype(float)

    return {
        "engine": "deterministic",
        "analysis_type": "best_vs_worst_clients",
        "df_name": df_name,
        "metrics": {"best_total": safe_float(best['total_paid'].sum()), "worst_total": safe_float(worst['total_paid'].sum()), "gap": safe_float(best['total_paid'].sum() - worst['total_paid'].sum())},
        "final_df": combined[['segment', 'client_id', 'business_name', 'total_paid']].copy(),
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": combined[['label', 'value']].to_dict('records'),
        "business_payload": {"summary_title": "Best vs worst clients comparison", "best_rows": serialize_for_prompt(best), "worst_rows": serialize_for_prompt(worst)}
    }

def compute_portfolio_health_review(plan, question):
    df_name = choose_best_dataframe(required_groups=[['client'], ['paid']], optional_groups=[['business'], ['risk'], ['capital'], ['amount'], ['credit']])
    if not df_name: return None
    base_df = dfs[df_name].copy()
    agg = aggregate_clients_frame(base_df)
    if agg is None: return None

    best = agg.sort_values(by='total_paid', ascending=False).head(10).copy()
    worst = agg.sort_values(by='total_paid', ascending=True).head(10).copy()
    total_portfolio_paid = safe_float(agg['total_paid'].sum()) if len(agg) else 0.0
    best_total = safe_float(best['total_paid'].sum()) if len(best) else 0.0
    worst_total = safe_float(worst['total_paid'].sum()) if len(worst) else 0.0
    concentration_pct = round((best_total / total_portfolio_paid) * 100, 2) if total_portfolio_paid else 0.0

    risk_summary = None
    risky = compute_risky_clients({"requested_chart_type": "bar"}, "detect risky clients")
    if risky and risky.get("final_df") is not None and not risky["final_df"].empty:
        risk_summary = risky["final_df"].head(5).copy()

    best['category'] = 'Best Clients'
    worst['category'] = 'Worst Clients'
    frames = [best[['category', 'client_id', 'business_name', 'total_paid']], worst[['category', 'client_id', 'business_name', 'total_paid']]]
    if risk_summary is not None:
        temp = risk_summary.copy()
        temp['category'] = 'Biggest Risks'
        keep_cols = [c for c in ['category', 'client_id', 'business_name', 'total_paid', 'risk_score'] if c in temp.columns]
        frames.append(temp[keep_cols])
    opportunities = best.head(5).copy()
    opportunities['category'] = 'Biggest Opportunities'
    frames.append(opportunities[['category', 'client_id', 'business_name', 'total_paid']])

    final_df = pd.concat(frames, ignore_index=True, sort=False)
    chart_rows = best.head(10).copy()

    badge = 'Green'
    if concentration_pct >= 70: badge = 'Red'
    elif concentration_pct >= 45: badge = 'Yellow'

    return {
        "engine": "deterministic",
        "analysis_type": "portfolio_health_review",
        "df_name": df_name,
        "metrics": {"client_count": int(len(agg)), "portfolio_total_paid": total_portfolio_paid, "best_total": best_total, "worst_total": worst_total, "payment_concentration_pct_top10": concentration_pct},
        "final_df": final_df,
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": [{"label": str(r['business_name']), "value": float(r['total_paid'])} for _, r in chart_rows.iterrows()],
        "health_badge": badge,
        "business_payload": {"summary_title": "Executive portfolio health review", "top10_share_pct": concentration_pct, "best_rows": serialize_for_prompt(best), "worst_rows": serialize_for_prompt(worst), "risk_rows": serialize_for_prompt(risk_summary) if risk_summary is not None else [], "opportunity_rows": serialize_for_prompt(opportunities)}
    }

def compute_generic_groupby(plan, question):
    tp = plan.get("tool_params", {})
    df_name = tp.get("df_name") or choose_best_dataframe()
    if not df_name or df_name not in dfs:
        return None

    df = dfs[df_name]
    groupby_col = tp.get("groupby_col")
    metric_col = tp.get("metric_col")
    agg_func = tp.get("agg_func", "sum")
    limit = tp.get("limit", 20)
    sort_ascending = tp.get("sort_ascending", False)
    label_col = tp.get("label_col")

    if not groupby_col or not metric_col:
        groupby_col, metric_col = _infer_groupby_cols(df, question)
    if not groupby_col or not metric_col:
        return None

    result = tool_groupby(df_name, groupby_col, metric_col, agg_func, sort_ascending, limit, label_col)
    if result is None:
        return None

    return {
        "engine": "deterministic",
        "analysis_type": "generic_groupby",
        "df_name": df_name,
        "metrics": result["metrics"],
        "final_df": result["final_df"],
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": result["chart_data"],
        "business_payload": {
            "summary_title": f"{agg_func.title()} of {metric_col} by {groupby_col}",
            "top_rows": serialize_for_prompt(result["final_df"].head(10)),
            "groupby_col": groupby_col,
            "metric_col": metric_col,
            "agg_func": agg_func,
        }
    }

def compute_generic_top_bottom(plan, question):
    tp = plan.get("tool_params", {})
    df_name = tp.get("df_name") or choose_best_dataframe()
    if not df_name or df_name not in dfs:
        return None

    df = dfs[df_name]
    metric_col = tp.get("metric_col")
    limit = tp.get("limit", 10)
    bottom = tp.get("bottom", False)
    label_col = tp.get("label_col")

    if not metric_col:
        for col in df.columns:
            if is_numeric_series(df[col]):
                metric_col = col
                break
    if not metric_col:
        return None

    if not label_col:
        for col in df.columns:
            if not is_numeric_series(df[col]):
                label_col = col
                break

    result = tool_top_bottom(df_name, metric_col, limit, bottom, label_col)
    if result is None:
        return None

    return {
        "engine": "deterministic",
        "analysis_type": "generic_top_bottom",
        "df_name": df_name,
        "metrics": result["metrics"],
        "final_df": result["final_df"],
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": result["chart_data"],
        "business_payload": {
            "summary_title": f"{'Bottom' if bottom else 'Top'} {limit} by {metric_col}",
            "top_rows": serialize_for_prompt(result["final_df"].head(10)),
        }
    }

def compute_generic_filter(plan, question):
    tp = plan.get("tool_params", {})
    df_name = tp.get("df_name") or choose_best_dataframe()
    if not df_name or df_name not in dfs:
        return None

    df = dfs[df_name]
    filter_col = tp.get("filter_col")
    operator = tp.get("operator", ">=")
    threshold = tp.get("threshold", 0)
    limit = tp.get("limit", 50)
    label_col = tp.get("label_col")

    if not filter_col:
        for col in df.columns:
            if is_numeric_series(df[col]):
                filter_col = col
                break
    if not filter_col:
        return None

    result = tool_filter_rank(df_name, filter_col, operator, threshold, label_col=label_col, limit=limit)
    if result is None:
        return None

    return {
        "engine": "deterministic",
        "analysis_type": "generic_filter",
        "df_name": df_name,
        "metrics": result["metrics"],
        "final_df": result["final_df"],
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": result["chart_data"],
        "business_payload": {
            "summary_title": f"Filtered: {filter_col} {operator} {threshold}",
            "top_rows": serialize_for_prompt(result["final_df"].head(10)),
        }
    }

def _infer_groupby_cols(df, question):
    schema = f"Columns: {', '.join(df.columns)}"
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"Given this dataframe schema: {schema}\nReturn JSON: {{\"groupby_col\": \"...\", \"metric_col\": \"...\"}}. Pick the most appropriate groupby column and numeric metric column to answer the user's question. Only return column names that exist exactly in the schema."},
                {"role": "user", "content": question}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        result = json.loads(response.choices[0].message.content)
        gc = result.get("groupby_col")
        mc = result.get("metric_col")
        if gc in df.columns and mc in df.columns:
            return gc, mc
    except:
        pass
    return None, None

def deterministic_engine(plan, question):
    analysis_type = plan.get("analysis_type", "generic_python")

    if analysis_type == "risky_clients":
        return compute_risky_clients(plan, question)
    if analysis_type == "top_clients":
        return compute_top_clients(plan, question, bottom=False)
    if analysis_type == "bottom_clients":
        return compute_top_clients(plan, question, bottom=True)
    if analysis_type == "best_vs_worst_clients":
        return compute_best_vs_worst_clients(plan, question)
    if analysis_type == "portfolio_health_review":
        return compute_portfolio_health_review(plan, question)
    if analysis_type == "generic_groupby":
        return compute_generic_groupby(plan, question)
    if analysis_type == "generic_top_bottom":
        return compute_generic_top_bottom(plan, question)
    if analysis_type == "generic_filter":
        return compute_generic_filter(plan, question)

    return None

# ==============================================================================
# 7. NARRATION LAYER — FIX 3: Inner Monologue (Draft → Critique → Final)
# ==============================================================================

def build_fallback_narrative(plan, computed, question):
    analysis_type = computed.get("analysis_type", "analysis")
    metrics = computed.get("metrics", {})
    headline = "Analysis completed."
    insight = "The requested analysis was completed successfully."
    badge = computed.get("health_badge", "None")

    if analysis_type == "risky_clients":
        threshold = metrics.get("threshold", 0)
        risky_count = metrics.get("risky_count", 0)
        headline = f"{risky_count} high-risk client records flagged above threshold {threshold:.2f}."
        insight = (f"This review flags {risky_count} client records whose risk score meets or exceeds {threshold:.2f}."
                   f"The output is intended to help collections, credit, or portfolio teams prioritize cases requiring attention.")
        badge = "Red"
    elif analysis_type == "top_clients":
        total = metrics.get("selected_total", 0)
        headline = f"Top clients identified — contributing {total:,.2f} in total payments."
        insight = f"These are your strongest client contributors by total paid amount, collectively representing {total:,.2f}."
        badge = "Green"
    elif analysis_type == "bottom_clients":
        total = metrics.get("selected_total", 0)
        headline = f"Lowest-performing clients identified — only {total:,.2f} in total payments."
        insight = f"These clients represent the weakest payment contributors, with a combined total of only {total:,.2f}."
        badge = "Yellow"
    elif analysis_type == "best_vs_worst_clients":
        best_total = metrics.get("best_total", 0)
        worst_total = metrics.get("worst_total", 0)
        gap = metrics.get("gap", 0)
        headline = f"Top 10 vs bottom 10 gap: {gap:,.2f} — concentration risk is real."
        insight = (f"Best clients contribute {best_total:,.2f} vs worst clients at {worst_total:,.2f}, "
                   f"a gap of {gap:,.2f}. The portfolio is heavily concentrated in a small group.")
        badge = "Yellow"
    elif analysis_type == "portfolio_health_review":
        conc = metrics.get("payment_concentration_pct_top10", 0)
        total = metrics.get("portfolio_total_paid", 0)
        headline = f"Portfolio health review: {total:,.2f} total, top 10 clients hold {conc:.1f}%."
        insight = f"Total realized payments: {total:,.2f}. Top 10 client concentration: {conc:.1f}%."
        badge = "Red" if conc >= 70 else ("Yellow" if conc >= 45 else "Green")
    elif analysis_type in ("generic_groupby", "generic_top_bottom", "generic_filter"):
        top_label = metrics.get("top_label") or metrics.get("top_segment")
        top_value = metrics.get("top_value")
        total = metrics.get("total") or metrics.get("selected_total")
        title = computed.get("business_payload", {}).get("summary_title", "Analysis")
        headline = f"{title} — top result: {top_label} at {top_value:,.2f}." if top_label and top_value else f"{title} completed."
        insight = f"The analysis identified {top_label} as the leading category with a value of {top_value:,.2f}." if top_label and top_value else "Analysis completed successfully."
        badge = "Green"

    return {
        "headline": headline,
        "sasha_insight": insight,
        "health_badge": badge,
        "suggestions": ["Show me the next level of detail behind these results.", "Compare this result against a previous period.", "Turn these findings into recommended actions."]
    }

def narrate_and_format(plan, computed, question, history, role="Viewer"):
    """
    FIX 3: INNER MONOLOGUE — Before producing the final narrative, Sasha goes
    through a hidden Draft → Critique → Final loop. This eliminates hallucinations
    and generic output because Sasha must fact-check herself against the actual
    computed numbers before speaking. The user never sees this reasoning —
    they only see the polished final result.
    """
    now = datetime.now().strftime("%A, %B %d, %Y - %H:%M:%S")
    # FIX 2: Only relevant memory in the narration context too
    relevant_memory = read_relevant_memory(question)
    rich_history = build_rich_history_context(history)

    # ---- STEP 1: DRAFT (initial narrative) ----
    draft_prompt = f"""
You are Sasha's narration layer. Write a FIRST DRAFT of the business intelligence narrative.
TIME: {now}
USER ROLE: {role}
RELEVANT RULES: {relevant_memory}
CONVERSATION CONTEXT: {rich_history}

Data payload:
{json.dumps({
    "question": question,
    "analysis_type": computed.get("analysis_type"),
    "metrics": serialize_for_prompt(computed.get("metrics")),
    "business_payload": serialize_for_prompt(computed.get("business_payload")),
    "sample_table": serialize_for_prompt(computed.get("final_df"), max_rows=10)
}, ensure_ascii=False)}

Return JSON: {{"draft_headline": "...", "draft_insight": "...", "draft_badge": "Red|Yellow|Green|None", "draft_suggestions": ["...","...","..."]}}
"""

    # ---- STEP 2: CRITIQUE (self-check draft against real numbers) ----
    critique_prompt = f"""
You are Sasha's internal critic. You receive a DRAFT narrative and the ACTUAL computed data.
Your job: check if every number in the draft matches the actual data. Flag any hallucination or generic filler.

ACTUAL METRICS: {json.dumps(serialize_for_prompt(computed.get("metrics")), ensure_ascii=False)}
ACTUAL SAMPLE DATA: {json.dumps(serialize_for_prompt(computed.get("final_df"), max_rows=5), ensure_ascii=False)}

{{DRAFT}}

Return JSON: {{"critique": "short critique text", "needs_revision": true_or_false, "issues": ["list of specific issues found"]}}
"""

    # ---- STEP 3: FINAL (produce polished output after self-correction) ----
    final_system_prompt = f"""
You are Sasha's narration and formatting layer. You are writing executive-grade business intelligence copy.
You have already DRAFTED and CRITIQUED your narrative internally. Now produce the FINAL, corrected version.

TIME: {now}
USER ROLE: {role}
RELEVANT MEMORY RULES: {relevant_memory}

CONVERSATION CONTEXT:
{rich_history}

RULES:
- NEVER use generic filler. Every sentence must reference the actual data numbers.
- If boardroom_style=true, write like a McKinsey slide deck narrator.
- For portfolio health: discuss concentration risk, tail risk, and strategic opportunities.
- For risk analysis: explain threshold rationale and operational implications.
- For groupby/filter/top-bottom: contextualize what the numbers mean for the business.
- Always respond in the user's language naturally.
- The sasha_insight must be at least 3 sentences and reference at least 2 specific numbers from the results.
- Sound like a CFO with a sense of humor — reference business implications, not just raw numbers.

Return STRICT JSON:
{{
  "headline": "...",
  "sasha_insight": "...",
  "health_badge": "Red | Yellow | Green | None",
  "suggestions": ["...", "...", "..."]
}}
"""

    payload = {
        "question": question,
        "plan": serialize_for_prompt(plan),
        "boardroom_style": plan.get("boardroom_style", False),
        "computed": {
            "analysis_type": computed.get("analysis_type"),
            "metrics": serialize_for_prompt(computed.get("metrics")),
            "business_payload": serialize_for_prompt(computed.get("business_payload")),
            "sample_table": serialize_for_prompt(computed.get("final_df"), max_rows=10)
        }
    }

    try:
        # DRAFT
        draft_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": draft_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
            response_format={"type": "json_object"},
            temperature=0.3
        )
        draft = json.loads(draft_response.choices[0].message.content)

        # CRITIQUE — inject draft into the critique prompt
        critique_with_draft = critique_prompt.replace("{DRAFT}", json.dumps(draft, ensure_ascii=False))
        critique_response = client.chat.completions.create(
            model="gpt-4o-mini",  # cheaper model is fine for self-critique
            messages=[
                {"role": "system", "content": critique_with_draft},
                {"role": "user", "content": "Critique this draft narrative."}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        critique = json.loads(critique_response.choices[0].message.content)

        # FINAL — produce corrected narrative, passing in the draft and critique
        final_payload = {
            **payload,
            "draft": draft,
            "critique": critique
        }

        final_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": final_system_prompt},
                {"role": "user", "content": json.dumps(final_payload, ensure_ascii=False)}
            ],
            response_format={"type": "json_object"},
            temperature=0.2
        )
        narrated = json.loads(final_response.choices[0].message.content)
        fallback = build_fallback_narrative(plan, computed, question)
        for k, v in fallback.items():
            narrated.setdefault(k, v)
        return narrated
    except:
        return build_fallback_narrative(plan, computed, question)

# ==============================================================================
# 8. LEGACY FALLBACK — Python generation
# ==============================================================================

def execute_agent_code(code_str, role="Viewer"):
    if re.search(r"import\s+(os|sys|subprocess|shutil|pathlib)", code_str):
        return None, None, None, None, None, None, None, "Security Violation: System imports blocked."

    forbidden_patterns = [r"open\s*\(", r"__import__", r"exec\s*\(", r"eval\s*\(", r"globals\s*\(", r"locals\s*\(", r"compile\s*\(", r"while\s+True\b"]
    for pattern in forbidden_patterns:
        if re.search(pattern, code_str):
            return None, None, None, None, None, None, None, "Security Violation: Unsafe code detected."

    local_env = {'dfs': dfs, 'pd': pd, 'datetime': datetime}
    if role in ['Admin', 'Collaborator']:
        local_env['save_to_memory'] = save_to_memory

    try:
        exec_globals = {
            "__builtins__": {"len": len, "sum": sum, "min": min, "max": max, "round": round, "str": str, "float": float, "int": int, "abs": abs, "sorted": sorted, "range": range, "__import__": __import__},
            "pd": pd,
            "datetime": datetime
        }
        exec(code_str, exec_globals, local_env)
        return (
            local_env.get('final_df'),
            local_env.get('headline'),
            local_env.get('chart_type', 'none'),
            local_env.get('chart_data', []),
            local_env.get('sasha_insight', ''),
            local_env.get('health_badge', 'None'),
            local_env.get('suggestions', []),
            None
        )
    except Exception:
        return None, None, None, None, None, None, None, traceback.format_exc()

def legacy_python_generation(question, history, role="Viewer"):
    now = datetime.now()
    current_time_str = now.strftime("%A, %B %d, %Y - %H:%M:%S")
    schema_info = build_schema_info()
    # FIX 2: Use semantic memory here too
    relevant_memory = read_relevant_memory(question)
    rich_history = build_rich_history_context(history)

    system_prompt = f"""
You are 'Sasha', an elite Python Data Agent and Enterprise Analyst.
TIME: {current_time_str}. DATA: {schema_info}
USER ROLE: {role}.
RELEVANT MEMORY (COMPANY RULES — only the rules most relevant to this question): {relevant_memory}

CONVERSATION CONTEXT (critical for follow-up questions):
{rich_history}

MISSION: Write a Python script to answer the user. Use conversation context to understand what "that", "it", "those" refer to.
PERMISSION: {f"You can save rules using save_to_memory('rule')" if role in ['Admin', 'Collaborator'] else "VIEWER ONLY: You cannot save or edit memory."}

OUTPUT: STRICT JSON: {{ "thought": "...", "python_code": "..." }}
RULES: Assign these variables in your Python code:
`final_df`: The pandas dataframe to show.
`headline`: A punchy, specific string summary that references actual numbers.
`chart_type`: 'line', 'bar', 'doughnut', or 'none'.
`chart_data`: A list of dicts with exactly {{"label": "...", "value": 123.45}} shape.
`sasha_insight`: A detailed explanation. If the user asks for explanation/analysis/detail/professional/executive, this MUST be substantial (3+ sentences with specific numbers).
`health_badge`: 'Red', 'Yellow', 'Green', or 'None'.
`suggestions`: A Python list of 3 strings — smart, contextual follow-up questions.

CRITICAL FORMAT RULE:
- If they ask for a chart and data has 2+ points, chart_type MUST NOT be 'none'.
- If they ask for a table, final_df MUST be populated.
- If they ask for explanation/detail, sasha_insight MUST be populated (3+ sentences).
- chart_data must be a list of {{"label": "...", "value": 123.45}} dicts.
- headline and sasha_insight must reference SPECIFIC numbers from the data, never generic filler.
- If answering a greeting or non-data question, use headline to respond naturally.

CRITICAL LANGUAGE RULE: Always respond in the user's language natively, keep JSON keys and Python variable names in English.
NEVER use destructive commands. Fuzzy search strings.
"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"] if isinstance(msg["content"], str) else json.dumps(msg["content"])})
    messages.append({"role": "user", "content": question})

    requirements = detect_user_requirements(question)

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0
            )
            ai_msg = json.loads(response.choices[0].message.content)
            df, headline, c_type, c_data, insight, badge, suggs, error = execute_agent_code(ai_msg['python_code'], role)

            if error:
                messages.append({"role": "assistant", "content": ai_msg['python_code']})
                messages.append({"role": "user", "content": f"Code failed: {error}. Fix and retry."})
                continue

            result = package_result(df=df, headline=headline, c_type=c_type, c_data=c_data, insight=insight, badge=badge, suggs=suggs, python_code=ai_msg['python_code'])
            critic = critic_validate(question, requirements, result)
            if not critic["passed"]:
                messages.append({"role": "assistant", "content": ai_msg['python_code']})
                messages.append({"role": "user", "content": critic["repair_prompt"]})
                continue

            return result

        except Exception as e:
            return {"error": str(e)}

    return {"error": "Sasha failed to resolve the code after 3 attempts."}

# ==============================================================================
# 9. CRITIC
# ==============================================================================

def sanitize_chart_data(c_data):
    try:
        if isinstance(c_data, pd.Series):
            c_data = [{"label": str(k), "value": float(v)} for k, v in c_data.items()]
        elif isinstance(c_data, pd.DataFrame):
            cols = c_data.columns
            if len(cols) >= 2:
                c_data = [{"label": str(row[cols[0]]), "value": float(row[cols[1]])} for _, row in c_data.iterrows()]
            else:
                c_data = [{"label": str(i), "value": float(row[cols[0]])} for i, row in c_data.iterrows()]

        if isinstance(c_data, list):
            clean_data = []
            for item in c_data:
                if isinstance(item, dict):
                    label = item.get('label')
                    value = item.get('value')
                    if hasattr(label, 'item'): label = label.item()
                    if hasattr(value, 'item'): value = value.item()
                    try:
                        value = float(value.replace(',', '').strip()) if isinstance(value, str) else float(value)
                    except:
                        continue
                    if pd.notnull(label) and pd.notnull(value):
                        clean_data.append({"label": str(label), "value": value})
            c_data = clean_data
    except Exception:
        c_data = []
    return c_data

def style_dataframe(df):
    styled_table = ""
    if df is not None:
        df = df.copy()
        for col in df.select_dtypes(include=['datetime64', 'object']):
            try:
                df[col] = pd.to_datetime(df[col]).dt.strftime('%Y-%m-%d')
            except:
                pass
        for col in df.select_dtypes(include=['number']).columns:
            df[col] = df[col].apply(lambda x: f"{x:,.2f}" if pd.notnull(x) else x)
        styled_table = df.to_html(classes='sasha-table', index=False, border=0)
    return styled_table

def package_result(df, headline, c_type, c_data, insight, badge, suggs, python_code):
    styled_table = style_dataframe(df)
    c_data = sanitize_chart_data(c_data)
    if not c_data or len(c_data) < 2:
        c_type = 'none'
    return {
        "headline": headline if headline else "Got it. / ¡Entendido!",
        "python_code": python_code,
        "html_table": styled_table,
        "chart_intent": c_type,
        "raw_data": c_data,
        "sasha_insight": insight if insight else "",
        "health_badge": badge if badge else "None",
        "suggestions": suggs if suggs else []
    }

def critic_validate(question, requirements, result):
    if requirements["wants_chart"]:
        if result.get("chart_intent") == 'none' or not result.get("raw_data") or len(result.get("raw_data")) < 2:
            return {"passed": False, "repair_prompt": 'You failed the chart requirement. Return valid chart_type plus chart_data: [{"label": "...", "value": 123.45}, ...] with at least 2 points.'}

    if requirements["wants_table"]:
        if not result.get("html_table"):
            return {"passed": False, "repair_prompt": "You failed the table requirement. Populate final_df."}

    if requirements["wants_explanation"]:
        insight = str(result.get("sasha_insight", "")).strip()
        if not insight or len(insight) < 80:
            return {"passed": False, "repair_prompt": "You failed the explanation requirement. Write a substantial sasha_insight (3+ sentences with specific numbers from the data)."}

    headline = str(result.get("headline", "")).strip().lower()
    if headline in ["analysis completed.", "done", "completed", "analysis complete."]:
        return {"passed": False, "repair_prompt": "Your headline is too generic. Rewrite it with specific numbers and business context from the actual results."}

    if requirements["wants_table"] or requirements["wants_chart"]:
        if not any(char.isdigit() for char in result.get("headline", "")):
            return {"passed": False, "repair_prompt": "Your headline lacks specific data. Include at least one concrete number or finding from the results."}

    return {"passed": True, "repair_prompt": ""}

def semantic_critic(question, result):
    try:
        headline = result.get("headline", "")
        chart_preview = str(result.get("raw_data", [])[:3])

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a strict QA reviewer. Answer only YES or NO."},
                {"role": "user", "content": f"User asked: '{question}'\nResult headline: '{headline}'\nChart preview: {chart_preview}\nDoes this result genuinely answer the user's question? YES or NO."}
            ],
            temperature=0.0,
            max_tokens=5
        )
        answer = response.choices[0].message.content.strip().upper()
        return answer.startswith("YES")
    except:
        return True

# ==============================================================================
# 10. ORCHESTRATOR — FIX 1: Working Workspace (State Machine)
# ==============================================================================

def deterministic_result_to_response(computed, narrated):
    final_df = computed.get("final_df")
    chart_data = sanitize_chart_data(computed.get("chart_data", []))
    chart_type = computed.get("chart_type", "none")
    if not chart_data or len(chart_data) < 2:
        chart_type = 'none'

    python_code = f"""# Deterministic Engine Route
# Analysis Type: {computed.get('analysis_type')}
# Source DataFrame: {computed.get('df_name')}
# Produced by Sasha's approved deterministic tool library,
# narrated by the LLM layer, validated by structural + semantic critic.
"""

    return {
        "headline": narrated.get("headline", "Analysis completed."),
        "python_code": python_code,
        "html_table": style_dataframe(final_df),
        "chart_intent": chart_type,
        "raw_data": chart_data,
        "sasha_insight": narrated.get("sasha_insight", ""),
        "health_badge": narrated.get("health_badge", computed.get("health_badge", "None")),
        "suggestions": narrated.get("suggestions", ["Show me the detail.", "Compare to another period.", "Turn this into actions."])
    }

def _get_active_workspace(history):
    """
    FIX 1: Working Workspace / State Machine.
    Scans conversation history backwards to find the most recent assistant
    result that has actual data (html_table or raw_data). This is what
    Sasha "has on her desk" right now — the active workspace state.

    This solves the Contextual Amnesia problem: when the user says
    "email that" or "send this to Carlos", the orchestrator can resolve
    EXACTLY what "that" refers to without re-asking or re-calculating.
    """
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", {})
            if isinstance(content, dict):
                has_table = bool(content.get("html_table"))
                has_chart = bool(content.get("raw_data") and len(content.get("raw_data", [])) > 0)
                if has_table or has_chart:
                    return content
    return None

def agentic_brain(question, history, role="Viewer"):
    requirements = detect_user_requirements(question)

    is_followup = any(word in question.lower() for word in ['that', 'those', 'it', 'them', 'previous', 'last', 'same', 'anterior', 'eso', 'aquello'])
    if not is_followup:
        cached = cache_get(question)
        if cached:
            return cached

    plan = planner_map_intent(question, history, role)

    # FIX 1: Email intent now resolves from the Working Workspace, not from re-asking
    if plan.get("analysis_type") == "send_email":
        recipient = plan.get("tool_params", {}).get("recipient_email")
        if not recipient:
            return {"headline": "Which email address should I send this to?", "suggestions": ["Send it to analytics@company.com"]}

        # Look at the active workspace (the "desk") — what did Sasha just produce?
        active_workspace = _get_active_workspace(history)

        if active_workspace:
            # We have a pinned result on the desk — attach it to the email action
            return {
                "headline": f"Packaging and delivering the latest analysis to {recipient} now.",
                "trigger_email": True,
                "recipient": recipient,
                "html_table": active_workspace.get("html_table", ""),
                "raw_data": active_workspace.get("raw_data", []),
                "chart_intent": active_workspace.get("chart_intent", "none"),
                "sasha_insight": active_workspace.get("sasha_insight", ""),
                "sasha_insight": f"I've retrieved the active workspace result and am dispatching it to {recipient}. The report contains the most recent analysis from our session."
            }
        else:
            return {"headline": "I don't have a report ready yet. What should I analyze first?"}

    if plan.get("deterministic_first", False):
        try:
            computed = deterministic_engine(plan, question)
            if computed is not None:
                narrated = narrate_and_format(plan, computed, question, history, role)
                result = deterministic_result_to_response(computed, narrated)

                critic = critic_validate(question, requirements, result)
                if critic["passed"]:
                    if semantic_critic(question, result):
                        if not is_followup:
                            cache_set(question, result)
                        return result
        except Exception as e:
            print("⚠️ Deterministic engine failed:", str(e))

    result = legacy_python_generation(question, history, role)
    if "error" not in result and not is_followup:
        cache_set(question, result)
    return result

# ==============================================================================
# 11. FLASK ROUTES & GMAIL INTEGRATION
# ==============================================================================

@app.route('/send_report', methods=['POST'])
@login_required
def send_report():
    data = request.json
    recipient = data.get('email')
    headline = data.get('headline', 'Sasha Intelligence Report')
    insight = data.get('sasha_insight', '')
    html_table = data.get('html_table', '')
    raw_data = data.get('raw_data', [])
    chart_type = data.get('chart_intent', 'bar')

    chart_img_url = generate_static_chart_url(chart_type, raw_data)

    subject = f"📊 Sasha Intelligence: {headline}"
    email_body = f"""
    <html>
    <body style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #1e293b; background-color: #f8fafc; padding: 40px;">
        <div style="max-width: 700px; margin: auto; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
            <div style="background-color: #6366f1; padding: 30px; color: white;">
                <h1 style="margin: 0; font-size: 24px; font-weight: 800;">Sasha Enterprise</h1>
                <p style="margin: 5px 0 0 0; opacity: 0.8; font-size: 14px; text-transform: uppercase; letter-spacing: 1px;">Intelligence Layer Distribution</p>
            </div>
            <div style="padding: 40px;">
                <h2 style="font-size: 28px; color: #0f172a; margin-top: 0; line-height: 1.2;">{headline}</h2>
                <div style="background: #f1f5f9; border-left: 4px solid #6366f1; padding: 20px; margin: 30px 0; font-style: italic; color: #334155;">
                    {insight}
                </div>
                {f'<div style="text-align: center; margin-bottom: 30px;"><img src="{chart_img_url}" width="600" style="max-width:100%; border-radius:8px;" /></div>' if chart_img_url else ''}
                <div style="overflow-x: auto; margin-top: 20px;">
                    <style>
                        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
                        th {{ text-align: left; background: #f8fafc; padding: 12px; border-bottom: 2px solid #e2e8f0; }}
                        td {{ padding: 12px; border-bottom: 1px solid #f1f5f9; }}
                    </style>
                    {html_table}
                </div>
            </div>
            <div style="background: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
                Generated by Sasha AI Layer • {datetime.now().strftime('%Y-%m-%d %H:%M')}
            </div>
        </div>
    </body>
    </html>
    """

    msg = Message(subject, recipients=[recipient])
    msg.html = email_body
    try:
        mail.send(msg)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/manifest.json')
def serve_manifest():
    return jsonify({
        "name": "Sasha Enterprise Intelligence",
        "short_name": "Sasha",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#fafafa",
        "theme_color": "#6366f1",
        "icons": [{"src": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Sparkles_emoji_by_Twitter.svg/512px-Sparkles_emoji_by_Twitter.svg.png", "sizes": "512x512", "type": "image/png"}]
    })

@app.route('/sw.js')
def serve_sw():
    return "self.addEventListener('fetch', function(event) {});", 200, {'Content-Type': 'application/javascript'}

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        user = request.form.get('username')
        pwd = request.form.get('password')
        if user in USERS and USERS[user]['pwd'] == pwd:
            session['user'] = user
            session['role'] = USERS[user]['role']
            session['history'] = []
            return redirect(url_for('home'))
        error = "Invalid Credentials"
    return render_template_string(LOGIN_PAGE, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def home():
    session['history'] = []
    return render_template_string(HTML_PAGE)

@app.route('/ask', methods=['POST'])
@login_required
def handle_ask():
    q = request.json.get('question')
    history = session.get('history', [])
    role = session.get('role', 'Viewer')

    response_data = agentic_brain(q, history, role)

    if "error" not in response_data:
        history.append({"role": "user", "content": q})
        history.append({
            "role": "assistant",
            "content": {
                "headline": response_data.get('headline', ''),
                "analysis_type": response_data.get('analysis_type', ''),
                "metrics": {},
                "chart_preview": str(response_data.get('raw_data', [])[:2]),
                "html_table": response_data.get('html_table', ''),
                "sasha_insight": response_data.get('sasha_insight', ''),
                "raw_data": response_data.get('raw_data', []),
                "chart_intent": response_data.get('chart_intent', 'none')
            }
        })
        if len(history) > 30:
            history = history[-30:]
        session['history'] = history

    return jsonify(response_data)

@app.route('/reset', methods=['POST'])
@login_required
def reset_chat():
    session['history'] = []
    return jsonify({"status": "cleared"})

# ==============================================================================
# 12. FRONTEND
# ==============================================================================

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en" class="light">
<head>
    <meta charset="UTF-8"><title>Sasha | Auth</title>
    <meta name="theme-color" content="#6366f1">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            color-scheme: light;
            --login-bg:
                radial-gradient(circle at top, rgba(99, 102, 241, 0.16) 0%, transparent 38%),
                radial-gradient(circle at 85% 15%, rgba(236, 72, 153, 0.12) 0%, transparent 28%),
                radial-gradient(circle at 15% 85%, rgba(14, 165, 233, 0.12) 0%, transparent 24%),
                linear-gradient(180deg, #fcfcff 0%, #f5f7ff 48%, #fafafa 100%);
            --login-card-bg: rgba(255, 255, 255, 0.78);
            --login-card-border: rgba(255, 255, 255, 0.75);
            --login-card-shadow: 0 30px 90px -28px rgba(79, 70, 229, 0.28), 0 14px 40px -20px rgba(15, 23, 42, 0.22);
            --login-text: #0f172a;
            --login-muted: #64748b;
            --login-input-bg: rgba(248, 250, 252, 0.92);
            --login-input-border: rgba(148, 163, 184, 0.18);
            --login-ring: rgba(99, 102, 241, 0.34);
            --login-button-shadow: 0 18px 45px -18px rgba(79, 70, 229, 0.65);
            --login-accent: #6366f1;
            --login-accent-2: #8b5cf6;
            --login-toggle-bg: rgba(255, 255, 255, 0.72);
            --login-toggle-border: rgba(148, 163, 184, 0.18);
        }
        html.dark {
            color-scheme: dark;
            --login-bg:
                radial-gradient(circle at top, rgba(99, 102, 241, 0.26) 0%, transparent 36%),
                radial-gradient(circle at 85% 18%, rgba(236, 72, 153, 0.18) 0%, transparent 24%),
                radial-gradient(circle at 10% 90%, rgba(14, 165, 233, 0.18) 0%, transparent 22%),
                linear-gradient(180deg, #060816 0%, #0b1020 48%, #020617 100%);
            --login-card-bg: rgba(7, 12, 26, 0.72);
            --login-card-border: rgba(148, 163, 184, 0.14);
            --login-card-shadow: 0 36px 110px -34px rgba(15, 23, 42, 0.9), 0 24px 60px -26px rgba(79, 70, 229, 0.34);
            --login-text: #f8fafc;
            --login-muted: #94a3b8;
            --login-input-bg: rgba(15, 23, 42, 0.78);
            --login-input-border: rgba(148, 163, 184, 0.14);
            --login-ring: rgba(129, 140, 248, 0.42);
            --login-button-shadow: 0 22px 50px -18px rgba(99, 102, 241, 0.7);
            --login-accent: #818cf8;
            --login-accent-2: #a855f7;
            --login-toggle-bg: rgba(15, 23, 42, 0.68);
            --login-toggle-border: rgba(148, 163, 184, 0.14);
        }
        * { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: var(--login-bg); color: var(--login-text); transition: background 0.35s ease, color 0.35s ease; }
        .login-shell { position: relative; isolation: isolate; }
        .login-shell::before, .login-shell::after { content: ""; position: absolute; inset: auto; pointer-events: none; filter: blur(70px); opacity: 0.8; z-index: -1; }
        .login-shell::before { width: 18rem; height: 18rem; top: 8%; left: 8%; background: rgba(99, 102, 241, 0.18); }
        .login-shell::after { width: 20rem; height: 20rem; right: 4%; bottom: 6%; background: rgba(236, 72, 153, 0.12); }
        .login-card { position: relative; overflow: hidden; background: var(--login-card-bg); border: 1px solid var(--login-card-border); box-shadow: var(--login-card-shadow); backdrop-filter: blur(28px); -webkit-backdrop-filter: blur(28px); }
        .login-card::before { content: ""; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(255,255,255,0.18) 0%, transparent 35%, transparent 65%, rgba(255,255,255,0.08) 100%); pointer-events: none; }
        .login-input { width: 100%; padding: 0.9rem 1rem; background: var(--login-input-bg); border: 1px solid var(--login-input-border); border-radius: 1rem; outline: none; color: var(--login-text); transition: all 0.22s ease; }
        .login-input::placeholder { color: var(--login-muted); }
        .login-input:focus { border-color: var(--login-ring); box-shadow: 0 0 0 4px color-mix(in srgb, var(--login-ring) 42%, transparent); transform: translateY(-1px); }
        .theme-toggle { background: var(--login-toggle-bg); border: 1px solid var(--login-toggle-border); color: var(--login-text); backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px); box-shadow: 0 12px 34px -18px rgba(15, 23, 42, 0.35); }
        .login-button { background: linear-gradient(135deg, var(--login-accent) 0%, var(--login-accent-2) 100%); box-shadow: var(--login-button-shadow); }
        .login-kicker { background: linear-gradient(135deg, rgba(99, 102, 241, 0.14) 0%, rgba(168, 85, 247, 0.14) 100%); border: 1px solid rgba(99, 102, 241, 0.16); color: var(--login-accent); }
        .login-subtitle { color: var(--login-muted); }
        /* Haycash branding on login */
        .haycash-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; justify-content: center; }
        .haycash-logo-text { font-size: 13px; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase; background: linear-gradient(135deg, var(--login-accent), var(--login-accent-2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
        .haycash-divider { width: 1px; height: 22px; background: var(--login-input-border); }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-6">
    <div class="login-shell w-full flex items-center justify-center">
        <button type="button" id="login-theme-toggle" class="theme-toggle fixed top-6 right-6 flex items-center gap-2 px-4 py-2.5 rounded-full text-sm font-semibold transition-all hover:scale-[1.02]">
            <span id="login-theme-icon">🌙</span>
            <span id="login-theme-label">Dark mode</span>
        </button>
        <div class="w-full max-w-sm login-card p-8 rounded-[2rem]">
            <!-- Haycash Branding -->
            <div class="haycash-brand">
                <svg width="26" height="26" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <rect width="32" height="32" rx="8" fill="url(#hc_grad_login)"/>
                    <path d="M8 10h4v5h8v-5h4v12h-4v-5h-8v5H8V10z" fill="white"/>
                    <defs><linearGradient id="hc_grad_login" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse"><stop stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs>
                </svg>
                <span class="haycash-logo-text">Haycash</span>
                <div class="haycash-divider"></div>
                <span style="font-size:11px; color: var(--login-muted); font-weight: 600; letter-spacing: 0.06em;">Intelligence Layer</span>
            </div>
            <div class="flex justify-center mb-5">
                <div class="login-kicker px-4 py-1.5 rounded-full text-[10px] font-extrabold uppercase tracking-[0.28em]">Sasha Enterprise</div>
            </div>
            <h1 class="text-3xl font-extrabold text-center mb-2" style="color: var(--login-text);">Sasha Intelligence</h1>
            <p class="login-subtitle text-sm text-center mb-6">Secure access to the enterprise intelligence layer.</p>
            <form method="POST" class="space-y-4">
                <input type="text" name="username" class="login-input" placeholder="Username" required>
                <input type="password" name="password" class="login-input" placeholder="Password" required>
                {% if error %}<p class="text-red-500 text-xs text-center">{{ error }}</p>{% endif %}
                <button type="submit" class="login-button w-full text-white font-bold py-3 rounded-xl transition-all hover:translate-y-[-1px]">Login</button>
            </form>
        </div>
    </div>
    <script>
        (function() {
            const html = document.documentElement;
            const themeToggle = document.getElementById('login-theme-toggle');
            const icon = document.getElementById('login-theme-icon');
            const label = document.getElementById('login-theme-label');
            const metaTheme = document.querySelector('meta[name="theme-color"]');
            function applyTheme(theme) {
                html.classList.toggle('dark', theme === 'dark');
                html.classList.toggle('light', theme !== 'dark');
                icon.textContent = theme === 'dark' ? '☀️' : '🌙';
                label.textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
                if (metaTheme) metaTheme.setAttribute('content', theme === 'dark' ? '#0b1020' : '#6366f1');
                localStorage.setItem('sasha-theme', theme);
            }
            const savedTheme = localStorage.getItem('sasha-theme');
            const initialTheme = savedTheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
            applyTheme(initialTheme);
            themeToggle.addEventListener('click', () => { applyTheme(html.classList.contains('dark') ? 'light' : 'dark'); });
        })();
    </script>
</body>
</html>
"""

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en" class="light">
<head>
    <meta charset="UTF-8">
    <title>Sasha | Enterprise Intelligence</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#6366f1">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <link rel="apple-touch-icon" href="https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Sparkles_emoji_by_Twitter.svg/192px-Sparkles_emoji_by_Twitter.svg.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --brand-color: #6366f1;
            --accent-color: #8b5cf6;
            --accent-pink: #ec4899;
            --accent-cyan: #0ea5e9;
            --bg-color: #fafafa;
            --bg-gradient: radial-gradient(circle at 50% 10%, rgba(99, 102, 241, 0.08) 0%, transparent 60%), radial-gradient(circle at 80% 80%, rgba(236, 72, 153, 0.05) 0%, transparent 50%);
            --body-text: #09090b;
            --title-color: #111827;
            --muted-text: #71717a;
            --soft-text: #a1a1aa;
            --card-bg: #ffffff;
            --card-border: rgba(228, 228, 231, 0.8);
            --card-shadow: 0 12px 40px -12px rgba(0,0,0,0.08);
            --glass-bg: rgba(255, 255, 255, 0.75);
            --glass-bg-hover: rgba(255, 255, 255, 0.9);
            --glass-border: rgba(255, 255, 255, 0.6);
            --glass-shadow: 0 8px 32px -4px rgba(0,0,0,0.06), 0 0 0 1px rgba(0,0,0,0.02);
            --glass-focus-shadow: 0 20px 60px -8px rgba(99, 102, 241, 0.15), 0 0 0 1.5px rgba(99, 102, 241, 0.4);
            --table-head-bg: #f4f4f5;
            --table-head-text: #71717a;
            --table-cell-text: #27272a;
            --table-row-border: #f4f4f5;
            --table-head-border: #e4e4e7;
            --table-row-hover: #fafafa;
            --button-bg: rgba(255, 255, 255, 0.6);
            --button-bg-hover: rgba(255, 255, 255, 0.9);
            --button-border: rgba(0,0,0,0.05);
            --button-shadow: 0 2px 10px rgba(0,0,0,0.02);
            --history-chip-bg: #f3f4f6;
            --history-chip-border: #e5e7eb;
            --history-chip-text: #9ca3af;
            --insight-bg: rgba(238, 242, 255, 0.7);
            --insight-border: rgba(199, 210, 254, 0.9);
            --insight-title: #312e81;
            --insight-text: #374151;
            --footer-bg: rgba(249, 250, 251, 0.75);
            --footer-border: #f3f4f6;
            --details-bg: #f9fafb;
            --details-hover: #f3f4f6;
            --code-bg: #09090b;
            --code-text: #a5b4fc;
            --error-bg: rgba(254, 242, 242, 0.65);
            --error-card-text: #b91c1c;
            --scrollbar-thumb: #d4d4d8;
            --loader-color: #6366f1;
            --input-text: #111827;
            --input-placeholder: #d1d5db;
            --toolbar-text: #4b5563;
            --surface-tint: linear-gradient(135deg, rgba(99, 102, 241, 0.06) 0%, rgba(236, 72, 153, 0.03) 100%);
            --mesh-glow-1: rgba(99, 102, 241, 0.16);
            --mesh-glow-2: rgba(236, 72, 153, 0.1);
            --mesh-glow-3: rgba(14, 165, 233, 0.08);
            --theme-chip-bg: rgba(255, 255, 255, 0.72);
            --theme-chip-border: rgba(0,0,0,0.05);
            --theme-chip-shadow: 0 16px 36px -18px rgba(15, 23, 42, 0.2);
            --chart-grid: #f4f4f5;
            --chart-text: #71717a;
            --chart-line: #6366f1;
            --chart-fill: rgba(99, 102, 241, 0.1);
            /* Sidebar */
            --sidebar-bg: rgba(255,255,255,0.88);
            --sidebar-border: rgba(228,228,231,0.9);
            --sidebar-shadow: 4px 0 32px -8px rgba(0,0,0,0.08);
            --sidebar-item-hover: rgba(99,102,241,0.07);
            --sidebar-item-active: rgba(99,102,241,0.13);
            --sidebar-item-text: #27272a;
            --sidebar-section-text: #a1a1aa;
            color-scheme: light;
        }
        html.dark {
            --brand-color: #818cf8;
            --accent-color: #a78bfa;
            --accent-pink: #f472b6;
            --accent-cyan: #38bdf8;
            --bg-color: #020617;
            --bg-gradient: radial-gradient(circle at 50% 0%, rgba(99, 102, 241, 0.28) 0%, transparent 40%), radial-gradient(circle at 80% 75%, rgba(236, 72, 153, 0.16) 0%, transparent 35%), radial-gradient(circle at 10% 90%, rgba(14, 165, 233, 0.14) 0%, transparent 28%);
            --body-text: #f8fafc;
            --title-color: #f8fafc;
            --muted-text: #cbd5e1;
            --soft-text: #94a3b8;
            --card-bg: rgba(7, 12, 26, 0.88);
            --card-border: rgba(148, 163, 184, 0.12);
            --card-shadow: 0 24px 70px -28px rgba(0,0,0,0.85), 0 12px 34px -18px rgba(79, 70, 229, 0.3);
            --glass-bg: rgba(7, 12, 26, 0.68);
            --glass-bg-hover: rgba(10, 16, 31, 0.86);
            --glass-border: rgba(148, 163, 184, 0.12);
            --glass-shadow: 0 16px 50px -22px rgba(0,0,0,0.75), 0 0 0 1px rgba(148, 163, 184, 0.08);
            --glass-focus-shadow: 0 24px 70px -18px rgba(99, 102, 241, 0.28), 0 0 0 1.5px rgba(129, 140, 248, 0.45);
            --table-head-bg: rgba(15, 23, 42, 0.94);
            --table-head-text: #cbd5e1;
            --table-cell-text: #f8fafc;
            --table-row-border: rgba(148, 163, 184, 0.08);
            --table-head-border: rgba(148, 163, 184, 0.12);
            --table-row-hover: rgba(30, 41, 59, 0.7);
            --button-bg: rgba(15, 23, 42, 0.72);
            --button-bg-hover: rgba(15, 23, 42, 0.94);
            --button-border: rgba(148, 163, 184, 0.12);
            --button-shadow: 0 10px 28px -16px rgba(0,0,0,0.65);
            --history-chip-bg: rgba(15, 23, 42, 0.88);
            --history-chip-border: rgba(148, 163, 184, 0.14);
            --history-chip-text: #94a3b8;
            --insight-bg: rgba(30, 41, 59, 0.66);
            --insight-border: rgba(99, 102, 241, 0.18);
            --insight-title: #c7d2fe;
            --insight-text: #e2e8f0;
            --footer-bg: rgba(15, 23, 42, 0.68);
            --footer-border: rgba(148, 163, 184, 0.08);
            --details-bg: rgba(7, 12, 26, 0.95);
            --details-hover: rgba(15, 23, 42, 0.95);
            --code-bg: #020617;
            --code-text: #c7d2fe;
            --error-bg: rgba(69, 10, 10, 0.5);
            --error-card-text: #fecaca;
            --scrollbar-thumb: #334155;
            --loader-color: #818cf8;
            --input-text: #f8fafc;
            --input-placeholder: #64748b;
            --toolbar-text: #e2e8f0;
            --surface-tint: linear-gradient(135deg, rgba(99, 102, 241, 0.14) 0%, rgba(236, 72, 153, 0.07) 100%);
            --mesh-glow-1: rgba(99, 102, 241, 0.28);
            --mesh-glow-2: rgba(236, 72, 153, 0.16);
            --mesh-glow-3: rgba(14, 165, 233, 0.14);
            --theme-chip-bg: rgba(15, 23, 42, 0.74);
            --theme-chip-border: rgba(148, 163, 184, 0.12);
            --theme-chip-shadow: 0 18px 40px -18px rgba(0, 0, 0, 0.65);
            --chart-grid: rgba(148, 163, 184, 0.12);
            --chart-text: #cbd5e1;
            --chart-line: #818cf8;
            --chart-fill: rgba(129, 140, 248, 0.16);
            /* Sidebar dark */
            --sidebar-bg: rgba(7,12,26,0.92);
            --sidebar-border: rgba(148,163,184,0.1);
            --sidebar-shadow: 4px 0 40px -8px rgba(0,0,0,0.6);
            --sidebar-item-hover: rgba(99,102,241,0.12);
            --sidebar-item-active: rgba(99,102,241,0.2);
            --sidebar-item-text: #e2e8f0;
            --sidebar-section-text: #64748b;
            color-scheme: dark;
        }
        * { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--body-text); overflow-x: hidden; scroll-behavior: smooth; transition: background-color 0.35s ease, color 0.35s ease; }
        .ambient-mesh { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2; background: var(--bg-gradient); transition: background 0.45s ease; }
        .ambient-orbit { position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden; }
        .ambient-orbit::before, .ambient-orbit::after { content: ""; position: absolute; border-radius: 9999px; filter: blur(90px); opacity: 0.9; }
        .ambient-orbit::before { width: 22rem; height: 22rem; top: 10%; left: -4%; background: var(--mesh-glow-1); animation: driftOne 14s ease-in-out infinite alternate; }
        .ambient-orbit::after { width: 24rem; height: 24rem; right: -6%; bottom: 4%; background: linear-gradient(135deg, var(--mesh-glow-2), var(--mesh-glow-3)); animation: driftTwo 16s ease-in-out infinite alternate; }
        @keyframes driftOne { from { transform: translate3d(0, 0, 0) scale(1); } to { transform: translate3d(60px, 30px, 0) scale(1.08); } }
        @keyframes driftTwo { from { transform: translate3d(0, 0, 0) scale(1); } to { transform: translate3d(-50px, -25px, 0) scale(1.06); } }

        /* ===================== HAYCASH HEADER ===================== */
        .haycash-nav-logo { display: flex; align-items: center; gap: 10px; }
        .haycash-nav-logo-icon { width: 32px; height: 32px; border-radius: 8px; background: linear-gradient(135deg, #6366f1, #8b5cf6); display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 14px -4px rgba(99,102,241,0.5); flex-shrink: 0; }
        .haycash-nav-logo-text { font-size: 12px; font-weight: 800; letter-spacing: 0.1em; text-transform: uppercase; color: var(--soft-text); }
        .haycash-divider-v { width: 1px; height: 20px; background: var(--card-border); }
        .haycash-sasha-label { display: flex; flex-direction: column; }
        .haycash-sasha-name { font-size: 18px; font-weight: 800; letter-spacing: -0.02em; color: var(--title-color); line-height: 1; }
        .haycash-sasha-sub { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3em; color: var(--soft-text); margin-top: 2px; }

        /* ===================== SIDEBAR ===================== */
        #sidebar { position: fixed; top: 0; left: 0; height: 100vh; width: 280px; z-index: 200; transform: translateX(-100%); transition: transform 0.38s cubic-bezier(0.16, 1, 0.3, 1); background: var(--sidebar-bg); border-right: 1px solid var(--sidebar-border); box-shadow: var(--sidebar-shadow); backdrop-filter: blur(32px); -webkit-backdrop-filter: blur(32px); display: flex; flex-direction: column; }
        #sidebar.open { transform: translateX(0); }
        #sidebar-overlay { position: fixed; inset: 0; z-index: 199; background: rgba(0,0,0,0.35); opacity: 0; pointer-events: none; transition: opacity 0.3s ease; backdrop-filter: blur(2px); }
        #sidebar-overlay.active { opacity: 1; pointer-events: all; }
        .sidebar-header { padding: 20px 20px 16px; border-bottom: 1px solid var(--sidebar-border); display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
        .sidebar-new-btn { display: flex; align-items: center; gap: 8px; width: 100%; padding: 10px 14px; border-radius: 12px; background: linear-gradient(135deg, var(--brand-color), var(--accent-color)); color: white; font-size: 13px; font-weight: 700; cursor: pointer; border: none; margin: 12px 16px; width: calc(100% - 32px); transition: all 0.2s; box-shadow: 0 6px 20px -6px rgba(99,102,241,0.5); }
        .sidebar-new-btn:hover { transform: translateY(-1px); box-shadow: 0 10px 28px -8px rgba(99,102,241,0.6); }
        .sidebar-section-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; color: var(--sidebar-section-text); padding: 8px 20px 4px; }
        .sidebar-thread-list { flex: 1; overflow-y: auto; padding: 8px 10px; }
        .sidebar-thread-item { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-radius: 10px; cursor: pointer; transition: all 0.18s; margin-bottom: 2px; group: true; }
        .sidebar-thread-item:hover { background: var(--sidebar-item-hover); }
        .sidebar-thread-item.active { background: var(--sidebar-item-active); }
        .sidebar-thread-title { font-size: 13px; font-weight: 500; color: var(--sidebar-item-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px; flex: 1; }
        .sidebar-thread-time { font-size: 10px; color: var(--sidebar-section-text); flex-shrink: 0; margin-left: 6px; }
        .sidebar-thread-rename { display: none; background: none; border: none; padding: 2px 6px; cursor: pointer; color: var(--sidebar-section-text); border-radius: 6px; font-size: 11px; }
        .sidebar-thread-item:hover .sidebar-thread-rename { display: block; }
        .sidebar-empty { padding: 24px 20px; text-align: center; color: var(--sidebar-section-text); font-size: 12px; }
        .sidebar-footer { padding: 14px 16px; border-top: 1px solid var(--sidebar-border); flex-shrink: 0; }
        .sidebar-footer-info { font-size: 11px; color: var(--sidebar-section-text); text-align: center; }

        /* Sasha orb */
        .sasha-core { width: 32px; height: 32px; border-radius: 50%; background: transparent; background-image: url('/static/logo.png'); background-size: contain; background-repeat: no-repeat; background-position: center; box-shadow: 0 0 15px var(--brand-color), 0 0 30px color-mix(in srgb, var(--brand-color) 80%, transparent); animation: breathe 3s infinite ease-in-out; transition: box-shadow 0.35s ease; }
        .sasha-core.thinking { animation: pulse-fast 0.8s infinite alternate; box-shadow: 0 0 20px var(--accent-pink), 0 0 42px color-mix(in srgb, var(--accent-pink) 75%, transparent); }
        @keyframes breathe { 0%, 100% { transform: scale(1); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } }
        @keyframes pulse-fast { 0% { transform: scale(0.9); opacity: 0.7; } 100% { transform: scale(1.3); opacity: 1; } }

        .spotlight-glass { background: var(--glass-bg); backdrop-filter: blur(40px); -webkit-backdrop-filter: blur(40px); border: 1px solid var(--glass-border); box-shadow: var(--glass-shadow); border-radius: 2rem; transition: all 0.4s cubic-bezier(0.2, 0.8, 0.2, 1); position: relative; }
        .spotlight-glass::before { content: ""; position: absolute; inset: 0; background: var(--surface-tint); opacity: 0.85; pointer-events: none; }
        .spotlight-glass > * { position: relative; z-index: 1; }
        .spotlight-glass:focus-within { box-shadow: var(--glass-focus-shadow); transform: translateY(-2px); background: var(--glass-bg-hover); }

        .executive-card { background: var(--card-bg); border-radius: 24px; box-shadow: var(--card-shadow); border: 1px solid var(--card-border); animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards; overflow: hidden; position: relative; transition: transform 0.28s ease, box-shadow 0.28s ease, background 0.28s ease, border-color 0.28s ease; }
        .executive-card::before { content: ""; position: absolute; inset: 0; background: linear-gradient(135deg, rgba(255,255,255,0.08) 0%, transparent 35%, transparent 70%, rgba(129,140,248,0.08) 100%); pointer-events: none; }
        .executive-card:hover { transform: translateY(-2px); box-shadow: 0 24px 70px -24px rgba(99, 102, 241, 0.18), var(--card-shadow); }

        /* Dark mode table fix — override any white backgrounds */
        html.dark .executive-card { background: var(--card-bg) !important; }
        html.dark .sasha-table thead { background: var(--table-head-bg) !important; }
        html.dark .sasha-table th { background: var(--table-head-bg) !important; color: var(--table-head-text) !important; }
        html.dark .sasha-table td { color: var(--table-cell-text) !important; background: transparent !important; }
        html.dark .sasha-table tr:hover td { background: var(--table-row-hover) !important; }
        html.dark .insight-block { background: var(--insight-bg) !important; border-color: var(--insight-border) !important; color: var(--insight-text) !important; }
        html.dark .card-footer-strip { background: var(--footer-bg) !important; border-color: var(--footer-border) !important; }
        html.dark .export-btn-excel { background: rgba(16,185,129,0.15) !important; color: #34d399 !important; border-color: rgba(16,185,129,0.25) !important; }
        html.dark .export-btn-pdf { background: rgba(239,68,68,0.15) !important; color: #f87171 !important; border-color: rgba(239,68,68,0.25) !important; }

        .sasha-table { width: 100%; border-collapse: collapse; text-align: left; }
        .sasha-table thead { background: var(--table-head-bg); }
        .sasha-table th { color: var(--table-head-text); font-weight: 600; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; padding: 14px 24px; border-bottom: 1px solid var(--table-head-border); white-space: nowrap; }
        .sasha-table td { padding: 16px 24px; color: var(--table-cell-text); font-size: 0.875rem; border-bottom: 1px solid var(--table-row-border); font-variant-numeric: tabular-nums; white-space: nowrap; }
        .sasha-table tr:hover td { background-color: var(--table-row-hover); }
        .sasha-table tr:last-child td { border-bottom: none; }

        /* Export buttons */
        .export-btn-excel { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 10px; font-size: 12px; font-weight: 700; cursor: pointer; border: 1px solid rgba(16,185,129,0.25); background: rgba(16,185,129,0.08); color: #059669; transition: all 0.2s; }
        .export-btn-excel:hover { transform: translateY(-1px); background: rgba(16,185,129,0.16); box-shadow: 0 6px 18px -6px rgba(16,185,129,0.3); }
        .export-btn-pdf { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 10px; font-size: 12px; font-weight: 700; cursor: pointer; border: 1px solid rgba(239,68,68,0.25); background: rgba(239,68,68,0.08); color: #dc2626; transition: all 0.2s; }
        .export-btn-pdf:hover { transform: translateY(-1px); background: rgba(239,68,68,0.16); box-shadow: 0 6px 18px -6px rgba(239,68,68,0.3); }

        /* Voice button */
        .voice-btn { display: inline-flex; align-items: center; justify-content: center; width: 44px; height: 44px; border-radius: 50%; border: none; cursor: pointer; transition: all 0.22s; background: var(--button-bg); border: 1px solid var(--button-border); color: var(--soft-text); flex-shrink: 0; }
        .voice-btn:hover { background: var(--button-bg-hover); transform: scale(1.08); }
        .voice-btn.recording { background: linear-gradient(135deg, #ef4444, #dc2626); color: white; animation: voice-pulse 1s infinite; box-shadow: 0 0 0 0 rgba(239,68,68,0.4); border-color: transparent; }
        @keyframes voice-pulse { 0% { box-shadow: 0 0 0 0 rgba(239,68,68,0.5); } 70% { box-shadow: 0 0 0 10px rgba(239,68,68,0); } 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0); } }

        /* Voice feedback toast */
        #voice-toast { position: fixed; bottom: 120px; left: 50%; transform: translateX(-50%) translateY(20px); background: var(--glass-bg); backdrop-filter: blur(20px); border: 1px solid var(--glass-border); border-radius: 999px; padding: 10px 22px; font-size: 13px; font-weight: 600; color: var(--body-text); opacity: 0; pointer-events: none; transition: all 0.3s ease; z-index: 100; display: flex; align-items: center; gap: 10px; white-space: nowrap; box-shadow: var(--card-shadow); }
        #voice-toast.visible { opacity: 1; transform: translateX(-50%) translateY(0); }
        .voice-dot { width: 8px; height: 8px; border-radius: 50%; background: #ef4444; animation: voice-pulse 1s infinite; }

        .btn-glass { background: var(--button-bg); color: var(--toolbar-text); backdrop-filter: blur(12px); border: 1px solid var(--button-border); box-shadow: var(--button-shadow); transition: all 0.2s; }
        .btn-glass:hover { background: var(--button-bg-hover); transform: translateY(-1px); }
        .theme-toggle-btn { background: var(--theme-chip-bg); border: 1px solid var(--theme-chip-border); box-shadow: var(--theme-chip-shadow); color: var(--toolbar-text); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); }
        .theme-toggle-btn:hover { transform: translateY(-1px) scale(1.01); }
        .aurora-line { position: absolute; inset: 0 auto auto 0; width: 100%; height: 1px; background: linear-gradient(90deg, transparent 0%, color-mix(in srgb, var(--brand-color) 65%, transparent) 18%, color-mix(in srgb, var(--accent-pink) 55%, transparent) 48%, color-mix(in srgb, var(--accent-cyan) 55%, transparent) 78%, transparent 100%); opacity: 0.85; }

        /* Suggestion chips */
        .suggestion-chip { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 999px; font-size: 12px; font-weight: 600; cursor: pointer; background: var(--button-bg); border: 1px solid var(--button-border); color: var(--toolbar-text); transition: all 0.2s; backdrop-filter: blur(8px); }
        .suggestion-chip:hover { background: var(--sidebar-item-active); border-color: var(--brand-color); color: var(--brand-color); transform: translateY(-1px); }

        .engine-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 9999px; font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em; }
        .engine-det { background: rgba(16,185,129,0.1); color: #059669; border: 1px solid rgba(16,185,129,0.2); }
        .engine-gen { background: rgba(245,158,11,0.1); color: #d97706; border: 1px solid rgba(245,158,11,0.2); }
        html.dark .engine-det { background: rgba(16,185,129,0.15); color: #34d399; border-color: rgba(16,185,129,0.25); }
        html.dark .engine-gen { background: rgba(245,158,11,0.15); color: #fbbf24; border-color: rgba(245,158,11,0.25); }

        @keyframes slideUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 10px; }

        /* Main content shift when sidebar is open */
        #main-content { transition: margin-left 0.38s cubic-bezier(0.16, 1, 0.3, 1); }
        body.sidebar-open #main-content { margin-left: 280px; }
        @media (max-width: 768px) { body.sidebar-open #main-content { margin-left: 0; } }
    </style>
</head>
<body class="min-h-screen flex flex-col items-center pt-28 pb-40 px-6 relative">
    <div class="ambient-mesh"></div>
    <div class="ambient-orbit"></div>

    <!-- SIDEBAR OVERLAY -->
    <div id="sidebar-overlay" onclick="closeSidebar()"></div>

    <!-- SIDEBAR -->
    <div id="sidebar">
        <div class="sidebar-header">
            <div style="display:flex;align-items:center;gap:8px;">
                <div class="haycash-nav-logo-icon">
                    <svg width="18" height="18" viewBox="0 0 32 32" fill="none"><path d="M8 10h4v5h8v-5h4v12h-4v-5h-8v5H8V10z" fill="white"/></svg>
                </div>
                <span style="font-size:13px;font-weight:800;color:var(--title-color);">Conversations</span>
            </div>
            <button onclick="closeSidebar()" style="background:none;border:none;cursor:pointer;padding:6px;border-radius:8px;color:var(--soft-text);" class="hover:bg-[var(--sidebar-item-hover)]">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
        </div>
        <button class="sidebar-new-btn" onclick="newThreadFromSidebar()">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12h14"/></svg>
            New Thread
        </button>
        <div class="sidebar-section-label">Recent</div>
        <div class="sidebar-thread-list" id="sidebar-thread-list">
            <div class="sidebar-empty">No conversations yet.<br>Ask Sasha something to begin.</div>
        </div>
        <div class="sidebar-footer">
            <div class="sidebar-footer-info">Haycash Intelligence Layer · Sasha</div>
        </div>
    </div>

    <!-- VOICE TOAST -->
    <div id="voice-toast">
        <div class="voice-dot"></div>
        <span id="voice-toast-text">Listening…</span>
    </div>

    <!-- HEADER -->
    <div class="fixed top-6 left-8 z-50 flex items-center space-x-3">
        <!-- Sidebar toggle -->
        <button onclick="toggleSidebar()" class="btn-glass flex items-center justify-center w-10 h-10 rounded-xl mr-1" title="Conversations">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--brand-color)"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
        </button>
        <!-- Haycash Logo + Sasha -->
       
        <div id="sasha-orb" class="sasha-core"></div>
        <div class="haycash-sasha-label">
            <span class="haycash-sasha-name">Sasha</span>
            <span class="haycash-sasha-sub">Enterprise Intelligence</span>
        </div>
    </div>

    <div class="fixed top-6 right-8 z-50 flex space-x-3">
        <button id="theme-toggle" onclick="toggleTheme()" class="theme-toggle-btn flex items-center px-5 py-2.5 rounded-full text-sm font-semibold">
            <i id="theme-toggle-icon" data-lucide="moon" class="w-4 h-4 mr-2" style="color: var(--brand-color);"></i>
            <span id="theme-toggle-text" class="hidden sm:inline">Dark Mode</span>
        </button>
        <button onclick="resetThread()" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold">
            <i data-lucide="refresh-cw" class="w-4 h-4 mr-2" style="color: var(--brand-color);"></i>
            <span class="hidden sm:inline">New Thread</span>
        </button>
        <a href="/logout" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold">
            <i data-lucide="log-out" class="w-4 h-4 mr-2 text-red-400"></i>
            <span class="hidden sm:inline">Log Out</span>
        </a>
    </div>

    <!-- MAIN CONTENT -->
    <div id="main-content" class="w-full max-w-5xl z-10 w-full mt-4">
        <div id="history-container" class="hidden space-y-10 relative mb-10">
            <div class="absolute -top-8 left-1/2 -translate-x-1/2 text-[10px] uppercase font-bold tracking-widest px-4 py-1.5 rounded-full border" style="background: var(--history-chip-bg); color: var(--history-chip-text); border-color: var(--history-chip-border);">Conversation Archive</div>
        </div>
        <div id="latest-result"></div>
    </div>

    <!-- INPUT BAR -->
    <div class="fixed bottom-8 left-1/2 transform -translate-x-1/2 w-full max-w-3xl z-50 px-4" id="input-bar-wrapper">
        <div class="spotlight-glass flex items-center px-6 py-3 relative overflow-hidden shadow-xl gap-3">
            <div class="aurora-line"></div>
            <div id="loader-bar" class="absolute bottom-0 left-0 h-0.5 w-0 transition-all duration-300" style="background: var(--loader-color);"></div>
            <i data-lucide="search" class="w-5 h-5 shrink-0" style="color: var(--soft-text);"></i>
            <input type="text" id="question" class="w-full bg-transparent text-xl py-4 outline-none font-medium" style="color: var(--input-text);" placeholder="Ask Sasha anything — or say 'Email this to...'">
            <div id="loading-text" class="hidden text-xs font-bold uppercase tracking-widest shrink-0 animate-pulse" style="color: var(--brand-color);">Thinking</div>
            <!-- Voice Button -->
            <button id="voice-btn" class="voice-btn" title="Voice command" onclick="toggleVoice()">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
            </button>
        </div>
    </div>

    <script>
        const htmlEl = document.documentElement;
        const themeMeta = document.querySelector('meta[name="theme-color"]');
        let currentAnalyticState = null;
        let threadHistory = []; // [{id, title, time, resultHtml, questionText}]
        let activeThreadId = null;
        let chartCounter = 0;
        let voiceRecognition = null;
        let isRecording = false;

        // ===================== THEME =====================
        function getThemePalette() {
            const styles = getComputedStyle(document.documentElement);
            const brand = styles.getPropertyValue('--chart-line').trim() || '#6366f1';
            const fill = styles.getPropertyValue('--chart-fill').trim() || 'rgba(99, 102, 241, 0.1)';
            const text = styles.getPropertyValue('--chart-text').trim() || '#71717a';
            const grid = styles.getPropertyValue('--chart-grid').trim() || '#f4f4f5';
            const border = htmlEl.classList.contains('dark') ? '#0f172a' : '#ffffff';
            return { brand, fill, text, grid, doughnut: [brand, '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#0ea5e9'], border };
        }

        function applyTheme(theme) {
            const isDark = theme === 'dark';
            htmlEl.classList.toggle('dark', isDark);
            htmlEl.classList.toggle('light', !isDark);
            localStorage.setItem('sasha-theme', theme);
            const toggleText = document.getElementById('theme-toggle-text');
            const toggleIcon = document.getElementById('theme-toggle-icon');
            if (toggleText) toggleText.innerText = isDark ? 'Light Mode' : 'Dark Mode';
            if (toggleIcon) toggleIcon.setAttribute('data-lucide', isDark ? 'sun-medium' : 'moon');
            if (themeMeta) themeMeta.setAttribute('content', isDark ? '#020617' : '#6366f1');
            Chart.defaults.color = getThemePalette().text;
            lucide.createIcons();
        }

        function toggleTheme() { applyTheme(htmlEl.classList.contains('dark') ? 'light' : 'dark'); }

        const savedTheme = localStorage.getItem('sasha-theme');
        const preferredTheme = savedTheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
        applyTheme(preferredTheme);

        if ('serviceWorker' in navigator) {
            window.addEventListener('load', function() {
                navigator.serviceWorker.register('/sw.js').catch(err => console.log('SW failed:', err));
            });
        }

        lucide.createIcons();
        Chart.defaults.font.family = "'Inter', sans-serif";
        Chart.defaults.color = getThemePalette().text;

        document.getElementById("question").addEventListener("keypress", (e) => { if (e.key === "Enter") ask(); });
        document.getElementById("question").style.setProperty('caret-color', getComputedStyle(document.documentElement).getPropertyValue('--brand-color').trim());

        // ===================== SIDEBAR =====================
        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const overlay = document.getElementById('sidebar-overlay');
            const isOpen = sidebar.classList.contains('open');
            if (isOpen) { closeSidebar(); }
            else {
                sidebar.classList.add('open');
                overlay.classList.add('active');
                document.body.classList.add('sidebar-open');
            }
        }

        function closeSidebar() {
            document.getElementById('sidebar').classList.remove('open');
            document.getElementById('sidebar-overlay').classList.remove('active');
            document.body.classList.remove('sidebar-open');
        }

        function renderSidebarThreads() {
            const list = document.getElementById('sidebar-thread-list');
            if (!threadHistory.length) {
                list.innerHTML = '<div class="sidebar-empty">No conversations yet.<br>Ask Sasha something to begin.</div>';
                return;
            }
            list.innerHTML = threadHistory.slice().reverse().map(t => `
                <div class="sidebar-thread-item ${t.id === activeThreadId ? 'active' : ''}" data-id="${t.id}" onclick="loadThread('${t.id}')">
                    <div class="sidebar-thread-title" id="thread-title-${t.id}">${escapeHtml(t.title)}</div>
                    <span class="sidebar-thread-time">${t.time}</span>
                    <button class="sidebar-thread-rename" onclick="renameThread(event,'${t.id}')" title="Rename">✏️</button>
                </div>
            `).join('');
        }

        function escapeHtml(str) {
            return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }

        function saveThread(questionText, resultHtml, resultData) {
            const id = 'thread_' + Date.now();
            const title = questionText.length > 42 ? questionText.slice(0, 42) + '…' : questionText;
            const now = new Date();
            const time = now.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
            threadHistory.push({ id, title, time, questionText, resultHtml, resultData });
            activeThreadId = id;
            renderSidebarThreads();
            persistThreads();
        }

        function loadThread(id) {
            const thread = threadHistory.find(t => t.id === id);
            if (!thread) return;
            activeThreadId = id;
            const latestContainer = document.getElementById('latest-result');
            const historyContainer = document.getElementById('history-container');
            // Move current latest to history
            if (latestContainer.children.length > 0) {
                const pastResult = latestContainer.firstElementChild;
                pastResult.classList.remove('executive-card');
                pastResult.classList.add('rounded-3xl', 'border', 'opacity-60');
                historyContainer.appendChild(pastResult);
                historyContainer.classList.remove('hidden');
            }
            latestContainer.innerHTML = thread.resultHtml;
            if (thread.resultData) {
                currentAnalyticState = thread.resultData;
                rebuildCharts(latestContainer, thread.resultData);
            }
            renderSidebarThreads();
            closeSidebar();
            lucide.createIcons();
        }

        function rebuildCharts(container, data) {
            if (!data || data.chart_intent === 'none' || !data.raw_data || data.raw_data.length < 2) return;
            const canvas = container.querySelector('canvas');
            if (!canvas) return;
            const palette = getThemePalette();
            try {
                new Chart(canvas, {
                    type: data.chart_intent,
                    data: {
                        labels: data.raw_data.map(r => r.label || 'N/A'),
                        datasets: [{ data: data.raw_data.map(r => r.value), backgroundColor: data.chart_intent === 'doughnut' ? palette.doughnut : palette.brand, borderColor: palette.border, borderWidth: 2 }]
                    },
                    options: buildChartOptions(data.chart_intent, palette)
                });
            } catch(e) {}
        }

        function renameThread(e, id) {
            e.stopPropagation();
            const thread = threadHistory.find(t => t.id === id);
            if (!thread) return;
            const newTitle = prompt('Rename conversation:', thread.title);
            if (newTitle && newTitle.trim()) {
                thread.title = newTitle.trim().slice(0, 60);
                renderSidebarThreads();
                persistThreads();
            }
        }

        function persistThreads() {
            try {
                const light = threadHistory.map(t => ({ id: t.id, title: t.title, time: t.time, questionText: t.questionText }));
                localStorage.setItem('sasha_threads_meta', JSON.stringify(light.slice(-50)));
            } catch(e) {}
        }

        function loadPersistedThreadMeta() {
            try {
                const raw = localStorage.getItem('sasha_threads_meta');
                if (raw) {
                    const meta = JSON.parse(raw);
                    threadHistory = meta.map(m => ({ ...m, resultHtml: '', resultData: null }));
                    renderSidebarThreads();
                }
            } catch(e) {}
        }

        loadPersistedThreadMeta();

        function newThreadFromSidebar() {
            closeSidebar();
            resetThread();
        }

        // ===================== VOICE =====================
        function initVoice() {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (!SpeechRecognition) { showVoiceToast('Voice not supported in this browser.', false); return null; }
            const rec = new SpeechRecognition();
            rec.lang = 'en-US';
            rec.interimResults = false;
            rec.maxAlternatives = 1;
            rec.onresult = (event) => {
                const transcript = event.results[0][0].transcript;
                document.getElementById('question').value = transcript;
                stopRecording();
                showVoiceToast('Got it! Processing…', false);
                setTimeout(() => ask(), 400);
            };
            rec.onerror = (e) => { stopRecording(); showVoiceToast('Could not hear you. Try again.', false); };
            rec.onend = () => { if (isRecording) stopRecording(); };
            return rec;
        }

        function toggleVoice() {
            if (isRecording) { stopRecording(); return; }
            if (!voiceRecognition) voiceRecognition = initVoice();
            if (!voiceRecognition) return;
            isRecording = true;
            const btn = document.getElementById('voice-btn');
            btn.classList.add('recording');
            showVoiceToast('Listening…', true);
            try { voiceRecognition.start(); } catch(e) { stopRecording(); }
        }

        function stopRecording() {
            isRecording = false;
            const btn = document.getElementById('voice-btn');
            btn.classList.remove('recording');
            hideVoiceToast();
            try { if (voiceRecognition) voiceRecognition.stop(); } catch(e) {}
        }

        function showVoiceToast(msg, showDot) {
            const toast = document.getElementById('voice-toast');
            const dot = toast.querySelector('.voice-dot');
            document.getElementById('voice-toast-text').textContent = msg;
            dot.style.display = showDot ? 'block' : 'none';
            toast.classList.add('visible');
        }

        function hideVoiceToast() {
            setTimeout(() => { document.getElementById('voice-toast').classList.remove('visible'); }, 1200);
        }

        function speakResponse(text) {
            if (!window.speechSynthesis || !text) return;
            const clean = text.replace(/<[^>]*>/g, '').replace(/\s+/g,' ').trim().slice(0, 280);
            const utt = new SpeechSynthesisUtterance(clean);
            utt.rate = 1.0;
            utt.pitch = 1.0;
            const voices = speechSynthesis.getVoices();
            const pref = voices.find(v => v.lang === 'en-US' && v.name.includes('Google')) || voices.find(v => v.lang === 'en-US') || voices[0];
            if (pref) utt.voice = pref;
            speechSynthesis.cancel();
            speechSynthesis.speak(utt);
        }

        // ===================== RESET =====================
        async function resetThread() {
            await fetch('/reset', { method: 'POST' });
            document.getElementById('latest-result').innerHTML = '';
            document.getElementById('history-container').innerHTML = `<div class="absolute -top-8 left-1/2 -translate-x-1/2 text-[10px] uppercase font-bold tracking-widest px-4 py-1.5 rounded-full border" style="background: var(--history-chip-bg); color: var(--history-chip-text); border-color: var(--history-chip-border);">Conversation Archive</div>`;
            document.getElementById('question').value = '';
            currentAnalyticState = null;
            activeThreadId = null;
            renderSidebarThreads();
        }

        function toggleHistory() {
            const hist = document.getElementById('history-container');
            const btnText = document.getElementById('history-btn-text');
            if (hist.classList.contains('hidden')) { hist.classList.remove('hidden'); if(btnText) btnText.innerText = 'Hide Archive'; }
            else { hist.classList.add('hidden'); if(btnText) btnText.innerText = 'Archive'; }
        }

        // ===================== EXPORTS =====================
        function exportExcel(containerId) {
            const container = document.getElementById(containerId);
            if (!container) return;
            const table = container.querySelector('.sasha-table');
            if (!table) { alert('No table data to export.'); return; }
            const wb = XLSX.utils.table_to_book(table, {sheet: "Sasha Analysis"});
            XLSX.writeFile(wb, "Sasha_Data_Export.xlsx");
        }

        function exportPDF(cardId) {
            const element = document.getElementById(cardId);
            if (!element) return;
            const opt = { margin: 0.5, filename: 'Sasha_Executive_Report.pdf', image: { type: 'jpeg', quality: 0.98 }, html2canvas: { scale: 2 }, jsPDF: { unit: 'in', format: 'letter', orientation: 'landscape' } };
            html2pdf().set(opt).from(element).save();
        }

        async function emailReport(email, data) {
            if (!email || !data) return;
            const res = await fetch('/send_report', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    email: email,
                    headline: data.headline,
                    sasha_insight: data.sasha_insight,
                    html_table: data.html_table,
                    raw_data: data.raw_data,
                    chart_intent: data.chart_intent
                })
            });
            const status = await res.json();
            if (status.status === "success") alert("Report successfully emailed to " + email);
            else alert("Failed to send email: " + status.message);
        }

        // ===================== CHART OPTIONS =====================
        function buildChartOptions(type, palette) {
            const base = {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: type === 'doughnut', labels: { color: palette.text, font: { family: "'Inter', sans-serif", size: 12 }, boxWidth: 12, padding: 16 } },
                    tooltip: { backgroundColor: 'rgba(15,23,42,0.92)', titleColor: '#f8fafc', bodyColor: '#cbd5e1', padding: 12, cornerRadius: 10, titleFont: { weight: '700' } }
                }
            };
            if (type !== 'doughnut') {
                base.scales = {
                    x: { grid: { color: palette.grid, drawBorder: false }, ticks: { color: palette.text, font: { size: 11 }, maxRotation: 35 } },
                    y: { grid: { color: palette.grid, drawBorder: false }, ticks: { color: palette.text, font: { size: 11 } } }
                };
            }
            return base;
        }

        // ===================== ASK =====================
        async function ask() {
            const q = document.getElementById('question').value.trim();
            if (!q) return;

            const latestContainer = document.getElementById('latest-result');
            const historyContainer = document.getElementById('history-container');
            const loaderText = document.getElementById('loading-text');
            const loaderBar = document.getElementById('loader-bar');
            const orb = document.getElementById('sasha-orb');

            orb.classList.add('thinking');
            loaderText.classList.remove('hidden');
            loaderBar.style.width = '30%';

            try {
                const res = await fetch('/ask', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({question: q})
                });

                const data = await res.json();
                loaderBar.style.width = '80%';

                // AI voice feedback on result
                if (data.headline && !data.trigger_email) {
                    speakResponse(data.headline);
                }

                // ACTION: Sasha triggered email hand-off using active workspace data
                if (data.trigger_email) {
                    // Pass the data returned directly from the server (it already has html_table, raw_data, etc.)
                    await emailReport(data.recipient, data);
                }

                if (latestContainer.children.length > 0 && !data.trigger_email) {
                    const pastResult = latestContainer.firstElementChild;
                    pastResult.classList.remove('executive-card');
                    pastResult.classList.add('rounded-3xl', 'border', 'opacity-60', 'hover:opacity-100', 'transition-all', 'duration-300', 'scale-[0.98]');
                    historyContainer.appendChild(pastResult);
                    historyContainer.classList.remove('hidden');
                }

                if (!data.trigger_email) currentAnalyticState = data;

                chartCounter++;
                const uniqueChartId = 'chart-' + chartCounter;
                const cardId = 'card-' + chartCounter;
                const wrapper = document.createElement('div');
                wrapper.className = "executive-card p-0 mb-10";
                wrapper.id = cardId;

                if (data.error) {
                    wrapper.innerHTML = `<div class="p-8" style="color:var(--error-card-text)">${data.error}</div>`;
                } else {
                    const badgeHtml = (data.health_badge === 'Red')
                        ? '<span style="background:rgba(239,68,68,0.1);color:#dc2626;border:1px solid rgba(239,68,68,0.25);padding:3px 10px;border-radius:999px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;">High Risk</span>'
                        : (data.health_badge === 'Yellow')
                        ? '<span style="background:rgba(245,158,11,0.1);color:#d97706;border:1px solid rgba(245,158,11,0.25);padding:3px 10px;border-radius:999px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;">Caution</span>'
                        : (data.health_badge === 'Green')
                        ? '<span style="background:rgba(16,185,129,0.1);color:#059669;border:1px solid rgba(16,185,129,0.25);padding:3px 10px;border-radius:999px;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;">Healthy</span>'
                        : '';

                    const suggestionsHtml = data.suggestions && data.suggestions.length
                        ? `<div style="padding:16px 28px 20px;display:flex;flex-wrap:wrap;gap:8px;">
                            ${data.suggestions.map(s => `<button class="suggestion-chip" onclick="askSuggestion('${s.replace(/'/g,"\\'")}')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12h14M12 5l7 7-7 7"/></svg>${escapeHtml(s)}</button>`).join('')}
                           </div>`
                        : '';

                    wrapper.innerHTML = `
                        <div style="padding:36px 40px 28px;">
                            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;flex-wrap:wrap;gap:8px;">
                                ${badgeHtml}
                                <span style="font-size:10px;color:var(--soft-text);font-weight:600;letter-spacing:0.06em;">${new Date().toLocaleString()}</span>
                            </div>
                            <h2 style="font-size:clamp(1.4rem,3vw,1.9rem);font-weight:800;line-height:1.25;color:var(--title-color);margin:0 0 20px;">${data.headline}</h2>
                            ${data.sasha_insight ? `<div class="insight-block" style="background:var(--insight-bg);border:1px solid var(--insight-border);border-radius:16px;padding:20px 24px;margin-bottom:24px;">
                                <p style="font-size:14px;line-height:1.75;color:var(--insight-text);margin:0;">${data.sasha_insight}</p>
                            </div>` : ''}
                            ${data.chart_intent !== 'none' && data.raw_data && data.raw_data.length >= 2 ? `<div style="height:300px;margin-bottom:28px;"><canvas id="${uniqueChartId}"></canvas></div>` : ''}
                        </div>
                        ${data.html_table ? `<div style="overflow-x:auto;border-top:1px solid var(--table-head-border);border-bottom:1px solid var(--table-head-border);">${data.html_table}</div>` : ''}
                        ${suggestionsHtml}
                        <div class="card-footer-strip" style="padding:16px 28px;display:flex;gap:10px;align-items:center;background:var(--footer-bg);border-top:1px solid var(--footer-border);">
                            <button class="export-btn-excel" onclick="exportExcel('${cardId}')">
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                                Download Excel
                            </button>
                            <button class="export-btn-pdf" onclick="exportPDF('${cardId}')">
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/></svg>
                                Download PDF
                            </button>
                        </div>
                    `;
                }

                latestContainer.innerHTML = '';
                latestContainer.appendChild(wrapper);

                // Build chart
                if (!data.error && data.chart_intent !== 'none' && data.raw_data && data.raw_data.length >= 2) {
                    const palette = getThemePalette();
                    const safeLabels = data.raw_data.map(r => (r.label !== null && r.label !== undefined && r.label !== 'undefined') ? String(r.label) : 'N/A');
                    const safeValues = data.raw_data.map(r => Number(r.value) || 0);
                    new Chart(document.getElementById(uniqueChartId), {
                        type: data.chart_intent,
                        data: {
                            labels: safeLabels,
                            datasets: [{
                                data: safeValues,
                                backgroundColor: data.chart_intent === 'doughnut' ? palette.doughnut : palette.brand,
                                borderColor: data.chart_intent === 'line' ? palette.brand : palette.border,
                                borderWidth: data.chart_intent === 'line' ? 2 : 1.5,
                                fill: data.chart_intent === 'line' ? { target: 'origin', above: palette.fill } : false,
                                tension: 0.4,
                                pointBackgroundColor: palette.brand,
                                pointRadius: 4
                            }]
                        },
                        options: buildChartOptions(data.chart_intent, palette)
                    });
                }

                // Save to sidebar thread history
                const resultHtml = wrapper.outerHTML;
                saveThread(q, resultHtml, data);

                document.getElementById('question').value = '';
                orb.classList.remove('thinking');
                loaderText.classList.add('hidden');
                loaderBar.style.width = '0%';
                window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
                lucide.createIcons();
            } catch (err) {
                orb.classList.remove('thinking');
                loaderText.classList.add('hidden');
                loaderBar.style.width = '0%';
            }
        }

        function askSuggestion(text) {
            document.getElementById('question').value = text;
            ask();
        }
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(port=5000, debug=True)