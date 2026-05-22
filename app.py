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
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# 1. 唯一初始化 Flask
app = Flask(__name__)
app.secret_key = "nthu_cheme_secret_key"

# 2. 設定本機 VS Code 開發的相對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'chem_courses_v2.db')
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

# 修改原本的 sqlite3 連線函數為新的 PostgreSQL 連線函數
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)
# ==========================================
# 🌟 課程資料載入與分類
# ==========================================
 # 👈 記得改回你的檔名

# 1. 建立一個智慧安全讀取函數，自動包容各種 JSON 格式
def fetch_all_courses(paths):
    combined_courses = []
    for path in paths:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    # 正常的 [ {...}, {...} ] 格式
                    combined_courses.extend(data)
                elif isinstance(data, dict):
                    # 忘記加中括號的單一 {...} 格式
                    if "name" in data:
                        combined_courses.append(data)
                    else:
                        # 特殊的 dict of dicts 格式
                        combined_courses.extend(data.values())
        except FileNotFoundError:
            print(f"⚠️ 找不到 {path}！")
        except Exception as e:
            print(f"⚠️ 讀取 {path} 時發生錯誤: {e}")
    return combined_courses

# 2. 取得合併後的所有課程 (確保裡面都是 dict)
# 2. 取得合併後的所有課程 (確保裡面都是 dict)
# 🌟 修正：只讀取主課程清單，不要把規則檔 (JSON_PATH_2) 混進來污染課程資料庫！
ALL_RAW_COURSES = fetch_all_courses([JSON_PATH])

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
            # 🌟 超級防呆：將所有空格與底線去掉並轉換為大寫，如 "Core GE 1" -> "COREGE1"
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


# 4. 建立供排課搜尋引擎使用的 COURSE_DATA (精準去噪智慧版)
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
        
        # 🌟 智慧分流第一軌：優先處理 "1-1", "2-2" 這種標準格式
        if '-' in sem_raw and re.match(r'^\d+-\d+$', sem_raw):
            y_part, s_part = sem_raw.split('-')
            if y_part == '1': default_year = '大一'
            elif y_part == '2': default_year = '大二'
            elif y_part == '3': default_year = '大三'
            elif y_part == '4': default_year = '大四'
            
            if s_part == '1': default_semester = '上學期'
            elif s_part == '2': default_semester = '下學期'
        else:
            # 🌟 智慧分流第二軌：針對特殊或留空格式，從課號 ID 進行解構
            c_id_clean = c_id.upper().replace(" ", "")
            
            # 精準抽離學期：看前 5 碼學期代碼 (10結尾為上學期，20結尾為下學期)
            if re.match(r'^\d{5}', c_id_clean):
                sem_code = c_id_clean[:5]
                if sem_code.endswith('10'): default_semester = '上學期'
                elif sem_code.endswith('20'): default_semester = '下學期'
            else:
                if '下' in sem_raw or sem_raw == '2': default_semester = '下學期'
                else: default_semester = '上學期'
                
            # 精準抽離年級：先剝離前 5 碼數字，再抓英文代碼後的第一個數字 (如 CHE 1160 -> 1 -> 大一)
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
    # 🌟 使用 SERIAL 取代 AUTOINCREMENT
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
    passed_courses = cursor.execute('SELECT * FROM courses WHERE user_id=%s AND status="passed"', (user_id,)).fetchall()
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
    
    for c in passed_courses:
        c_dict = dict(c)
        db_name = c_dict.get('name', '')
        
        # 🌟 修正：只要名稱裡面包含 blank 就跳過，避免算入總學分
        if not db_name or "blank" in db_name.lower():
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
        
        # 🎯 第一道防線：JSON 的 type 欄位有明確寫出數字
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
            
        # 🚑 第二道防線 (救援機制)：如果有 GEC 或 COREGE 標籤，但 JSON 漏寫向度數字
        elif "COREGE" in raw_type or "GEC" in c_id:
            ge_total_credits += cred
            
            # 根據課程名稱關鍵字，自動推測對應的四大核心向度
            if any(k in db_name for k in ["經濟","大氣", "社會", "政治", "法律", "天文", "醫學"]):
                ge_results["Core GE4"].append(db_name) 
            elif any(k in db_name for k in [ "藝術", "文學", "邏輯", "倫理"]):
                ge_results["Core GE3"].append(db_name)
            elif any(k in db_name for k in ["腦", "心智", "科學", "心理","哲學", "科技"]):
                ge_results["Core GE2"].append(db_name)
            elif any(k in db_name for k in [ "思維","文明", "歷史", "文化"]):
                ge_results["Core GE1"].append(db_name) 
            else:
                # 真的猜不出來，才丟到一般通識
                general_ge_list.append(db_name)
                
        # 第三道防線：純一般通識
        elif "GE" in raw_type:
            general_ge_list.append(db_name)
            ge_total_credits += cred
            
        # 🌟 修正：語文領域強制攔截機制 (不論 type 是什麼，只要課名有中文就抓進來)
        elif "LANG" in raw_type or "大學中文" in db_name or "大一中文" in db_name:
            if "中文" in db_name or "CHINESE" in db_name.upper():
                chinese_credits += cred
                chinese_list.append(db_name)
            else:
                english_credits += cred
                english_list.append(db_name)
                
        elif "PE" in raw_type:
            pe_count += 1
            
        else:
            compulsory_credits += cred

    return {
        'total_credits': int(total_credits),
        'compulsory': {'current': int(compulsory_credits), 'max': 64, 'percent': min(100, int((compulsory_credits/64)*100))},
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
            'chinese_list': chinese_list
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

# ⚠️ 注意：為了解決 405 衝突，訪客登入的網址已改為 /guest-login
@app.route('/guest-login', methods=['GET', 'POST'])
def login_guest():
    guest_username = f"guest_{uuid.uuid4().hex[:8]}"
    guest_name = "訪客 (Guest)"
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute('INSERT INTO users (username, password) VALUES (%s, %s)', (guest_username, 'guest_dummy'))
    conn.commit()
    cursor.execute("SELECT LASTVAL()")
    user_id = cursor.fetchone()[0]
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
# 🌟 多頁面架構路由 (修課總覽、通識、語文、體育)
# ==========================================
@app.route('/')  # 👈 請改成你實際對應修課概況的路由路徑 (例如 /dashboard)
def home():
    if 'user_id' not in session:
        return redirect(url_for('login_guest'))
        
    user_id = session['user_id']
    
    # 🌟 核心修正：必須把 get_user_dashboard_data 移入函數「內部」！
    # 這樣每次使用者點擊這個頁面，程式才會強迫去資料庫重新撈取最新修課進度
    dashboard_data = get_user_dashboard_data(user_id)
    
    # 將動態計算出的最新資料傳送給前端網頁
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
# TSMC 製程學程專區 (讀取 tsmc_program_rules.json 版)
# ==========================================

# ==========================================
# 🌟 全新動態多學程管理引擎 (課號去噪精準對位版)
# ==========================================

@app.route('/api/add_tsmc_courses', methods=['POST'])
def add_tsmc_courses():
    """一鍵帶入指定學程的所有相關課程 (自動去噪對位)"""
    try:
        if 'user_id' not in session:
            return jsonify({"status": "error", "message": "請先登入"}), 401
            
        user_id = session['user_id']
        data = request.get_json() or {}
        selected_program = data.get('program_name')
        
        with open(JSON_PATH_2, 'r', encoding='utf-8') as f:
            tsmc_rules = json.load(f)
            
        if not selected_program or selected_program not in tsmc_rules:
            return jsonify({"status": "error", "message": "未指定有效的學程名稱"}), 400
            
        tsmc_data = tsmc_rules.get(selected_program, {})
        
        # 收集該指定學程在 rules.json 裡的所有合法課程 ID
        tsmc_course_ids = set()
        for cat_name, cat_info in tsmc_data.items():
            if isinstance(cat_info, dict):
                for sub_name, sub_info in cat_info.get('subjects', {}).items():
                    for c in sub_info.get('courses', []):
                        c_id = str(c.get('id', '')).replace(" ", "").upper()
                        if c_id:
                            tsmc_course_ids.add(c_id)
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('SELECT name FROM courses WHERE user_id=%s AND status IN ("tsmc_pending", "taking", "passed")', (user_id,))
        existing_courses = {row[0] for row in cursor.fetchall()}
        
        added_count = 0
        for details in ALL_RAW_COURSES:
            if not isinstance(details, dict): continue
            c_name = details.get('name', '')
            if not c_name or c_name.lower() == 'blank': continue
            
            raw_id = str(details.get('id', '')).upper().replace(" ", "")
            # 🌟 核心去噪：撥開大資料庫課號前方的 5 位學期前綴 (如 11410)
            short_raw_id = re.sub(r'^\d{5}', '', raw_id)
            
            full_key = f"{c_name} ({details.get('id', '')})" if details.get('id') else c_name
            if full_key in existing_courses: continue
            
            # 比對：去噪後的短課號或原始長課號是否命中規則庫
            is_match = (short_raw_id in tsmc_course_ids) or (raw_id in tsmc_course_ids)
            if not is_match:
                prog_cats = details.get("program_categories", [])
                if isinstance(prog_cats, list):
                    for cat in prog_cats:
                        if str(selected_program) in str(cat):
                            is_match = True
                            break
                            
            if is_match:
                cursor.execute('''
                    INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', (user_id, full_key, float(details.get('credits', 0)), 'tsmc_pending', '預設', '預設', ""))
                added_count += 1
                existing_courses.add(full_key)

        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": f"成功將「{selected_program}」的 {added_count} 門課程帶入追蹤清單！請至學程頁面查看。"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"後端錯誤：\n{traceback.format_exc()}"}), 500

@app.route('/tsmc_program')
def tsmc_program():
    """動態學程進度展示頁面 (完美整合版：動態分類 + 標籤系統 + 門數解析)"""
    try:
        if 'user_id' not in session: 
            return redirect(url_for('login_page'))
        user_id = session['user_id']

        with open(JSON_PATH_2, 'r', encoding='utf-8') as f:
            tsmc_rules = json.load(f)
        all_programs = list(tsmc_rules.keys())

        current_program = request.args.get('program', '').strip()
        if not current_program or current_program not in tsmc_rules:
            current_program = all_programs[0] if all_programs else ""

        tsmc_data = tsmc_rules.get(current_program, {}) if current_program else {}

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        tsmc_courses = cursor.execute(
            'SELECT * FROM courses WHERE user_id=%s AND status IN ("tsmc_pending", "taking", "passed")', 
            (user_id,)
        ).fetchall()
        conn.close()

        # 🌟 核心去噪引擎 Helper
        def get_core_id(cid):
            if not cid: return ""
            cid = str(cid).upper().replace(" ", "")
            cid = re.sub(r'^\d{5}', '', cid)
            match = re.search(r'([A-Z]+)(\d{4})', cid)
            return match.group(1) + match.group(2) if match else cid

        # 🌟 標籤轉譯 Helper (系必修、系必選、系選修)
        def get_type_label(ctype):
            ctype = str(ctype).lower()
            if 'compulsory' in ctype or '必修' in ctype: return "系必修"
            if 'elective_required' in ctype or '必選' in ctype: return "系必選"
            return "" # 預設留空，不再到處擴散系選修！ # 預設類別

        # 1. 根據 JSON 檔動態建置官方科目骨架
        tsmc_progress = {}
        program_course_lookup = {}
        
        if tsmc_data:
            for cat_name, cat_info in tsmc_data.items():
                if isinstance(cat_info, dict):
                    tsmc_progress[cat_name] = {
                        'count': 0,
                        'rule_text': cat_info.get('rule_text', '無特定規則'),
                        'subjects': {}
                    }
                    for sub_name, sub_info in cat_info.get('subjects', {}).items():
                        tsmc_progress[cat_name]['subjects'][sub_name] = {
                            'courses': [], 
                            'has_passed': False,
                            'labels': set() # 初始化標籤集合
                        }
                        for rule_c in sub_info.get('courses', []):
                            r_core_id = get_core_id(rule_c.get('id', ''))
                            if r_core_id:
                                program_course_lookup[r_core_id] = (cat_name, sub_name, rule_c.get('is_recommended', False))

        # 2. 流經資料庫課程進行分類與精準匹配
        for c in tsmc_courses:
            c_dict = dict(c)
            db_name = str(c_dict.get('name', '')).strip()
            
            db_id = ""
            if ' (' in db_name:
                parts = db_name.split(' (')
                if len(parts) > 1:
                    db_id = parts[1].replace(')', '').strip()
            
            core_db_id = get_core_id(db_id)
            tsmc_cat, tsmc_sub, is_rec = "", "", False
            matched = False

            # 軌道一：核心課號匹配 JSON
            if core_db_id in program_course_lookup:
                tsmc_cat, tsmc_sub, is_rec = program_course_lookup[core_db_id]
                matched = True

            # 軌道二：大資料庫原生標籤補救
            if not matched:
                for raw_c in ALL_RAW_COURSES:
                    if not isinstance(raw_c, dict): continue
                    raw_id = str(raw_c.get('id', '')).upper()
                    raw_core_id = get_core_id(raw_id)
                    
                    if (core_db_id and core_db_id == raw_core_id) or (db_name.split(' (')[0] == str(raw_c.get('name', '')).strip()):
                        prog_cats = raw_c.get("program_categories", [])
                        prog_subs = raw_c.get("program_subjects", [])
                        
                        temp_cat, temp_sub = "", ""
                        if isinstance(prog_cats, list):
                            for cat in prog_cats:
                                for valid_cat in tsmc_progress.keys():
                                    if valid_cat in str(cat) or str(cat) in valid_cat:
                                        temp_cat = valid_cat
                                        break
                                if temp_cat: break
                        
                        if isinstance(prog_subs, list):
                            for sub in prog_subs:
                                if current_program in str(sub):  
                                    sub_str = str(sub)
                                    parsed_sub = sub_str.split('-')[-1].strip() if '-' in sub_str else sub_str.strip()
                                    if temp_cat and parsed_sub in tsmc_progress[temp_cat]['subjects']:
                                        temp_sub = parsed_sub
                        
                        if temp_cat and temp_sub:
                            tsmc_cat = temp_cat
                            tsmc_sub = temp_sub
                            is_rec = raw_c.get('is_tsmc_recommended', False) or raw_c.get('is_recommended', False)
                            matched = True
                        break

            # 絕對隔離：不符合則淘汰，不允許出現未定義
            if not matched or not tsmc_cat or not tsmc_sub:
                continue

            if tsmc_cat in tsmc_progress and tsmc_sub in tsmc_progress[tsmc_cat]['subjects']:
                # 取得該課程的型態資訊
                course_type = ""
                for raw_c in ALL_RAW_COURSES:
                    if isinstance(raw_c, dict) and get_core_id(raw_c.get('id', '')) == core_db_id:
                        course_type = str(raw_c.get('type', '')).lower().strip()
                        break

                # 🌟 產生標籤，且「只有當標籤有文字時」才存入學科的集合中
                c_label = get_type_label(course_type)
                if c_label:
                    tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['labels'].add(c_label)

                tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['courses'].append({
                    'id': c_dict['id'], 
                    'name': db_name, 
                    'status': c_dict['status'],
                    'type_label': c_label, # 傳給前端 (若無則為空字串)
                    # ... 下方維持不變
                })
                if c_dict['status'] == 'passed': 
                    tsmc_progress[tsmc_cat]['subjects'][tsmc_sub]['has_passed'] = True

        # 3. 統計與動態解析規範門數
        for cat_name, cat_data in tsmc_progress.items():
            cat_data['count'] = sum(1 for sub in cat_data['subjects'].keys() if cat_data['subjects'][sub]['has_passed'])
            
            rt = str(cat_data.get('rule_text', ''))
            total_subs = len(cat_data['subjects'])
            req_num = total_subs # 預設全修
            
            if rt:
                match_select = re.search(r'選(\d+)', rt)
                if match_select:
                    req_num = int(match_select.group(1))
                else:
                    nums = re.findall(r'\d+', rt)
                    if nums:
                        req_num = int(nums[-1]) 
            
            if req_num <= 0: req_num = 1
            cat_data['required_num'] = req_num

        # 為了前端 Jinja 渲染穩定，將集合 (set) 轉為列表 (list)
        for cat_name, cat_data in tsmc_progress.items():
            for sub_name, sub_data in cat_data['subjects'].items():
                sub_data['labels'] = list(sub_data['labels'])

        return render_template('tsmc_program.html', progress=tsmc_progress, all_programs=all_programs, current_program=current_program)
        
    except Exception as e:
        import traceback
        return f"渲染錯誤，完整堆疊資訊：\n<pre>{traceback.format_exc()}</pre>"
# ==========================================
# 🌟 排課系統與其他操作
# ==========================================
@app.route('/planning', methods=['GET', 'POST'])
def planning():
    if 'user_id' not in session:
        if google.authorized:
            resp = google.get("/oauth2/v2/userinfo")
            if resp.ok:
                email = resp.json()["email"]
                name = resp.json().get("name", email.split('@')[0])
                conn = sqlite3.connect(DB_PATH); cursor_factory=RealDictCursor
                user = conn.execute('SELECT * FROM users WHERE username = %s', (email,)).fetchone()
                if not user:
                    cursor = conn.cursor()
                    cursor.execute('INSERT INTO users (username, password) VALUES (%s, %s)', (email, 'google_sso_dummy'))
                    conn.commit(); user_id = cursor.lastrowid
                else:
                    user_id = user['id']
                conn.close()
                session['user_id'] = user_id; session['username'] = name
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
        new_credits = COURSE_DATA.get(new_name, {}).get('credits', 0)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (%s, %s, %s, %s, %s, %s, %s)', 
                       (user_id, new_name, new_credits, new_status, target_year, target_semester, ""))
        conn.commit(); conn.close()
        return redirect(url_for('planning'))

    conn = sqlite3.connect(DB_PATH); cursor_factory=RealDictCursor
    # ✅ 修正後：過濾掉 tsmc_only 的課程，讓排課表保持乾淨
    # ✅ 修正後：只有完全未決定的 'tsmc_pending' 才會被排課系統無視。
    # 一旦用戶選了已修畢(passed)或排課中(taking)，它就會自動飛進排課系統與課表格子，進行衝堂/擋修判定！
    db_courses = [dict(r) for r in conn.execute('SELECT * FROM courses WHERE user_id = %s AND status != "tsmc_pending"', (user_id,)).fetchall()]
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
                # 🌟 改用「學期 + 時間」作為唯一的衝突識別碼 (Key)
                # 這樣確保只有在「同學期」的情況下，衝堂才會成立
                conflict_key = f"{course['final_semester']}_{t}"
                
                # 檢查這個「學期+時間」組合中，是否有超過一門課
                if len(taking_times.get(conflict_key, [])) > 1:
                    course['is_conflict'] = True
                    # 將與此課程衝突的其他課程名稱存入
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

    # ==========================================================
    # 🌟 終極整合：年級學分預警引擎 ＋ 多學程選單變數傳遞 (後半段)
    # ==========================================================
    grade_summary = {
        '大一': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大二': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大三': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}},
        '大四': {'上學期': {'courses': [], 'credits': 0, 'warnings': []}, '下學期': {'courses': [], 'credits': 0, 'warnings': []}}
    }
    
    for c in db_courses:
        c_dict = dict(c)
        # 安全獲取判定年級與學期
        g_key = c_dict.get('final_year') or c_dict.get('target_year') or '預設'
        s_key = c_dict.get('final_semester') or c_dict.get('target_semester') or '預設'
        
        if g_key in grade_summary and s_key in ['上學期', '下學期']:
            grade_summary[g_key][s_key]['courses'].append(c_dict)
            grade_summary[g_key][s_key]['credits'] += c_dict.get('credits', 0)
            
    # 智慧學分高低限與衝堂擋修預警檢測
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

    # 計算累積已通過學分
    my_credits = sum(c['credits'] for c in db_courses if c['status'] == 'passed')
    
    mock_data = {
        "total_credits": my_credits, "required_credits": 128, "percentage": min(round((my_credits / 128) * 100, 1), 100),
        "grouped_courses": grouped_courses, "course_dict": COURSE_DATA,
        "gen_ed_progress": {"核心通識": 0, "一般通識": 0}, "gen_ed_percentage": 0,
        "periods": periods, "days": days, "semesters": semesters, "yearly_grids": yearly_grids
    }
    
    # 🌟 動態讀取學程設定檔，提取所有學程清單供前端下拉選單渲染
    try:
        with open(JSON_PATH_2, 'r', encoding='utf-8') as f:
            tsmc_rules = json.load(f)
        all_programs = list(tsmc_rules.keys())
    except Exception:
        all_programs = ["台積電半導體製程模組學程"] # 安全防呆備援
    
    # 🌟 最終回傳：務必同時打包 data, grade_summary, all_programs 三個原物料送往前端！
    return render_template('planning.html', data=mock_data, grade_summary=grade_summary, all_programs=all_programs)
    
@app.route('/import_compulsory', methods=['POST'])
def import_compulsory():
    """帶入必修課核心路由 (嚴格去噪與系所白名單版)"""
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
    
    conn = sqlite3.connect(DB_PATH); cursor_factory=RealDictCursor; cursor = conn.cursor()
    db_courses = cursor.execute('SELECT id, name, status FROM courses WHERE user_id=%s', (user_id,)).fetchall()
    
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
        
        # 🛡️ 防線一：嚴格過濾所有非本系主要必修的干擾源 (排除通識核心、體育、外語等)
        if any(kw in c_type_clean for kw in ['ge', 'pe', 'lang', 'general', 'sport', 'option', 'ext']):
            continue
        if any(kw in base_name for kw in ['通識', '體育', '服務學習', '外文', '英文', '外語', '全民國防', '專題', '學術倫理', '歷史', '當代']):
            continue
            
        # 🛡️ 防線二：精準白名單鎖定，撤銷 core 與 required，防止核心通識課偽裝滲入
        is_compulsory = any(kw in c_type_clean for kw in ['compulsory', '必修', '必選'])
        
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

    conn.commit(); conn.close()
    return redirect(url_for('planning'))

@app.route('/delete/<int:course_id>', methods=['POST'])
def delete_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM courses WHERE id = %s AND user_id = %s', (course_id, session['user_id']))
    conn.commit(); conn.close()
    return redirect(url_for('planning'))

@app.route('/edit/<int:course_id>', methods=['POST'])
def edit_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    updated_status = request.form.get('status')
    updated_target = request.form.get('target_year')
    updated_sem = request.form.get('target_semester')
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('UPDATE courses SET status = %s, target_year = %s, target_semester = %s WHERE id = %s AND user_id = %s', 
                   (updated_status, updated_target, updated_sem, course_id, session['user_id']))
    conn.commit(); conn.close()
    return redirect(url_for('planning'))

@app.route('/api/update_tsmc_settings', methods=['POST'])
def update_tsmc_settings():
    """更新台積電學程課程的修課設定 (PostgreSQL 版本)"""
    try:
        if 'user_id' not in session:
            return jsonify({"status": "error", "message": "請先登入"}), 401
            
        data = request.get_json()
        
        # 使用你剛才定義的 PostgreSQL 連線函數
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # PostgreSQL 的佔位符使用 %s 而非 %s
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
    print("🚀 伺服器啟動中！請在瀏覽器輸入 http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
