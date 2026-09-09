import os, json, re, sqlite3, tempfile, urllib.request, urllib.error, socket, threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote
from pathlib import Path

try:
    from flask import Flask, Response, request
except Exception:
    Flask = None
    Response = None
    request = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None
try:
    import docx
except Exception:
    docx = None

ROOT = Path(__file__).parent
PUBLIC = ROOT / 'public'
DATA = ROOT / 'data'
DB_PATH = Path(os.getenv('KARMAYOGI_DB_PATH', '/tmp/karmayogi.db' if os.getenv('VERCEL') else str(ROOT / 'karmayogi.db')))
PORT = int(os.getenv('PORT') or '3000')
MAX_UPLOAD = 12 * 1024 * 1024
MAX_TEXT = 120_000


def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


load_dotenv(ROOT / '.env')
COURSES = json.loads((DATA / 'courses.json').read_text(encoding='utf-8'))

ROLE_PROFILES = {
    'Statistical Officer': {
        'domain': 'Official Statistics',
        'skills': {'Statistical Analysis': 81, 'Data Analysis': 72, 'Data Visualization': 64, 'Python': 54, 'SQL': 38, 'Survey Design': 68, 'Communication': 84}
    },
    'Data Analyst / Research Officer': {
        'domain': 'Data & Research',
        'skills': {'Data Analysis': 74, 'Data Visualization': 58, 'Python': 52, 'SQL': 48, 'Statistical Analysis': 65, 'AI/ML': 30, 'Communication': 55}
    },
    'Section Officer / Administration': {
        'domain': 'Administration',
        'skills': {'Governance': 72, 'Administrative Law': 61, 'Communication': 58, 'Project Management': 49, 'Digital Governance': 44, 'Data Analysis': 32, 'Cybersecurity': 35}
    },
    'Program / Project Manager': {
        'domain': 'Program Delivery',
        'skills': {'Project Management': 70, 'Leadership': 64, 'Communication': 68, 'Data Analysis': 46, 'Governance': 56, 'Digital Governance': 48, 'AI/ML': 30}
    },
    'IT / Digital Governance Officer': {
        'domain': 'Digital Governance',
        'skills': {'Cybersecurity': 66, 'Digital Governance': 72, 'SQL': 58, 'Python': 55, 'AI/ML': 50, 'Cloud Computing': 42, 'Data Analysis': 52}
    }
}

QUEST_TEMPLATES = [
    ('Prove a weak skill', 'Complete one diagnostic or reassessment on your priority gap.', 'assessment', 40),
    ('Close the gap', 'Open a recommended learning resource and log a learning action.', 'learning', 60),
    ('Practice at work', 'Complete one role-specific workplace simulation.', 'simulation', 80),
    ('Ask with context', 'Use the AI Tutor to unpack one difficult concept.', 'chat', 20),
]

SIMULATION_BANK = {
    'Statistical Officer': {
        'title': 'District Applications: The 35% Spike',
        'prompt': 'A district reports a sudden 35% increase in applications this month. Before drawing a conclusion, explain the possible causes and what additional data you would request. Your response should show how you would test whether the increase reflects real demand, a reporting change, or a data-quality issue.',
        'rubric': ['reasoning', 'data interpretation', 'decision making', 'role-specific knowledge', 'communication']
    },
    'Data Analyst / Research Officer': {
        'title': 'Dashboard Drift: A Sudden Metric Change',
        'prompt': 'A monitoring dashboard shows a sudden 22% drop in service completion after a data pipeline change. Explain how you would investigate the issue before briefing leadership, including the checks you would run and the evidence you would want.',
        'rubric': ['reasoning', 'data interpretation', 'decision making', 'role-specific knowledge', 'communication']
    },
    'Section Officer / Administration': {
        'title': 'Urgent File, Missing Record',
        'prompt': 'A senior officer asks for an urgent decision note, but the supporting record is incomplete. Explain how you would proceed while protecting due process, documenting assumptions, and keeping the file moving.',
        'rubric': ['reasoning', 'decision making', 'role-specific knowledge', 'risk awareness', 'communication']
    },
    'Program / Project Manager': {
        'title': 'Delivery at Risk',
        'prompt': 'A flagship program is on schedule overall, but one critical vendor milestone is two weeks late. Explain the actions you would take in the next 48 hours, what evidence you need, and how you would communicate the risk.',
        'rubric': ['reasoning', 'decision making', 'role-specific knowledge', 'risk awareness', 'communication']
    },
    'IT / Digital Governance Officer': {
        'title': 'Citizen Portal Incident',
        'prompt': 'A citizen-facing portal experiences repeated failed logins and a spike in unusual traffic. Explain your first-response priorities, what evidence you would collect, and how you would communicate while preserving security and continuity.',
        'rubric': ['reasoning', 'data interpretation', 'decision making', 'role-specific knowledge', 'communication']
    }
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


def init_db():
    db = connect()
    db.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      designation TEXT NOT NULL,
      department TEXT NOT NULL,
      role TEXT NOT NULL,
      xp INTEGER NOT NULL DEFAULT 720,
      streak INTEGER NOT NULL DEFAULT 7,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_skills (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      skill_name TEXT NOT NULL,
      score INTEGER NOT NULL,
      required_score INTEGER NOT NULL DEFAULT 80,
      confidence TEXT NOT NULL DEFAULT 'Low',
      baseline_score INTEGER NOT NULL,
      last_updated TEXT NOT NULL,
      UNIQUE(user_id, skill_name)
    );
    CREATE TABLE IF NOT EXISTS chat_sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      summary TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chat_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
      role TEXT NOT NULL CHECK(role IN ('user','assistant')),
      content TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS learning_materials (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      filename TEXT NOT NULL,
      file_type TEXT NOT NULL,
      extracted_text TEXT NOT NULL,
      uploaded_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS assessments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      topic TEXT NOT NULL,
      source_material_id INTEGER REFERENCES learning_materials(id) ON DELETE SET NULL,
      question_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS questions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
      topic TEXT NOT NULL,
      difficulty TEXT NOT NULL DEFAULT 'medium',
      question TEXT NOT NULL,
      options_json TEXT NOT NULL,
      correct_answer INTEGER NOT NULL,
      explanation TEXT NOT NULL,
      source_reference TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS assessment_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      score INTEGER NOT NULL,
      percentage INTEGER NOT NULL,
      completed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS assessment_responses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      attempt_id INTEGER NOT NULL REFERENCES assessment_attempts(id) ON DELETE CASCADE,
      question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
      selected_answer INTEGER,
      correct INTEGER NOT NULL,
      response_time INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS learning_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      course_id TEXT NOT NULL,
      course_name TEXT NOT NULL,
      status TEXT NOT NULL,
      completion_percentage INTEGER NOT NULL DEFAULT 0,
      started_at TEXT NOT NULL,
      completed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS recommendations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      skill TEXT NOT NULL,
      course_id TEXT NOT NULL,
      course_name TEXT NOT NULL,
      reason TEXT NOT NULL,
      priority TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS skill_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      skill TEXT NOT NULL,
      event_type TEXT NOT NULL,
      score_delta INTEGER NOT NULL DEFAULT 0,
      evidence_reference TEXT DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS quests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      description TEXT NOT NULL,
      skill TEXT DEFAULT '',
      xp_reward INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      created_at TEXT NOT NULL,
      completed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS simulation_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      prompt TEXT NOT NULL,
      response TEXT NOT NULL,
      score INTEGER NOT NULL,
      feedback TEXT NOT NULL,
      strengths_json TEXT NOT NULL,
      weaknesses_json TEXT NOT NULL,
      completed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS spaced_reviews (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      skill TEXT NOT NULL,
      interval_days INTEGER NOT NULL DEFAULT 1,
      due_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'due',
      UNIQUE(user_id, skill)
    );
    ''')
    db.commit()
    db.close()


def seed_user_if_needed(db, name, department, role):
    row = db.execute('SELECT id FROM users WHERE lower(name)=lower(?) AND role=?', (name, role)).fetchone()
    if row:
        return row['id']
    ts = now_iso()
    cur = db.execute('INSERT INTO users(name,designation,department,role,created_at) VALUES(?,?,?,?,?)', (name, role, department, role, ts))
    user_id = cur.lastrowid
    prof = ROLE_PROFILES.get(role, ROLE_PROFILES['Statistical Officer'])['skills']
    for skill, score in prof.items():
        db.execute('INSERT INTO user_skills(user_id,skill_name,score,required_score,confidence,baseline_score,last_updated) VALUES(?,?,?,?,?,?,?)', (user_id, skill, score, 80, 'Low', score, ts))
    for title, desc, kind, xp in QUEST_TEMPLATES:
        db.execute('INSERT INTO quests(user_id,title,description,skill,xp_reward,status,created_at) VALUES(?,?,?,?,?,?,?)', (user_id, title, desc, kind, xp, 'open', ts))
    db.commit()
    return user_id


def get_user(db, user_id):
    return db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()


def confidence_for(db, user_id, skill):
    cnt = db.execute('SELECT COUNT(*) c FROM skill_events WHERE user_id=? AND skill=?', (user_id, skill)).fetchone()['c']
    sims = db.execute('SELECT COUNT(*) c FROM simulation_attempts WHERE user_id=?', (user_id,)).fetchone()['c']
    if cnt >= 5 or (cnt >= 3 and sims >= 1):
        return 'High'
    if cnt >= 2:
        return 'Medium'
    return 'Low'


def score_gap(score, required=80):
    gap = max(0, required - score)
    if gap <= 10:
        return 'On track'
    if gap <= 25:
        return 'Needs improvement'
    return 'Priority gap'


def normalize_skill_name(topic, skills):
    t = topic.lower()
    for key in skills:
        if key.lower() in t or t in key.lower():
            return key
    aliases = {'statistics':'Statistical Analysis','analytics':'Data Analysis','visualization':'Data Visualization','sql':'SQL','python':'Python','communication':'Communication'}
    for k, v in aliases.items():
        if k in t and v in skills:
            return v
    return min(skills, key=lambda k: skills[k]['score'])


def build_recommendations(db, user_id):
    skills = {r['skill_name']: r['score'] for r in db.execute('SELECT skill_name,score FROM user_skills WHERE user_id=?', (user_id,))}
    gaps = sorted(skills.items(), key=lambda x: x[1])
    recs = []
    for course in COURSES:
        overlaps = [(s, skills.get(s, 0)) for s in course['skills'] if s in skills]
        if overlaps:
            avg_gap = sum(max(0, 80 - v) for _, v in overlaps) / max(1, len(overlaps))
            match = min(99, int(55 + avg_gap))
            focus = max(overlaps, key=lambda x: 80 - x[1])
            reason = f"{focus[0]} is at {focus[1]}/100; this resource directly supports that gap."
            priority = 'HIGH' if focus[1] < 55 else ('MEDIUM' if focus[1] < 70 else 'LOW')
            recs.append((match, course, focus[0], reason, priority))
    recs.sort(key=lambda x: (-x[0], x[1]['title']))
    for rank, (_, c, skill, reason, priority) in enumerate(recs[:6]):
        db.execute('''INSERT INTO recommendations(user_id,skill,course_id,course_name,reason,priority,status,created_at)
                      VALUES(?,?,?,?,?,?,?,?)''', (user_id, skill, c['id'], c['title'], reason, priority, 'open', now_iso()))
    db.commit()
    return gaps, recs[:6]


def user_state(db, user_id):
    user = get_user(db, user_id)
    if not user:
        return None
    skills = []
    for r in db.execute('SELECT * FROM user_skills WHERE user_id=? ORDER BY score ASC', (user_id,)).fetchall():
        skills.append({**dict(r), 'gap': max(0, r['required_score'] - r['score']), 'status': score_gap(r['score'], r['required_score'])})
    attempts = db.execute('''SELECT a.id,a.assessment_id,a.score,a.percentage,a.completed_at,ass.title,ass.topic
                            FROM assessment_attempts a JOIN assessments ass ON ass.id=a.assessment_id
                            WHERE a.user_id=? ORDER BY a.completed_at DESC LIMIT 8''', (user_id,)).fetchall()
    quests = [dict(r) for r in db.execute('SELECT * FROM quests WHERE user_id=? ORDER BY status ASC,id ASC', (user_id,)).fetchall()]
    sessions = [dict(r) for r in db.execute('SELECT id,title,created_at,updated_at FROM chat_sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 12', (user_id,)).fetchall()]
    sims = [dict(r) for r in db.execute('SELECT id,title,score,feedback,completed_at FROM simulation_attempts WHERE user_id=? ORDER BY completed_at DESC LIMIT 5', (user_id,)).fetchall()]
    learning = [dict(r) for r in db.execute('SELECT * FROM learning_history WHERE user_id=? ORDER BY started_at DESC LIMIT 8', (user_id,)).fetchall()]
    skills_map = {x['skill_name']: x['score'] for x in skills}
    avg = round(sum(skills_map.values()) / max(1, len(skills_map)))
    gaps = sorted(skills, key=lambda x: x['score'])[:4]
    recommendations = []
    for row in db.execute('SELECT * FROM recommendations WHERE user_id=? AND status="open" ORDER BY CASE priority WHEN "HIGH" THEN 1 WHEN "MEDIUM" THEN 2 ELSE 3 END,created_at DESC LIMIT 6', (user_id,)).fetchall():
        recommendations.append(dict(row))
    return {
        'user': dict(user), 'skills': skills, 'avg_competency': avg, 'gaps': gaps,
        'assessments': [dict(x) for x in attempts], 'quests': quests,
        'chat_sessions': sessions, 'simulations': sims, 'learning': learning,
        'recommendations': recommendations
    }


def json_body(handler, limit=2_000_000):
    n = int(handler.headers.get('Content-Length', '0'))
    if n > limit:
        raise ValueError('Request body is too large')
    raw = handler.rfile.read(n) if n else b'{}'
    return json.loads(raw or b'{}')


def _gemini_headers(key):
    return {'Content-Type': 'application/json', 'x-goog-api-key': key}


def available_models(key):
    req = urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models', headers=_gemini_headers(key), method='GET')
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return [m for m in data.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]


def choose_model(key):
    configured = os.getenv('GEMINI_MODEL', '').strip()
    models = available_models(key)
    names = [m.get('name', '').replace('models/', '') for m in models]
    preferred = ('gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.5-flash', 'gemini-3.6-flash', 'gemini-3.7-flash')
    if configured and configured in names:
        return configured
    for candidate in preferred:
        if candidate in names:
            return candidate
    for name in names:
        if 'flash' in name.lower() and 'image' not in name.lower():
            return name
    raise RuntimeError('No model available to this API key supports generateContent')


def gemini(prompt, response_schema=None, timeout=40):
    key = os.getenv('GEMINI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('GEMINI_API_KEY is not configured')
    model = choose_model(key)
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{quote(model)}:generateContent'
    generation = {'temperature': 0.25, 'maxOutputTokens': 1600}
    if response_schema:
        generation['responseMimeType'] = 'application/json'
        generation['responseSchema'] = response_schema
    payload = json.dumps({'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': generation}).encode()
    req = urllib.request.Request(url, data=payload, headers=_gemini_headers(key), method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        raise RuntimeError(f'Gemini HTTP {e.code}: {body[:700]}')
    except Exception as e:
        raise RuntimeError(f'Gemini request failed: {e}')
    candidates = data.get('candidates', [])
    if not candidates:
        raise RuntimeError('Gemini returned no candidates')
    text = ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', []) if isinstance(p, dict))
    if not text:
        raise RuntimeError('Gemini returned no text')
    return text, model


QUIZ_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'description': {'type': 'string'},
        'questions': {'type': 'array', 'minItems': 3, 'maxItems': 10, 'items': {
            'type': 'object', 'properties': {
                'id': {'type': 'integer'}, 'topic': {'type': 'string'}, 'difficulty': {'type': 'string', 'enum': ['easy','medium','hard']},
                'question': {'type': 'string'}, 'options': {'type': 'array', 'minItems': 4, 'maxItems': 4, 'items': {'type': 'string'}},
                'answer': {'type': 'integer', 'minimum': 0, 'maximum': 3}, 'explanation': {'type': 'string'}, 'source_reference': {'type': 'string'}
            }, 'required': ['id','topic','difficulty','question','options','answer','explanation','source_reference']
        }}
    }, 'required': ['title','description','questions']
}

SIM_SCHEMA = {
    'type': 'object', 'properties': {
        'score': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        'feedback': {'type': 'string'},
        'strengths': {'type': 'array', 'items': {'type': 'string'}},
        'weaknesses': {'type': 'array', 'items': {'type': 'string'}}
    }, 'required': ['score','feedback','strengths','weaknesses']
}


def clean_json(text):
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, re.I)
    s = m.group(1) if m else text
    a, b = s.find('{'), s.rfind('}')
    if a < 0 or b <= a:
        raise ValueError('Model did not return JSON')
    return json.loads(s[a:b+1])


def validate_quiz(q, expected_count):
    if not isinstance(q, dict) or not isinstance(q.get('questions'), list):
        raise ValueError('Malformed assessment')
    qs = q['questions'][:expected_count]
    if len(qs) < 3:
        raise ValueError('Fewer than 3 valid questions')
    seen = set()
    valid = []
    for x in qs:
        if not all(isinstance(x.get(k), str) and x[k].strip() for k in ('topic','question','explanation')):
            continue
        opts = x.get('options')
        ans = x.get('answer')
        if not isinstance(opts, list) or len(opts) != 4 or any(not str(o).strip() for o in opts):
            continue
        if not isinstance(ans, int) or ans < 0 or ans > 3:
            continue
        key = re.sub(r'\W+', ' ', x['question'].lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        valid.append({**x, 'options': [str(o) for o in opts], 'answer': ans, 'difficulty': x.get('difficulty','medium'), 'source_reference': x.get('source_reference','')})
    if len(valid) < min(3, expected_count):
        raise ValueError('Assessment validation failed')
    q['questions'] = [dict(x, id=i+1) for i, x in enumerate(valid)]
    return q


def fallback_quiz(topic, count):
    base = [
        ('Core reasoning', f'Which approach best supports competency development in {topic}?', ['Use the same course for every employee','Assess role needs, measure gaps, then target learning','Only track course completion','Avoid reassessment'], 1, 'Competency development is strongest when role needs, evidence, targeted learning and reassessment are connected.'),
        ('Evidence', 'What should a competency score represent?', ['An unquestionable truth about the employee','A summary of evidence with a confidence level','Only time spent learning','Only the latest quiz'], 1, 'A useful prototype score summarizes evidence and should disclose confidence.'),
        ('Gap analysis', 'What is the most useful reason to calculate a skill gap?', ['To rank people publicly','To identify the next learning action','To increase XP only','To replace workplace practice'], 1, 'Skill gaps help prioritize the next learning action.'),
        ('Learning loop', 'What should happen after a learner completes targeted practice?', ['Stop tracking the skill','Reassess and update the evidence','Reset the score','Assign unrelated content'], 1, 'Reassessment closes the loop and reveals whether capability improved.'),
        ('Grounding', 'When questions are generated from uploaded material, what should the system do?', ['Invent missing facts freely','Base questions on extracted source content','Ignore the material','Hide the source context'], 1, 'Source-grounded generation reduces unsupported content and improves traceability.'),
        ('Decision making', 'Which action best demonstrates learning effectiveness?', ['Course opened','Score or practical performance improves after learning','Screen time increased','More notifications sent'], 1, 'Improvement in demonstrated performance is stronger evidence than activity alone.')
    ]
    out = []
    for i in range(min(count, len(base))):
        t,q,opts,a,exp = base[i]
        out.append({'id': i+1,'topic': t,'difficulty': 'medium','question': q,'options': opts,'answer': a,'explanation': exp,'source_reference': 'Local fallback'})
    return {'title': f'{topic} — Skill Check', 'description': 'Local assessment mode. Connect Gemini for source-grounded AI generation.', 'questions': out}


def extract_file(filename, data):
    ext = Path(filename).suffix.lower()
    if len(data) > MAX_UPLOAD:
        raise ValueError('File exceeds the 12 MB upload limit')
    if ext not in {'.pdf', '.docx', '.txt', '.md'}:
        raise ValueError('Unsupported file type')
    if ext == '.pdf':
        if not PdfReader:
            raise RuntimeError('PDF support is unavailable in this environment')
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(data); path = f.name
        try:
            return '\n'.join((p.extract_text() or '') for p in PdfReader(path).pages)
        finally:
            os.unlink(path)
    if ext == '.docx':
        if not docx:
            raise RuntimeError('DOCX support is unavailable in this environment')
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
            f.write(data); path = f.name
        try:
            d = docx.Document(path)
            return '\n'.join(x.text for x in d.paragraphs)
        finally:
            os.unlink(path)
    return data.decode('utf-8', 'ignore')


def chunk_text(text, size=5000):
    cleaned = re.sub(r'\s+', ' ', text).strip()
    return [cleaned[i:i+size] for i in range(0, len(cleaned), size)]


def choose_source(text, query, limit=18000):
    chunks = chunk_text(text)
    if not chunks:
        return ''
    if len(chunks) * 5000 <= limit:
        return text[:limit]
    terms = [t.lower() for t in re.findall(r'[A-Za-z0-9]{4,}', query)]
    scored = []
    for ch in chunks:
        low = ch.lower()
        score = sum(low.count(t) for t in terms)
        scored.append((score, ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    return '\n'.join(ch for _, ch in scored[:max(1, limit // 5000)])[:limit]


class Handler(BaseHTTPRequestHandler):
    server_version = 'KarmayogiAI/1.0'

    def log_message(self, fmt, *args):
        print(f'[{self.log_date_time_string()}] {fmt % args}')

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = urlparse(self.path).path.rstrip('/') or '/'
        try:
            if p == '/api/health':
                key = os.getenv('GEMINI_API_KEY', '').strip()
                result = {'ok': True, 'aiConfigured': bool(key), 'aiReachable': False, 'model': os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite'), 'error': None}
                if key:
                    try:
                        chosen = choose_model(key); result['aiReachable'] = True; result['model'] = chosen
                    except Exception as e:
                        result['error'] = str(e)
                return self.send_json(result)
            if p == '/api/roles':
                return self.send_json([{'name': k, 'domain': v['domain'], 'skills': v['skills']} for k,v in ROLE_PROFILES.items()])
            if p == '/api/courses':
                return self.send_json(COURSES)
            if p == '/api/state':
                uid = int(urlparse(self.path).query.split('user_id=')[-1]) if 'user_id=' in self.path else 0
                db = connect(); state = user_state(db, uid); db.close()
                return self.send_json(state or {'error': 'User not found'}, 200 if state else 404)
            if p == '/api/assessments':
                uid = int(urlparse(self.path).query.split('user_id=')[-1]) if 'user_id=' in self.path else 0
                db = connect(); rows = db.execute('''SELECT a.id,a.title,a.topic,a.question_count,a.created_at,
                    (SELECT percentage FROM assessment_attempts aa WHERE aa.assessment_id=a.id ORDER BY aa.completed_at DESC LIMIT 1) latest_score
                    FROM assessments a WHERE a.user_id=? ORDER BY a.created_at DESC''',(uid,)).fetchall(); db.close()
                return self.send_json([dict(r) for r in rows])
            m = re.match(r'^/api/assessments/(\d+)$', p)
            if m:
                aid = int(m.group(1)); db=connect(); a=db.execute('SELECT * FROM assessments WHERE id=?',(aid,)).fetchone()
                if not a: db.close(); return self.send_json({'error':'Assessment not found'},404)
                qs=[{**dict(q),'options':json.loads(q['options_json'])} for q in db.execute('SELECT * FROM questions WHERE assessment_id=? ORDER BY id',(aid,)).fetchall()]
                attempts=[dict(x) for x in db.execute('SELECT * FROM assessment_attempts WHERE assessment_id=? ORDER BY completed_at DESC',(aid,)).fetchall()]
                db.close(); return self.send_json({'assessment':dict(a),'questions':qs,'attempts':attempts})
            m = re.match(r'^/api/chat/sessions/(\d+)$', p)
            if m:
                sid=int(m.group(1)); db=connect(); s=db.execute('SELECT * FROM chat_sessions WHERE id=?',(sid,)).fetchone();
                if not s: db.close(); return self.send_json({'error':'Conversation not found'},404)
                msgs=[dict(x) for x in db.execute('SELECT * FROM chat_messages WHERE session_id=? ORDER BY id',(sid,)).fetchall()]; db.close()
                return self.send_json({'session':dict(s),'messages':msgs})
            if p == '/api/chat/sessions':
                uid = int(urlparse(self.path).query.split('user_id=')[-1]) if 'user_id=' in self.path else 0
                db=connect(); rows=[dict(x) for x in db.execute('SELECT id,title,created_at,updated_at FROM chat_sessions WHERE user_id=? ORDER BY updated_at DESC',(uid,)).fetchall()]; db.close(); return self.send_json(rows)
            if p == '/api/admin/analytics':
                db=connect();
                counts = {
                    'employees': db.execute('SELECT COUNT(*) c FROM users').fetchone()['c'],
                    'assessments': db.execute('SELECT COUNT(*) c FROM assessments').fetchone()['c'],
                    'attempts': db.execute('SELECT COUNT(*) c FROM assessment_attempts').fetchone()['c'],
                    'simulations': db.execute('SELECT COUNT(*) c FROM simulation_attempts').fetchone()['c']
                }
                skill=[]
                for r in db.execute('SELECT skill_name,ROUND(AVG(score)) avg_score,COUNT(*) people FROM user_skills GROUP BY skill_name ORDER BY avg_score ASC').fetchall(): skill.append(dict(r))
                db.close(); return self.send_json({'counts':counts,'skills':skill,'dataset':'Representative prototype data; not an official workforce dataset.'})
            file = PUBLIC/'index.html' if p=='/' else PUBLIC/p.lstrip('/')
            if file.exists() and file.is_file():
                data=file.read_bytes(); ctype='text/html; charset=utf-8' if file.suffix=='.html' else 'text/plain; charset=utf-8'
                self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
            self.send_error(404)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path.rstrip('/') or '/'
        db=None
        try:
            if p == '/api/config':
                b=json_body(self); key=(b.get('apiKey') or '').strip()
                if key: os.environ['GEMINI_API_KEY']=key
                elif b.get('clear'): os.environ.pop('GEMINI_API_KEY',None)
                configured=bool(os.getenv('GEMINI_API_KEY'))
                if not configured: return self.send_json({'ok':True,'aiConfigured':False,'aiReachable':False})
                try: model=choose_model(os.environ['GEMINI_API_KEY']); return self.send_json({'ok':True,'aiConfigured':True,'aiReachable':True,'model':model})
                except Exception as e: return self.send_json({'ok':True,'aiConfigured':True,'aiReachable':False,'error':str(e)})

            if p == '/api/login':
                b=json_body(self); name=(b.get('name') or 'Ananya Sharma').strip()[:80]; dept=(b.get('department') or 'Ministry / Department of Statistics').strip()[:120]; role=b.get('role') or 'Statistical Officer'
                if role not in ROLE_PROFILES: return self.send_json({'error':'Unsupported role'},400)
                db=connect(); uid=seed_user_if_needed(db,name,dept,role); gaps,_=build_recommendations(db,uid); db.commit(); state=user_state(db,uid); db.close(); return self.send_json({'user_id':uid,'state':state})

            if p == '/api/quests/complete':
                b=json_body(self); uid=int(b['user_id']); qid=int(b['quest_id']); db=connect(); q=db.execute('SELECT * FROM quests WHERE id=? AND user_id=?',(qid,uid)).fetchone()
                if not q: db.close(); return self.send_json({'error':'Quest not found'},404)
                if q['status']=='complete': db.close(); return self.send_json({'ok':True,'already':True,'xp':0})
                db.execute('UPDATE quests SET status="complete",completed_at=? WHERE id=?',(now_iso(),qid)); db.execute('UPDATE users SET xp=xp+? WHERE id=?',(q['xp_reward'],uid)); db.commit(); state=user_state(db,uid); db.close(); return self.send_json({'ok':True,'xp':q['xp_reward'],'state':state})

            if p == '/api/materials/upload':
                ctype=self.headers.get('Content-Type',''); length=int(self.headers.get('Content-Length','0'))
                if length > MAX_UPLOAD + 20000: return self.send_json({'error':'Upload exceeds 12 MB limit'},413)
                data=self.rfile.read(length); boundary=ctype.split('boundary=')[-1].encode();
                parts=data.split(b'--'+boundary)
                for part in parts:
                    if b'filename=' in part:
                        head,body=part.split(b'\r\n\r\n',1); body=body.rsplit(b'\r\n',1)[0]; m=re.search(br'filename="([^"]+)"',head); name=m.group(1).decode(errors='ignore') if m else 'upload'
                        uid_match=re.search(br'name="user_id"\s*\r\n\r\n(\d+)', part); uid=int(uid_match.group(1)) if uid_match else 0
                        text=extract_file(name,body).replace('\x00',' ')[:MAX_TEXT]
                        db=connect(); cur=db.execute('INSERT INTO learning_materials(user_id,filename,file_type,extracted_text,uploaded_at) VALUES(?,?,?,?,?)',(uid,name,Path(name).suffix.lower().lstrip('.'),text,now_iso())); db.commit(); mid=cur.lastrowid; db.close(); return self.send_json({'id':mid,'filename':name,'chars':len(text),'preview':text[:1200]})
                return self.send_json({'error':'No file found'},400)

            if p == '/api/ai/quiz':
                b=json_body(self); uid=int(b.get('user_id',0)); topic=str(b.get('topic','Government Competency'))[:120]; count=max(3,min(10,int(b.get('count',6)))); material=(b.get('material') or '')[:MAX_TEXT]; material_id=b.get('material_id'); role=b.get('role','Statistical Officer'); prof=ROLE_PROFILES.get(role,ROLE_PROFILES['Statistical Officer'])
                if material_id:
                    db=connect(); row=db.execute('SELECT extracted_text FROM learning_materials WHERE id=? AND user_id=?',(int(material_id),uid)).fetchone(); db.close();
                    if row: material=row['extracted_text']
                source=choose_source(material,topic,limit=18000) if material else 'No uploaded source material. Use the competency profile and topic.'
                prompt=f'''You generate concise, source-grounded workplace assessments. Role: {role}. Domain: {prof['domain']}. Target topic: {topic}. Competencies: {json.dumps(prof['skills'])}. Create exactly {count} MCQs. Each must have 4 distinct options, one unambiguous answer, an explanation, a difficulty, and a source_reference. If source text exists, do not use facts outside it except basic reasoning needed to interpret it. Keep questions practical and role-relevant. SOURCE:\n{source}'''
                source_kind='fallback'; warning=None; model=None
                try:
                    raw,model=gemini(prompt,QUIZ_SCHEMA,timeout=40); q=validate_quiz(clean_json(raw),count); source_kind='gemini'
                except Exception as e:
                    warning=str(e); q=validate_quiz(fallback_quiz(topic,count),count)
                db=connect(); cur=db.execute('INSERT INTO assessments(user_id,title,topic,source_material_id,question_count,created_at) VALUES(?,?,?,?,?,?)',(uid,q['title'],topic,material_id,count,now_iso())); aid=cur.lastrowid
                for x in q['questions']:
                    db.execute('INSERT INTO questions(assessment_id,topic,difficulty,question,options_json,correct_answer,explanation,source_reference) VALUES(?,?,?,?,?,?,?,?)',(aid,x['topic'],x.get('difficulty','medium'),x['question'],json.dumps(x['options']),x['answer'],x['explanation'],x.get('source_reference','')))
                db.commit(); db.close(); q['id']=aid; return self.send_json({'assessment_id':aid,'quiz':q,'source':source_kind,'model':model,'warning':warning})

            if p == '/api/assessments/submit':
                b=json_body(self); uid=int(b['user_id']); aid=int(b['assessment_id']); answers=b.get('answers',[])
                db=connect(); a=db.execute('SELECT * FROM assessments WHERE id=? AND user_id=?',(aid,uid)).fetchone();
                if not a: db.close(); return self.send_json({'error':'Assessment not found'},404)
                qs=db.execute('SELECT * FROM questions WHERE assessment_id=? ORDER BY id',(aid,)).fetchall();
                if not qs: db.close(); return self.send_json({'error':'No questions'},400)
                correct=0; topic_map={}; rows=[]
                for i,q in enumerate(qs):
                    sel=answers[i] if i < len(answers) else None; ok=isinstance(sel,int) and sel==q['correct_answer']; correct+=1 if ok else 0; t=q['topic']; topic_map.setdefault(t,[0,0]); topic_map[t][1]+=1; topic_map[t][0]+=1 if ok else 0; rows.append((q['id'],sel,1 if ok else 0))
                pct=round(correct/len(qs)*100); cur=db.execute('INSERT INTO assessment_attempts(assessment_id,user_id,score,percentage,completed_at) VALUES(?,?,?,?,?)',(aid,uid,correct,pct,now_iso())); atid=cur.lastrowid
                for qid,sel,ok in rows: db.execute('INSERT INTO assessment_responses(attempt_id,question_id,selected_answer,correct,response_time) VALUES(?,?,?,?,?)',(atid,qid,sel,ok,0))
                skills={r['skill_name']:r for r in db.execute('SELECT * FROM user_skills WHERE user_id=?',(uid,)).fetchall()}
                evidence=[]
                for topic,(c,n) in topic_map.items():
                    sc=round(c/n*100); skill=normalize_skill_name(topic,skills); old=skills[skill]['score'] if skill in skills else sc
                    new=round(old*0.7+sc*0.3); delta=new-old
                    if skill in skills:
                        conf=confidence_for(db,uid,skill)
                        db.execute('UPDATE user_skills SET score=?,confidence=?,last_updated=? WHERE user_id=? AND skill_name=?',(max(0,min(100,new)),conf,now_iso(),uid,skill)); db.execute('INSERT INTO skill_events(user_id,skill,event_type,score_delta,evidence_reference,created_at) VALUES(?,?,?,?,?,?)',(uid,skill,'ASSESSMENT_COMPLETED',delta,f'assessment:{aid}',now_iso()));
                        evidence.append({'skill':skill,'before':old,'after':new,'topic_score':sc,'delta':delta})
                        next_days=1 if sc<55 else (3 if sc<70 else 7 if sc<85 else 14)
                        due=(datetime.now(timezone.utc)+timedelta(days=next_days)).isoformat(timespec='seconds')
                        db.execute('''INSERT INTO spaced_reviews(user_id,skill,interval_days,due_at,status) VALUES(?,?,?,?,?)
                                       ON CONFLICT(user_id,skill) DO UPDATE SET interval_days=excluded.interval_days,due_at=excluded.due_at,status='scheduled' ''',(uid,skill,next_days,due,'scheduled'))
                db.execute('UPDATE users SET xp=xp+? WHERE id=?',(correct*10,uid)); db.commit();
                details=[]
                for q in qs:
                    details.append({'id':q['id'],'topic':q['topic'],'question':q['question'],'options':json.loads(q['options_json']),'correct_answer':q['correct_answer'],'explanation':q['explanation']})
                state=user_state(db,uid); db.close(); return self.send_json({'attempt_id':atid,'score':correct,'percentage':pct,'topics':{k:round(v[0]/v[1]*100) for k,v in topic_map.items()},'evidence':evidence,'details':details,'state':state})

            if p == '/api/ai/chat':
                b=json_body(self); uid=int(b.get('user_id',0)); msg=(b.get('message') or '').strip()[:6000]; role=b.get('role','Statistical Officer'); skills=b.get('skills',{}); material=(b.get('material') or '')[:12000]; sid=b.get('session_id')
                if not msg: return self.send_json({'error':'Message is required'},400)
                db=connect()
                if sid:
                    session=db.execute('SELECT * FROM chat_sessions WHERE id=? AND user_id=?',(int(sid),uid)).fetchone()
                else: session=None
                if not session:
                    cur=db.execute('INSERT INTO chat_sessions(user_id,title,created_at,updated_at) VALUES(?,?,?,?)',(uid,msg[:48],now_iso(),now_iso())); sid=cur.lastrowid; session=db.execute('SELECT * FROM chat_sessions WHERE id=?',(sid,)).fetchone()
                history=db.execute('SELECT role,content FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT 8',(sid,)).fetchall(); history=list(reversed(history))
                source=choose_source(material,msg,limit=8000) if material else ''
                gaps=sorted(skills.items(),key=lambda x:x[1])[:4] if isinstance(skills,dict) else []
                prompt=f'''You are a role-aware learning tutor. Role: {role}. Priority skills: {json.dumps(gaps)}. Use concise Markdown. Explain concepts with practical public-service examples where useful. Never invent official policy or claim a source was consulted when it was not. User context material is below. Conversation history is below.\nMATERIAL:\n{source or 'none'}\nHISTORY:\n{json.dumps([dict(h) for h in history])}\nUSER:\n{msg}'''
                model=None; src='fallback'
                try: reply,model=gemini(prompt,None,timeout=35); src='gemini'
                except Exception:
                    weak=gaps[0][0] if gaps else 'your priority competency'; reply=f'''## A practical way to think about this\n\nStart by connecting the concept to **{weak}** and the decision you need to make at work. Break it into:\n\n- what the concept means\n- what evidence you would look for\n- what action you would take\n\nFor the next step, try a quick check on the same topic so the tutor conversation turns into measurable evidence.'''
                ts=now_iso(); db.execute('INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,?,?,?)',(sid,'user',msg,ts)); db.execute('INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,?,?,?)',(sid,'assistant',reply,now_iso())); db.execute('UPDATE chat_sessions SET updated_at=? WHERE id=?',(now_iso(),sid)); db.execute('UPDATE users SET xp=xp+5 WHERE id=?',(uid,)); db.commit(); db.close(); return self.send_json({'session_id':sid,'reply':reply,'source':src,'model':model})

            if p == '/api/chat/session':
                b=json_body(self); uid=int(b['user_id']); title=(b.get('title') or 'New conversation')[:80]
                db=connect(); cur=db.execute('INSERT INTO chat_sessions(user_id,title,created_at,updated_at) VALUES(?,?,?,?)',(uid,title,now_iso(),now_iso())); db.commit(); sid=cur.lastrowid; db.close(); return self.send_json({'session_id':sid,'title':title})
            if p == '/api/simulation/evaluate':
                b=json_body(self); uid=int(b['user_id']); role=b.get('role','Statistical Officer'); response=(b.get('response') or '').strip()[:8000]
                scenario=SIMULATION_BANK.get(role,SIMULATION_BANK['Statistical Officer']);
                if len(response)<40: return self.send_json({'error':'Write a little more so the rubric can evaluate your reasoning.'},400)
                prompt=f'''Evaluate this workplace simulation for a {role}. Scenario: {scenario['prompt']} Rubric dimensions: {scenario['rubric']}. Candidate response: {response}. Score 0-100 based on reasoning, evidence use, decision quality, role relevance and communication. Give actionable feedback, 2-4 strengths and 2-4 weaknesses. Do not invent official policy.'''
                try:
                    raw,model=gemini(prompt,SIM_SCHEMA,timeout=35); result=clean_json(raw)
                    score=int(result['score']); feedback=result['feedback']; strengths=result['strengths']; weaknesses=result['weaknesses']; src='gemini'
                except Exception as e:
                    score=min(92,max(38,40+min(45,len(response)//120))); strengths=['Structured response' if len(response)>180 else 'Clear starting point']; weaknesses=['Add more explicit evidence checks','Name a concrete decision rule']; feedback='A strong prototype response should state plausible causes, distinguish evidence from assumptions, and show what you would verify before acting.'; model=None; src='fallback'
                db=connect(); db.execute('INSERT INTO simulation_attempts(user_id,title,prompt,response,score,feedback,strengths_json,weaknesses_json,completed_at) VALUES(?,?,?,?,?,?,?,?,?)',(uid,scenario['title'],scenario['prompt'],response,score,feedback,json.dumps(strengths),json.dumps(weaknesses),now_iso()));
                skill='Decision Making' if any('Decision Making'==r['skill_name'] for r in db.execute('SELECT skill_name FROM user_skills WHERE user_id=?',(uid,)).fetchall()) else next(iter(ROLE_PROFILES.get(role,ROLE_PROFILES['Statistical Officer'])['skills']))
                # Practical evidence should move a relevant competency gradually, not jump to a perfect score.
                rows=db.execute('SELECT * FROM user_skills WHERE user_id=? ORDER BY score ASC',(uid,)).fetchall(); target=rows[0] if rows else None
                evidence=[]
                if target:
                    new=min(100,target['score']+max(2,min(12,round((score-50)/10)))); delta=new-target['score']; conf='High' if score>=75 else 'Medium'; db.execute('UPDATE user_skills SET score=?,confidence=?,last_updated=? WHERE user_id=? AND id=?',(new,conf,now_iso(),uid,target['id'])); db.execute('INSERT INTO skill_events(user_id,skill,event_type,score_delta,evidence_reference,created_at) VALUES(?,?,?,?,?,?)',(uid,target['skill_name'],'SIMULATION_COMPLETED',delta,'simulation',now_iso())); evidence=[{'skill':target['skill_name'],'before':target['score'],'after':new,'delta':delta}]
                db.execute('UPDATE users SET xp=xp+? WHERE id=?',(max(20,score//2),uid)); db.commit(); state=user_state(db,uid); db.close(); return self.send_json({'score':score,'feedback':feedback,'strengths':strengths,'weaknesses':weaknesses,'evidence':evidence,'source':src,'model':model,'state':state})

            if p == '/api/learning/log':
                b=json_body(self); uid=int(b['user_id']); cid=b.get('course_id'); db=connect(); c=next((x for x in COURSES if x['id']==cid),None)
                if not c: db.close(); return self.send_json({'error':'Course not found'},404)
                row=db.execute('SELECT id,completion_percentage FROM learning_history WHERE user_id=? AND course_id=? ORDER BY id DESC LIMIT 1',(uid,cid)).fetchone()
                if row: db.execute('UPDATE learning_history SET completion_percentage=min(100,completion_percentage+25),status=CASE WHEN min(100,completion_percentage+25)=100 THEN "completed" ELSE "in_progress" END,completed_at=CASE WHEN min(100,completion_percentage+25)=100 THEN ? ELSE completed_at END WHERE id=?',(now_iso(),row['id']))
                else: db.execute('INSERT INTO learning_history(user_id,course_id,course_name,status,completion_percentage,started_at) VALUES(?,?,?,?,?,?)',(uid,cid,c['title'],'in_progress',25,now_iso()))
                db.execute('UPDATE users SET xp=xp+60 WHERE id=?',(uid,)); db.commit(); state=user_state(db,uid); db.close(); return self.send_json({'ok':True,'state':state})

            if p == '/api/igot/recommend':
                b=json_body(self); uid=int(b['user_id']); db=connect(); gaps,recs=build_recommendations(db,uid); out=[]
                for match,c,skill,reason,priority in recs:
                    out.append({**c,'match':match,'focus_skill':skill,'reason':reason,'priority':priority,'source':'prototype-catalog'})
                db.close(); return self.send_json({'gaps':[{'skill':s,'score':v} for s,v in gaps[:5]],'recommendations':out,'mode':'prototype-catalog'})

            return self.send_json({'error':'Not found'},404)
        except Exception as e:
            if db: db.close()
            self.send_json({'error': str(e)},500)


# ---------------------------------------------------------------------------
# Vercel compatibility
#
# The original prototype uses ThreadingHTTPServer for local development.
# Vercel expects a web application callable rather than a long-running socket
# server. The Flask bridge below keeps the existing Handler and exposes it as
# a WSGI application for Vercel.
#
# Local usage remains unchanged:
#     python server.py
#
# On Vercel, the exported `app` object is used.
# ---------------------------------------------------------------------------

class _VercelServer:
    server_version = 'KarmayogiAI/1.0'
    sys_version = ''
    timeout = None


def _run_handler_request(method, target, headers, body):
    """Run the existing BaseHTTPRequestHandler over a socket pair."""
    client_sock, server_sock = socket.socketpair()
    client_sock.settimeout(55)
    server_sock.settimeout(55)

    request_lines = [f'{method} {target} HTTP/1.1']
    for key, value in headers.items():
        if key.lower() not in {'connection', 'content-length'}:
            request_lines.append(f'{key}: {value}')
    request_lines.append('Connection: close')
    request_lines.append(f'Content-Length: {len(body)}')
    raw_request = ('\r\n'.join(request_lines) + '\r\n\r\n').encode('iso-8859-1') + body

    error_holder = []

    def serve():
        try:
            Handler(server_sock, ('127.0.0.1', 0), _VercelServer())
        except Exception as exc:
            error_holder.append(exc)
        finally:
            try:
                server_sock.close()
            except Exception:
                pass

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    try:
        client_sock.sendall(raw_request)
        client_sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = client_sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        try:
            client_sock.close()
        except Exception:
            pass

    worker.join(timeout=2)
    raw_response = b''.join(chunks)

    if error_holder and not raw_response:
        raise error_holder[0]

    header_end = raw_response.find(b'\r\n\r\n')
    if header_end < 0:
        raise RuntimeError('Backend did not return a valid HTTP response')

    header_block = raw_response[:header_end].decode('iso-8859-1')
    response_body = raw_response[header_end + 4:]
    response_lines = header_block.split('\r\n')
    status_parts = response_lines[0].split(' ', 2)
    status_code = int(status_parts[1]) if len(status_parts) > 1 else 500

    response_headers = []
    for line in response_lines[1:]:
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        if key.lower() not in {'content-length', 'connection', 'server', 'date'}:
            response_headers.append((key, value.strip()))

    return status_code, response_headers, response_body


if Flask is not None:
    app = Flask(__name__)

    @app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    @app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    def vercel_app(path):
        target = request.full_path
        if target.endswith('?'):
            target = target[:-1]
        body = request.get_data(cache=False) or b''
        headers = {key: value for key, value in request.headers.items()}

        try:
            status_code, response_headers, response_body = _run_handler_request(
                request.method, target, headers, body
            )
            return Response(response_body, status=status_code, headers=dict(response_headers))
        except Exception as exc:
            return Response(
                json.dumps({'error': str(exc)}),
                status=500,
                content_type='application/json',
            )

    try:
        init_db()
    except Exception as exc:
        print(f'[startup] database initialization warning: {exc}')
else:
    app = None


if __name__ == '__main__':
    init_db()
    print(f'Karmayogi AI running at http://127.0.0.1:{PORT}')
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
