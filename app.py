import os
import sqlite3
import json
import re
import uuid
from flask import Flask, render_template, request, redirect, url_for, session
from flask_dance.contrib.google import make_google_blueprint, google

# 允許在本地端 (http) 測試 Google 登入
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# 🌟 這裡就是你遺失的關鍵！必須先建立 app
app = Flask(__name__)
app.secret_key = "nthu_cheme_secret_key"

# 🌟 設定本機 VS Code 開發的相對路徑
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'chem_courses_v2.db')
JSON_PATH = os.path.join(BASE_DIR, 'requirements(3).json')

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# ==========================================
# 1. 載入 Google 登入藍圖
# ==========================================
blueprint = make_google_blueprint(
    client_id="377112972961-22stlmtreke461al4o4b1o5p7l4nqif0.apps.googleusercontent.com",
    client_secret="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", # ⚠️ 測試用，上線前建議隱藏
    scope=["profile", "email"],
    offline=True
)
app.register_blueprint(blueprint, url_prefix="/login")

# ==========================================
# 2. 解析課程與擋修資料
# ==========================================
COURSE_DATA = {}
try:
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        raw_courses = json.load(f)
    for c in raw_courses:
        c_name = c.get('name', '')
        c_id = c.get('id', '')
        key = f"{c_name} ({c_id})"
        t_str = c.get('time_slots') or ''
        times = [t[0].upper() + t[1:].lower() for t in re.findall(r'[MTWRFS][1-9a-cnA-CN]', t_str)]
        sem_raw = str(c.get('semester', ''))
        ctype = c.get('type', '')
        
        default_year, default_semester = '其他', '上學期'
        if '-' in sem_raw:
            parts = sem_raw.split('-')
            if parts[0] == '1': default_year = '大一'
            elif parts[0] == '2': default_year = '大二'
            elif parts[0] == '3': default_year = '大三'
            elif parts[0] == '4': default_year = '大四'
            if len(parts) > 1 and parts[1] == '2': default_semester = '下學期'
            
        COURSE_DATA[key] = {
            "base_name": c_name, "display_name": f"{c_name} ({c.get('instructor') or '未知'})", 
            "credits": float(c.get('credits', 0)), "year": default_year, "semester": default_semester, 
            "times": times, "type": ctype, "orig_sem": sem_raw
        }
except FileNotFoundError:
    print(f"⚠️ 找不到 {JSON_PATH}！請確保它跟 app.py 放在同一個資料夾。")

PREREQUISITE_RULES = {
    "物理化學一": [ ["普通化學一"], ["普通化學二"], ["微積分二", "微積分Ｂ二"] ],
    "程序控制": [ ["資訊系統應用"] ],
    "程序設計": [ ["化工單操"] ],
    "儀器分析及實驗一": [ ["物理化學一"], ["物理化學二"] ],
    "輸送現象及單元操作一": [ ["工程數學一"], ["工程數學二"] ],
    "輸送現象及單元操作二": [ ["輸送現象及單元操作一"] ],
    "化工單操": [ ["質能均衡"] ],
    "工程數學一": [ ["微積分一", "微積分Ｂ一"], ["微積分二"] ],
    "工程數學二": [ ["工程數學一"] ],
    "基礎高分子化學": [ ["有機化學一", "有機化學二"] ]
}

def init_db():
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS courses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, credits INTEGER NOT NULL, status TEXT NOT NULL, target_year TEXT, target_semester TEXT, warning TEXT, FOREIGN KEY (user_id) REFERENCES users (id))')
    conn.commit(); conn.close()
init_db()

# ==========================================
# 3. 路由與 API 邏輯 (融合 Google 與 訪客登入)
# ==========================================

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/login')
def login_page():
    if 'user_id' in session or google.authorized:
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/login/guest')
def login_guest():
    # 產生訪客專屬 ID
    guest_username = f"guest_{uuid.uuid4().hex[:8]}"
    guest_name = "訪客 (Guest)"
    
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (guest_username, 'guest_dummy'))
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    
    session['user_id'] = user_id
    session['username'] = guest_name
    return redirect(url_for('home'))

@app.route('/', methods=['GET', 'POST'])
def home():
    if 'user_id' not in session:
        if google.authorized:
            resp = google.get("/oauth2/v2/userinfo")
            if resp.ok:
                email = resp.json()["email"]
                name = resp.json().get("name", email.split('@')[0])
                
                conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
                user = conn.execute('SELECT * FROM users WHERE username = ?', (email,)).fetchone()
                if not user:
                    cursor = conn.cursor()
                    cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (email, 'google_sso_dummy'))
                    conn.commit()
                    user_id = cursor.lastrowid
                else:
                    user_id = user['id']
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
        new_credits = COURSE_DATA.get(new_name, {}).get('credits', 0)
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                       (user_id, new_name, new_credits, new_status, target_year, target_semester, ""))
        conn.commit(); conn.close()
        return redirect(url_for('home'))

    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    db_courses = [dict(r) for r in conn.execute('SELECT * FROM courses WHERE user_id = ?', (user_id,)).fetchall()]
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
                if len(taking_times.get(t, [])) > 1:
                    course['is_conflict'] = True
                    c_conflicts.extend([COURSE_DATA.get(n, {}).get('base_name', n) for n in taking_times[t] if n != c_key])
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

    my_credits = sum(c['credits'] for c in db_courses if c['status'] == 'passed')
    mock_data = {
        "total_credits": my_credits, "required_credits": 128, "percentage": min(round((my_credits / 128) * 100, 1), 100),
        "grouped_courses": grouped_courses, "course_dict": COURSE_DATA,
        "gen_ed_progress": {"核心通識": 0, "一般通識": 0}, "gen_ed_percentage": 0,
        "periods": periods, "days": days, "semesters": semesters, "yearly_grids": yearly_grids
    }
    return render_template('dashboard.html', data=mock_data)

@app.route('/import_compulsory', methods=['POST'])
def import_compulsory():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    user_id = session['user_id']
    target_sem_code = request.form.get('year_sem')
    target_status = request.form.get('status', 'taking')
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # 1. 取得已經在課表中的課程本名 (防重複)
    db_courses = cursor.execute('SELECT name FROM courses WHERE user_id=?', (user_id,)).fetchall()
    existing_base_names = set()
    for row in db_courses:
        c_name = row['name']
        base = COURSE_DATA.get(c_name, {}).get('base_name', c_name)
        existing_base_names.add(base)
        
    # 2. 掃描課程資料庫
    for key, details in COURSE_DATA.items():
        # 🌟 完美擴充雷達：加入 'compulsory_elective' 捕捉所有必選修與微積分等課程
        valid_types = ['compulsory', 'elective', 'compulsory_elective', 'common', 'required', 'core']
        
        if details['type'] in valid_types and details['orig_sem'] == target_sem_code:
            base_name = details['base_name']
            
            # 確保同名的課程只會匯入 JSON 檔裡的第一個老師
            if base_name not in existing_base_names:
                cursor.execute('INSERT INTO courses (user_id, name, credits, status, target_year, target_semester, warning) VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (user_id, key, details['credits'], target_status, '預設', '預設', ""))
                
                # 記錄已匯入的課程本名，擋下後續同名課程
                existing_base_names.add(base_name)
                
    conn.commit()
    conn.close()
    return redirect(url_for('home'))

@app.route('/delete/<int:course_id>', methods=['POST'])
def delete_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM courses WHERE id = ? AND user_id = ?', (course_id, session['user_id']))
    conn.commit(); conn.close()
    return redirect(url_for('home'))

@app.route('/edit/<int:course_id>', methods=['POST'])
def edit_course(course_id):
    if 'user_id' not in session: return redirect(url_for('login_page'))
    updated_status = request.form.get('status')
    updated_target = request.form.get('target_year')
    updated_sem = request.form.get('target_semester')
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('UPDATE courses SET status = ?, target_year = ?, target_semester = ? WHERE id = ? AND user_id = ?', 
                   (updated_status, updated_target, updated_sem, course_id, session['user_id']))
    conn.commit(); conn.close()
    return redirect(url_for('home'))

if __name__ == '__main__':
    print("🚀 伺服器啟動中！請在瀏覽器輸入 http://127.0.0.1:5000")
    app.run(debug=True, port=5000)