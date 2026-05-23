import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import json
import traceback
import re
import uuid
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_dance.contrib.google import make_google_blueprint, google

# 允許在本地端 (http) 測試 Google 登入
if os.getenv("FLASK_ENV") == "development":
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# 1. 核心智慧比對特徵萃取工具
def get_core_id(cid):
    if not cid: return ""
    cid = str(cid).upper().replace(" ", "")
    cid = re.sub(r'^\d{5}', '', cid)
    match = re.search(r'([A-Z]+)(\d{4})', cid)
    return match.group(1) + match.group(2) if match else cid

# ==========================================
# 🌟 終極衝堂防護引擎 (嚴格同學期 + time_slots 標籤比對)
# ==========================================

def get_time_slots_from_raw(course_name):
    """【步驟 1】精準從 JSON 資料庫提取 time_slots 標籤"""
    if not course_name: return ""
    
    # 拆解出純課名與課號，例如 "微積分一 (MATH1010)" -> pure_name="微積分一", db_id="MATH1010"
    pure_name = course_name.split(' (')[0].strip()
    db_id = course_name.split(' (')[1].replace(')', '').strip() if ' (' in course_name else ""
    
    for rc in ALL_RAW_COURSES:
        if not isinstance(rc, dict): continue
        # 優先以課號比對，最精準
        if db_id and str(rc.get('id', '')).strip() == db_id:
            return str(rc.get('time_slots', ''))
        # 備用防線：以課名比對
        if str(rc.get('name', '')).strip() == pure_name:
            return str(rc.get('time_slots', ''))
    return ""

def parse_time_slots(time_str):
    """
    將時間字串（如 M3M4W1）轉換為一組唯一的節次代碼集合
    例如: {'Mon-3', 'Mon-4', 'Wed-1'}
    """
    if not time_str or time_str.lower() in ['none', 'null', '無', 'tba', '']:
        return set()
    
    # 建立星期對應表
    day_map = {'M': 'Mon', 'T': 'Tue', 'W': 'Wed', 'R': 'Thu', 'F': 'Fri', 'S': 'Sat'}
    
    # 修正後的邏輯：使用 Regex 分組捕捉「星期」與「節次」
    pattern = r'([MTWRFS])([1-9ABC N])'
    matches = re.findall(pattern, str(time_str).upper())
    
    slots = set()
    for day, period in matches:
        # 將每個課堂時段轉換為 "Mon-3" 這種唯一的格式
        slots.add(f"{day_map.get(day, day)}-{period}")
        
    return slots

def check_time_conflict(user_id, new_course_name, target_year, target_semester, ignore_course_id=None):
    """【修正版】加入 status 篩選，確保不會與已通過的課程產生衝堂誤判"""
    if target_year == '預設' or target_semester == '預設': 
        return False, ""
    
    # 取得新課程時間
    new_time = "".join(COURSE_DATA.get(new_course_name, {}).get('times', []))
    new_slots = parse_time_slots(new_time)
    if not new_slots: return False, ""
        
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    # 🌟 關鍵修復：這裡加上了 AND status='taking'
    # 確保只拿「正在修習中」的課來比對，忽略 'passed' 的課程
    cursor.execute("""
        SELECT id, name FROM courses 
        WHERE user_id=%s AND target_year=%s AND target_semester=%s 
        AND status = 'taking'
    """, (user_id, target_year, target_semester))
    
    taking_courses = cursor.fetchall()
    cursor.close()
    conn.close()
    
    for ec in taking_courses:
        if ignore_course_id and str(ec['id']) == str(ignore_course_id): 
            continue
            
        ec_time = "".join(COURSE_DATA.get(ec['name'], {}).get('times', []))
        ec_slots = parse_time_slots(ec_time)
        
        conflicts = new_slots.intersection(ec_slots)
        if conflicts:
            return True, f"與【{ec['name'].split(' (')[0]}】在 {', '.join(sorted(conflicts))} 發生時間衝突！"
            
    return False, ""

# 1. 唯一初始化 Flask
app = Flask(__name__)
app.secret_key = "nthu_cheme_secret_key"

# 2. 設定本機 VS Code 開發的相對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

JSON_PATH = os.path.join(BASE_DIR, 'requirements(3).json') 
JSON_PATH_2 = os.path.join(BASE_DIR, 'tsmc_program_rules.json')

# 3. 防止瀏覽器快取 (避免上一頁卡住)
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# ==========================================
# 🌟 Google 登入藍圖
# ==========================================
blueprint = make_google_blueprint(
    client_id="377112972961-22stlmtreke461al4o4b1o5p7l4nqif0.apps.googleusercontent.com",
    client_secret="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", 
    scope=["profile", "email"],
    offline=True
)
app.register_blueprint(blueprint, url_prefix="/login")

load_dotenv() # 讀取 .env
DATABASE_URL = os.getenv("DATABASE_URL")

# 新的 PostgreSQL 連線函數
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ==========================================
# 🌟 課程資料載入與分類
# ==========================================

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
        except FileNotFoundError:
            print(f"⚠️ 嚴重警告：找不到課程資料檔 {path}！")
        except Exception as e:
            print(f"⚠️ 讀取 {path} 時發生錯誤: {e}")
    return combined_courses

ALL_RAW_COURSES = fetch_all_courses([JSON_PATH])

TSMC_RULES = {}
try:
    with open(JSON_PATH_2, 'r', encoding='utf-8') as f:
        TSMC_RULES = json.load(f)
except Exception as e:
    print(f"⚠️ 嚴重警告：讀取 {JSON_PATH_2} 發生錯誤: {e}")

# 確保所有程式都在全域有抓到 key
ALL_TSMC_PROGRAMS = list(TSMC_RULES.keys())

# 3. 執行課程分類
def classify_courses(raw_courses):
    categorized = {
        "compulsory": [], "compulsory_elective": [], "elective": [], 
        "LANG": [], "PE": [], "Core_GE1": [], "Core_GE2": [], 
        "Core_GE3": [], "Core_GE4": [], "GE": [], "others": []
    }
    for course in raw_courses:
        if isinstance(course, dict):
            c_type = course.get("type", "").strip()
            c_type_clean = c_type.replace(" ", "").replace("_", "").upper()
            
            if "COREGE1" in c_type_clean:
                categorized["Core_GE1"].append(course)
            elif "COREGE2" in c_type_clean:
                categorized["Core_GE2"].append(course)
            elif "COREGE3" in c_type_clean:
                categorized["Core_GE3"].append(course)
            elif "COREGE4" in c_type_clean:
                categorized["Core_GE4"].append(course)
            elif c_type_clean == "GE" or "一般通識" in c_type_clean:
                categorized["GE"].append(course)
            elif c_type in categorized:
                categorized[c_type].append(course)
            else:
                categorized["others"].append(course)
    return categorized

COURSE_DATA_CLASSIFIED = classify_courses(ALL_RAW_COURSES)

# 4. 建立供排課搜尋引擎使用的 COURSE_DATA
COURSE_DATA = {}
for c in ALL_RAW_COURSES:
    if isinstance(c, dict):
        c_name = str(c.get('name', '')).strip()
        c_id = str(c.get('id', '')).strip()
        t_str = str(c.get('time_slots', ''))
        
        if c_name.lower() == 'blank':
            c_id = f"空堂-{t_str}" if t_str else f"空堂-{uuid.uuid4().hex[:4]}"
            
        key = f"{c_name} ({c_id})" if c_id else c_name
        
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
# 🌟 資料庫初始化與學分大腦
# ==========================================
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 使用 SERIAL 取代 AUTOINCREMENT
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, 
            username TEXT UNIQUE NOT NULL, 
            password TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS courses (
            id SERIAL PRIMARY KEY, 
            user_id INTEGER NOT NULL, 
            name TEXT NOT NULL, 
            credits NUMERIC NOT NULL, 
            status TEXT NOT NULL, 
            target_year TEXT, 
            target_semester TEXT, 
            warning TEXT, 
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

def get_user_dashboard_data(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM courses WHERE user_id=%s AND status='passed'", (user_id,))
    passed_courses = cursor.fetchall()
    cursor.close()
    conn.close()

    total_credits = 0
    compulsory_credits = 0
    ge_total_credits = 0
    pe_count = 0
    chinese_credits = 0
    english_credits = 0
    
    ge_results = {"Core GE1": [], "Core GE2": [], "Core GE3": [], "Core GE4": []}
    general_ge_list = []
    chinese_list = []
    english_list = []
    
    has_reading = False
    has_listening = False
    
    for c in passed_courses:
        c_dict = dict(c)
        db_name = c_dict.get('name', '')
        
        if not db_name:
            continue
            
        # 🌟 空白課程計入畢業總學分，然後跳過
        if "blank" in db_name.lower():
            total_credits += float(c_dict.get('credits', 0))
            continue
            
        c_info = None
        if db_name in COURSE_DATA:
            c_info = COURSE_DATA[db_name]
        else:
            pure_name = db_name.split(' (')[0].strip()
            for details in COURSE_DATA.values():
                if isinstance(details, dict):
                    dict_name = details.get('base_name', details.get('name', '')).split(' (')[0].strip()
                    if dict_name == pure_name:
                        c_info = details
                        break
        
        if not isinstance(c_info, dict):
            continue
            
        cred = float(c_dict.get('credits', c_info.get('credits', 0)))
        total_credits += cred
        
        raw_type = str(c_info.get("type", "")).upper().replace(" ", "").replace("_", "")
        c_id = str(c_info.get("id", "")).upper()
        
        if "Core GE1" in raw_type:
            ge_results["Core GE1"].append(db_name)
            ge_total_credits += cred
        elif "Core GE2" in raw_type:
            ge_results["Core GE2"].append(db_name)
            ge_total_credits += cred
        elif "Core GE3" in raw_type:
            ge_results["Core GE3"].append(db_name)
            ge_total_credits += cred
        elif "Core GE4" in raw_type:
            ge_results["Core GE4"].append(db_name)
            ge_total_credits += cred
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
        else:
            compulsory_credits += cred

    return {
        'total_credits': int(total_credits),
        'compulsory': {'current': int(compulsory_credits), 'max': 86, 'percent': min(100, int((compulsory_credits/86)*100))},
        'ge': {
            'current': int(ge_total_credits), 
            'dim_count': sum(1 for v in ge_results.values() if len(v) > 0), 
            'percent': min(100, int((ge_total_credits/20)*100)), 
            'details': ge_results,
            'general_list': general_ge_list
        },
        'language': {
            'chinese': int(chinese_credits), 
            'english': int(english_credits), 
            'english_list': english_list, 
            'chinese_list': chinese_list,
            'reading_passed': has_reading,
            'listening_passed': has_listening
        },
        'pe': {'count': pe_count}
    }


# ==========================================
# 🌟 登入與認證路由
# ==========================================
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'user_id' in session or google.authorized:
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/guest-login', methods=['GET', 'POST'])
def login_guest():
    guest_username = f"guest_{uuid.uuid4().hex[:8]}"
    guest_name = "訪客 (Guest)"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id', (guest_username, 'guest_dummy'))
    user_id = cursor.fetchone()['id']
    conn.commit()
    cursor.close()
    conn.close()
    
    session['user_id'] = user_id
    session['username'] = guest_name
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# ==========================================
# 🌟 多頁面架構路由
# ==========================================
@app.route('/')
def home():
    if 'user_id' not in session:
        if google.authorized:
            return redirect(url_for('planning')) 
        return redirect(url_for('login_page'))
        
    user_id = session['user_id']
    dashboard_data = get_user_dashboard_data(user_id)
    return render_template('overview.html', data=dashboard_data)

@app.route('/general-ed')
def general_ed():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    data = get_user_dashboard_data(session['user_id'])
    return render_template('general_ed.html', data=data)

@app.route('/language')
def language():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    data = get_user_dashboard_data(session['user_id'])
    return render_template('language.html', data=data)

@app.route('/pe')
def pe():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    data = get_user_dashboard_data(session['user_id'])
    return render_template('pe.html', data=data)


# ==========================================
# 🌟 全新動態多學程管理引擎 (標籤精準識別版)
# ==========================================

@app.route('/api/add_tsmc_courses', methods=['POST'])
def add_tsmc_courses():
    try:
        if 'user_id' not in session:
            return jsonify({"status": "error", "message": "請先登入"}), 401
        user_id = session['user_id']
        
        selected_program = ""
        data = request.get_json(silent=True, force=True) or {}
        if data:
            selected_program = data.get('program_name') or data.get('program') or ""
        if not selected_program:
            selected_program = request.args.get('program_name') or request.args.get('program') or ""
        if not selected_program:
            selected_program = request.form.get('program_name') or request.form.get('program') or ""
            
        selected_program = str(selected_program).strip()
        
        with open(JSON_PATH_2, 'r', encoding='utf-8-sig') as f:
            tsmc_rules = json.load(f)
        all_programs = list(tsmc_rules.keys())
        
        if not selected_program or selected_program not in tsmc_rules:
            if all_programs:
                selected_program = all_programs[0]
            else:
                return jsonify({"status": "error", "message": "學程規則資料庫內容為空"}), 400
                
        tsmc_data = tsmc_rules.get(selected_program, {})
        
        tsmc_core_ids = set()
        for cat_name, cat_info in tsmc_data.items():
            if isinstance(cat_info, dict) and 'subjects' in cat_info:
                for sub_name, sub_info in cat_info['subjects'].items():
                    for c in sub_info.get('courses', []):
                        cid = c.get('id', '')
                        if cid:
                            tsmc_core_ids.add(str(cid).upper().replace(" ", ""))
                            tsmc_core_ids.add(get_core_id(cid))
        
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
            core_id = get_core_id(raw_id)
            
            prog_cats = details.get("program_categories", [])
            is_match = isinstance(prog_cats, list) and selected_program in prog_cats
            
            if not is_match:
                is_match = (raw_id in tsmc_core_ids) or (core_id in tsmc_core_ids)
                
            if is_match:
                full_key = f"{c_name} ({details.get('id', '')})" if details.get('id') else c_name
                if full_key not in existing_courses:
                    cursor.execute('''
                        INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', (user_id, full_key, float(details.get('credits', 3.0)), 'tsmc_pending', '預設', '預設', ""))
                    added_count += 1
                    existing_courses.add(full_key)

        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({"status": "success", "message": f"成功將「{selected_program}」的 {added_count} 門學程標籤課程帶入追蹤清單！"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"後端錯誤：\n{traceback.format_exc()}"}), 500

@app.route('/tsmc_program')
def tsmc_program():
    try:
        if 'user_id' not in session: return redirect(url_for('login_page'))
        user_id = session['user_id']

        with open(JSON_PATH_2, 'r', encoding='utf-8-sig') as f:
            tsmc_rules = json.load(f)
        all_programs = list(tsmc_rules.keys())

        current_program = request.args.get('program', '').strip()
        if not current_program or current_program not in tsmc_rules:
            current_program = all_programs[0] if all_programs else ""

        tsmc_data = tsmc_rules.get(current_program, {})

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM courses WHERE user_id=%s AND status IN ('tsmc_pending', 'taking', 'passed')", (user_id,))
        tsmc_courses = cursor.fetchall()
        cursor.close()
        conn.close()

        tsmc_progress = {}
        program_course_lookup = {} 
        
        if tsmc_data:
            for category_name, category_content in tsmc_data.items():
                if not isinstance(category_content, dict) or 'subjects' not in category_content:
                    continue
                    
                tsmc_progress[category_name] = {
                    'count': 0,
                    'rule_text': category_content.get('rule_text', '無特定規則'),
                    'subjects': {}
                }
                
                for sub_name, sub_info in category_content['subjects'].items():
                    req_num = sub_info.get('subject_num', 1)
                    if isinstance(req_num, str):
                        req_num = int(float(req_num)) if req_num.replace('.','',1).isdigit() else 1
                    else:
                        req_num = int(req_num)

                    tsmc_progress[category_name]['subjects'][sub_name] = {
                        'courses': [], 
                        'has_passed': False,
                        'labels': set(),
                        'required_num': req_num
                    }
                    
                    for rule_c in sub_info.get('courses', []):
                        r_core_id = get_core_id(rule_c.get('id', ''))
                        if r_core_id:
                            program_course_lookup[r_core_id] = (category_name, sub_name)

        for c in tsmc_courses:
            c_dict = dict(c)
            db_name = str(c_dict.get('name', '')).strip()
            
            pure_name = db_name.split(' (')[0].strip()
            db_id = db_name.split(' (')[1].replace(')', '').strip() if ' (' in db_name else ""
            core_db_id = get_core_id(db_id)

            found_raw = None
            for raw_c in ALL_RAW_COURSES:
                if not isinstance(raw_c, dict): continue
                if db_id and str(raw_c.get('id', '')).strip() == db_id:
                    found_raw = raw_c
                    break
                if str(raw_c.get('name', '')).strip() == pure_name:
                    found_raw = raw_c
                    break
            
            prog_cats = []
            prog_subs = []
            rec_tags = []  # 🌟 新增：用來存放推薦標籤
            if found_raw:
                prog_cats = found_raw.get("program_categories", [])
                prog_subs = found_raw.get("program_subjects", [])
                
                # 🌟 新增：安全讀取推薦標籤，並確保它是陣列格式
                raw_tags = found_raw.get("is_recommended_programs", [])
                rec_tags = raw_tags if isinstance(raw_tags, list) else [raw_tags] if raw_tags else []
            
            tsmc_cat, tsmc_sub = "", ""
            matched = False

            for category_name, category_content in tsmc_data.items():
                if not isinstance(category_content, dict) or 'subjects' not in category_content: continue
                for sub_name in category_content['subjects'].keys():
                    for ps in prog_subs:
                        ps_str = str(ps)
                        if sub_name in ps_str and (category_name in ps_str or len(tsmc_data) == 1):
                            tsmc_cat, tsmc_sub = category_name, sub_name
                            matched = True
                            break
                        elif ps_str == sub_name:
                            tsmc_cat, tsmc_sub = category_name, sub_name
                            matched = True
                            break
                    if matched: break
                if matched: break

            if not matched:
                if core_db_id in program_course_lookup:
                    tsmc_cat, tsmc_sub = program_course_lookup[core_db_id]
                    matched = True
                elif pure_name in tsmc_progress.get("必修", {}).get("subjects", {}):
                    tsmc_cat, tsmc_sub = "必修", pure_name
                    matched = True
                else:
                    for category_name, category_content in tsmc_data.items():
                        if not isinstance(category_content, dict) or 'subjects' not in category_content: continue
                        if pure_name in category_content['subjects']:
                            tsmc_cat, tsmc_sub = category_name, pure_name
                            matched = True
                            break

            if matched and tsmc_cat in tsmc_progress and tsmc_sub in tsmc_progress[tsmc_cat]['subjects']:
                status_map = {
                    'passed': ('已通過', 'success'),
                    'taking': ('修課中', 'primary'),
                    'tsmc_pending': ('待修/追蹤中', 'warning text-dark')
                }
                s_label, s_class = status_map.get(c_dict['status'], ('未知', 'secondary'))

                rec_tags = found_raw.get("is_recommended_programs", []) if found_raw else []
    
                tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['courses'].append({
                    'id': c_dict['id'], 
                    'name': db_name, 
                    'status': c_dict['status'], 
                    'status_label': s_label, 
                    'status_class': s_class, 
                    'type_label': "",
                    'is_recommended': len(rec_tags) > 0, # 傳遞給前端判斷
                    'recommended_tags': rec_tags         # 實際標籤文字
                })
                
                if c_dict['status'] == 'passed': 
                    tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['has_passed'] = True

        for cat_name, cat_data in tsmc_progress.items():
            cat_data['count'] = sum(1 for sub in cat_data['subjects'].keys() if cat_data['subjects'][sub]['has_passed'])
            rt = str(cat_data.get('rule_text', ''))
            req_num = len(cat_data['subjects'])
            if rt:
                match_select = re.search(r'選(\d+)', rt)
                if match_select: req_num = int(match_select.group(1))
                else:
                    nums = re.findall(r'\d+', rt)
                    if nums: req_num = int(nums[-1]) 
            if req_num <= 0: req_num = 1
            cat_data['required_num'] = req_num

        # 🌟 統計各大分類與小學科是否包含推薦課程
        # 找到這一段並確認更新為以下邏輯
        for cat_name, cat_data in tsmc_progress.items():
            cat_has_rec = False
            for sub_name, sub_data in cat_data['subjects'].items():
                sub_data['labels'] = list(sub_data['labels'])
                
                # 檢查這個學科底下的課程，是否有任何一堂是推薦課程
                sub_has_rec = any(c.get('is_recommended', False) for c in sub_data.get('courses', []))
                sub_data['has_recommended'] = sub_has_rec
                
                if sub_has_rec:
                    cat_has_rec = True
            
            cat_data['has_recommended'] = cat_has_rec

        return render_template('tsmc_program.html', progress=tsmc_progress, all_programs=all_programs, current_program=current_program)
        
    except Exception as e:
        import traceback
        return f"渲染錯誤，完整堆疊資訊：\n<pre>{traceback.format_exc()}</pre>"

# ==========================================
# 🌟 排課系統與其他操作 (截圖修復核心區)
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
                
                cursor.close()
                conn.close()
                session['user_id'] = user_id
                session['username'] = name
            else:
                return redirect(url_for('login_page'))
        else:
            return redirect(url_for('login_page'))
            
    user_id = session['user_id']

    if request.method == 'POST':
        new_name = request.form.get('course_name')
        new_status = request.form.get('status')
        target_year = request.form.get('target_year')
        target_semester = request.form.get('target_semester')
        
        is_conflict, conflict_msg = check_time_conflict(user_id, new_name, target_year, target_semester)
        if is_conflict:
            return f"<script>alert('排課失敗：{conflict_msg}'); window.history.back();</script>"
        
        new_credits = COURSE_DATA.get(new_name, {}).get('credits', 0)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (%s, %s, %s, %s, %s, %s, %s)', 
                       (user_id, new_name, new_credits, new_status, target_year, target_semester, ""))
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for('planning'))

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM courses WHERE user_id = %s AND status != 'tsmc_pending'", (user_id,))
    db_courses = cursor.fetchall()
    cursor.close()
    conn.close()

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
        display_name = c_meta.get('display_name', c_key)
        c_times = c_meta.get('times', [])
        course['time_str'] = ", ".join(c_times) if c_times else "時間未定"
        course['display_name'] = display_name
        
        default_year = c_meta.get('year', '其他')
        default_semester = c_meta.get('semester', '上學期')
        target_y = course.get('target_year')
        year_group = target_y if target_y and target_y != '預設' else default_year
        course['final_year'] = year_group
        target_s = course.get('target_semester')
        sem_group = target_s if target_s and target_s != '預設' else default_semester
        course['final_semester'] = sem_group
        
        if base_name in PREREQUISITE_RULES:
            unmet = [" 或 ".join(g) for g in PREREQUISITE_RULES[base_name] if not any(req in passed_base_names for req in g)]
            if unmet and course['status'] != 'passed':
                course['is_blocked'] = True
                course['prereq_warning'] = f"需先修畢【{'】及【'.join(unmet)}】"

        if course['status'] == 'taking':
            c_conflicts = []
            for t in c_times:
                conflict_key = f"{course['final_semester']}_{t}"
                if len(taking_times.get(conflict_key, [])) > 1:
                    course['is_conflict'] = True
                    c_conflicts.extend([
                        COURSE_DATA.get(n, {}).get('base_name', n) 
                        for n in taking_times[conflict_key] if n != c_key
                    ])
            if c_conflicts:
                course['conflict_warning'] = f"與【{', '.join(set(c_conflicts))}】衝堂"

        if course['status'] in ['taking', 'passed']:
            for t in c_times:
                if len(t) >= 2:
                    d, p = t[0].upper(), t[1:].lower()
                    if p in yearly_grids.get(year_group, {}).get(sem_group, {}) and d in yearly_grids[year_group][sem_group][p]:
                        yearly_grids[year_group][sem_group][p][d].append({
                            "name": base_name, "is_blocked": course['is_blocked'], 
                            "is_conflict": course['is_conflict'], "is_passed": course['status'] == 'passed'
                        })
        if year_group in grouped_courses:
            grouped_courses[year_group].append(course)

    grade_summary = {
        '大一': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大二': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大三': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大四': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}}
    }
    
    for c in db_courses:
        c_dict = dict(c)
        g_key = c_dict.get('final_year') or c_dict.get('target_year') or '預設'
        s_key = c_dict.get('final_semester') or c_dict.get('target_semester') or '預設'
        
        if g_key in grade_summary and s_key in ['上學期', '下學期']:
            grade_summary[g_key][s_key]['courses'].append(c_dict)
            grade_summary[g_key][s_key]['credits'] += c_dict.get('credits', 0)
            
    for g_name, sem_dict in grade_summary.items():
        for sem_name, data in sem_dict.items():
            total_credits = data['credits']
            min_limit = 9 if g_name == '大四' else 16
            max_limit = 25
            
            if total_credits > 0:
                if total_credits < min_limit:
                    data['warnings'].append(f"⚠️ 學分總計 {total_credits} 學分，低於最低應修 {min_limit} 學分限制！")
                elif total_credits > max_limit:
                    data['warnings'].append(f"🚨 學分總計 {total_credits} 學分，超出高限 {max_limit} 學分，請評估減選！")
            
            for course in data['courses']:
                if course.get('prereq_warning'):
                    data['warnings'].append(f"🚫 {course['name'].split(' (')[0]}: {course['prereq_warning']}")
                if course.get('conflict_warning'):
                    data['warnings'].append(f"⚡ {course['name'].split(' (')[0]}: {course['conflict_warning']}")

    my_credits = sum(c['credits'] for c in db_courses if c['status'] == 'passed')
    
    mock_data = {
        "total_credits": my_credits, "required_credits": 128, "percentage": min(round((my_credits / 128) * 100, 1), 100),
        "grouped_courses": grouped_courses, "course_dict": COURSE_DATA,
        "gen_ed_progress": {"核心通識": 0, "一般通識": 0}, "gen_ed_percentage": 0,
        "periods": periods, "days": days, "semesters": semesters, "yearly_grids": yearly_grids
    }
    
    try:
        with open(JSON_PATH_2, 'r', encoding='utf-8') as f:
            tsmc_rules = json.load(f)
        all_programs = list(tsmc_rules.keys())
    except Exception:
        all_programs = ["台積電半導體製程模組學程"]
    
    return render_template('planning.html', data=mock_data, grade_summary=grade_summary, all_programs=all_programs)
    
@app.route('/import_compulsory', methods=['POST'])
def import_compulsory():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    
    target_sem_code = request.form.get('year_sem', '').strip() 
    target_status = request.form.get('status', 'taking')
    
    target_year, target_sem = '其他', '預設'
    if '-' in target_sem_code:
        y_code, s_code = target_sem_code.split('-')
        if y_code == '1': target_year = '大一'
        elif y_code == '2': target_year = '大二'
        elif y_code == '3': target_year = '大三'
        elif y_code == '4': target_year = '大四'
        if s_code == '1': target_sem = '上學期'
        elif s_code == '2': target_sem = '下學期'
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('SELECT id, name, status FROM courses WHERE user_id=%s', (user_id,))
    db_courses = cursor.fetchall()
    
    existing_base_names = {}
    for row in db_courses:
        c_name = row['name']
        base_name = COURSE_DATA.get(c_name, {}).get('base_name', c_name)
        existing_base_names[base_name] = {'id': row['id'], 'status': row['status']}
        
    added_this_round = set()
        
    for key, details in COURSE_DATA.items():
        raw_type = str(details.get('type', '')).strip().lower()
        c_type_clean = raw_type.replace(" ", "").replace("_", "")
        base_name = details.get('base_name', '')
        
        if '適應體育' in base_name:
            continue
            
        is_compulsory = any(kw in c_type_clean for kw in ['compulsory', '必修', '必選']) or ('體育' in base_name) or ('服務學習' in base_name)
        
        if not is_compulsory:
            if any(kw in c_type_clean for kw in ['ge', 'pe', 'lang', 'general', 'sport', 'option', 'ext']):
                continue
            if any(kw in base_name for kw in ['通識', '外文', '英文', '外語', '全民國防', '專題', '學術倫理', '歷史', '當代']):
                continue
            
        c_year = details.get('year', '')
        c_sem = details.get('semester', '')
        sem_match = (c_year == target_year and c_sem == target_sem)
        
        if is_compulsory and sem_match:
            if base_name in existing_base_names:
                if existing_base_names[base_name]['status'] == 'tsmc_pending':
                    cursor.execute('UPDATE courses SET status = %s, target_year = %s, target_semester = %s WHERE id = %s', 
                                   (target_status, target_year, target_sem, existing_base_names[base_name]['id']))
                    existing_base_names[base_name]['status'] = target_status
            else:
                if base_name not in added_this_round:
                    cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                                   (user_id, key, details['credits'], target_status, target_year, target_sem, ""))
                    added_this_round.add(base_name)

    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('planning'))

# 🌟 新增：一鍵修畢該學期所有課程 API 路由
@app.route('/complete_semester', methods=['POST'])
def complete_semester():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    
    target_sem_code = request.form.get('year_sem', '').strip() 
    
    target_year, target_sem = '其他', '預設'
    if '-' in target_sem_code:
        y_code, s_code = target_sem_code.split('-')
        if y_code == '1': target_year = '大一'
        elif y_code == '2': target_year = '大二'
        elif y_code == '3': target_year = '大三'
        elif y_code == '4': target_year = '大四'
        if s_code == '1': target_sem = '上學期'
        elif s_code == '2': target_sem = '下學期'
        
    conn = get_db_connection()
    cursor = conn.cursor()
    # 將該學期所有已在課表上的課程（非追蹤中狀態），一鍵改為 'passed' 狀態
    cursor.execute('''
        UPDATE courses 
        SET status = 'passed' 
        WHERE user_id = %s AND target_year = %s AND target_semester = %s AND status != 'tsmc_pending'
    ''', (user_id, target_year, target_sem))
    
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('planning'))

@app.route('/revert_semester', methods=['POST'])
def revert_semester():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    
    target_sem_code = request.form.get('year_sem', '').strip() 
    
    target_year, target_sem = '其他', '預設'
    if '-' in target_sem_code:
        y_code, s_code = target_sem_code.split('-')
        if y_code == '1': target_year = '大一'
        elif y_code == '2': target_year = '大二'
        elif y_code == '3': target_year = '大三'
        elif y_code == '4': target_year = '大四'
        if s_code == '1': target_sem = '上學期'
        elif s_code == '2': target_sem = '下學期'
        
    conn = get_db_connection()
    cursor = conn.cursor()
    # 將該學期所有已在課表上的課程（非追蹤中狀態），一鍵退回 'taking' (修課中/預排) 狀態
    cursor.execute('''
        UPDATE courses 
        SET status = 'taking' 
        WHERE user_id = %s AND target_year = %s AND target_semester = %s AND status != 'tsmc_pending'
    ''', (user_id, target_year, target_sem))
    
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('planning'))

@app.route('/delete/<int:course_id>', methods=['POST'])
def delete_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM courses WHERE id = %s AND user_id = %s', (course_id, session['user_id']))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('planning'))

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
    
    if course_info:
        is_conflict, conflict_msg = check_time_conflict(session['user_id'], course_info['name'], updated_target, updated_sem, ignore_course_id=course_id)
        if is_conflict:
            cursor.close()
            conn.close()
            return f"<script>alert('更新失敗：{conflict_msg}'); window.history.back();</script>"

    cursor.execute('UPDATE courses SET status = %s, target_year = %s, target_semester = %s WHERE id = %s AND user_id = %s', 
                   (updated_status, updated_target, updated_sem, course_id, session['user_id']))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('planning'))

@app.route('/api/update_tsmc_settings', methods=['POST'])
def update_tsmc_settings():
    try:
        if 'user_id' not in session:
            return jsonify({"status": "error", "message": "請先登入"}), 401
    
        data = request.get_json(silent=True, force=True) or {}
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = '''
            UPDATE courses 
            SET status = %s, target_year = %s, target_semester = %s 
            WHERE id = %s AND user_id = %s
        '''
        params = (
            data.get('status'), 
            data.get('target_year'), 
            data.get('target_semester'), 
            data.get('course_id'), 
            session['user_id']
        )
        
        cursor.execute(query, params)
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({"status": "success", "message": "修課設定更新成功！"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    print("🚀 伺服器啟動中！")
    app.run(debug=True, port=5000)
