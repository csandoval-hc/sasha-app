import pandas as pd
import json
import os
import sys
import re
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
    with open(MEMORY_FILE, 'w') as f: json.dump({"_init": "Memory initialized."}, f)

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
# 3. THE AGENTIC BRAIN WITH CONVERSATION MEMORY & NEW CAPABILITIES
# ==============================================================================
def execute_agent_code(code_str, role="Viewer"): # <-- ADDED ROLE PARAMETER
    if re.search(r"import\s+(os|sys|subprocess|shutil|pathlib)", code_str):
        return None, None, None, None, None, None, None, "Security Violation: System imports blocked."
    
    forbidden_patterns = [
        r"\bimport\b",
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
    
    # <-- ADDED: GIVE THE 'HANDS' ONLY TO ADMINS AND COLLABORATORS
    if role in ['Admin', 'Collaborator']:
        local_env['save_to_memory'] = save_to_memory
        
    try:
        exec_globals = {"__builtins__": {}, "pd": pd, "datetime": datetime}
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

def agentic_brain(question, history, role="Viewer"): # <-- ADDED ROLE PARAMETER
    now = datetime.now()
    current_time_str = now.strftime("%A, %B %d, %Y - %H:%M:%S")
    
    # <-- ADDED: READ LONG TERM MEMORY
    try:
        with open(MEMORY_FILE, 'r') as f:
            long_term_memory = json.dumps(json.load(f))
    except:
        long_term_memory = "No custom rules saved yet."
    
    schema_info = ""
    for name, df in dfs.items():
        schema_info += f"DataFrame: dfs['{name}'] | Columns: {', '.join(df.columns)}\n"

    # <-- ADDED MEMORY & PERMISSIONS TO YOUR UNTOUCHED PROMPT
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
    `sasha_insight`: A string paragraph explaining 'Why' or providing deep analysis ONLY IF the user asks for explanation/analysis. Otherwise set to ''.
    `health_badge`: String 'Red' (high risk/bad), 'Yellow' (neutral/warning), 'Green' (healthy/good), or 'None'.
    `suggestions`: A Python list of 3 strings representing smart, proactive follow-up questions based on this data.
    
    CRITICAL LANGUAGE RULE: Always respond in the user's language natively, but keep the JSON keys and Python variable names strictly in English.
    NEVER use destructive commands. Fuzzy search strings.
    """

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-10:]: 
        messages.append(msg)
    messages.append({"role": "user", "content": question})

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0
            )
            ai_msg = json.loads(response.choices[0].message.content)
            
            # <-- ADDED ROLE INJECTION HERE
            df, headline, c_type, c_data, insight, badge, suggs, error = execute_agent_code(ai_msg['python_code'], role)
            
            if error:
                messages.append({"role": "assistant", "content": ai_msg['python_code']})
                messages.append({"role": "user", "content": f"Code failed: {error}. Fix and retry."})
                continue

            # --- CRASH FIX APPLIED HERE: Only format if df exists ---
            styled_table = ""
            if df is not None:
                for col in df.select_dtypes(include=['datetime64', 'object']):
                    try: df[col] = pd.to_datetime(df[col]).dt.strftime('%Y-%m-%d')
                    except: pass

                # --- NEW FIX: Format all numbers to prevent scientific notation (e+...) and add commas ---
                for col in df.select_dtypes(include=['number']).columns:
                    df[col] = df[col].apply(lambda x: f"{x:,.2f}" if pd.notnull(x) else x)
                # ---------------------------------------------------------------------------------------

                styled_table = df.to_html(classes='sasha-table', index=False, border=0)
            # --------------------------------------------------------

            # ==================================================================
            # THE ULTIMATE SAFETY NET: Catches Series, DataFrames, & Numpy errors
            # ==================================================================
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
                            clean_item = {}
                            for k, v in item.items():
                                if hasattr(v, 'item'): 
                                    clean_item[k] = v.item()
                                else:
                                    clean_item[k] = v
                            clean_data.append(clean_item)
                    c_data = clean_data
            except Exception:
                c_data = [] # Ultimate fail-safe

            # --- NEW FIX: Hard override to kill useless empty charts ---
            if not c_data or len(c_data) < 2:
                c_type = 'none'
            # -----------------------------------------------------------
            # ==================================================================

            # --- NULL CATCHER APPLIED HERE ---
            return {
                "headline": headline if headline else "Got it! Memory updated. / ¡Entendido! Memoria actualizada.",
                "python_code": ai_msg['python_code'],
                "html_table": styled_table,
                "chart_intent": c_type,
                "raw_data": c_data,
                "sasha_insight": insight if insight else "",
                "health_badge": badge if badge else "None",
                "suggestions": suggs if suggs else []
            }
        except Exception as e:
            return {"error": str(e)}
    return {"error": "Sasha failed to resolve the code after 3 attempts."}

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
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Sasha | Auth</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
    <style>body { font-family: 'Inter', sans-serif; background-color: #fafafa; }</style>
</head>
<body class="min-h-screen flex items-center justify-center p-6">
    <div class="w-full max-w-sm bg-white p-8 rounded-[2rem] shadow-xl border border-gray-100">
        <h1 class="text-2xl font-bold text-gray-900 text-center mb-6">Sasha Intelligence</h1>
        <form method="POST" class="space-y-4">
            <input type="text" name="username" class="w-full px-4 py-3 bg-gray-50 border border-gray-100 rounded-xl outline-none" placeholder="Username" required>
            <input type="password" name="password" class="w-full px-4 py-3 bg-gray-50 border border-gray-100 rounded-xl outline-none" placeholder="Password" required>
            {% if error %}<p class="text-red-500 text-xs text-center">{{ error }}</p>{% endif %}
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3 rounded-xl transition-all">Login</button>
        </form>
    </div>
</body>
</html>
"""

# ==============================================================================
# UNTOUCHED 600-LINE HTML BLOCK EXACTLY AS YOU PROVIDED IT
# ==============================================================================
HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
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
            --bg-color: #fafafa;
        }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: #09090b; overflow-x: hidden; scroll-behavior: smooth; }
        
        .ambient-mesh {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -1;
            background: radial-gradient(circle at 50% 10%, rgba(99, 102, 241, 0.08) 0%, transparent 60%),
                        radial-gradient(circle at 80% 80%, rgba(236, 72, 153, 0.05) 0%, transparent 50%);
        }

        .sasha-core {
            width: 32px; height: 32px; border-radius: 50%;
            background: transparent;
            background-image: url('/static/logo.png');
            background-size: contain;
            background-repeat: no-repeat;
            background-position: center;
            box-shadow: 0 0 15px var(--brand-color), 0 0 30px var(--brand-color);
            animation: breathe 3s infinite ease-in-out;
        }
        .sasha-core.thinking { animation: pulse-fast 0.8s infinite alternate; box-shadow: 0 0 20px #ec4899; }
        
        @keyframes breathe { 0%, 100% { transform: scale(1); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } }
        @keyframes pulse-fast { 0% { transform: scale(0.9); opacity: 0.7; } 100% { transform: scale(1.3); opacity: 1; } }

        .spotlight-glass {
            background: rgba(255, 255, 255, 0.75);
            backdrop-filter: blur(40px);
            -webkit-backdrop-filter: blur(40px);
            border: 1px solid rgba(255, 255, 255, 0.6);
            box-shadow: 0 8px 32px -4px rgba(0,0,0,0.06), 0 0 0 1px rgba(0,0,0,0.02);
            border-radius: 2rem;
            transition: all 0.4s cubic-bezier(0.2, 0.8, 0.2, 1);
        }
        .spotlight-glass:focus-within {
            box-shadow: 0 20px 60px -8px rgba(99, 102, 241, 0.15), 0 0 0 1.5px rgba(99, 102, 241, 0.4);
            transform: translateY(-2px); background: rgba(255, 255, 255, 0.9);
        }

        .executive-card {
            background: #FFFFFF; border-radius: 24px;
            box-shadow: 0 12px 40px -12px rgba(0,0,0,0.08);
            border: 1px solid rgba(228, 228, 231, 0.8);
            animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            overflow: hidden;
        }

        .sasha-table { width: 100%; border-collapse: collapse; text-align: left; }
        .sasha-table thead { background: #f4f4f5; }
        .sasha-table th { color: #71717a; font-weight: 600; font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; padding: 14px 24px; border-bottom: 1px solid #e4e4e7; white-space: nowrap; }
        .sasha-table td { padding: 16px 24px; color: #27272a; font-size: 0.875rem; border-bottom: 1px solid #f4f4f5; font-variant-numeric: tabular-nums; white-space: nowrap; }
        .sasha-table tr:hover td { background-color: #fafafa; }
        .sasha-table tr:last-child td { border-bottom: none; }

        .btn-glass { background: rgba(255, 255, 255, 0.6); backdrop-filter: blur(12px); border: 1px solid rgba(0,0,0,0.05); box-shadow: 0 2px 10px rgba(0,0,0,0.02); transition: all 0.2s; }
        .btn-glass:hover { background: rgba(255, 255, 255, 0.9); transform: translateY(-1px); }
        
        @keyframes slideUp { from { opacity: 0; transform: translateY(30px); } to { opacity: 1; transform: translateY(0); } }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #d4d4d8; border-radius: 10px; }
    </style>
</head>
<body class="min-h-screen flex flex-col items-center pt-28 pb-40 px-6 relative">
    
    <div class="ambient-mesh"></div>

    <div class="fixed top-6 left-8 z-50 flex items-center space-x-3">
        <div id="sasha-orb" class="sasha-core"></div>
        <h1 class="text-3xl font-bold tracking-tight text-gray-900">Sasha</h1>
    </div>

    <div class="fixed top-6 right-8 z-50 flex space-x-3">
        <button onclick="resetThread()" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold text-gray-700">
            <i data-lucide="refresh-cw" class="w-4 h-4 mr-2 text-indigo-500"></i>
            <span class="hidden sm:inline">New Thread</span>
        </button>
        <button onclick="toggleHistory()" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold text-gray-700">
            <i data-lucide="layers" class="w-4 h-4 mr-2 text-indigo-500"></i>
            <span id="history-btn-text" class="hidden sm:inline">Archive</span>
        </button>
        <a href="/logout" class="btn-glass flex items-center px-5 py-2.5 rounded-full text-sm font-semibold text-gray-700">
            <i data-lucide="log-out" class="w-4 h-4 mr-2 text-red-400"></i>
            <span class="hidden sm:inline">Log Out</span>
        </a>
    </div>

    <div class="w-full max-w-5xl z-10 w-full mt-4">
        <div id="history-container" class="hidden space-y-10 relative mb-10">
            <div class="absolute -top-8 left-1/2 -translate-x-1/2 bg-gray-100 text-gray-400 text-[10px] uppercase font-bold tracking-widest px-4 py-1.5 rounded-full border border-gray-200">Conversation Archive</div>
        </div>
        
        <div id="latest-result"></div>
    </div>

    <div class="fixed bottom-8 left-1/2 transform -translate-x-1/2 w-full max-w-3xl z-50 px-4">
        <div class="spotlight-glass flex items-center px-6 py-3 relative overflow-hidden shadow-xl">
            <div id="loader-bar" class="absolute bottom-0 left-0 h-0.5 bg-indigo-500 w-0 transition-all duration-300"></div>
            <i data-lucide="search" class="w-6 h-6 text-gray-400 mr-4 shrink-0"></i>
            <input type="text" id="question" class="w-full bg-transparent text-xl py-4 outline-none text-gray-900 placeholder-gray-300 font-medium" placeholder="Ask Sasha a question...">
            <div id="loading-text" class="hidden text-xs font-bold text-indigo-500 uppercase tracking-widest ml-4 shrink-0 animate-pulse">Calculating</div>
        </div>
    </div>

    <script>
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
        Chart.defaults.color = '#71717a';

        document.getElementById("question").addEventListener("keypress", (e) => { if (e.key === "Enter") ask(); });

        async function resetThread() {
            await fetch('/reset', { method: 'POST' });
            document.getElementById('latest-result').innerHTML = '';
            document.getElementById('history-container').innerHTML = `<div class="absolute -top-8 left-1/2 -translate-x-1/2 bg-gray-100 text-gray-400 text-[10px] uppercase font-bold tracking-widest px-4 py-1.5 rounded-full border border-gray-200">Conversation Archive</div>`;
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
                pastResult.classList.add('bg-white', 'rounded-3xl', 'border', 'border-gray-100', 'opacity-60', 'hover:opacity-100', 'transition-all', 'duration-300', 'scale-[0.98]');
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
                <div class="executive-card p-10 mb-10 border-indigo-100 border-2" id="loading-card">
                    <div class="flex items-center space-x-4 mb-6">
                        <div class="sasha-core thinking shrink-0"></div>
                        <span class="inline-flex items-center px-3 py-1 rounded-full bg-indigo-50 text-indigo-600 text-[10px] font-bold uppercase tracking-widest">
                            <i data-lucide="sparkles" class="w-3 h-3 mr-1.5"></i> Q: ${q}
                        </span>
                    </div>
                    <div class="animate-pulse flex space-x-4 mt-6">
                        <div class="flex-1 space-y-4 py-1">
                            <div class="h-4 bg-gray-100 rounded w-3/4"></div>
                            <div class="space-y-3">
                                <div class="h-4 bg-gray-50 rounded"></div>
                                <div class="h-4 bg-gray-50 rounded w-5/6"></div>
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
                        <div class="p-8 flex items-start space-x-4 bg-red-50/50">
                            <i data-lucide="shield-alert" class="w-6 h-6 text-red-500 shrink-0"></i>
                            <div>
                                <h3 class="font-bold text-gray-900 mb-1">Execution Halted</h3>
                                <p class="text-red-700 font-medium text-sm leading-relaxed">${data.error}</p>
                            </div>
                        </div>
                    `;
                } else {
                    let badgeHtml = '';
                    if (data.health_badge === 'Red') badgeHtml = `<span class="px-3 py-1 rounded-full bg-red-50 border border-red-200 text-red-700 text-[10px] font-bold uppercase tracking-widest flex items-center"><div class="w-1.5 h-1.5 rounded-full bg-red-500 mr-2"></div> High Risk</span>`;
                    else if (data.health_badge === 'Yellow') badgeHtml = `<span class="px-3 py-1 rounded-full bg-yellow-50 border border-yellow-200 text-yellow-700 text-[10px] font-bold uppercase tracking-widest flex items-center"><div class="w-1.5 h-1.5 rounded-full bg-yellow-500 mr-2"></div> Attention</span>`;
                    else if (data.health_badge === 'Green') badgeHtml = `<span class="px-3 py-1 rounded-full bg-green-50 border border-green-200 text-green-700 text-[10px] font-bold uppercase tracking-widest flex items-center"><div class="w-1.5 h-1.5 rounded-full bg-green-500 mr-2"></div> Healthy</span>`;

                    let insightHtml = '';
                    if (data.sasha_insight) {
                        insightHtml = `
                        <div class="mb-8 bg-indigo-50/50 border border-indigo-100 rounded-2xl p-6">
                            <div class="flex items-center mb-3">
                                <i data-lucide="brain-circuit" class="w-4 h-4 text-indigo-500 mr-2"></i>
                                <h4 class="text-xs font-bold text-indigo-900 uppercase tracking-widest">Sasha's Insight</h4>
                            </div>
                            <p class="text-sm text-gray-700 leading-relaxed">${data.sasha_insight}</p>
                        </div>`;
                    }

                    let suggestionsHtml = '';
                    if (data.suggestions && data.suggestions.length > 0) {
                        const btns = data.suggestions.map(s => `<button onclick="document.getElementById('question').value='${s}'; ask();" class="text-xs bg-gray-50 hover:bg-indigo-50 border border-gray-200 text-gray-600 hover:text-indigo-700 px-4 py-2 rounded-full transition-colors">${s}</button>`).join('');
                        suggestionsHtml = `<div class="p-6 bg-white border-t border-gray-100 flex flex-wrap gap-2 items-center"><span class="text-[10px] font-bold text-gray-400 uppercase tracking-widest mr-2">Suggested Next Steps:</span>${btns}</div>`;
                    }

                    wrapper.innerHTML = `
                        <div class="p-10 pb-8">
                            <div class="flex justify-between items-start mb-6">
                                <span class="inline-flex items-center px-3 py-1 rounded-full bg-indigo-50 text-indigo-600 text-[10px] font-bold uppercase tracking-widest">
                                    <i data-lucide="sparkles" class="w-3 h-3 mr-1.5"></i> Q: ${q}
                                </span>
                                ${badgeHtml}
                            </div>
                            <h2 class="text-3xl md:text-4xl font-bold text-gray-900 leading-tight tracking-tight mb-8">${data.headline}</h2>
                            
                            ${insightHtml}
                            
                            ${data.chart_intent !== 'none' && data.raw_data && data.raw_data.length > 0 ? `<div class="h-[350px] w-full mb-10"><canvas id="${uniqueChartId}"></canvas></div>` : ''}
                        </div>
                        
                        <div id="table-${chartCounter}" class="overflow-x-auto border-y border-gray-100 bg-white">
                            ${data.html_table}
                        </div>

                        <div class="px-10 py-4 bg-gray-50/50 flex space-x-3">
                            <button onclick="exportExcel('table-${chartCounter}')" class="flex items-center px-4 py-2 bg-white hover:bg-green-50 text-green-700 text-xs font-bold rounded-lg transition-colors border border-green-200 shadow-sm">
                                <i data-lucide="file-spreadsheet" class="w-4 h-4 mr-2"></i> Download Excel
                            </button>
                            <button onclick="exportPDF('card-${chartCounter}')" class="flex items-center px-4 py-2 bg-white hover:bg-red-50 text-red-700 text-xs font-bold rounded-lg transition-colors border border-red-200 shadow-sm">
                                <i data-lucide="file-text" class="w-4 h-4 mr-2"></i> Export to PDF
                            </button>
                        </div>
                        
                        ${suggestionsHtml}
                        
                        <details class="group bg-gray-50 border-t border-gray-100">
                            <summary class="cursor-pointer px-10 py-4 text-xs font-semibold text-gray-500 uppercase tracking-widest list-none flex items-center hover:bg-gray-100 transition-colors">
                                <i data-lucide="terminal" class="w-4 h-4 mr-2"></i> View Agent Logic
                                <i data-lucide="chevron-down" class="w-4 h-4 ml-auto transition-transform group-open:rotate-180"></i>
                            </summary>
                            <div class="px-10 py-6 bg-[#09090b] overflow-x-auto">
                                <code class="text-[11px] text-[#a5b4fc] font-mono whitespace-pre-wrap leading-relaxed">${data.python_code}</code>
                            </div>
                        </details>
                    `;
                }

                latestContainer.innerHTML = '';
                latestContainer.appendChild(wrapper);

                if (!data.error && data.chart_intent !== 'none' && data.raw_data && data.raw_data.length > 0) {
                    const ctx = document.getElementById(uniqueChartId).getContext('2d');
                    const isDoughnut = data.chart_intent === 'doughnut';
                    const chartColors = ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#0ea5e9'];

                    new Chart(ctx, {
                        type: data.chart_intent,
                        data: {
                            labels: data.raw_data.map(r => r.label),
                            datasets: [{
                                data: data.raw_data.map(r => r.value),
                                backgroundColor: isDoughnut ? chartColors : 'rgba(99, 102, 241, 0.1)',
                                borderColor: isDoughnut ? '#ffffff' : '#6366f1',
                                borderWidth: 2,
                                borderRadius: isDoughnut ? 0 : 4,
                                fill: data.chart_intent === 'line',
                                tension: 0.4
                            }]
                        },
                        options: { 
                            responsive: true, maintainAspectRatio: false, 
                            plugins: { legend: { display: isDoughnut, position: 'right' } },
                            scales: {
                                y: { display: !isDoughnut, border: { display: false }, grid: { color: '#f4f4f5' } },
                                x: { display: !isDoughnut, border: { display: false }, grid: { display: false } }
                            }
                        }
                    });
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
                latestContainer.innerHTML = `<div class="executive-card p-8 text-center text-red-500 font-medium">System failure: Sasha offline.</div>`;
            }
        }
    </script>
</body>

</html>
"""

if __name__ == '__main__':
    app.run(port=5000, debug=True)