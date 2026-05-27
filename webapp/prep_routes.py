from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps

prep = Blueprint('prep', __name__, url_prefix='/prep')
DB_PATH    = None

PREP_STATIONS = [
    {'id':1,'name_en':'Banh Mi Station',       'name_vi':'Khu bánh mì',             'color':'#FF9800'},
    {'id':2,'name_en':'Pho / Kitchen Station', 'name_vi':'Khu phở / bếp chính',     'color':'#F44336'},
    {'id':3,'name_en':'Drink Station',          'name_vi':'Khu nước uống',           'color':'#00BCD4'},
    {'id':4,'name_en':'Chef / General Prep',    'name_vi':'Sơ chế chung / phụ bếp', 'color':'#4CAF50'},
]
STATIONS_MAP = {s['id']:s for s in PREP_STATIONS}

DAYS       = ['mon','tue','wed','thu','fri','sat','sun']
DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
DAY_LONG   = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
ALL_DAYS   = 'mon,tue,wed,thu,fri,sat,sun'

# (station_id, en, vi, time, active_days, assignee, is_supplier, supplier_name)
PREP_TASKS_SEED = [
    (1,'Prepare ham / cha lua',          'Chuẩn bị ham / chả lụa',          '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Prepare gio thu',                'Chuẩn bị giò thủ',                '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Prepare pickles',                'Chuẩn bị đồ chua',               '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Prepare banh mi sauce',          'Chuẩn bị sốt bánh mì',           '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Prepare soy sauce',              'Chuẩn bị nước tương',            '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Check pate',                     'Kiểm tra pate',                  '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',1,'Morley'),
    (1,'Wash and prepare coriander',     'Rửa và chuẩn bị ngò',            '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Slice cucumber',                 'Cắt dưa leo',                    '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Prepare chilli',                 'Chuẩn bị ớt',                   '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Refill mayo',                    'Châm thêm sốt mayo',             '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (1,'Refill butter',                  'Châm thêm bơ',                   '',ALL_DAYS,            'NGUYEN, THI NGOC PHUC',0,''),
    (2,'Start the pho broth',            'Lên xương nấu nước phở',         '','mon,wed,fri',       'Thang Nguyen',          0,''),
    (2,'Finish the pho broth',           'Ra nước phở / hoàn thiện nước phở','','tue,thu,sat',    'Thang Nguyen',          0,''),
    (2,'Prepare brisket / beef shin',    'Chuẩn bị nạm / bắp bò',         '','sat',              'Thang Nguyen',          0,''),
    (2,'Prepare xa xiu meat for banh mi','Chuẩn bị xá xíu thịt bánh mì',  '','tue,thu,sat',      'Thang Nguyen',          0,''),
    (2,'Prepare pork hock / ribs',       'Chuẩn bị giò heo / sườn',       '','mon',              'Thang Nguyen',          0,''),
    (2,'Check bun bo sauce',             'Kiểm tra sốt bún bò',           '',ALL_DAYS,            'Thang Nguyen',          1,'Morley'),
    (2,'Check pickled com tam',          'Kiểm tra đồ chua cơm tấm',      '',ALL_DAYS,            'Thang Nguyen',          1,'Morley'),
    (2,'Prepare goi cuon',               'Chuẩn bị gỏi cuốn',             '','mon',              'Thang Nguyen',          0,''),
    (2,'Prepare stir-fry sauce',         'Chuẩn bị sốt xào',              '','tue,fri,sat',      'Thang Nguyen',          0,''),
    (2,'Prepare com tam meat',           'Chuẩn bị thịt cơm tấm',         '','mon,fri',          'Thang Nguyen',          0,''),
    (2,'Marinate chicken',               'Ướp gà',                        '','mon,tue,wed,sat,sun','Thang Nguyen',        0,''),
    (2,'Prepare crispy roast pork',      'Chuẩn bị heo quay',              '',ALL_DAYS,            'Thang Nguyen',          0,''),
    (2,'Slice pork / beef',              'Cắt thịt heo / thịt bò',        '',ALL_DAYS,            'Thang Nguyen',          0,''),
    (2,'Prepare tofu',                   'Chuẩn bị đậu hũ',               '','mon,thu',          'Thang Nguyen',          0,''),
    (2,'Slice brown onion',              'Cắt hành tây nâu',              '',ALL_DAYS,            'MA, THANH PHUNG',       0,''),
    (3,'Prepare black coffee base',      'Chuẩn bị cà phê đen base',      '',ALL_DAYS,            '',                      0,''),
    (3,'Cut fruit',                      'Cắt trái cây',                  '',ALL_DAYS,            '',                      0,''),
    (4,'Cut spring onion',               'Cắt hành lá',                   '',ALL_DAYS,            'MA, THANH PHUNG',       0,''),
    (4,'Soak the glutinous rice',        'Ngâm gạo nếp',                  '',ALL_DAYS,            'MA, THANH PHUNG',       0,''),
    (4,'Soak the rice noodles',          'Ngâm bún',                      '',ALL_DAYS,            'MA, THANH PHUNG',       0,''),
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
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

def get_week_start(d=None):
    if d is None: d = date.today()
    return d - timedelta(days=d.weekday())

def get_week_dates(week_start_str):
    ws = datetime.strptime(week_start_str, '%Y-%m-%d').date()
    return [(ws + timedelta(days=i)).isoformat() for i in range(7)]

def fmt_time(t):
    if not t: return ''
    try:
        h, m = map(int, t.split(':'))
        return f'{h%12 or 12}:{m:02d} {"AM" if h<12 else "PM"}'
    except:
        return t

# ── DB Init ────────────────────────────────────────────────────────────────────

def _get_staff():
    """Always pull active staff from DB — stays in sync with Staff Management page."""
    try:
        with _get_db() as conn:
            rows = conn.execute(
                'SELECT name FROM staff_members WHERE active=1 ORDER BY name').fetchall()
            return [r['name'] for r in rows]
    except Exception:
        return []

def init_prep_tables(db_path, staff_list):
    global DB_PATH
    DB_PATH = db_path
    with _get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS prep_weekly_schedules (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL UNIQUE,
                created_by TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                locked     INTEGER DEFAULT 0,
                locked_by  TEXT,
                locked_at  TEXT,
                notes      TEXT
            );
            CREATE TABLE IF NOT EXISTS prep_task_templates (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name_en     TEXT NOT NULL,
                task_name_vi     TEXT NOT NULL,
                station_id       INTEGER NOT NULL,
                default_time     TEXT,
                active_days      TEXT DEFAULT 'mon,tue,wed,thu,fri,sat,sun',
                default_assignee TEXT,
                is_supplier      INTEGER DEFAULT 0,
                supplier_name    TEXT,
                instruction_en   TEXT,
                instruction_vi   TEXT,
                active           INTEGER DEFAULT 1,
                sort_order       INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS prep_weekly_tasks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id    INTEGER NOT NULL REFERENCES prep_weekly_schedules(id) ON DELETE CASCADE,
                template_id    INTEGER REFERENCES prep_task_templates(id),
                task_name_en   TEXT NOT NULL,
                task_name_vi   TEXT NOT NULL,
                station_id     INTEGER NOT NULL,
                scheduled_time TEXT,
                assigned_to    TEXT,
                active_days    TEXT,
                is_supplier    INTEGER DEFAULT 0,
                supplier_name  TEXT,
                sort_order     INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS prep_daily_status (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                weekly_task_id INTEGER NOT NULL REFERENCES prep_weekly_tasks(id) ON DELETE CASCADE,
                date           TEXT NOT NULL,
                day_of_week    TEXT NOT NULL,
                is_required    INTEGER DEFAULT 1,
                scheduled_time TEXT,
                status         TEXT DEFAULT 'pending',
                done_by        TEXT,
                done_at        TEXT,
                note           TEXT,
                issue_flag     INTEGER DEFAULT 0,
                UNIQUE(weekly_task_id, date)
            );
            CREATE TABLE IF NOT EXISTS prep_supplier_status (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                weekly_task_id INTEGER NOT NULL REFERENCES prep_weekly_tasks(id) ON DELETE CASCADE,
                date           TEXT NOT NULL,
                ordered        INTEGER DEFAULT 0,
                ordered_by     TEXT,
                ordered_at     TEXT,
                received       INTEGER DEFAULT 0,
                received_by    TEXT,
                received_at    TEXT,
                checked_by     TEXT,
                note           TEXT,
                issue_flag     INTEGER DEFAULT 0,
                UNIQUE(weekly_task_id, date)
            );
        ''')
        # Migrate: add move columns if they don't exist yet
        for _col in ['moved_to_day','moved_to_date','moved_from_day','moved_from_date','moved_reason']:
            try:
                conn.execute(f'ALTER TABLE prep_daily_status ADD COLUMN {_col} TEXT')
            except Exception:
                pass
        try:
            conn.execute('ALTER TABLE prep_daily_status ADD COLUMN is_faved INTEGER DEFAULT 0')
        except Exception:
            pass

        if conn.execute('SELECT COUNT(*) as c FROM prep_task_templates').fetchone()['c'] == 0:
            for i, t in enumerate(PREP_TASKS_SEED):
                conn.execute('''INSERT INTO prep_task_templates
                    (station_id,task_name_en,task_name_vi,default_time,active_days,
                     default_assignee,is_supplier,supplier_name,sort_order)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (t[0],t[1],t[2],t[3],t[4],t[5],t[6],t[7],i))

def _ensure_schedule(week_start_str, conn):
    """Auto-create schedule from templates if it doesn't exist yet. Always called — no manual step needed."""
    sched = conn.execute('SELECT id FROM prep_weekly_schedules WHERE week_start=?',(week_start_str,)).fetchone()
    if not sched:
        _build_schedule(week_start_str, conn)

def _build_schedule(week_start_str, conn):
    ws = datetime.strptime(week_start_str, '%Y-%m-%d').date()
    conn.execute('INSERT OR IGNORE INTO prep_weekly_schedules (week_start,created_by) VALUES (?,?)',
                 (week_start_str, session.get('role','admin')))
    sched = conn.execute('SELECT id FROM prep_weekly_schedules WHERE week_start=?', (week_start_str,)).fetchone()
    sid = sched['id']
    if conn.execute('SELECT COUNT(*) as c FROM prep_weekly_tasks WHERE schedule_id=?', (sid,)).fetchone()['c']:
        return sid
    for i, t in enumerate(conn.execute(
            'SELECT * FROM prep_task_templates WHERE active=1 ORDER BY station_id,sort_order').fetchall()):
        cur = conn.execute('''INSERT INTO prep_weekly_tasks
            (schedule_id,template_id,task_name_en,task_name_vi,station_id,
             scheduled_time,assigned_to,active_days,is_supplier,supplier_name,sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (sid,t['id'],t['task_name_en'],t['task_name_vi'],t['station_id'],
             t['default_time'],t['default_assignee'],t['active_days'],
             t['is_supplier'],t['supplier_name'],i))
        wt_id = cur.lastrowid
        active = (t['active_days'] or '').split(',')
        for di, day in enumerate(DAYS):
            d = (ws + timedelta(days=di)).isoformat()
            req = 1 if day in active else 0
            conn.execute('''INSERT OR IGNORE INTO prep_daily_status
                (weekly_task_id,date,day_of_week,is_required,scheduled_time,status)
                VALUES (?,?,?,?,?,?)''',
                (wt_id,d,day,req,t['default_time'],'na' if not req else 'pending'))
            if t['is_supplier'] and req:
                conn.execute('INSERT OR IGNORE INTO prep_supplier_status (weekly_task_id,date) VALUES (?,?)',
                             (wt_id,d))
    return sid

# ── Routes ─────────────────────────────────────────────────────────────────────

@prep.route('/')
@_login_required
def prep_index():
    return redirect(url_for('prep.prep_weekly_view',
        week_start=get_week_start().isoformat()) if _is_admin()
        else url_for('prep.prep_today'))

@prep.route('/dashboard')
@_admin_required
def prep_dashboard():
    today_str = date.today().isoformat()
    with _get_db() as conn:
        total  = conn.execute("SELECT COUNT(*) as c FROM prep_daily_status WHERE date=? AND is_required=1",(today_str,)).fetchone()['c']
        done   = conn.execute("SELECT COUNT(*) as c FROM prep_daily_status WHERE date=? AND status='done'",(today_str,)).fetchone()['c']
        issues = conn.execute("SELECT COUNT(*) as c FROM prep_daily_status WHERE date=? AND issue_flag=1",(today_str,)).fetchone()['c']
        station_stats = {}
        for s in PREP_STATIONS:
            r = conn.execute('''SELECT COUNT(*) as tot,SUM(CASE WHEN ds.status='done' THEN 1 ELSE 0 END) as dn
                FROM prep_daily_status ds JOIN prep_weekly_tasks wt ON wt.id=ds.weekly_task_id
                WHERE ds.date=? AND ds.is_required=1 AND wt.station_id=?''',(today_str,s['id'])).fetchone()
            station_stats[s['id']] = {'total':r['tot'] or 0,'done':r['dn'] or 0,
                'pct': round((r['dn'] or 0)/max(r['tot'] or 1,1)*100)}
        staff_stats = [dict(r) for r in conn.execute('''
            SELECT wt.assigned_to as name,COUNT(*) as total,
                   SUM(CASE WHEN ds.status='done' THEN 1 ELSE 0 END) as done
            FROM prep_daily_status ds JOIN prep_weekly_tasks wt ON wt.id=ds.weekly_task_id
            WHERE ds.date=? AND ds.is_required=1 AND wt.assigned_to!=''
            GROUP BY wt.assigned_to ORDER BY total DESC''',(today_str,)).fetchall()]
        for s in staff_stats:
            s['pct'] = round(s['done']/max(s['total'],1)*100)
        recent_issues = [dict(r) for r in conn.execute('''
            SELECT ds.*,wt.task_name_en,wt.assigned_to,wt.station_id
            FROM prep_daily_status ds JOIN prep_weekly_tasks wt ON wt.id=ds.weekly_task_id
            WHERE ds.date=? AND (ds.issue_flag=1 OR (ds.note IS NOT NULL AND ds.note!=''))
            ORDER BY ds.id DESC LIMIT 10''',(today_str,)).fetchall()]
    for r in recent_issues:
        r['station'] = STATIONS_MAP.get(r['station_id'],{})
    return render_template('prep_dashboard.html',
        today=today_str, day_name=date.today().strftime('%A'),
        total=total, done=done, pending=total-done, issues=issues,
        pct=round(done/max(total,1)*100),
        station_stats=station_stats, staff_stats=staff_stats,
        recent_issues=recent_issues, stations=PREP_STATIONS)

@prep.route('/today')
@_login_required
def prep_today():
    return redirect(url_for('prep.prep_weekly_view', week_start=get_week_start().isoformat()))

@prep.route('/weekly/<week_start>')
@_login_required
def prep_weekly_view(week_start):
    try:
        ws_date = datetime.strptime(week_start,'%Y-%m-%d').date()
    except Exception:
        ws_date = get_week_start()
    ws_date    = get_week_start(ws_date)   # always normalize to Monday
    week_start = ws_date.isoformat()
    if request.view_args.get('week_start') != week_start:
        return redirect(url_for('prep.prep_weekly_view', week_start=week_start))
    week_dates     = get_week_dates(week_start)
    prev_week      = (ws_date - timedelta(days=7)).isoformat()
    next_week      = (ws_date + timedelta(days=7)).isoformat()
    station_filter = request.args.get('station','')
    today          = date.today().isoformat()
    with _get_db() as conn:
        _ensure_schedule(week_start, conn)
        sched = conn.execute('SELECT * FROM prep_weekly_schedules WHERE week_start=?',(week_start,)).fetchone()
        sched = dict(sched) if sched else None
        tasks = []
        if sched:
            q = 'SELECT * FROM prep_weekly_tasks WHERE schedule_id=?'
            params = [sched['id']]
            if station_filter: q += ' AND station_id=?'; params.append(int(station_filter))
            q += ' ORDER BY station_id, scheduled_time, id'
            for wt in conn.execute(q, params).fetchall():
                wt = dict(wt)
                wt['station']  = STATIONS_MAP.get(wt['station_id'],{})
                wt['fmt_time'] = fmt_time(wt.get('scheduled_time'))
                ds_rows = conn.execute(
                    'SELECT * FROM prep_daily_status WHERE weekly_task_id=? ORDER BY date',(wt['id'],)).fetchall()
                wt['days'] = {r['day_of_week']: dict(r) for r in ds_rows}
                tasks.append(wt)
    today_weekday   = date.today().weekday()
    today_day_code  = DAYS[today_weekday]
    today_day_label = DAY_LABELS[today_weekday]
    return render_template('prep_weekly.html',
        sched=sched, tasks=tasks,
        week_start=week_start, week_end=week_dates[-1],
        week_dates=week_dates, prev_week=prev_week, next_week=next_week,
        days=DAYS, day_labels=DAY_LABELS,
        stations=PREP_STATIONS, station_filter=station_filter,
        is_admin=_is_admin(), today=today, staff_list=_get_staff(),
        today_day_code=today_day_code, today_day_label=today_day_label)

@prep.route('/weekly/<week_start>/create', methods=['POST'])
@_admin_required
def prep_create_schedule(week_start):
    with _get_db() as conn:
        _build_schedule(week_start, conn)
    return redirect(url_for('prep.prep_weekly_view', week_start=week_start))

@prep.route('/weekly/<week_start>/report')
@_admin_required
def weekly_report(week_start):
    try:
        ws_date = datetime.strptime(week_start, '%Y-%m-%d').date()
    except Exception:
        ws_date = get_week_start()
    ws_date    = get_week_start(ws_date)
    week_start = ws_date.isoformat()
    week_dates = get_week_dates(week_start)
    today      = date.today().isoformat()
    with _get_db() as conn:
        _ensure_schedule(week_start, conn)
        sched = conn.execute('SELECT * FROM prep_weekly_schedules WHERE week_start=?', (week_start,)).fetchone()
        sched = dict(sched) if sched else None
        tasks = []
        if sched:
            for wt in conn.execute(
                    'SELECT * FROM prep_weekly_tasks WHERE schedule_id=? ORDER BY station_id,scheduled_time,id',
                    (sched['id'],)).fetchall():
                wt = dict(wt)
                wt['station']  = STATIONS_MAP.get(wt['station_id'], {})
                wt['fmt_time'] = fmt_time(wt.get('scheduled_time'))
                ds_rows = conn.execute(
                    'SELECT * FROM prep_daily_status WHERE weekly_task_id=? ORDER BY date', (wt['id'],)).fetchall()
                wt['days'] = {r['day_of_week']: dict(r) for r in ds_rows}
                tasks.append(wt)
    # summary stats
    total_req  = sum(1 for t in tasks for d in DAYS if t['days'].get(d, {}).get('is_required'))
    total_done = sum(1 for t in tasks for d in DAYS if t['days'].get(d, {}).get('status') == 'done')
    issues     = [{'task': t['task_name_en'], 'day': d.upper(), 'note': t['days'][d].get('note',''), 'date': t['days'][d].get('date','')}
                  for t in tasks for d in DAYS
                  if t['days'].get(d, {}).get('issue_flag')]
    return render_template('prep_report.html',
        sched=sched, tasks=tasks, week_start=week_start,
        week_end=week_dates[-1], week_dates=week_dates,
        days=DAYS, day_labels=DAY_LABELS,
        stations=PREP_STATIONS, today=today,
        total_req=total_req, total_done=total_done, issues=issues,
        pct=round(total_done / max(total_req, 1) * 100))

@prep.route('/weekly/<week_start>/lock', methods=['POST'])
@_admin_required
def lock_week(week_start):
    with _get_db() as conn:
        conn.execute('''UPDATE prep_weekly_schedules
            SET locked=1,locked_by=?,locked_at=datetime('now','localtime') WHERE week_start=?''',
            (session.get('role','admin'), week_start))
    return redirect(url_for('prep.prep_weekly_view', week_start=week_start))

@prep.route('/weekly/batch-save', methods=['POST'])
@_login_required
def weekly_batch_save():
    items = request.get_json() or []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _get_db() as conn:
        for item in items:
            sid = item.get('id')
            done = item.get('done', False)
            if not sid:
                continue
            row = conn.execute('SELECT status FROM prep_daily_status WHERE id=?', (sid,)).fetchone()
            if not row or row['status'] == 'moved':
                continue
            new_status = 'done' if done else 'pending'
            done_at = now if done else None
            conn.execute('UPDATE prep_daily_status SET status=?, done_at=? WHERE id=?',
                         (new_status, done_at, sid))
    return jsonify({'ok': True})

@prep.route('/daily/<int:status_id>/toggle', methods=['POST'])
@_login_required
def toggle_task(status_id):
    done_by = request.form.get('done_by','')
    with _get_db() as conn:
        row = conn.execute('SELECT status FROM prep_daily_status WHERE id=?',(status_id,)).fetchone()
        if not row: return jsonify({'error':'not found'}),404
        new_status = 'pending' if row['status']=='done' else 'done'
        done_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if new_status=='done' else None
        conn.execute('UPDATE prep_daily_status SET status=?,done_by=?,done_at=? WHERE id=?',
                     (new_status, done_by if new_status=='done' else None, done_at, status_id))
    if request.headers.get('X-Requested-With')=='XMLHttpRequest':
        return jsonify({'status':new_status})
    return redirect(request.referrer or url_for('prep.prep_today'))

@prep.route('/daily/<int:status_id>/move', methods=['POST'])
@_admin_required
def move_prep_task(status_id):
    to_day = request.form.get('to_day', '').strip()
    reason = request.form.get('reason', '').strip()
    if to_day not in DAYS:
        return jsonify({'error': 'invalid day'}), 400
    with _get_db() as conn:
        orig = conn.execute('SELECT * FROM prep_daily_status WHERE id=?', (status_id,)).fetchone()
        if not orig:
            return jsonify({'error': 'not found'}), 404
        orig_day_idx = DAYS.index(orig['day_of_week'])
        to_day_idx   = DAYS.index(to_day)
        if to_day_idx >= orig_day_idx:
            return jsonify({'error': 'target day must be earlier'}), 400
        week_mon = datetime.strptime(orig['date'], '%Y-%m-%d').date() - timedelta(days=orig_day_idx)
        to_date  = (week_mon + timedelta(days=to_day_idx)).isoformat()
        # Mark original day as moved
        conn.execute('''UPDATE prep_daily_status
            SET status='moved', moved_to_day=?, moved_to_date=?, moved_reason=?
            WHERE id=?''', (to_day, to_date, reason, status_id))
        # Update or create target day record
        target = conn.execute(
            'SELECT id FROM prep_daily_status WHERE weekly_task_id=? AND date=?',
            (orig['weekly_task_id'], to_date)).fetchone()
        if target:
            conn.execute('''UPDATE prep_daily_status
                SET is_required=1, status='pending', moved_from_day=?, moved_from_date=?
                WHERE id=?''', (orig['day_of_week'], orig['date'], target['id']))
        else:
            conn.execute('''INSERT INTO prep_daily_status
                (weekly_task_id, date, day_of_week, is_required, status, moved_from_day, moved_from_date)
                VALUES (?,?,?,1,'pending',?,?)''',
                (orig['weekly_task_id'], to_date, to_day, orig['day_of_week'], orig['date']))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(request.referrer or url_for('prep.prep_weekly_view',
        week_start=(datetime.strptime(orig['date'],'%Y-%m-%d').date()-timedelta(days=orig_day_idx)).isoformat()))

@prep.route('/daily/<int:status_id>/note', methods=['POST'])
@_login_required
def save_note(status_id):
    note = request.form.get('note','').strip()
    with _get_db() as conn:
        conn.execute('UPDATE prep_daily_status SET note=? WHERE id=?',(note, status_id))
    if request.headers.get('X-Requested-With')=='XMLHttpRequest':
        return jsonify({'ok':True})
    return redirect(request.referrer or url_for('prep.prep_today'))

@prep.route('/daily/<int:status_id>/issue', methods=['POST'])
@_login_required
def toggle_issue(status_id):
    note = request.form.get('note','').strip()
    flag = int(request.form.get('flag', 1))   # 1=flag/update, 0=resolve
    with _get_db() as conn:
        if flag == 0:
            conn.execute('UPDATE prep_daily_status SET issue_flag=0, note=NULL WHERE id=?', (status_id,))
        else:
            conn.execute('UPDATE prep_daily_status SET issue_flag=1, note=? WHERE id=?',
                         (note or None, status_id))
    if request.headers.get('X-Requested-With')=='XMLHttpRequest':
        return jsonify({'issue_flag': flag})
    return redirect(request.referrer or url_for('prep.prep_today'))


@prep.route('/weekly-task/<int:task_id>/time', methods=['POST'])
@_admin_required
def update_task_time(task_id):
    new_time    = request.form.get('scheduled_time','').strip()
    update_type = request.form.get('update_type','week')
    with _get_db() as conn:
        conn.execute('UPDATE prep_weekly_tasks SET scheduled_time=? WHERE id=?',(new_time,task_id))
        conn.execute('UPDATE prep_daily_status SET scheduled_time=? WHERE weekly_task_id=?',(new_time,task_id))
        if update_type=='all':
            row = conn.execute('SELECT template_id FROM prep_weekly_tasks WHERE id=?',(task_id,)).fetchone()
            if row and row['template_id']:
                conn.execute('UPDATE prep_task_templates SET default_time=? WHERE id=?',(new_time,row['template_id']))
    return jsonify({'ok':True,'fmt_time':fmt_time(new_time)})

@prep.route('/weekly-task/<int:task_id>/assign', methods=['POST'])
@_admin_required
def assign_task(task_id):
    assigned_to = request.form.get('assigned_to','')
    with _get_db() as conn:
        conn.execute('UPDATE prep_weekly_tasks SET assigned_to=? WHERE id=?',(assigned_to,task_id))
    return jsonify({'ok':True})

@prep.route('/templates')
@_admin_required
def prep_templates_view():
    with _get_db() as conn:
        templates = [dict(r) for r in conn.execute(
            'SELECT * FROM prep_task_templates ORDER BY station_id,sort_order').fetchall()]
    for t in templates:
        t['station'] = STATIONS_MAP.get(t['station_id'],{})
    return render_template('prep_templates.html',
        templates=templates, stations=PREP_STATIONS,
        staff_list=_get_staff(), days=DAYS, day_labels=DAY_LABELS)

@prep.route('/templates/add', methods=['POST'])
@_admin_required
def add_template():
    active_days = ','.join(request.form.getlist('active_days') or DAYS)
    with _get_db() as conn:
        conn.execute('''INSERT INTO prep_task_templates
            (task_name_en,task_name_vi,station_id,default_time,active_days,
             default_assignee,is_supplier,supplier_name,sort_order)
            VALUES (?,?,?,?,?,?,?,?,999)''',
            (request.form.get('task_name_en',''), request.form.get('task_name_vi',''),
             int(request.form.get('station_id',1)), request.form.get('default_time',''),
             active_days, request.form.get('default_assignee',''),
             1 if request.form.get('is_supplier') else 0, request.form.get('supplier_name','')))
    return redirect(url_for('prep.prep_templates_view'))

@prep.route('/templates/<int:tid>/edit', methods=['POST'])
@_admin_required
def edit_template(tid):
    active_days = ','.join(request.form.getlist('active_days') or DAYS)
    with _get_db() as conn:
        conn.execute('''UPDATE prep_task_templates SET
            task_name_en=?,task_name_vi=?,station_id=?,default_time=?,active_days=?,
            default_assignee=?,is_supplier=?,supplier_name=? WHERE id=?''',
            (request.form.get('task_name_en',''), request.form.get('task_name_vi',''),
             int(request.form.get('station_id',1)), request.form.get('default_time',''),
             active_days, request.form.get('default_assignee',''),
             1 if request.form.get('is_supplier') else 0, request.form.get('supplier_name',''), tid))
    return redirect(url_for('prep.prep_templates_view'))

@prep.route('/templates/<int:tid>/toggle', methods=['POST'])
@_admin_required
def toggle_template(tid):
    with _get_db() as conn:
        row = conn.execute('SELECT active FROM prep_task_templates WHERE id=?',(tid,)).fetchone()
        if row:
            conn.execute('UPDATE prep_task_templates SET active=? WHERE id=?',(0 if row['active'] else 1,tid))
    return redirect(url_for('prep.prep_templates_view'))

@prep.route('/suppliers')
@_login_required
def prep_suppliers():
    week_start = request.args.get('week', get_week_start().isoformat())
    week_dates = get_week_dates(week_start)
    with _get_db() as conn:
        sched = conn.execute('SELECT id FROM prep_weekly_schedules WHERE week_start=?',(week_start,)).fetchone()
        items = []
        if sched:
            rows = conn.execute('''
                SELECT wt.id as wt_id, wt.task_name_en, wt.task_name_vi,
                       wt.station_id, wt.scheduled_time, wt.supplier_name,
                       ss.id as ss_id, ss.date, ss.ordered, ss.ordered_by,
                       ss.received, ss.received_by, ss.note, ss.issue_flag
                FROM prep_weekly_tasks wt
                JOIN prep_supplier_status ss ON ss.weekly_task_id=wt.id
                WHERE wt.schedule_id=? AND wt.is_supplier=1
                ORDER BY wt.scheduled_time, ss.date''', (sched['id'],)).fetchall()
            for r in rows:
                r = dict(r)
                r['station']  = STATIONS_MAP.get(r['station_id'],{})
                r['fmt_time'] = fmt_time(r.get('scheduled_time'))
                r['day_code'] = DAYS[week_dates.index(r['date'])] if r['date'] in week_dates else ''
                r['day_label']= DAY_LABELS[week_dates.index(r['date'])] if r['date'] in week_dates else ''
                items.append(r)
    return render_template('prep_suppliers.html',
        items=items, week_start=week_start, week_dates=week_dates,
        days=DAYS, day_labels=DAY_LABELS, staff_list=_get_staff(), is_admin=_is_admin())

@prep.route('/suppliers/<int:ss_id>/update', methods=['POST'])
@_login_required
def update_supplier(ss_id):
    field = request.form.get('field')
    value = 1 if request.form.get('value')=='1' else 0
    by    = request.form.get('by','')
    note  = request.form.get('note','')
    now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _get_db() as conn:
        if field=='ordered':
            conn.execute('UPDATE prep_supplier_status SET ordered=?,ordered_by=?,ordered_at=? WHERE id=?',
                         (value, by if value else None, now if value else None, ss_id))
        elif field=='received':
            conn.execute('UPDATE prep_supplier_status SET received=?,received_by=?,received_at=? WHERE id=?',
                         (value, by if value else None, now if value else None, ss_id))
        elif field=='issue':
            conn.execute('UPDATE prep_supplier_status SET issue_flag=? WHERE id=?',(value, ss_id))
        if note:
            conn.execute('UPDATE prep_supplier_status SET note=? WHERE id=?',(note,ss_id))
    return jsonify({'ok':True})
