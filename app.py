import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import json
import traceback
import re
import uuid
import threading
import time
import requests
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_dance.contrib.google import make_google_blueprint, google

# 🌟 第一步：立刻讀取 .env 檔案！
load_dotenv()

# 允許在本地端測試 Google 登入
if os.getenv("FLASK_ENV") == "development":
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# 初始化 Flask
app = Flask(__name__)
# 密碼改由環境變數讀取，增強安全性
app.secret_key = os.getenv("FLASK_SECRET_KEY", "nthu_cheme_secret_key_fallback")

# 設定路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(BASE_DIR, 'requirements(3).json') 
JSON_PATH_2 = os.path.join(BASE_DIR, 'tsmc_program_rules.json')


# 防止瀏覽器快取 (避免上一頁卡住)
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# ==========================================
# 🌟 Google 登入與資料庫連線
# ==========================================
blueprint = make_google_blueprint(
    client_id=os.getenv("Client_ID"),
    client_secret=os.getenv("Client_Secret"), 
    scope=["profile", "email"],
    offline=True
)
app.register_blueprint(blueprint, url_prefix="/login")

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ==========================================
# 🌟 核心工具與資料處理
# ==========================================
def get_core_id(cid):
    if not cid: return ""
    cid = str(cid).upper().replace(" ", "")
    cid = re.sub(r'^\d{5}', '', cid)
    match = re.search(r'([A-Z]+)(\d{4})', cid)
    return match.group(1) + match.group(2) if match else cid

def fetch_all_courses(paths):
    combined_courses = []
    for path in paths:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    combined_courses.extend(data)
                elif isinstance(data, dict):
                    if "name" in data:
                        combined_courses.append(data)
                    else:
                        combined_courses.extend(data.values())
        except Exception as e:
            print(f"⚠️ 讀取 {path} 時發生錯誤: {e}")
    return combined_courses
def filter_latest_semester_courses(raw_courses):
    """
    改良版過濾器：
    1. 找出每門課（依據課名）最新的學期代號（例如 11510）。
    2. 保留該最新學期的「所有班級/節次」，避免不同班級互相覆蓋。
    3. 只有舊學期才開的課（例如 11420），依然完整保留。
    """
    # 步驟一：記錄每門課的最大學期代號
    max_terms = {}
    for c in raw_courses:
        if not isinstance(c, dict): continue
        name = str(c.get('name', '')).strip()
        c_id = str(c.get('id', '')).strip()
        
        if name.lower() == 'blank' or not name:
            continue
            
        # 擷取課號前五碼 (例如 '11510')，若無則視為 '00000'
        term = c_id[:5] if len(c_id) >= 5 and c_id[:5].isdigit() else "00000"
        
        if name not in max_terms:
            max_terms[name] = term
        else:
            if term > max_terms[name]:
                max_terms[name] = term
                
    # 步驟二：只保留符合最大學期代號的課程（保留多個班級）
    filtered_courses = []
    for c in raw_courses:
        if not isinstance(c, dict): continue
        name = str(c.get('name', '')).strip()
        c_id = str(c.get('id', '')).strip()
        
        if name.lower() == 'blank' or not name:
            filtered_courses.append(c)
            continue
            
        term = c_id[:5] if len(c_id) >= 5 and c_id[:5].isdigit() else "00000"
        
        # 只要這堂課的學期等於該課名紀錄的「最新學期」，就保留它！
        # 這樣同一個學期的 A 班、B 班就都能順利活下來
        if term == max_terms[name]:
            filtered_courses.append(c)
            
    return filtered_courses

# 1. 先把 JSON 裡所有的資料不管新舊全部抓出來
ALL_RAW_COURSES = fetch_all_courses([JSON_PATH])

TSMC_RULES = {}
try:
    with open(JSON_PATH_2, 'r', encoding='utf-8') as f:
        TSMC_RULES = json.load(f)
except Exception as e:
    print(f"⚠️ 嚴重警告：讀取 {JSON_PATH_2} 發生錯誤: {e}")

ALL_TSMC_PROGRAMS = list(TSMC_RULES.keys())

# 建立供排課搜尋引擎使用的 COURSE_DATA
COURSE_DATA = {}
for c in ALL_RAW_COURSES:
    if isinstance(c, dict):
        c_name = str(c.get('name', '')).strip()
        c_id = str(c.get('id', '')).strip()
        t_str = str(c.get('time_slots', ''))
        
        if c_name.lower() == 'blank':
            c_id = f"空堂-{t_str}" if t_str else f"空堂-{uuid.uuid4().hex[:4]}"
            
        key = f"{c_name} ({c_id})" if c_id else c_name
        
        # 轉換為標準時間陣列，如 ['M3', 'M4', 'W1']
        times = [t[0].upper() + t[1:].lower() for t in re.findall(r'[MTWRFS][1-9a-cnA-CN]', t_str)]
        sem_raw = str(c.get('semester', '')).strip()
        ctype = str(c.get('type', '')).upper()
        
        default_year, default_semester = '其他', '上學期'
        if '-' in sem_raw and re.match(r'^\d+-\d+$', sem_raw):
            y_part, s_part = sem_raw.split('-')
            if y_part == '1': default_year = '大一'
            elif y_part == '2': default_year = '大二'
            elif y_part == '3': default_year = '大三'
            elif y_part == '4': default_year = '大四'
            if s_part == '1': default_semester = '上學期'
            elif s_part == '2': default_semester = '下學期'
        else:
            c_id_clean = c_id.upper().replace(" ", "")
            if re.match(r'^\d{5}', c_id_clean):
                sem_code = c_id_clean[:5]
                if sem_code.endswith('10'): default_semester = '上學期'
                elif sem_code.endswith('20'): default_semester = '下學期'
            else:
                if '下' in sem_raw or sem_raw == '2': default_semester = '下學期'
                else: default_semester = '上學期'
                
            id_no_prefix = re.sub(r'^\d{5}', '', c_id_clean)
            id_match = re.search(r'[A-Z]+(\d)', id_no_prefix)
            if id_match:
                lv = id_match.group(1)
                if lv == '1': default_year = '大一'
                elif lv == '2': default_year = '大二'
                elif lv == '3': default_year = '大三'
                elif lv == '4': default_year = '大四'

        if ("GE" in ctype or "CORE" in ctype) and default_year == '其他':
            default_year = '通識'
            
        COURSE_DATA[key] = {
            "base_name": c_name, "display_name": f"{c_name} ({c.get('instructor') or '未知'})", 
            "credits": float(c.get('credits', 0)), "year": default_year, "semester": default_semester, 
            "times": times, "type": c.get('type', ''), "orig_sem": sem_raw
        }
# 2. 建立一個全新的過濾器，專門針對建立好的 COURSE_DATA 進行「最新學期篩選」
def generate_search_dropdown_data(full_course_data):
    """
    從完整的課程字典中，針對每個相同課名，只抓出最新學期的班級，供前端搜尋欄使用。
    """
    max_terms = {}
    # 第一輪：找出每門課的最大學期前綴
    for key, details in full_course_data.items():
        base_name = details['base_name']
        match = re.search(r'\((\d{5})', key) # 從 "課名 (11510CH...)" 中抓取學期
        term = match.group(1) if match else "00000"
        
        if base_name not in max_terms or term > max_terms[base_name]:
            max_terms[base_name] = term
            
    # 第二輪：只保留符合最新學期的課程
    filtered_search_dict = {}
    for key, details in full_course_data.items():
        base_name = details['base_name']
        match = re.search(r'\((\d{5})', key)
        term = match.group(1) if match else "00000"
        
        # 只要是最新學期，或是使用者自訂的空堂，就放入搜尋清單
        if term == max_terms[base_name] or "空堂" in key:
            filtered_search_dict[key] = details
            
    return filtered_search_dict

# 3. 產生專門給前端搜尋下拉選單使用的字典
SEARCH_COURSE_DATA = generate_search_dropdown_data(COURSE_DATA)

PREREQUISITE_RULES = {
    "物理化學一": [ ["普通化學一"], ["普通化學二"], ["微積分二", "微積分Ｂ二"] ],
    "程序控制": [ ["資訊系統應用"] ],
    "程序設計": [ ["化工單操"] ],
    "儀器分析及實驗一": [ ["物理化學一"], ["物理化學二"] ],
    "輸送現象及單元操作一": [ ["工程數學一"], ["工程數學二"] ],
    "輸送現象及單元操作二": [ ["輸送現象及單元操作一"] ],
    "化工單操": [ ["質能均衡"] ],
    "工程數學一": [ ["微積分一", "微積分Ｂ一"], ["微積分二", "微積分Ｂ二"] ],
    "工程數學二": [ ["工程數學一"] ],
    "基礎高分子化學": [ ["有機化學一", "有機化學二"] ]
}

# ==========================================
# 🌟 終極衝堂防護引擎 (完美解決版)
# ==========================================
def check_time_conflict(user_id, new_course_name, target_year, target_semester, ignore_course_id=None):
    """精確比對真實落點的年級與學期，避免 '預設' 成為漏洞"""
    
    # 1. 計算新課程「實際」會被排入的年級與學期
    c_meta = COURSE_DATA.get(new_course_name, {})
    eff_new_year = target_year if target_year and target_year != '預設' else c_meta.get('year', '其他')
    eff_new_sem = target_semester if target_semester and target_semester != '預設' else c_meta.get('semester', '上學期')
    
    new_times = set(c_meta.get('times', []))
    if not new_times: 
        return False, "" # 沒有時間資料的課不會衝堂
        
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    # 只撈取「正在修課中」的課程，已修畢的課不列入衝堂計算
    cursor.execute("SELECT id, name, target_year, target_semester FROM courses WHERE user_id=%s AND status = 'taking'", (user_id,))
    taking_courses = cursor.fetchall()
    cursor.close()
    conn.close()
    
    for ec in taking_courses:
        if ignore_course_id and str(ec['id']) == str(ignore_course_id): 
            continue
            
        ec_meta = COURSE_DATA.get(ec['name'], {})
        # 計算現有課程「實際」排入的年級與學期
        ec_eff_year = ec['target_year'] if ec['target_year'] and ec['target_year'] != '預設' else ec_meta.get('year', '其他')
        ec_eff_sem = ec['target_semester'] if ec['target_semester'] and ec['target_semester'] != '預設' else ec_meta.get('semester', '上學期')
        
        # 🌟 只有落在【同一年級】且【同一學期】，才需要比對時間！
        if eff_new_year == ec_eff_year and eff_new_sem == ec_eff_sem:
            ec_times = set(ec_meta.get('times', []))
            conflicts = new_times.intersection(ec_times)
            
            if conflicts:
                pure_name = ec['name'].split(' (')[0]
                return True, f"與【{pure_name}】在 {', '.join(sorted(conflicts))} 發生時間衝突！"
            
    return False, ""

# ==========================================
# 🌟 學分大腦與核心資料產生器
# ==========================================
def get_user_dashboard_data(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM courses WHERE user_id=%s AND status='passed'", (user_id,))
    passed_courses = cursor.fetchall()
    cursor.close()
    conn.close()

    total_credits, compulsory_credits, ge_total_credits = 0, 0, 0
    pe_count, chinese_credits, english_credits = 0, 0, 0
    
    ge_results = {"Core GE1": [], "Core GE2": [], "Core GE3": [], "Core GE4": []}
    general_ge_list, chinese_list, english_list, pe_list = [], [], [], []
    has_reading, has_listening = False, False
    
    for c in passed_courses:
        c_dict = dict(c)
        db_name = c_dict.get('name', '')
        if not db_name: continue
            
        if "blank" in db_name.lower():
            total_credits += float(c_dict.get('credits', 0))
            continue
            
        c_info = COURSE_DATA.get(db_name)
        if not c_info:
            pure_name = db_name.split(' (')[0].strip()
            for details in COURSE_DATA.values():
                if details.get('base_name') == pure_name:
                    c_info = details
                    break
        
        if not isinstance(c_info, dict): continue
            
        cred = float(c_dict.get('credits', c_info.get('credits', 0)))
        total_credits += cred
        
        raw_type = str(c_info.get("type", "")).upper().replace(" ", "").replace("_", "")
        c_id = str(c_info.get("id", "")).upper()
        
        if "Core GE1" in raw_type:
            ge_results["Core GE1"].append(db_name); ge_total_credits += cred
        elif "Core GE2" in raw_type:
            ge_results["Core GE2"].append(db_name); ge_total_credits += cred
        elif "Core GE3" in raw_type:
            ge_results["Core GE3"].append(db_name); ge_total_credits += cred
        elif "Core GE4" in raw_type:
            ge_results["Core GE4"].append(db_name); ge_total_credits += cred
        elif "COREGE" in raw_type or "GEC" in c_id:
            ge_total_credits += cred
            if any(k in db_name for k in ["經濟","大氣", "社會", "政治", "法律", "天文", "醫學"]):
                ge_results["Core GE4"].append(db_name) 
            elif any(k in db_name for k in [ "藝術", "文學", "邏輯", "倫理"]):
                ge_results["Core GE3"].append(db_name)
            elif any(k in db_name for k in ["腦", "心智", "科學", "心理","哲學", "科技"]):
                ge_results["Core GE2"].append(db_name)
            elif any(k in db_name for k in [ "思維","文明", "歷史", "文化"]):
                ge_results["Core GE1"].append(db_name) 
            else:
                general_ge_list.append(db_name)
        elif "GE" in raw_type:
            general_ge_list.append(db_name)
            ge_total_credits += cred
        elif "LANG" in raw_type or "大學中文" in db_name or "大一中文" in db_name:
            if "中文" in db_name or "CHINESE" in db_name.upper():
                chinese_credits += cred
                chinese_list.append(db_name)
            else:
                english_credits += cred
                english_list.append(db_name)
                if "中高級英文三-閱讀" in db_name: has_reading = True
                if "中高級英文三-聽講" in db_name: has_listening = True
        elif "PE" in raw_type:
            pe_count += 1
            pe_list.append(db_name)  # 解決體育課清單未顯示問題
        elif "COMPULSORY" in raw_type or "必修" in raw_type or "必選" in raw_type:
            compulsory_credits += cred # 解決選修課算入必修學分問題

    return {
        'total_credits': int(total_credits),
        'compulsory': {'current': int(compulsory_credits), 'max': 86, 'percent': min(100, int((compulsory_credits/86)*100))},
        'ge': {
            'current': int(ge_total_credits), 'dim_count': sum(1 for v in ge_results.values() if len(v) > 0), 
            'percent': min(100, int((ge_total_credits/20)*100)), 'details': ge_results, 'general_list': general_ge_list
        },
        'language': {
            'chinese': int(chinese_credits), 'english': int(english_credits), 
            'english_list': english_list, 'chinese_list': chinese_list,
            'reading_passed': has_reading, 'listening_passed': has_listening
        },
        'pe': {'count': pe_count, 'list': pe_list}
    }


# ==========================================
# 🌟 基礎路由 (登入與儀表板)
# ==========================================
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'user_id' in session or google.authorized: return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/guest-login', methods=['GET', 'POST'])
def login_guest():
    guest_username = f"guest_{uuid.uuid4().hex[:8]}"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id', (guest_username, 'guest_dummy'))
    user_id = cursor.fetchone()['id']
    conn.commit()
    cursor.close()
    conn.close()
    session['user_id'] = user_id
    session['username'] = "訪客 (Guest)"
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/')
def home():
    if 'user_id' not in session:
        if google.authorized: return redirect(url_for('planning')) 
        return redirect(url_for('login_page'))
    data = get_user_dashboard_data(session['user_id'])
    return render_template('overview.html', data=data)

def keep_alive():
    url = "https://nthu-che-credit-tracker.onrender.com/"
    while True:
        try: requests.get(url)
        except: pass
        time.sleep(720) 

@app.route('/general-ed')
def general_ed():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    return render_template('general_ed.html', data=get_user_dashboard_data(session['user_id']))

@app.route('/language')
def language():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    return render_template('language.html', data=get_user_dashboard_data(session['user_id']))

@app.route('/pe')
def pe():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    return render_template('pe.html', data=get_user_dashboard_data(session['user_id']))


# ==========================================
# 🌟 排課系統 (含修復的衝堂機制)
# ==========================================
@app.route('/planning', methods=['GET', 'POST'])
def planning():
    if 'user_id' not in session:
        if google.authorized:
            resp = google.get("/oauth2/v2/userinfo")
            if resp.ok:
                email = resp.json()["email"]
                name = resp.json().get("name", email.split('@')[0])
                conn = get_db_connection()
                cursor = conn.cursor(cursor_factory=RealDictCursor)
                cursor.execute('SELECT * FROM users WHERE username = %s', (email,))
                user = cursor.fetchone()
                if not user:
                    cursor.execute('INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id', (email, 'google_sso_dummy'))
                    user_id = cursor.fetchone()['id']
                    conn.commit()
                else:
                    user_id = user['id']
                cursor.close(); conn.close()
                session['user_id'] = user_id
                session['username'] = name
            else: return redirect(url_for('login_page'))
        else: return redirect(url_for('login_page'))
            
    user_id = session['user_id']

    if request.method == 'POST':
        new_name = request.form.get('course_name')
        new_status = request.form.get('status')
        target_year = request.form.get('target_year')
        target_semester = request.form.get('target_semester')
        
        # 🌟 修復點：只有要排入「修課中」才檢查衝堂
        if new_status == 'taking':
            is_conflict, conflict_msg = check_time_conflict(user_id, new_name, target_year, target_semester)
            if is_conflict:
                return f"<script>alert('排課失敗：{conflict_msg}'); window.history.back();</script>"
        
        new_credits = COURSE_DATA.get(new_name, {}).get('credits', 0)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (%s, %s, %s, %s, %s, %s, %s)', 
                       (user_id, new_name, new_credits, new_status, target_year, target_semester, ""))
        conn.commit()
        cursor.close(); conn.close()
        return redirect(url_for('planning'))

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM courses WHERE user_id = %s AND status != 'tsmc_pending'", (user_id,))
    db_courses = cursor.fetchall()
    cursor.close(); conn.close()

    passed_base_names = [COURSE_DATA.get(c['name'], {}).get('base_name', c['name']) for c in db_courses if c['status'] == 'passed']
    taking_times = {}
    for course in db_courses:
        if course['status'] == 'taking':
            for t in COURSE_DATA.get(course['name'], {}).get('times', []):
                taking_times.setdefault(t, []).append(course['name'])

    periods = ['1', '2', '3', '4', 'n', '5', '6', '7', '8', '9', 'a', 'b', 'c']
    days = ['M', 'T', 'W', 'R', 'F', 'S']
    semesters = ['上學期', '下學期']
    grouped_courses = {"大一": [], "大二": [], "大三": [], "大四": [], "通識": [], "體育": [], "其他": []}
    yearly_grids = {year: {sem: {p: {d: [] for d in days} for p in periods} for sem in semesters} for year in grouped_courses.keys()}

    for course in db_courses:
        course = dict(course)
        course['is_blocked'] = False
        course['is_conflict'] = False
        course['prereq_warning'] = ""
        course['conflict_warning'] = ""
        c_key = course['name']
        c_meta = COURSE_DATA.get(c_key, {})
        base_name = c_meta.get('base_name', c_key)
        
        course['time_str'] = ", ".join(c_meta.get('times', [])) if c_meta.get('times', []) else "時間未定"
        course['display_name'] = c_meta.get('display_name', c_key)
        
        default_year, default_semester = c_meta.get('year', '其他'), c_meta.get('semester', '上學期')
        course['final_year'] = course.get('target_year') if course.get('target_year') and course.get('target_year') != '預設' else default_year
        course['final_semester'] = course.get('target_semester') if course.get('target_semester') and course.get('target_semester') != '預設' else default_semester
        
        if base_name in PREREQUISITE_RULES:
            unmet = [" 或 ".join(g) for g in PREREQUISITE_RULES[base_name] if not any(req in passed_base_names for req in g)]
            if unmet and course['status'] != 'passed':
                course['is_blocked'] = True
                course['prereq_warning'] = f"需先修畢【{'】及【'.join(unmet)}】"

        if course['status'] == 'taking':
            c_conflicts = []
            for t in c_meta.get('times', []):
                conflict_key = f"{course['final_semester']}_{t}"
                if len(taking_times.get(conflict_key, [])) > 1:
                    course['is_conflict'] = True
                    c_conflicts.extend([COURSE_DATA.get(n, {}).get('base_name', n) for n in taking_times[conflict_key] if n != c_key])
            if c_conflicts: course['conflict_warning'] = f"與【{', '.join(set(c_conflicts))}】衝堂"

        if course['status'] in ['taking', 'passed']:
            for t in c_meta.get('times', []):
                if len(t) >= 2:
                    d, p = t[0].upper(), t[1:].lower()
                    if p in yearly_grids.get(course['final_year'], {}).get(course['final_semester'], {}) and d in yearly_grids[course['final_year']][course['final_semester']][p]:
                        yearly_grids[course['final_year']][course['final_semester']][p][d].append({
                            "name": base_name, "is_blocked": course['is_blocked'], 
                            "is_conflict": course['is_conflict'], "is_passed": course['status'] == 'passed'
                        })
        if course['final_year'] in grouped_courses:
            grouped_courses[course['final_year']].append(course)

    grade_summary = {
        '大一': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大二': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大三': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大四': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}}
    }
    
    for c in db_courses:
        c_dict = dict(c)
        g_key, s_key = c_dict.get('final_year', '預設'), c_dict.get('final_semester', '預設')
        if g_key in grade_summary and s_key in ['上學期', '下學期']:
            grade_summary[g_key][s_key]['courses'].append(c_dict)
            grade_summary[g_key][s_key]['credits'] += c_dict.get('credits', 0)
            
    for g_name, sem_dict in grade_summary.items():
        for sem_name, data in sem_dict.items():
            total_credits = data['credits']
            min_limit, max_limit = (9 if g_name == '大四' else 16), 25
            if total_credits > 0:
                if total_credits < min_limit: data['warnings'].append(f"⚠️ 總計 {total_credits} 學分，低於應修 {min_limit} 學分限制！")
                elif total_credits > max_limit: data['warnings'].append(f"🚨 總計 {total_credits} 學分，超出高限 {max_limit} 學分！")
            for course in data['courses']:
                if course.get('prereq_warning'): data['warnings'].append(f"🚫 {course['name'].split(' (')[0]}: {course['prereq_warning']}")
                if course.get('conflict_warning'): data['warnings'].append(f"⚡ {course['name'].split(' (')[0]}: {course['conflict_warning']}")

    my_credits = sum(c['credits'] for c in db_courses if c['status'] == 'passed')
    mock_data = {
        "total_credits": my_credits, "required_credits": 128, "percentage": min(round((my_credits / 128) * 100, 1), 100),
        "grouped_courses": grouped_courses, "course_dict": COURSE_DATA, "periods": periods, "days": days, "semesters": semesters, "yearly_grids": yearly_grids
    }
    
    return render_template('planning.html', data=mock_data, grade_summary=grade_summary, all_programs=ALL_TSMC_PROGRAMS)

@app.route('/edit/<int:course_id>', methods=['POST'])
def edit_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    updated_status = request.form.get('status')
    updated_target = request.form.get('target_year')
    updated_sem = request.form.get('target_semester')
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT name FROM courses WHERE id=%s AND user_id=%s", (course_id, session['user_id']))
    course_info = cursor.fetchone()
    
    # 🌟 修復點：只有改成「修課中」才檢查衝堂
    if course_info and updated_status == 'taking':
        is_conflict, conflict_msg = check_time_conflict(session['user_id'], course_info['name'], updated_target, updated_sem, ignore_course_id=course_id)
        if is_conflict:
            cursor.close(); conn.close()
            return f"<script>alert('更新失敗：{conflict_msg}'); window.history.back();</script>"

    cursor.execute('UPDATE courses SET status = %s, target_year = %s, target_semester = %s WHERE id = %s AND user_id = %s', 
                   (updated_status, updated_target, updated_sem, course_id, session['user_id']))
    conn.commit()
    cursor.close(); conn.close()
    return redirect(url_for('planning'))

@app.route('/delete/<int:course_id>', methods=['POST'])
def delete_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM courses WHERE id = %s AND user_id = %s', (course_id, session['user_id']))
    conn.commit()
    cursor.close(); conn.close()
    return redirect(url_for('planning'))


# ==========================================
# 🌟 學程系統
# ==========================================
@app.route('/tsmc_program')
def tsmc_program():
    try:
        if 'user_id' not in session: return redirect(url_for('login_page'))
        user_id = session['user_id']

        current_program = request.args.get('program', '').strip()
        if not current_program or current_program not in TSMC_RULES:
            current_program = ALL_TSMC_PROGRAMS[0] if ALL_TSMC_PROGRAMS else ""

        tsmc_data = TSMC_RULES.get(current_program, {})

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM courses WHERE user_id=%s AND status IN ('tsmc_pending', 'taking', 'passed')", (user_id,))
        tsmc_courses = cursor.fetchall()
        cursor.close(); conn.close()

        tsmc_progress, program_course_lookup = {}, {} 
        
        if tsmc_data:
            for category_name, category_content in tsmc_data.items():
                if not isinstance(category_content, dict) or 'subjects' not in category_content: continue
                tsmc_progress[category_name] = {'count': 0, 'rule_text': category_content.get('rule_text', '無特定規則'), 'subjects': {}}
                
                for sub_name, sub_info in category_content['subjects'].items():
                    req_num = int(float(sub_info.get('subject_num', 1))) if isinstance(sub_info.get('subject_num'), str) and sub_info.get('subject_num').replace('.','',1).isdigit() else int(sub_info.get('subject_num', 1))
                    tsmc_progress[category_name]['subjects'][sub_name] = {'courses': [], 'has_passed': False, 'labels': set(), 'required_num': req_num}
                    for rule_c in sub_info.get('courses', []):
                        r_core_id = get_core_id(rule_c.get('id', ''))
                        if r_core_id: program_course_lookup[r_core_id] = (category_name, sub_name)

        for c in tsmc_courses:
            c_dict = dict(c)
            db_name = str(c_dict.get('name', '')).strip()
            pure_name = db_name.split(' (')[0].strip()
            db_id = db_name.split(' (')[1].replace(')', '').strip() if ' (' in db_name else ""
            core_db_id = get_core_id(db_id)

            found_raw = None
            for raw_c in ALL_RAW_COURSES:
                if not isinstance(raw_c, dict): continue
                if db_id and str(raw_c.get('id', '')).strip() == db_id: found_raw = raw_c; break
                if str(raw_c.get('name', '')).strip() == pure_name: found_raw = raw_c; break
            
            prog_subs = found_raw.get("program_subjects", []) if found_raw else []
            raw_tags = found_raw.get("is_recommended_programs", []) if found_raw else []
            rec_tags = raw_tags if isinstance(raw_tags, list) else [raw_tags] if raw_tags else []
            
            tsmc_cat, tsmc_sub, matched = "", "", False

            for category_name, category_content in tsmc_data.items():
                if not isinstance(category_content, dict) or 'subjects' not in category_content: continue
                for sub_name in category_content['subjects'].keys():
                    for ps in prog_subs:
                        ps_str = str(ps)
                        if sub_name in ps_str and (category_name in ps_str or len(tsmc_data) == 1):
                            tsmc_cat, tsmc_sub, matched = category_name, sub_name, True; break
                        elif ps_str == sub_name:
                            tsmc_cat, tsmc_sub, matched = category_name, sub_name, True; break
                    if matched: break
                if matched: break

            if not matched:
                if core_db_id in program_course_lookup:
                    tsmc_cat, tsmc_sub, matched = program_course_lookup[core_db_id][0], program_course_lookup[core_db_id][1], True
                elif pure_name in tsmc_progress.get("必修", {}).get("subjects", {}):
                    tsmc_cat, tsmc_sub, matched = "必修", pure_name, True
                else:
                    for category_name, category_content in tsmc_data.items():
                        if not isinstance(category_content, dict) or 'subjects' not in category_content: continue
                        if pure_name in category_content['subjects']:
                            tsmc_cat, tsmc_sub, matched = category_name, pure_name, True; break

            if matched and tsmc_cat in tsmc_progress and tsmc_sub in tsmc_progress[tsmc_cat]['subjects']:
                s_label, s_class = {'passed': ('已通過', 'success'), 'taking': ('修課中', 'primary'), 'tsmc_pending': ('待修/追蹤中', 'warning text-dark')}.get(c_dict['status'], ('未知', 'secondary'))
                c_meta = COURSE_DATA.get(db_name, {})
                
                tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['courses'].append({
                    'id': c_dict['id'], 'name': db_name, 'status': c_dict['status'], 
                    'status_label': s_label, 'status_class': s_class, 'type_label': "",
                    'is_recommended': len(rec_tags) > 0, 'recommended_tags': rec_tags,
                    'target_year': c_dict.get('target_year', '預設'), 'target_semester': c_dict.get('target_semester', '預設'),
                    'default_sem': c_meta.get('semester', '未知學期'), 
                    'time_str': ", ".join(c_meta.get('times', [])) if c_meta.get('times', []) else "時間未定"
                })
                if c_dict['status'] == 'passed': tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['has_passed'] = True

        for cat_name, cat_data in tsmc_progress.items():
            cat_data['count'] = sum(1 for sub in cat_data['subjects'].keys() if cat_data['subjects'][sub]['has_passed'])
            rt, req_num = str(cat_data.get('rule_text', '')), len(cat_data['subjects'])
            if rt:
                match_select = re.search(r'選(\d+)', rt)
                if match_select: req_num = int(match_select.group(1))
                else:
                    nums = re.findall(r'\d+', rt)
                    if nums: req_num = int(nums[-1]) 
            cat_data['required_num'] = max(req_num, 1)

            cat_has_rec = False
            for sub_name, sub_data in cat_data['subjects'].items():
                sub_data['labels'] = list(sub_data['labels'])
                sub_has_rec = any(c.get('is_recommended', False) for c in sub_data.get('courses', []))
                sub_data['has_recommended'] = sub_has_rec
                if sub_has_rec: cat_has_rec = True
            cat_data['has_recommended'] = cat_has_rec

        return render_template('tsmc_program.html', progress=tsmc_progress, all_programs=ALL_TSMC_PROGRAMS, current_program=current_program)
    except Exception as e:
        return f"渲染錯誤：<pre>{traceback.format_exc()}</pre>"

@app.route('/api/add_tsmc_courses', methods=['POST'])
def add_tsmc_courses():
    try:
        if 'user_id' not in session: return jsonify({"status": "error", "message": "請先登入"}), 401
        user_id = session['user_id']
        data = request.get_json(silent=True, force=True) or {}
        selected_program = str(data.get('program_name') or data.get('program') or "").strip()
        
        if not selected_program or selected_program not in TSMC_RULES: return jsonify({"status": "error", "message": "學程庫無效"}), 400
            
        tsmc_data, tsmc_core_ids = TSMC_RULES.get(selected_program, {}), set()
        for cat_name, cat_info in tsmc_data.items():
            if isinstance(cat_info, dict) and 'subjects' in cat_info:
                for sub_info in cat_info['subjects'].values():
                    for c in sub_info.get('courses', []):
                        if c.get('id'):
                            tsmc_core_ids.add(str(c['id']).upper().replace(" ", ""))
                            tsmc_core_ids.add(get_core_id(c['id']))
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT name FROM courses WHERE user_id=%s AND status IN ('tsmc_pending', 'taking', 'passed')", (user_id,))
        existing_courses = {row['name'] for row in cursor.fetchall()}
        
        added_count = 0
        for details in ALL_RAW_COURSES:
            if not isinstance(details, dict): continue
            c_name = details.get('name', '')
            if not c_name or c_name.lower() == 'blank': continue
            
            raw_id = str(details.get('id', '')).upper().replace(" ", "")
            prog_cats = details.get("program_categories", [])
            
            is_match = (isinstance(prog_cats, list) and selected_program in prog_cats) or (raw_id in tsmc_core_ids) or (get_core_id(raw_id) in tsmc_core_ids)
                
            if is_match:
                full_key = f"{c_name} ({details.get('id', '')})" if details.get('id') else c_name
                if full_key not in existing_courses:
                    cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (%s, %s, %s, %s, %s, %s, %s)', 
                                   (user_id, full_key, float(details.get('credits', 3.0)), 'tsmc_pending', '預設', '預設', ""))
                    added_count += 1
                    existing_courses.add(full_key)

        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"status": "success", "message": f"成功帶入 {added_count} 門學程課程！"})
    except Exception as e: return jsonify({"status": "error", "message": f"錯誤：\n{traceback.format_exc()}"}), 500

@app.route('/api/update_tsmc_settings', methods=['POST'])
def update_tsmc_settings():
    try:
        if 'user_id' not in session: return jsonify({"status": "error", "message": "請先登入"}), 401
    
        data = request.get_json(silent=True, force=True) or {}
        user_id, course_id, new_status = session['user_id'], data.get('course_id'), data.get('status')
        new_year, new_sem = data.get('target_year', '預設'), data.get('target_semester', '預設')

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT name FROM courses WHERE id=%s AND user_id=%s", (course_id, user_id))
        course_info = cursor.fetchone()
        
        # 🌟 修復點：只有改成「修課中」才檢查衝堂
        if course_info and new_status == 'taking':
            is_conflict, conflict_msg = check_time_conflict(user_id, course_info['name'], new_year, new_sem, ignore_course_id=course_id)
            if is_conflict:
                cursor.close(); conn.close()
                return jsonify({"status": "error", "message": conflict_msg})
        
        cursor.execute('UPDATE courses SET status = %s, target_year = %s, target_semester = %s WHERE id = %s AND user_id = %s', 
                       (new_status, new_year, new_sem, course_id, user_id))
        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"status": "success", "message": "設定更新成功！"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# 🌟 一鍵批次作業路由
# ==========================================
@app.route('/import_compulsory', methods=['POST'])
def import_compulsory():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    target_sem_code, target_status = request.form.get('year_sem', '').strip(), request.form.get('status', 'taking')
    
    target_year, target_sem = '其他', '預設'
    if '-' in target_sem_code:
        y_code, s_code = target_sem_code.split('-')
        target_year = {'1':'大一', '2':'大二', '3':'大三', '4':'大四'}.get(y_code, '其他')
        target_sem = {'1':'上學期', '2':'下學期'}.get(s_code, '預設')
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT id, name, status FROM courses WHERE user_id=%s', (user_id,))
    existing_base_names = {COURSE_DATA.get(row['name'], {}).get('base_name', row['name']): {'id': row['id'], 'status': row['status']} for row in cursor.fetchall()}
        
    added_this_round = set()
    for key, details in COURSE_DATA.items():
        base_name, c_type_clean = details.get('base_name', ''), str(details.get('type', '')).strip().lower().replace(" ", "").replace("_", "")
        if '適應體育' in base_name: continue
            
        is_compulsory = any(kw in c_type_clean for kw in ['compulsory', '必修', '必選']) or ('體育' in base_name) or ('服務學習' in base_name)
        if not is_compulsory: continue
            
        if is_compulsory and details.get('year') == target_year and details.get('semester') == target_sem:
            if base_name in existing_base_names:
                if existing_base_names[base_name]['status'] == 'tsmc_pending':
                    cursor.execute('UPDATE courses SET status = %s, target_year = %s, target_semester = %s WHERE id = %s', 
                                   (target_status, target_year, target_sem, existing_base_names[base_name]['id']))
                    existing_base_names[base_name]['status'] = target_status
            elif base_name not in added_this_round:
                cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                               (user_id, key, details['credits'], target_status, target_year, target_sem, ""))
                added_this_round.add(base_name)

    conn.commit()
    cursor.close(); conn.close()
    return redirect(url_for('planning'))

@app.route('/complete_semester', methods=['POST'])
def complete_semester():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    target_sem_code = request.form.get('year_sem', '').strip() 
    
    target_year, target_sem = '其他', '預設'
    if '-' in target_sem_code:
        y_code, s_code = target_sem_code.split('-')
        target_year = {'1':'大一', '2':'大二', '3':'大三', '4':'大四'}.get(y_code, '其他')
        target_sem = {'1':'上學期', '2':'下學期'}.get(s_code, '預設')
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE courses SET status = 'passed' WHERE user_id = %s AND target_year = %s AND target_semester = %s AND status != 'tsmc_pending'", (user_id, target_year, target_sem))
    conn.commit()
    cursor.close(); conn.close()
    return redirect(url_for('planning'))

@app.route('/revert_semester', methods=['POST'])
def revert_semester():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    target_sem_code = request.form.get('year_sem', '').strip() 
    
    target_year, target_sem = '其他', '預設'
    if '-' in target_sem_code:
        y_code, s_code = target_sem_code.split('-')
        target_year = {'1':'大一', '2':'大二', '3':'大三', '4':'大四'}.get(y_code, '其他')
        target_sem = {'1':'上學期', '2':'下學期'}.get(s_code, '預設')
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE courses SET status = 'taking' WHERE user_id = %s AND target_year = %s AND target_semester = %s AND status != 'tsmc_pending'", (user_id, target_year, target_sem))
    conn.commit()
    cursor.close(); conn.close()
    return redirect(url_for('planning'))

# ==========================================
# 監控並實質活化資料庫
# ==========================================
@app.route('/api/healthcheck', methods=['GET'])
def health_check():
    conn = None
    cursor = None
    try:
        # 1. 呼叫你既有的連線函式，實質建立與 Supabase PostgreSQL 的連線
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 2. 執行 PostgreSQL 核心心跳指令，測試資料庫是否清醒
        cursor.execute("SELECT 1;")
        cursor.fetchone()
        
        # 3. 釋放資源
        cursor.close()
        conn.close()
        
        return jsonify({"status": "healthy", "database": "connected"}), 200
        
    except Exception as e:
        # 安全防禦：確保發生異常時，資源依然有被釋放，避免連線洩漏 (Connection Leak)
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            
        # 當 Supabase 處於休眠或連不上時，回傳 500 錯誤與詳細原因
        return jsonify({"status": "unhealthy", "error": str(e)}), 500

if __name__ == '__main__':
    threading.Thread(target=keep_alive, daemon=True).start()
    print("🚀 伺服器啟動中！")
    app.run(debug=True, port=5000)
