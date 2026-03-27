import pandas as pd
import json
import os
import sys
import re
import math
import traceback
import warnings
from datetime import datetime
from functools import wraps # <-- ADDED FOR LOGIN BOUNCER
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for # <-- ADDED REDIRECT
from openai import OpenAI
from dotenv import load_dotenv # <-- THE BRIDGE IMPORT

# Silence Pandas date parsing warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

# ==============================================================================
# 1. FILE SYSTEM & AGENTIC DATA LOADER (UNTOUCHED)
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
# 2. CORE AI CONFIGURATION (UPGRADED WITH THE BRIDGE)
# ==============================================================================
load_dotenv() # <-- THIS ACTIVATES THE BRIDGE TO YOUR .env FILE

# 👉 YOUR API KEY IS NO LONGER HARDCODED. IT PULLS FROM THE .env FILE.
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_KEY)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "sasha_agent_secret_key_123")

if not os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, 'w') as f:
        json.dump({"_init": "Memory initialized."}, f)

# ==============================================================================
# 2.5 ADDED: PERMISSION ROLES & THE "HANDS" (LONG-TERM MEMORY)
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
# 3. SASHA ENTERPRISE ENGINE: PLANNER -> DETERMINISTIC ENGINE -> NARRATOR -> CRITIC
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

def detect_user_requirements(question):
    q = (question or "").lower()
    wants_chart = any(term in q for term in ['chart', 'graph', 'bar', 'line', 'pie', 'doughnut', 'plot', 'visual'])
    wants_table = any(term in q for term in ['table', 'breakdown', 'compare', 'comparison', 'list', 'ranking', 'rank', 'top', 'worst', 'best'])
    wants_explanation = any(term in q for term in [
        'explain', 'analysis', 'analisis', 'reasoning', 'detail', 'detailed', 'context',
        'summary', 'interpret', 'interpretation', 'insight', 'insights', 'why', 'how',
        'report', 'presentation', 'professional summary', 'professional', 'executive',
        'boardroom', 'code explanation', 'explain code'
    ])
    requested_chart_type = 'none'
    if 'doughnut' in q or 'donut' in q or 'pie' in q:
        requested_chart_type = 'doughnut'
    elif 'line' in q:
        requested_chart_type = 'line'
    elif 'bar' in q or 'graph' in q or 'chart' in q:
        requested_chart_type = 'bar'
    return {
        "wants_chart": wants_chart,
        "wants_table": wants_table,
        "wants_explanation": wants_explanation,
        "requested_chart_type": requested_chart_type,
        "question_lower": q
    }

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

def make_chart_rows_from_df(df, label_col, value_col, limit=20):
    if df is None or label_col not in df.columns or value_col not in df.columns:
        return []
    out = []
    for _, row in df.head(limit).iterrows():
        label = row[label_col]
        value = safe_float(row[value_col])
        if pd.notnull(label) and value is not None and math.isfinite(value):
            out.append({"label": str(label), "value": float(value)})
    return out

def aggregate_clients_frame(df):
    client_id_col = pick_first_existing(
        df,
        [['client_id'], ['client id'], ['customer_id'], ['customer id'], ['id_cliente'], ['cliente id']],
        prefer_numeric=True
    )
    business_name_col = pick_first_existing(
        df,
        [['business_name'], ['business name'], ['client_name'], ['client name'], ['customer_name'], ['customer name'], ['razon social'], ['nombre negocio'], ['nombre cliente']],
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

    # force clean working column names immediately
    rename_map = {
        client_id_col: 'client_id',
        total_paid_col: 'total_paid'
    }
    if business_name_col:
        rename_map[business_name_col] = 'business_name'

    work = work.rename(columns=rename_map)

    # if duplicate column names still exist, keep first
    work = work.loc[:, ~work.columns.duplicated()]

    work = work.dropna(subset=['total_paid'])

    if 'business_name' in work.columns:
        agg = (
            work.groupby(['client_id', 'business_name'], as_index=False, dropna=False)['total_paid']
            .sum()
            .copy()
        )
    else:
        agg = (
            work.groupby(['client_id'], as_index=False, dropna=False)['total_paid']
            .sum()
            .copy()
        )
        agg['business_name'] = agg['client_id'].apply(lambda x: f"Client {x}")

    return agg[['client_id', 'business_name', 'total_paid']]

    agg = agg.rename(columns=rename_map)

    if business_name_col and business_name_col in agg.columns:
        agg = agg.rename(columns={business_name_col: 'business_name'})
    else:
        agg['business_name'] = agg['client_id'].apply(lambda x: f"Client {x}")

    return agg[['client_id', 'business_name', 'total_paid']]
# ------------------------------------------------------------------------------
# PLANNER
# ------------------------------------------------------------------------------
def heuristic_plan(question, requirements):
    q = requirements["question_lower"]
    analysis_type = "generic_python"

    if 'portfolio' in q and ('health' in q or 'review' in q):
        analysis_type = "portfolio_health_review"
    elif ('best' in q and 'worst' in q) or ('top 10' in q and 'worst' in q):
        analysis_type = "best_vs_worst_clients"
    elif 'risky client' in q or 'risky clients' in q or ('risk' in q and 'client' in q):
        analysis_type = "risky_clients"
    elif 'top client' in q or ('best client' in q and 'worst' not in q):
        analysis_type = "top_clients"
    elif 'worst client' in q:
        analysis_type = "bottom_clients"
    elif 'explain code' in q or 'code explanation' in q:
        analysis_type = "code_explanation"

    return {
        "analysis_type": analysis_type,
        "wants_chart": requirements["wants_chart"],
        "wants_table": requirements["wants_table"],
        "wants_explanation": requirements["wants_explanation"],
        "requested_chart_type": requirements["requested_chart_type"],
        "boardroom_style": any(x in q for x in ['executive', 'boardroom', 'professional', 'portfolio health review', 'presentation']),
        "deterministic_first": analysis_type in [
            "risky_clients", "top_clients", "bottom_clients", "best_vs_worst_clients", "portfolio_health_review"
        ],
        "user_intent_summary": question
    }

def planner_map_intent(question, history, role="Viewer"):
    requirements = detect_user_requirements(question)
    now = datetime.now().strftime("%A, %B %d, %Y - %H:%M:%S")
    long_term_memory = read_long_term_memory()
    schema_info = build_schema_info()

    system_prompt = f"""
    You are Sasha's planning layer.
    TIME: {now}
    USER ROLE: {role}
    LONG-TERM MEMORY: {long_term_memory}
    DATA SCHEMA:
    {schema_info}

    Your job is to classify the request into an approved business intent before any computation happens.

    Return STRICT JSON with exactly these keys:
    {{
      "analysis_type": "risky_clients | top_clients | bottom_clients | best_vs_worst_clients | portfolio_health_review | code_explanation | generic_python",
      "wants_chart": true,
      "wants_table": true,
      "wants_explanation": true,
      "requested_chart_type": "bar | line | doughnut | none",
      "boardroom_style": false,
      "deterministic_first": true,
      "user_intent_summary": "..."
    }}

    Planning rules:
    - Prefer deterministic_first=true for common business analyses when possible.
    - If the user asks for a boardroom/professional/executive review, boardroom_style=true.
    - If the user asks for code explanation, analysis_type="code_explanation".
    - If uncertain, use "generic_python".
    - Never generate Python here.
    """

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-8:]:
        messages.append(msg)
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
        return plan
    except:
        return heuristic_plan(question, requirements)

# ------------------------------------------------------------------------------
# DETERMINISTIC ENGINE
# ------------------------------------------------------------------------------
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

    explicit = re.search(r'(above|over|greater than|>=|>|threshold)\s*(\d+)', q)
    if explicit:
        try:
            threshold = float(explicit.group(2))
        except:
            pass

    risky = work[work[risk_col] >= threshold].copy()
    risky = risky.sort_values(by=risk_col, ascending=False)

    rename_map = {risk_col: 'risk_score'}
    if client_id_col:
        rename_map[client_id_col] = 'client_id'
    if business_name_col:
        rename_map[business_name_col] = 'business_name'
    if total_paid_col:
        rename_map[total_paid_col] = 'total_paid'
    if credit_id_col:
        rename_map[credit_id_col] = 'credit_id'
    if exposure_col:
        rename_map[exposure_col] = 'capital_exposure'

    risky = risky.rename(columns=rename_map)

    if 'client_id' not in risky.columns:
        risky['client_id'] = range(1, len(risky) + 1)
    if 'business_name' not in risky.columns:
        risky['business_name'] = risky['client_id'].apply(lambda x: f"Client {x}")
    if 'total_paid' not in risky.columns:
        risky['total_paid'] = 0.0

    table_cols = [c for c in ['client_id', 'business_name', 'risk_score', 'total_paid', 'credit_id', 'capital_exposure'] if c in risky.columns]
    final_df = risky[table_cols].head(50).copy()

    chart_source = final_df[['business_name', 'risk_score']].head(15).copy()
    chart_source['label'] = chart_source['business_name'].astype(str)
    chart_source['value'] = chart_source['risk_score'].astype(float)

    return {
        "engine": "deterministic",
        "analysis_type": "risky_clients",
        "df_name": df_name,
        "metrics": {
            "threshold": threshold,
            "risky_count": int(len(risky)),
            "max_risk_score": safe_float(risky['risk_score'].max()) if len(risky) else None
        },
        "final_df": final_df,
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": chart_source[['label', 'value']].to_dict('records'),
        "business_payload": {
            "summary_title": "Risky clients review",
            "threshold_used": threshold,
            "risky_count": int(len(risky)),
            "top_rows": serialize_for_prompt(final_df.head(10))
        }
    }

def compute_top_clients(plan, question, bottom=False):
    df_name = choose_best_dataframe(
        required_groups=[['client'], ['paid']],
        optional_groups=[['business'], ['customer']]
    )
    if not df_name:
        return None

    agg = aggregate_clients_frame(dfs[df_name])
    if agg is None:
        return None

    ranked = agg.sort_values(by='total_paid', ascending=bottom).head(10).copy()
    ranked['segment'] = 'Worst Clients' if bottom else 'Best Clients'
    ranked['label'] = ranked['business_name'].astype(str)
    ranked['value'] = ranked['total_paid'].astype(float)

    return {
        "engine": "deterministic",
        "analysis_type": "bottom_clients" if bottom else "top_clients",
        "df_name": df_name,
        "metrics": {
            "client_count": int(len(agg)),
            "selected_total": safe_float(ranked['total_paid'].sum()) if len(ranked) else 0.0
        },
        "final_df": ranked[['segment', 'client_id', 'business_name', 'total_paid']],
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": ranked[['label', 'value']].to_dict('records'),
        "business_payload": {
            "summary_title": "Top clients review" if not bottom else "Worst clients review",
            "top_rows": serialize_for_prompt(ranked)
        }
    }

def compute_best_vs_worst_clients(plan, question):
    df_name = choose_best_dataframe(
        required_groups=[['client'], ['paid']],
        optional_groups=[['business'], ['customer']]
    )
    if not df_name:
        return None

    agg = aggregate_clients_frame(dfs[df_name])
    if agg is None:
        return None

    best = agg.sort_values(by='total_paid', ascending=False).head(10).copy()
    worst = agg.sort_values(by='total_paid', ascending=True).head(10).copy()

    best['segment'] = 'Best Clients'
    worst['segment'] = 'Worst Clients'

    combined = pd.concat([best, worst], ignore_index=True)
    combined['label'] = combined.apply(
        lambda row: f"{'Best' if row['segment'] == 'Best Clients' else 'Worst'} - {row['business_name']}",
        axis=1
    )
    combined['value'] = combined['total_paid'].astype(float)

    final_df = combined[['segment', 'client_id', 'business_name', 'total_paid']].copy()

    return {
        "engine": "deterministic",
        "analysis_type": "best_vs_worst_clients",
        "df_name": df_name,
        "metrics": {
            "best_total": safe_float(best['total_paid'].sum()) if len(best) else 0.0,
            "worst_total": safe_float(worst['total_paid'].sum()) if len(worst) else 0.0,
            "gap": safe_float(best['total_paid'].sum() - worst['total_paid'].sum()) if len(best) and len(worst) else None
        },
        "final_df": final_df,
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": combined[['label', 'value']].to_dict('records'),
        "business_payload": {
            "summary_title": "Best vs worst clients comparison",
            "best_rows": serialize_for_prompt(best),
            "worst_rows": serialize_for_prompt(worst)
        }
    }

def compute_portfolio_health_review(plan, question):
    df_name = choose_best_dataframe(
        required_groups=[['client'], ['paid']],
        optional_groups=[['business'], ['risk'], ['capital'], ['amount'], ['credit']]
    )
    if not df_name:
        return None

    base_df = dfs[df_name].copy()
    agg = aggregate_clients_frame(base_df)
    if agg is None:
        return None

    best = agg.sort_values(by='total_paid', ascending=False).head(10).copy()
    worst = agg.sort_values(by='total_paid', ascending=True).head(10).copy()

    total_portfolio_paid = safe_float(agg['total_paid'].sum()) if len(agg) else 0.0
    best_total = safe_float(best['total_paid'].sum()) if len(best) else 0.0
    worst_total = safe_float(worst['total_paid'].sum()) if len(worst) else 0.0
    concentration_pct = round((best_total / total_portfolio_paid) * 100, 2) if total_portfolio_paid else 0.0

    risk_summary = None
    risky = compute_risky_clients(
        {"requested_chart_type": "bar"},
        "detect risky clients"
    )
    if risky and risky.get("final_df") is not None and not risky["final_df"].empty:
        risk_summary = risky["final_df"].head(5).copy()

    best['category'] = 'Best Clients'
    worst['category'] = 'Worst Clients'

    frames = [best[['category', 'client_id', 'business_name', 'total_paid']]]
    frames.append(worst[['category', 'client_id', 'business_name', 'total_paid']])

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
    chart_rows['label'] = chart_rows['business_name'].astype(str)
    chart_rows['value'] = chart_rows['total_paid'].astype(float)

    badge = 'Green'
    if concentration_pct >= 70:
        badge = 'Red'
    elif concentration_pct >= 45:
        badge = 'Yellow'

    return {
        "engine": "deterministic",
        "analysis_type": "portfolio_health_review",
        "df_name": df_name,
        "metrics": {
            "client_count": int(len(agg)),
            "portfolio_total_paid": total_portfolio_paid,
            "best_total": best_total,
            "worst_total": worst_total,
            "payment_concentration_pct_top10": concentration_pct
        },
        "final_df": final_df,
        "chart_type": plan.get("requested_chart_type") if plan.get("requested_chart_type") != 'none' else 'bar',
        "chart_data": chart_rows[['label', 'value']].to_dict('records'),
        "health_badge": badge,
        "business_payload": {
            "summary_title": "Executive portfolio health review",
            "top10_share_pct": concentration_pct,
            "best_rows": serialize_for_prompt(best),
            "worst_rows": serialize_for_prompt(worst),
            "risk_rows": serialize_for_prompt(risk_summary) if risk_summary is not None else [],
            "opportunity_rows": serialize_for_prompt(opportunities)
        }
    }

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

    return None

# ------------------------------------------------------------------------------
# NARRATION / FORMATTING LAYER
# ------------------------------------------------------------------------------
def build_fallback_narrative(plan, computed, question):
    analysis_type = computed.get("analysis_type", "analysis")
    metrics = computed.get("metrics", {})
    payload = computed.get("business_payload", {})
    headline = "Analysis completed."
    insight = "The requested analysis was completed successfully."
    badge = computed.get("health_badge", "None")

    if analysis_type == "risky_clients":
        threshold = metrics.get("threshold")
        risky_count = metrics.get("risky_count")
        headline = f"Detected {risky_count} high-risk client records for follow-up review."
        insight = (
            f"This review flags client records whose risk score meets or exceeds the working threshold of "
            f"{threshold:.2f}. The output is intended to help collections, credit, or portfolio teams prioritize "
            f"cases requiring attention. The most important managerial takeaway is not only who is risky, but also "
            f"how large the risky population is and whether those clients still represent meaningful exposure or low payment recovery."
        )
        badge = "Red"

    elif analysis_type == "top_clients":
        total = metrics.get("selected_total")
        headline = "Top clients identified based on total payments."
        insight = (
            f"This ranking isolates the strongest client contributors by total paid amount. These accounts are the "
            f"core value drivers in the current portfolio slice and should be reviewed as strategic accounts for "
            f"retention, expansion, and service quality protection. The selected group contributes {total:,.2f} "
            f"in cumulative payments."
        )
        badge = "Green"

    elif analysis_type == "bottom_clients":
        total = metrics.get("selected_total")
        headline = "Lowest-performing clients identified based on total payments."
        insight = (
            f"This ranking highlights the weakest payment contributors in the selected portfolio view. These clients "
            f"should be examined for inactivity, onboarding issues, weak collections performance, or structural low value. "
            f"The selected group contributes only {total:,.2f} in cumulative payments."
        )
        badge = "Yellow"

    elif analysis_type == "best_vs_worst_clients":
        best_total = metrics.get("best_total", 0.0)
        worst_total = metrics.get("worst_total", 0.0)
        gap = metrics.get("gap", 0.0)
        headline = "Top 10 best vs. top 10 worst clients show a sharp performance gap."
        insight = (
            f"This comparison shows a strong concentration of value in the best-performing client group. The top 10 "
            f"clients contribute {best_total:,.2f}, while the bottom 10 contribute {worst_total:,.2f}, creating a "
            f"gap of {gap:,.2f}. In business terms, this means the portfolio is not balanced: a relatively small group "
            f"drives most realized value, while the weakest tail contributes little and may require either activation strategies or cleanup."
        )
        badge = "Yellow" if safe_float(worst_total) is not None and worst_total <= 0 else "Green"

    elif analysis_type == "portfolio_health_review":
        conc = metrics.get("payment_concentration_pct_top10", 0.0)
        total = metrics.get("portfolio_total_paid", 0.0)
        headline = "Executive portfolio health review completed with a clear view of concentration, risks, and opportunities."
        insight = (
            f"This portfolio health review shows total realized payments of {total:,.2f}. The top 10 clients account "
            f"for {conc:.2f}% of all payments, which indicates the degree of dependency on a small number of accounts. "
            f"The strongest accounts represent the clearest growth and retention opportunity, while the weakest or risk-flagged "
            f"clients represent the clearest need for intervention, monitoring, or segmentation cleanup. For leadership, the "
            f"most important takeaway is whether this level of concentration is strategically acceptable or operationally dangerous."
        )
        if conc >= 70:
            badge = "Red"
        elif conc >= 45:
            badge = "Yellow"
        else:
            badge = "Green"

    suggestions = [
        "Show me the next level of detail behind these results.",
        "Compare this result against a previous period.",
        "Translate these findings into management actions."
    ]

    return {
        "headline": headline,
        "sasha_insight": insight,
        "health_badge": badge,
        "suggestions": suggestions
    }

def narrate_and_format(plan, computed, question, history, role="Viewer"):
    now = datetime.now().strftime("%A, %B %d, %Y - %H:%M:%S")
    long_term_memory = read_long_term_memory()

    system_prompt = f"""
    You are Sasha's narration and formatting layer.
    TIME: {now}
    USER ROLE: {role}
    LONG-TERM MEMORY: {long_term_memory}

    You will receive:
    1. The user's question
    2. The planner decision
    3. Deterministic business results already computed

    Your job:
    - Write a world-class executive-quality `headline`
    - Write a substantial `sasha_insight`
    - Choose `health_badge`
    - Write `suggestions`

    Return STRICT JSON with exactly:
    {{
      "headline": "...",
      "sasha_insight": "...",
      "health_badge": "Red | Yellow | Green | None",
      "suggestions": ["...", "...", "..."]
    }}

    Formatting rules:
    - If the user asked for professional / executive / boardroom style, the writing must sound polished and specific.
    - The explanation must reference the actual computed findings. Never use generic filler.
    - If the user asked for explanation or detail, `sasha_insight` must be substantial.
    - If the analysis is risky clients, explain threshold logic and operational use.
    - If the analysis is portfolio health, explain concentration, risks, opportunities, and management implications.
    - Always respond in the user's language naturally.
    """

    payload = {
        "question": question,
        "plan": serialize_for_prompt(plan),
        "computed": {
            "analysis_type": computed.get("analysis_type"),
            "metrics": serialize_for_prompt(computed.get("metrics")),
            "business_payload": serialize_for_prompt(computed.get("business_payload")),
            "sample_table": serialize_for_prompt(computed.get("final_df"), max_rows=10)
        }
    }

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
            response_format={"type": "json_object"},
            temperature=0.2
        )
        narrated = json.loads(response.choices[0].message.content)
        fallback = build_fallback_narrative(plan, computed, question)
        for k, v in fallback.items():
            narrated.setdefault(k, v)
        return narrated
    except:
        return build_fallback_narrative(plan, computed, question)

# ------------------------------------------------------------------------------
# LEGACY FALLBACK PYTHON GENERATOR (KEPT AS SAFETY NET)
# ------------------------------------------------------------------------------
def execute_agent_code(code_str, role="Viewer"): # <-- ADDED ROLE PARAMETER
    if re.search(r"import\s+(os|sys|subprocess|shutil|pathlib)", code_str):
        return None, None, None, None, None, None, None, "Security Violation: System imports blocked."

    forbidden_patterns = [
        r"open\s*\(",
        r"__import__",
        r"exec\s*\(",
        r"eval\s*\(",
        r"globals\s*\(",
        r"locals\s*\(",
        r"compile\s*\(",
        r"while\s+True\b"
    ]
    for pattern in forbidden_patterns:
        if re.search(pattern, code_str):
            return None, None, None, None, None, None, None, "Security Violation: Unsafe code detected."

    local_env = {'dfs': dfs, 'pd': pd, 'datetime': datetime}

    if role in ['Admin', 'Collaborator']:
        local_env['save_to_memory'] = save_to_memory

    try:
        exec_globals = {
            "__builtins__": {
                "len": len, "sum": sum, "min": min, "max": max, "round": round, "str": str,
                "float": float, "int": int, "abs": abs, "sorted": sorted, "range": range, "__import__": __import__
            },
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
    long_term_memory = read_long_term_memory()

    system_prompt = f"""
    You are 'Sasha', an elite Python Data Agent and Enterprise Analyst.
    TIME: {current_time_str}. DATA: {schema_info}
    USER ROLE: {role}.
    LONG-TERM MEMORY (COMPANY RULES): {long_term_memory}

    MISSION: Write a Python script to answer the user.
    CONTEXT: Use the conversation history below to understand follow-up questions.
    PERMISSION: {f"You can save rules using save_to_memory('rule')" if role in ['Admin', 'Collaborator'] else "VIEWER ONLY: You cannot save or edit memory."}

    OUTPUT: STRICT JSON: {{ "thought": "...", "python_code": "..." }}
    RULES: Assign the following variables in your Python code:
    `final_df`: The pandas dataframe to show.
    `headline`: A punchy string summary. (CRITICAL RULE: The `headline` variable is your Voice. Whether you are explaining a complex financial pivot, saving a rule, or just answering a greeting like 'Hello', you must ALWAYS write your natural, human-like AI response inside this `headline` variable. If there is no data to analyze, converse freely using this variable).
    `chart_type`: 'line', 'bar', 'doughnut', or 'none'. (CRITICAL: Set to 'none' if the data is a single row, a single value, or if a chart provides no visual value).
    `chart_data`: A list of dicts.
    `sasha_insight`: A detailed explanation section. If the user asks for explanation, analysis, reasoning, detail, context, breakdown, summary, interpretation, insights, why/how, asks to explain code, or asks for a report/presentation style answer, this MUST be a substantial paragraph or multiple paragraphs. Only set to '' if the user clearly wants just the raw answer with no explanation.
    `health_badge`: String 'Red' (high risk/bad), 'Yellow' (neutral/warning), 'Green' (healthy/good), or 'None'.
    `suggestions`: A Python list of 3 strings representing smart, proactive follow-up questions based on this data.

    CRITICAL FORMAT RULE:
    If the user explicitly asks for a chart, graph, table, breakdown, trend, comparison, pie chart, bar chart, line chart, doughnut chart, code explanation, or detailed explanation, you MUST honor that format request.
    - If they ask for a chart/graph and the result has 2 or more comparable data points, chart_type MUST NOT be 'none'.
    - If they ask for a table, final_df MUST be populated.
    - If they ask for explanation/detail or code explanation, sasha_insight MUST be populated.
    - chart_data MUST be chart-ready as a Python list of dicts using exactly this shape:
      [{{"label": "...", "value": 123.45}}, ...]
    - Never leave chart_data empty when a chart was explicitly requested and the data can support one.
    - For charts, ALWAYS build `chart_data` from clean numeric values and human-readable labels.
    - Do not use dataframe column names like client_id / total_paid directly as chart_data keys. Convert them into exact `label` and `value` keys.
    - If the user asks for a professional summary, executive summary, presentation summary, or analysis, `headline` and `sasha_insight` must read like polished business communication, not generic filler.
    - If the user asks to explain code, `sasha_insight` must explain what the code is doing, step by step, in plain business language.

    CRITICAL LANGUAGE RULE: Always respond in the user's language natively, but keep the JSON keys and Python variable names strictly in English.
    NEVER use destructive commands. Fuzzy search strings.
    """

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-10:]:
        messages.append(msg)
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

            result = package_result(
                df=df,
                headline=headline,
                c_type=c_type,
                c_data=c_data,
                insight=insight,
                badge=badge,
                suggs=suggs,
                python_code=ai_msg['python_code']
            )

            critic = critic_validate(question, requirements, result)
            if not critic["passed"]:
                messages.append({"role": "assistant", "content": ai_msg['python_code']})
                messages.append({"role": "user", "content": critic["repair_prompt"]})
                continue

            return result

        except Exception as e:
            return {"error": str(e)}

    return {"error": "Sasha failed to resolve the code after 3 attempts."}

# ------------------------------------------------------------------------------
# CRITIC
# ------------------------------------------------------------------------------
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

                    if hasattr(label, 'item'):
                        label = label.item()
                    if hasattr(value, 'item'):
                        value = value.item()

                    try:
                        if isinstance(value, str):
                            value = float(value.replace(',', '').strip())
                        else:
                            value = float(value)
                    except:
                        continue

                    if pd.notnull(label) and pd.notnull(value):
                        clean_data.append({
                            "label": str(label),
                            "value": value
                        })
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
        "headline": headline if headline else "Got it! Memory updated. / ¡Entendido! Memoria actualizada.",
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
            return {
                "passed": False,
                "repair_prompt": "You failed the chart requirement. The user explicitly requested a chart/graph. Retry and return valid chart_type plus chart_data in EXACT format: [{\"label\": \"...\", \"value\": 123.45}, ...] with at least 2 valid data points whenever the data supports it."
            }

    if requirements["wants_table"]:
        if not result.get("html_table"):
            return {
                "passed": False,
                "repair_prompt": "You failed the table requirement. The user explicitly requested a table/breakdown/comparison. Retry and populate final_df."
            }

    if requirements["wants_explanation"]:
        insight = str(result.get("sasha_insight", "")).strip()
        if not insight or len(insight) < 80:
            return {
                "passed": False,
                "repair_prompt": "You failed the explanation requirement. The user explicitly requested explanation/analysis/detail/professional summary. Retry and populate sasha_insight with a substantial business-quality explanation grounded in the findings."
            }

    headline = str(result.get("headline", "")).strip().lower()
    if headline in ["analysis completed.", "done", "completed"]:
        return {
            "passed": False,
            "repair_prompt": "Your headline is too generic. Rewrite it to sound specific, business-ready, and grounded in the actual results."
        }

    return {"passed": True, "repair_prompt": ""}

# ------------------------------------------------------------------------------
# ORCHESTRATOR
# ------------------------------------------------------------------------------
def deterministic_result_to_response(computed, narrated):
    final_df = computed.get("final_df")
    chart_data = sanitize_chart_data(computed.get("chart_data", []))
    chart_type = computed.get("chart_type", "none")
    if not chart_data or len(chart_data) < 2:
        chart_type = 'none'

    python_code = f"""# Deterministic Engine Route
# Analysis Type: {computed.get('analysis_type')}
# Source DataFrame: {computed.get('df_name')}
# This result was produced by Sasha's approved deterministic business engine,
# then narrated and validated by the LLM layers.
"""

    return {
        "headline": narrated.get("headline", "Analysis completed."),
        "python_code": python_code,
        "html_table": style_dataframe(final_df),
        "chart_intent": chart_type,
        "raw_data": chart_data,
        "sasha_insight": narrated.get("sasha_insight", ""),
        "health_badge": narrated.get("health_badge", computed.get("health_badge", "None")),
        "suggestions": narrated.get("suggestions", ["Show me the detail behind this result.", "Compare this against another period.", "Turn this into actions."])
    }

def agentic_brain(question, history, role="Viewer"): # <-- ADDED ROLE PARAMETER
    requirements = detect_user_requirements(question)

    # STEP 1: Planner maps user request to approved business intent
    plan = planner_map_intent(question, history, role)

    # STEP 2: Deterministic engine tries approved analyses first
    if plan.get("deterministic_first", False):
        try:
            computed = deterministic_engine(plan, question)

            if computed is not None:
                narrated = narrate_and_format(plan, computed, question, history, role)
                result = deterministic_result_to_response(computed, narrated)

                critic = critic_validate(question, requirements, result)
                if critic["passed"]:
                    return result

        except Exception as e:
            print("⚠️ Deterministic engine failed:", str(e))
            # Do nothing here; fallback will run below

    # FALLBACK: Original Python generation path remains as safety net
    return legacy_python_generation(question, history, role)
# ==============================================================================
# 4. THE FRONTEND: SASHA 2.0 DEMO EDITION & PWA APP ROUTES
# ==============================================================================

# --- PWA ADDITION: The Manifest File (Tells the browser it's an app) ---
@app.route('/manifest.json')
def serve_manifest():
    return jsonify({
        "name": "Sasha Enterprise Intelligence",
        "short_name": "Sasha",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#fafafa",
        "theme_color": "#6366f1",
        "icons": [{
            "src": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Sparkles_emoji_by_Twitter.svg/512px-Sparkles_emoji_by_Twitter.svg.png",
            "sizes": "512x512",
            "type": "image/png"
        }]
    })

# --- PWA ADDITION: The Service Worker (Required for Install Button) ---
@app.route('/sw.js')
def serve_sw():
    js_code = "self.addEventListener('fetch', function(event) {});"
    return js_code, 200, {'Content-Type': 'application/javascript'}
# ------------------------------------------------------------------------

# <-- ADDED LOGIN ROUTE
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

# <-- ADDED LOGOUT ROUTE
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required # <-- BOUNCER ADDED
def home():
    session['history'] = []
    return render_template_string(HTML_PAGE)

@app.route('/ask', methods=['POST'])
@login_required # <-- BOUNCER ADDED
def handle_ask():
    q = request.json.get('question')
    history = session.get('history', [])
    role = session.get('role', 'Viewer') # <-- PULL ROLE FROM LOGIN

    response_data = agentic_brain(q, history, role) # <-- PASS ROLE TO BRAIN

    if "error" not in response_data:
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": response_data['headline']})
        session['history'] = history

    return jsonify(response_data)

@app.route('/reset', methods=['POST'])
@login_required # <-- BOUNCER ADDED
def reset_chat():
    session['history'] = []
    return jsonify({"status": "cleared"})

# <-- ADDED CLEAN LOGIN PAGE UI
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
        body {
            font-family: 'Inter', sans-serif;
            background: var(--login-bg);
            color: var(--login-text);
            transition: background 0.35s ease, color 0.35s ease;
        }
        .login-shell { position: relative; isolation: isolate; }
        .login-shell::before,
        .login-shell::after {
            content: "";
            position: fixed;
            inset: auto;
            pointer-events: none;
            filter: blur(70px);
            opacity: 0.8;
            z-index: -1;
        }
        .login-shell::before {
            width: 18rem; height: 18rem; top: 8%; left: 8%;
            background: rgba(99, 102, 241, 0.18);
        }
        .login-shell::after {
            width: 20rem; height: 20rem; right: 4%; bottom: 6%;
            background: rgba(236, 72, 153, 0.12);
        }
        .login-card {
            position: relative;
            overflow: hidden;
            background: var(--login-card-bg);
            border: 1px solid var(--login-card-border);
            box-shadow: var(--login-card-shadow);
            backdrop-filter: blur(28px);
            -webkit-backdrop-filter: blur(28px);
        }
        .login-card::before {
            content: "";
            position: absolute;
            inset: 0;
            background:
                linear-gradient(135deg, rgba(255,255,255,0.18) 0%, transparent 35%, transparent 65%, rgba(255,255,255,0.08) 100%);
            pointer-events: none;
        }
        .login-input {
            width: 100%;
            padding: 0.9rem 1rem;
            background: var(--login-input-bg);
            border: 1px solid var(--login-input-border);
            border-radius: 1rem;
            outline: none;
            color: var(--login-text);
            transition: all 0.22s ease;
        }
        .login-input::placeholder { color: var(--login-muted); }
        .login-input:focus {
            border-color: var(--login-ring);
            box-shadow: 0 0 0 4px color-mix(in srgb, var(--login-ring) 42%, transparent);
            transform: translateY(-1px);
        }
        .theme-toggle {
            background: var(--login-toggle-bg);
            border: 1px solid var(--login-toggle-border);
            color: var(--login-text);
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            box-shadow: 0 12px 34px -18px rgba(15, 23, 42, 0.35);
        }
        .login-button {
            background: linear-gradient(135deg, var(--login-accent) 0%, var(--login-accent-2) 100%);
            box-shadow: var(--login-button-shadow);
        }
        .login-kicker {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.14) 0%, rgba(168, 85, 247, 0.14) 100%);
            border: 1px solid rgba(99, 102, 241, 0.16);
            color: var(--login-accent);
        }
        .login-subtitle { color: var(--login-muted); }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-6">
    <div class="login-shell w-full flex items-center justify-center">
        <button type="button" id="login-theme-toggle" class="theme-toggle fixed top-6 right-6 flex items-center gap-2 px-4 py-2.5 rounded-full text-sm font-semibold transition-all hover:scale-[1.02]">
            <span id="login-theme-icon">🌙</span>
            <span id="login-theme-label">Dark mode</span>
        </button>

        <div class="w-full max-w-sm login-card p-8 rounded-[2rem]">
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

            themeToggle.addEventListener('click', () => {
                applyTheme(html.classList.contains('dark') ? 'light' : 'dark');
            });
        })();
    </script>
</body>
</html>
"""

# ==============================================================================
# UNTOUCHED 600-LINE HTML BLOCK EXACTLY AS YOU PROVIDED IT
# ==============================================================================

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
            --brand-color: #6366f1; /* Indigo */
            --accent-color: #8b5cf6;
            --accent-pink: #ec4899;
            --accent-cyan: #0ea5e9;
            --bg-color: #fafafa;
            --bg-gradient:
                radial-gradient(circle at 50% 10%, rgba(99, 102, 241, 0.08) 0%, transparent 60%),
                radial-gradient(circle at 80% 80%, rgba(236, 72, 153, 0.05) 0%, transparent 50%);
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
            color-scheme: light;
        }

        html.dark {
            --brand-color: #818cf8;
            --accent-color: #a78bfa;
            --accent-pink: #f472b6;
            --accent-cyan: #38bdf8;
            --bg-color: #020617;
            --bg-gradient:
                radial-gradient(circle at 50% 0%, rgba(99, 102, 241, 0.28) 0%, transparent 40%),
                radial-gradient(circle at 80% 75%, rgba(236, 72, 153, 0.16) 0%, transparent 35%),
                radial-gradient(circle at 10% 90%, rgba(14, 165, 233, 0.14) 0%, transparent 28%);
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
            color-scheme: dark;
        }

        * { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--body-text); overflow-x: hidden; scroll-behavior: smooth; transition: background-color 0.35s ease, color 0.35s ease; }

        .ambient-mesh {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -2;
            background: var(--bg-gradient);
            transition: background 0.45s ease;
        }

        .ambient-orbit {
            position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden;
        }
        .ambient-orbit::before,
        .ambient-orbit::after {
            content: "";
            position: absolute; border-radius: 9999px; filter: blur(90px); opacity: 0.9;
        }
        .ambient-orbit::before {
            width: 22rem; height: 22rem; top: 10%; left: -4%;
            background: var(--mesh-glow-1);
            animation: driftOne 14s ease-in-out infinite alternate;
        }
        .ambient-orbit::after {
            width: 24rem; height: 24rem; right: -6%; bottom: 4%;
            background: linear-gradient(135deg, var(--mesh-glow-2), var(--mesh-glow-3));
            animation: driftTwo 16s ease-in-out infinite alternate;
        }

        @keyframes driftOne { from { transform: translate3d(0, 0, 0) scale(1); } to { transform: translate3d(60px, 30px, 0) scale(1.08); } }
        @keyframes driftTwo { from { transform: translate3d(0, 0, 0) scale(1); } to { transform: translate3d(-50px, -25px, 0) scale(1.06); } }

        .sasha-core {
            width: 32px; height: 32px; border-radius: 50%;
            background: transparent;
            background-image: url('/static/logo.png');
            background-size: contain;
            background-repeat: no-repeat;
            background-position: center;
            box-shadow: 0 0 15px var(--brand-color), 0 0 30px color-mix(in srgb, var(--brand-color) 80%, transparent);
            animation: breathe 3s infinite ease-in-out;
            transition: box-shadow 0.35s ease;
        }
        .sasha-core.thinking { animation: pulse-fast 0.8s infinite alternate; box-shadow: 0 0 20px var(--accent-pink), 0 0 42px color-mix(in srgb, var(--accent-pink) 75%, transparent); }

        @keyframes breathe { 0%, 100% { transform: scale(1); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } }
        @keyframes pulse-fast { 0% { transform: scale(0.9); opacity: 0.7; } 100% { transform: scale(1.3); opacity: 1; } }

        .spotlight-glass {
            background: var(--glass-bg);
            backdrop-filter: blur(40px);
            -webkit-backdrop-filter: blur(40px);
            border: 1px solid var(--glass-border);
            box-shadow: var(--glass-shadow);
            border-radius: 2rem;
            transition: all 0.4s cubic-bezier(0.2, 0.8, 0.2, 1);
            position: relative;
        }
        .spotlight-glass::before {
            content: "";
            position: absolute; inset: 0;
            background: var(--surface-tint);
            opacity: 0.85;
            pointer-events: none;
        }
        .spotlight-glass > * { position: relative; z-index: 1; }
        .spotlight-glass:focus-within {
            box-shadow: var(--glass-focus-shadow);
            transform: translateY(-2px); background: var(--glass-bg-hover);
        }

        .executive-card {
            background: var(--card-bg); border-radius: 24px;
            box-shadow: var(--card-shadow);
            border: 1px solid var(--card-border);
            animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            overflow: hidden;
            position: relative;
            transition: transform 0.28s ease, box-shadow 0.28s ease, background 0.28s ease, border-color 0.28s ease;
        }
        .executive-card::before {
            content: "";
            position: absolute; inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.08) 0%, transparent 35%, transparent 70%, rgba(129,140,248,0.08) 100%);
            pointer-events: none;
        }
        .executive-card:hover { transform: translateY(-2px); box-shadow: 0 24px 70px -24px rgba(99, 102, 241, 0.18), var(--card-shadow); }

        .sasha-table { width: 100%; border-collapse: collapse; text-align: left; }
        .sasha-table thead { background: var(--table-head-bg); }
        .sasha-table th { color: var(--table-head-text); font-weight: 600; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; padding: 14px 24px; border-bottom: 1px solid var(--table-head-border); white-space: nowrap; }
        .sasha-table td { padding: 16px 24px; color: var(--table-cell-text); font-size: 0.875rem; border-bottom: 1px solid var(--table-row-border); font-variant-numeric: tabular-nums; white-space: nowrap; }
        .sasha-table tr:hover td { background-color: var(--table-row-hover); }
        .sasha-table tr:last-child td { border-bottom: none; }

        .btn-glass { background: var(--button-bg); color: var(--toolbar-text); backdrop-filter: blur(12px); border: 1px solid var(--button-border); box-shadow: var(--button-shadow); transition: all 0.2s; }
        .btn-glass:hover { background: var(--button-bg-hover); transform: translateY(-1px); }

        .theme-toggle-btn {
            background: var(--theme-chip-bg);
            border: 1px solid var(--theme-chip-border);
            box-shadow: var(--theme-chip-shadow);
            color: var(--toolbar-text);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
        }
        .theme-toggle-btn:hover { transform: translateY(-1px) scale(1.01); }

        .aurora-line {
            position: absolute; inset: 0 auto auto 0; width: 100%; height: 1px;
            background: linear-gradient(90deg, transparent 0%, color-mix(in srgb, var(--brand-color) 65%, transparent) 18%, color-mix(in srgb, var(--accent-pink) 55%, transparent) 48%, color-mix(in srgb, var(--accent-cyan) 55%, transparent) 78%, transparent 100%);
            opacity: 0.85;
        }

        @keyframes slideUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 10px; }
    </style>
</head>
<body class="min-h-screen flex flex-col items-center pt-28 pb-40 px-6 relative">

    <div class="ambient-mesh"></div>
    <div class="ambient-orbit"></div>

    <div class="fixed top-6 left-8 z-50 flex items-center space-x-3">
        <div id="sasha-orb" class="sasha-core"></div>
        <div>
            <h1 class="text-3xl font-bold tracking-tight" style="color: var(--title-color);">Sasha</h1>
            <p class="text-[10px] font-bold uppercase tracking-[0.32em] mt-0.5" style="color: var(--soft-text);">Enterprise Intelligence</p>
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
        <button onclick="toggleHistory()" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold">
            <i data-lucide="layers" class="w-4 h-4 mr-2" style="color: var(--brand-color);"></i>
            <span id="history-btn-text" class="hidden sm:inline">Archive</span>
        </button>
        <a href="/logout" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold">
            <i data-lucide="log-out" class="w-4 h-4 mr-2 text-red-400"></i>
            <span class="hidden sm:inline">Log Out</span>
        </a>
    </div>

    <div class="w-full max-w-5xl z-10 w-full mt-4">
        <div id="history-container" class="hidden space-y-10 relative mb-10">
            <div class="absolute -top-8 left-1/2 -translate-x-1/2 text-[10px] uppercase font-bold tracking-widest px-4 py-1.5 rounded-full border" style="background: var(--history-chip-bg); color: var(--history-chip-text); border-color: var(--history-chip-border);">Conversation Archive</div>
        </div>

        <div id="latest-result"></div>
    </div>

    <div class="fixed bottom-8 left-1/2 transform -translate-x-1/2 w-full max-w-3xl z-50 px-4">
        <div class="spotlight-glass flex items-center px-6 py-3 relative overflow-hidden shadow-xl">
            <div class="aurora-line"></div>
            <div id="loader-bar" class="absolute bottom-0 left-0 h-0.5 w-0 transition-all duration-300" style="background: var(--loader-color);"></div>
            <i data-lucide="search" class="w-6 h-6 mr-4 shrink-0" style="color: var(--soft-text);"></i>
            <input type="text" id="question" class="w-full bg-transparent text-xl py-4 outline-none font-medium" style="color: var(--input-text);" placeholder="Ask Sasha a question...">
            <div id="loading-text" class="hidden text-xs font-bold uppercase tracking-widest ml-4 shrink-0 animate-pulse" style="color: var(--brand-color);">Calculating</div>
        </div>
    </div>

    <script>
        const htmlEl = document.documentElement;
        const themeMeta = document.querySelector('meta[name="theme-color"]');

        function getThemePalette() {
            const styles = getComputedStyle(document.documentElement);
            const brand = styles.getPropertyValue('--chart-line').trim() || '#6366f1';
            const fill = styles.getPropertyValue('--chart-fill').trim() || 'rgba(99, 102, 241, 0.1)';
            const text = styles.getPropertyValue('--chart-text').trim() || '#71717a';
            const grid = styles.getPropertyValue('--chart-grid').trim() || '#f4f4f5';
            const border = htmlEl.classList.contains('dark') ? '#0f172a' : '#ffffff';
            return {
                brand,
                fill,
                text,
                grid,
                doughnut: [brand, '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#0ea5e9'],
                border
            };
        }

        function applyTheme(theme) {
            const isDark = theme === 'dark';
            htmlEl.classList.toggle('dark', isDark);
            htmlEl.classList.toggle('light', !isDark);
            localStorage.setItem('sasha-theme', theme);

            const toggleText = document.getElementById('theme-toggle-text');
            const toggleIcon = document.getElementById('theme-toggle-icon');
            if (toggleText) toggleText.innerText = isDark ? 'Light Mode' : 'Dark Mode';
            if (toggleIcon) {
                toggleIcon.setAttribute('data-lucide', isDark ? 'sun-medium' : 'moon');
            }
            if (themeMeta) themeMeta.setAttribute('content', isDark ? '#020617' : '#6366f1');

            Chart.defaults.color = getThemePalette().text;
            lucide.createIcons();
        }

        function toggleTheme() {
            applyTheme(htmlEl.classList.contains('dark') ? 'light' : 'dark');
        }

        const savedTheme = localStorage.getItem('sasha-theme');
        const preferredTheme = savedTheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
        applyTheme(preferredTheme);

        // PWA ADDITION: Register the Service Worker
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', function() {
                navigator.serviceWorker.register('/sw.js').then(function(registration) {
                    console.log('Sasha PWA ready.');
                }, function(err) {
                    console.log('ServiceWorker registration failed: ', err);
                });
            });
        }

        lucide.createIcons();
        let chartCounter = 0;

        Chart.defaults.font.family = "'Inter', sans-serif";
        Chart.defaults.color = getThemePalette().text;

        document.getElementById("question").addEventListener("keypress", (e) => { if (e.key === "Enter") ask(); });
        document.getElementById("question").style.setProperty('caret-color', getComputedStyle(document.documentElement).getPropertyValue('--brand-color').trim());

        async function resetThread() {
            await fetch('/reset', { method: 'POST' });
            document.getElementById('latest-result').innerHTML = '';
            document.getElementById('history-container').innerHTML = `<div class="absolute -top-8 left-1/2 -translate-x-1/2 text-[10px] uppercase font-bold tracking-widest px-4 py-1.5 rounded-full border" style="background: var(--history-chip-bg); color: var(--history-chip-text); border-color: var(--history-chip-border);">Conversation Archive</div>`;
            document.getElementById('question').value = '';
        }

        function toggleHistory() {
            const hist = document.getElementById('history-container');
            const btnText = document.getElementById('history-btn-text');
            if (hist.classList.contains('hidden')) {
                hist.classList.remove('hidden'); btnText.innerText = 'Hide Archive';
            } else {
                hist.classList.add('hidden'); btnText.innerText = 'Archive';
            }
        }

        function exportExcel(tableId) {
            const table = document.getElementById(tableId);
            const wb = XLSX.utils.table_to_book(table, {sheet: "Sasha Analysis"});
            XLSX.writeFile(wb, "Sasha_Data_Export.xlsx");
        }

        function exportPDF(cardId) {
            const element = document.getElementById(cardId);
            const opt = {
                margin: 0.5, filename: 'Sasha_Executive_Report.pdf',
                image: { type: 'jpeg', quality: 0.98 },
                html2canvas: { scale: 2 },
                jsPDF: { unit: 'in', format: 'letter', orientation: 'landscape' }
            };
            html2pdf().set(opt).from(element).save();
        }

        async function ask() {
            const q = document.getElementById('question').value;
            if (!q) return;

            const latestContainer = document.getElementById('latest-result');
            const historyContainer = document.getElementById('history-container');
            const loaderText = document.getElementById('loading-text');
            const loaderBar = document.getElementById('loader-bar');
            const orb = document.getElementById('sasha-orb');

            if (latestContainer.children.length > 0) {
                const pastResult = latestContainer.firstElementChild;
                pastResult.classList.remove('executive-card');
                pastResult.classList.add('rounded-3xl', 'border', 'opacity-60', 'hover:opacity-100', 'transition-all', 'duration-300', 'scale-[0.98]');
                pastResult.style.background = 'var(--card-bg)';
                pastResult.style.borderColor = 'var(--card-border)';
                historyContainer.appendChild(pastResult); // Append to bottom of history
                historyContainer.classList.remove('hidden'); // Auto show history so flow is natural
                document.getElementById('history-btn-text').innerText = 'Hide Archive';
            }

            // UI Loading State
            orb.classList.add('thinking');
            loaderText.classList.remove('hidden');
            loaderBar.style.width = '30%';

            // --- NEW FIX: Instantly drop a Loading Card with the user's Question ---
            latestContainer.innerHTML = `
                <div class="executive-card p-10 mb-10 border-2" id="loading-card" style="border-color: color-mix(in srgb, var(--brand-color) 18%, var(--card-border));">
                    <div class="flex items-center space-x-4 mb-6">
                        <div class="sasha-core thinking shrink-0"></div>
                        <span class="inline-flex items-center px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest" style="background: color-mix(in srgb, var(--brand-color) 10%, transparent); color: var(--brand-color); border: 1px solid color-mix(in srgb, var(--brand-color) 12%, transparent);">
                            <i data-lucide="sparkles" class="w-3 h-3 mr-1.5"></i> Q: ${q}
                        </span>
                    </div>
                    <div class="animate-pulse flex space-x-4 mt-6">
                        <div class="flex-1 space-y-4 py-1">
                            <div class="h-4 rounded w-3/4" style="background: color-mix(in srgb, var(--brand-color) 10%, var(--table-row-hover));"></div>
                            <div class="space-y-3">
                                <div class="h-4 rounded" style="background: color-mix(in srgb, var(--brand-color) 6%, var(--table-row-hover));"></div>
                                <div class="h-4 rounded w-5/6" style="background: color-mix(in srgb, var(--brand-color) 6%, var(--table-row-hover));"></div>
                            </div>
                        </div>
                    </div>
                </div>
            `;
            lucide.createIcons();
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
            // -----------------------------------------------------------------------

            let progress = 30;
            const progressInterval = setInterval(() => { if(progress < 85) { progress += 5; loaderBar.style.width = `${progress}%`; } }, 800);

            try {
                const res = await fetch('/ask', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({question: q})
                });

                clearInterval(progressInterval);
                loaderBar.style.width = '100%';
                const data = await res.json();

                setTimeout(() => {
                    orb.classList.remove('thinking');
                    loaderText.classList.add('hidden');
                    loaderBar.style.width = '0%';
                }, 400);

                chartCounter++;
                const uniqueChartId = 'chart-' + chartCounter;
                const wrapper = document.createElement('div');
                wrapper.className = "executive-card p-0 mb-10";
                wrapper.id = `card-${chartCounter}`;

                if (data.error) {
                    wrapper.innerHTML = `
                        <div class="p-8 flex items-start space-x-4" style="background: var(--error-bg);">
                            <i data-lucide="shield-alert" class="w-6 h-6 text-red-500 shrink-0"></i>
                            <div>
                                <h3 class="font-bold mb-1" style="color: var(--title-color);">Execution Halted</h3>
                                <p class="font-medium text-sm leading-relaxed" style="color: var(--error-card-text);">${data.error}</p>
                            </div>
                        </div>
                    `;
                } else {
                    let badgeHtml = '';
                    if (data.health_badge === 'Red') badgeHtml = `<span class="px-3 py-1 rounded-full bg-red-50/90 border border-red-200 text-red-700 text-[10px] font-bold uppercase tracking-widest flex items-center dark:bg-red-950/50 dark:border-red-900 dark:text-red-200"><div class="w-1.5 h-1.5 rounded-full bg-red-500 mr-2"></div> High Risk</span>`;
                    else if (data.health_badge === 'Yellow') badgeHtml = `<span class="px-3 py-1 rounded-full bg-yellow-50/90 border border-yellow-200 text-yellow-700 text-[10px] font-bold uppercase tracking-widest flex items-center dark:bg-yellow-950/40 dark:border-yellow-900 dark:text-yellow-200"><div class="w-1.5 h-1.5 rounded-full bg-yellow-500 mr-2"></div> Attention</span>`;
                    else if (data.health_badge === 'Green') badgeHtml = `<span class="px-3 py-1 rounded-full bg-green-50/90 border border-green-200 text-green-700 text-[10px] font-bold uppercase tracking-widest flex items-center dark:bg-green-950/40 dark:border-green-900 dark:text-green-200"><div class="w-1.5 h-1.5 rounded-full bg-green-500 mr-2"></div> Healthy</span>`;

                    let insightHtml = '';
                    if (data.sasha_insight) {
                        insightHtml = `
                        <div class="mb-8 rounded-2xl p-6" style="background: var(--insight-bg); border: 1px solid var(--insight-border);">
                            <div class="flex items-center mb-3">
                                <i data-lucide="brain-circuit" class="w-4 h-4 mr-2" style="color: var(--brand-color);"></i>
                                <h4 class="text-xs font-bold uppercase tracking-widest" style="color: var(--insight-title);">Sasha's Insight</h4>
                            </div>
                            <p class="text-sm leading-relaxed" style="color: var(--insight-text);">${data.sasha_insight}</p>
                        </div>`;
                    }

                    let suggestionsHtml = '';
                    if (data.suggestions && data.suggestions.length > 0) {
                        const btns = data.suggestions.map(s => `<button onclick="document.getElementById('question').value='${s}'; ask();" class="text-xs px-4 py-2 rounded-full transition-colors border" style="background: var(--button-bg); border-color: var(--button-border); color: var(--toolbar-text);">${s}</button>`).join('');
                        suggestionsHtml = `<div class="p-6 border-t flex flex-wrap gap-2 items-center" style="background: var(--card-bg); border-color: var(--footer-border);"><span class="text-[10px] font-bold uppercase tracking-widest mr-2" style="color: var(--soft-text);">Suggested Next Steps:</span>${btns}</div>`;
                    }

                    wrapper.innerHTML = `
                        <div class="p-10 pb-8">
                            <div class="flex justify-between items-start mb-6">
                                <span class="inline-flex items-center px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest" style="background: color-mix(in srgb, var(--brand-color) 10%, transparent); color: var(--brand-color); border: 1px solid color-mix(in srgb, var(--brand-color) 12%, transparent);">
                                    <i data-lucide="sparkles" class="w-3 h-3 mr-1.5"></i> Q: ${q}
                                </span>
                                ${badgeHtml}
                            </div>
                            <h2 class="text-3xl md:text-4xl font-bold leading-tight tracking-tight mb-8" style="color: var(--title-color);">${data.headline}</h2>

                            ${insightHtml}

                            ${data.chart_intent !== 'none' && data.raw_data && data.raw_data.length > 0 ? `<div class="h-[350px] w-full mb-10"><canvas id="${uniqueChartId}"></canvas></div>` : ''}
                        </div>

                        <div id="table-${chartCounter}" class="overflow-x-auto border-y" style="border-color: var(--footer-border); background: var(--card-bg);">
                            ${data.html_table}
                        </div>

                        <div class="px-10 py-4 flex space-x-3" style="background: var(--footer-bg);">
                            <button onclick="exportExcel('table-${chartCounter}')" class="flex items-center px-4 py-2 text-xs font-bold rounded-lg transition-colors border shadow-sm bg-white hover:bg-green-50 text-green-700 border-green-200 dark:bg-slate-900 dark:hover:bg-green-950/40 dark:text-green-300 dark:border-green-900">
                                <i data-lucide="file-spreadsheet" class="w-4 h-4 mr-2"></i> Download Excel
                            </button>
                            <button onclick="exportPDF('card-${chartCounter}')" class="flex items-center px-4 py-2 text-xs font-bold rounded-lg transition-colors border shadow-sm bg-white hover:bg-red-50 text-red-700 border-red-200 dark:bg-slate-900 dark:hover:bg-red-950/40 dark:text-red-300 dark:border-red-900">
                                <i data-lucide="file-text" class="w-4 h-4 mr-2"></i> Export to PDF
                            </button>
                        </div>

                        ${suggestionsHtml}

                        <details class="group border-t" style="background: var(--details-bg); border-color: var(--footer-border);">
                            <summary class="cursor-pointer px-10 py-4 text-xs font-semibold uppercase tracking-widest list-none flex items-center transition-colors" style="color: var(--soft-text);">
                                <i data-lucide="terminal" class="w-4 h-4 mr-2"></i> View Agent Logic
                                <i data-lucide="chevron-down" class="w-4 h-4 ml-auto transition-transform group-open:rotate-180"></i>
                            </summary>
                            <div class="px-10 py-6 overflow-x-auto" style="background: var(--code-bg);">
                                <code class="text-[11px] font-mono whitespace-pre-wrap leading-relaxed" style="color: var(--code-text);">${data.python_code}</code>
                            </div>
                        </details>
                    `;
                }

                latestContainer.innerHTML = '';
                latestContainer.appendChild(wrapper);

                if (!data.error && data.chart_intent !== 'none' && data.raw_data && data.raw_data.length > 0) {
                    const chartRows = data.raw_data
                        .map(r => ({
                            label: String(r.label ?? ''),
                            value: Number(
                                typeof r.value === 'string'
                                    ? r.value.replace(/,/g, '').trim()
                                    : r.value
                            )
                        }))
                        .filter(r => r.label && Number.isFinite(r.value));

                    if (chartRows.length >= 2) {
                        const palette = getThemePalette();
                        const ctx = document.getElementById(uniqueChartId).getContext('2d');
                        const isDoughnut = data.chart_intent === 'doughnut';

                        new Chart(ctx, {
                            type: data.chart_intent,
                            data: {
                                labels: chartRows.map(r => r.label),
                                datasets: [{
                                    data: chartRows.map(r => r.value),
                                    backgroundColor: isDoughnut ? palette.doughnut : palette.fill,
                                    borderColor: isDoughnut ? palette.border : palette.brand,
                                    borderWidth: 2,
                                    borderRadius: isDoughnut ? 0 : 8,
                                    fill: data.chart_intent === 'line',
                                    tension: 0.4
                                }]
                            },
                            options: {
                                responsive: true,
                                maintainAspectRatio: false,
                                plugins: {
                                    legend: {
                                        display: isDoughnut,
                                        position: 'right',
                                        labels: { color: palette.text, boxWidth: 14, boxHeight: 14, usePointStyle: true, pointStyle: 'circle' }
                                    }
                                },
                                scales: {
                                    y: {
                                        display: !isDoughnut,
                                        border: { display: false },
                                        grid: { color: palette.grid },
                                        ticks: { color: palette.text }
                                    },
                                    x: {
                                        display: !isDoughnut,
                                        border: { display: false },
                                        grid: { display: false },
                                        ticks: { color: palette.text }
                                    }
                                }
                            }
                        });
                    }
                }
                lucide.createIcons();
                document.getElementById('question').value = '';

                // Infinite Scroll Action
                window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

            } catch (err) {
                orb.classList.remove('thinking');
                clearInterval(progressInterval);
                loaderText.classList.add('hidden');
                loaderBar.style.width = '0%';
                latestContainer.innerHTML = `<div class="executive-card p-8 text-center font-medium" style="color: var(--error-card-text); background: var(--card-bg);">System failure: Sasha offline.</div>`;
            }
        }
    </script>
</body>

</html>
"""

if __name__ == '__main__':
    app.run(port=5000, debug=True)