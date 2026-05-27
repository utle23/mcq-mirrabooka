from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
import sqlite3, json
from datetime import datetime
from functools import wraps

training_bp = Blueprint('training', __name__, url_prefix='/training')
DB_PATH = None

ROLES = ['FOH', 'BOH', 'Kitchen', 'Counter', 'Banh Mi', 'Cashier', 'General']

SHIFTS = [('morning', 'Morning'), ('afternoon', 'Afternoon'), ('full', 'Full Day')]

RATINGS = [
    ('excellent',        'Excellent',         '#2E7D32', '#E8F5E9'),
    ('good',             'Good',              '#1565C0', '#E3F2FD'),
    ('developing',       'Developing',        '#E65100', '#FFF3E0'),
    ('needs_improvement','Needs Improvement', '#C62828', '#FFEBEE'),
]

# Topics: role → [(category, [item, ...])]
TOPIC_SEED = {
    'all': [
        ('Food Safety & Hygiene', [
            'Hand washing procedure',
            'Gloves & PPE usage',
            'Personal hygiene standards',
            'Food temperature awareness',
        ]),
        ('Uniform & Presentation', [
            'Full uniform worn correctly (cap, MCQ shirt)',
            'Clean & neat presentation during shift',
        ]),
        ('iPad & Records', [
            'Daily checklist completion on iPad',
            'Issue reporting on iPad',
        ]),
        ('Conduct', [
            'Phone policy during working hours',
            'Staying at assigned position',
            'Communicating issues to manager',
        ]),
    ],
    'FOH': [
        ('Customer Service', [
            'Greeting customers warmly',
            'Taking orders accurately',
            'Handling payments & POS system',
            'Handling complaints politely',
            'Upselling & suggesting items',
        ]),
        ('Operations', [
            'Opening procedures',
            'Closing procedures',
            'Temperature records (FOH)',
            'Pastry display setup & labelling',
        ]),
    ],
    'BOH': [
        ('Food Preparation', [
            'Food prep procedures',
            'Portion control',
            'Food labelling & dating',
            'FIFO stock rotation',
        ]),
        ('Equipment', [
            'Safe equipment use',
            'Cleaning equipment after use',
        ]),
        ('Operations', [
            'BOH checklist completion',
            'Temperature recording (BOH)',
            'Stock management & low stock reporting',
        ]),
    ],
    'Kitchen': [
        ('Food Preparation', [
            'Food prep & cooking procedures',
            'Portion control & presentation',
            'Food labelling & dating',
            'FIFO stock rotation',
        ]),
        ('Food Safety', [
            'Correct cooking temperatures',
            'Cross-contamination prevention',
        ]),
        ('Equipment', [
            'Safe equipment use',
            'Cleaning kitchen equipment',
        ]),
        ('Operations', [
            'Kitchen checklist completion',
            'Temperature records (Kitchen)',
        ]),
    ],
    'Counter': [
        ('Counter Service', [
            'Greeting & serving customers',
            'Banh Mi assembly & customisation',
            'Handling payments',
            'Upselling',
        ]),
        ('Operations', [
            'Counter setup & organisation',
            'Temperature records (Counter)',
            'Closing & cleaning counter',
        ]),
    ],
    'Banh Mi': [
        ('Banh Mi Skills', [
            'Bread selection & preparation',
            'Filling portions & assembly',
            'Customisation handling',
            'Speed & efficiency under pressure',
            'Presentation & wrapping',
        ]),
        ('Operations', [
            'Stock management for fillings',
            'Temperature records',
        ]),
    ],
    'Cashier': [
        ('Cashier Skills', [
            'POS system operation',
            'Cash handling & counting',
            'EFTPOS / card processing',
            'End-of-day cash reconciliation',
        ]),
        ('Customer Service', [
            'Greeting & serving customers',
            'Handling complaints',
        ]),
        ('Operations', [
            'Temperature records (Cashier)',
            'Daily checklist',
        ]),
    ],
    'General': [],
}


def _build_topics(role):
    """Return combined topic list for a role: all + role-specific."""
    result = []
    for cat, items in TOPIC_SEED.get('all', []):
        result.append({'category': cat, 'items': items})
    for cat, items in TOPIC_SEED.get(role, []):
        # Merge if category already exists
        existing = next((r for r in result if r['category'] == cat), None)
        if existing:
            existing['items'] += items
        else:
            result.append({'category': cat, 'items': items})
    return result


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _is_admin():
    return session.get('role') == 'admin'

def _login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('logged_in'):
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d

def _admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('logged_in'):
            return redirect(url_for('login_page'))
        if session.get('role') != 'admin':
            return render_template('access_denied.html'), 403
        return f(*a, **kw)
    return d


def init_training_tables(db_path):
    global DB_PATH
    DB_PATH = db_path
    with _get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS training_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trainee_name    TEXT NOT NULL,
            trainee_role    TEXT NOT NULL DEFAULT '',
            trainer_name    TEXT NOT NULL DEFAULT '',
            session_date    TEXT NOT NULL,
            shift           TEXT DEFAULT 'full',
            overall_rating  TEXT DEFAULT '',
            key_achievements    TEXT DEFAULT '',
            needs_improvement   TEXT DEFAULT '',
            next_session_focus  TEXT DEFAULT '',
            general_notes       TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now','localtime')),
            created_by      TEXT DEFAULT '',
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            updated_by      TEXT DEFAULT ''
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS training_session_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
            category    TEXT DEFAULT '',
            item_name   TEXT NOT NULL,
            status      TEXT DEFAULT 'not_covered',
            notes       TEXT DEFAULT '',
            sort_order  INTEGER DEFAULT 0
        )''')


def _get_staff_list():
    with _get_db() as conn:
        return [r['name'] for r in conn.execute(
            'SELECT name FROM staff_members WHERE active=1 ORDER BY name').fetchall()]


def _save_items(conn, session_id, form):
    conn.execute('DELETE FROM training_session_items WHERE session_id=?', (session_id,))
    categories = form.getlist('topic_category[]')
    names      = form.getlist('topic_name[]')
    statuses   = form.getlist('topic_status[]')
    notes_list = form.getlist('topic_notes[]')
    for i, (cat, name, status, note) in enumerate(zip(categories, names, statuses, notes_list)):
        if name.strip():
            conn.execute('''INSERT INTO training_session_items
                (session_id, category, item_name, status, notes, sort_order)
                VALUES (?,?,?,?,?,?)''',
                (session_id, cat, name.strip(), status or 'not_covered', note, i))


# ── Routes ─────────────────────────────────────────────────────────────────────

@training_bp.route('/')
@_login_required
def training_list():
    filter_trainee = request.args.get('trainee', '')
    filter_role    = request.args.get('role', '')

    with _get_db() as conn:
        q = 'SELECT * FROM training_sessions WHERE 1=1'
        params = []
        if filter_trainee:
            q += ' AND trainee_name=?'; params.append(filter_trainee)
        if filter_role:
            q += ' AND trainee_role=?'; params.append(filter_role)
        q += ' ORDER BY session_date DESC, id DESC'
        sessions = [dict(r) for r in conn.execute(q, params).fetchall()]

        for s in sessions:
            rows = conn.execute(
                'SELECT status, COUNT(*) c FROM training_session_items WHERE session_id=? GROUP BY status',
                (s['id'],)).fetchall()
            s['cnt_achieved']      = sum(r['c'] for r in rows if r['status'] == 'achieved')
            s['cnt_needs_practice'] = sum(r['c'] for r in rows if r['status'] == 'needs_practice')
            s['cnt_total']          = sum(r['c'] for r in rows)

        trainees = [r['trainee_name'] for r in conn.execute(
            'SELECT DISTINCT trainee_name FROM training_sessions ORDER BY trainee_name').fetchall()]

    return render_template('training_list.html',
        sessions=sessions, trainees=trainees, roles=ROLES,
        filter_trainee=filter_trainee, filter_role=filter_role,
        ratings={r[0]: (r[1], r[2], r[3]) for r in RATINGS},
        is_admin=_is_admin())


@training_bp.route('/new', methods=['GET', 'POST'])
@_login_required
def training_new():
    if request.method == 'POST':
        with _get_db() as conn:
            cur = conn.execute('''INSERT INTO training_sessions
                (trainee_name, trainee_role, trainer_name, session_date, shift,
                 overall_rating, key_achievements, needs_improvement,
                 next_session_focus, general_notes, created_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (request.form.get('trainee_name','').strip(),
                 request.form.get('trainee_role','').strip(),
                 request.form.get('trainer_name','').strip(),
                 request.form.get('session_date', datetime.now().strftime('%Y-%m-%d')),
                 request.form.get('shift','full'),
                 request.form.get('overall_rating',''),
                 request.form.get('key_achievements','').strip(),
                 request.form.get('needs_improvement','').strip(),
                 request.form.get('next_session_focus','').strip(),
                 request.form.get('general_notes','').strip(),
                 session.get('role','')))
            sid = cur.lastrowid
            _save_items(conn, sid, request.form)
        return redirect(url_for('training.training_detail', session_id=sid))

    topics_json = {role: _build_topics(role) for role in ROLES}
    return render_template('training_form.html',
        staff_list=_get_staff_list(), roles=ROLES, shifts=SHIFTS, ratings=RATINGS,
        topics_json=json.dumps(topics_json), edit_mode=False, s=None, items=[],
        today=datetime.now().strftime('%Y-%m-%d'), is_admin=_is_admin())


@training_bp.route('/<int:session_id>')
@_login_required
def training_detail(session_id):
    with _get_db() as conn:
        s = conn.execute('SELECT * FROM training_sessions WHERE id=?', (session_id,)).fetchone()
        if not s:
            return redirect(url_for('training.training_list'))
        s = dict(s)
        items = [dict(r) for r in conn.execute(
            'SELECT * FROM training_session_items WHERE session_id=? ORDER BY sort_order',
            (session_id,)).fetchall()]

    by_cat = {}
    for item in items:
        by_cat.setdefault(item['category'] or 'General', []).append(item)

    rating_info = {r[0]: (r[1], r[2], r[3]) for r in RATINGS}
    cnt = {'achieved': 0, 'needs_practice': 0, 'not_covered': 0}
    for item in items:
        cnt[item['status']] = cnt.get(item['status'], 0) + 1

    return render_template('training_detail.html',
        s=s, items=items, by_cat=by_cat,
        rating_info=rating_info, cnt=cnt, total=len(items),
        is_admin=_is_admin())


@training_bp.route('/<int:session_id>/edit', methods=['GET', 'POST'])
@_login_required
def training_edit(session_id):
    with _get_db() as conn:
        s = conn.execute('SELECT * FROM training_sessions WHERE id=?', (session_id,)).fetchone()
        if not s:
            return redirect(url_for('training.training_list'))

        if request.method == 'POST':
            conn.execute('''UPDATE training_sessions SET
                trainee_name=?, trainee_role=?, trainer_name=?, session_date=?, shift=?,
                overall_rating=?, key_achievements=?, needs_improvement=?,
                next_session_focus=?, general_notes=?,
                updated_at=datetime('now','localtime'), updated_by=?
                WHERE id=?''',
                (request.form.get('trainee_name','').strip(),
                 request.form.get('trainee_role','').strip(),
                 request.form.get('trainer_name','').strip(),
                 request.form.get('session_date',''),
                 request.form.get('shift','full'),
                 request.form.get('overall_rating',''),
                 request.form.get('key_achievements','').strip(),
                 request.form.get('needs_improvement','').strip(),
                 request.form.get('next_session_focus','').strip(),
                 request.form.get('general_notes','').strip(),
                 session.get('role',''), session_id))
            _save_items(conn, session_id, request.form)
            return redirect(url_for('training.training_detail', session_id=session_id))

        s = dict(s)
        items = [dict(r) for r in conn.execute(
            'SELECT * FROM training_session_items WHERE session_id=? ORDER BY sort_order',
            (session_id,)).fetchall()]
        staff_list = _get_staff_list()

    topics_json = {role: _build_topics(role) for role in ROLES}
    return render_template('training_form.html',
        staff_list=staff_list, roles=ROLES, shifts=SHIFTS, ratings=RATINGS,
        topics_json=json.dumps(topics_json), edit_mode=True, s=s, items=items,
        today=datetime.now().strftime('%Y-%m-%d'), is_admin=_is_admin())


@training_bp.route('/<int:session_id>/delete', methods=['POST'])
@_admin_required
def training_delete(session_id):
    with _get_db() as conn:
        conn.execute('DELETE FROM training_session_items WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM training_sessions WHERE id=?', (session_id,))
    return redirect(url_for('training.training_list'))
