from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file
import sqlite3, re
from io import BytesIO
from datetime import datetime, date, timedelta
from functools import wraps

try:
    import email_service
except Exception:
    email_service = None

prep = Blueprint('prep', __name__, url_prefix='/prep')
DB_PATH    = None

PREP_STATIONS_SEED = [
    {'id':1,'name_en':'Banh Mi Station',       'name_vi':'Khu bánh mì',             'color':'#FF9800'},
    {'id':2,'name_en':'Pho / Kitchen Station', 'name_vi':'Khu phở / bếp chính',     'color':'#F44336'},
    {'id':3,'name_en':'Drink Station',          'name_vi':'Khu nước uống',           'color':'#00BCD4'},
    {'id':4,'name_en':'Chef / General Prep',    'name_vi':'Sơ chế chung / phụ bếp', 'color':'#4CAF50'},
]
# PREP_STATIONS and STATIONS_MAP are kept as module-level mutables so the rest
# of the code can keep using them like before. _refresh_stations() reloads
# them from the DB whenever stations are added / edited / deleted.
PREP_STATIONS = list(PREP_STATIONS_SEED)
STATIONS_MAP  = {s['id']: s for s in PREP_STATIONS}


def _refresh_stations():
    """Reload PREP_STATIONS / STATIONS_MAP from DB. Mutates in place so all
    references in this module and templates keep working."""
    try:
        with _get_db() as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT id, name_en, name_vi, color FROM prep_stations '
                'WHERE active=1 ORDER BY sort_order, id').fetchall()]
    except Exception:
        return
    if not rows:
        return
    PREP_STATIONS.clear()
    PREP_STATIONS.extend(rows)
    STATIONS_MAP.clear()
    STATIONS_MAP.update({s['id']: s for s in rows})

DAYS       = ['mon','tue','wed','thu','fri','sat','sun']
DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
DAY_LONG   = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
ALL_DAYS   = 'mon,tue,wed,thu,fri,sat,sun'
SEEDED_PREP_ASSIGNEES = ('NGUYEN, THI NGOC PHUC', 'Thang Nguyen', 'MA, THANH PHUNG')

# (station_id, en, vi, time, active_days, assignee, is_supplier, supplier_name)
PREP_TASKS_SEED = [
    (1,'Prepare ham / cha lua',          'Chuẩn bị ham / chả lụa',          '',ALL_DAYS,            '',0,''),
    (1,'Prepare gio thu',                'Chuẩn bị giò thủ',                '',ALL_DAYS,            '',0,''),
    (1,'Prepare pickles',                'Chuẩn bị đồ chua',               '',ALL_DAYS,            '',0,''),
    (1,'Prepare banh mi sauce',          'Chuẩn bị sốt bánh mì',           '',ALL_DAYS,            '',0,''),
    (1,'Prepare soy sauce',              'Chuẩn bị nước tương',            '',ALL_DAYS,            '',0,''),
    (1,'Check pate',                     'Kiểm tra pate',                  '',ALL_DAYS,            '',1,'Morley'),
    (1,'Wash and prepare coriander',     'Rửa và chuẩn bị ngò',            '',ALL_DAYS,            '',0,''),
    (1,'Slice cucumber',                 'Cắt dưa leo',                    '',ALL_DAYS,            '',0,''),
    (1,'Prepare chilli',                 'Chuẩn bị ớt',                   '',ALL_DAYS,            '',0,''),
    (1,'Refill mayo',                    'Châm thêm sốt mayo',             '',ALL_DAYS,            '',0,''),
    (1,'Refill butter',                  'Châm thêm bơ',                   '',ALL_DAYS,            '',0,''),
    (2,'Start the pho broth',            'Lên xương nấu nước phở',         '','mon,wed,fri',       '',0,''),
    (2,'Finish the pho broth',           'Ra nước phở / hoàn thiện nước phở','','tue,thu,sat',    '',0,''),
    (2,'Prepare brisket / beef shin',    'Chuẩn bị nạm / bắp bò',         '','sat',              '',0,''),
    (2,'Prepare xa xiu meat for banh mi','Chuẩn bị xá xíu thịt bánh mì',  '','tue,thu,sat',      '',0,''),
    (2,'Prepare pork hock / ribs',       'Chuẩn bị giò heo / sườn',       '','mon',              '',0,''),
    (2,'Check bun bo sauce',             'Kiểm tra sốt bún bò',           '',ALL_DAYS,            '',1,'Morley'),
    (2,'Check pickled com tam',          'Kiểm tra đồ chua cơm tấm',      '',ALL_DAYS,            '',1,'Morley'),
    (2,'Prepare goi cuon',               'Chuẩn bị gỏi cuốn',             '','mon',              '',0,''),
    (2,'Prepare stir-fry sauce',         'Chuẩn bị sốt xào',              '','tue,fri,sat',      '',0,''),
    (2,'Prepare com tam meat',           'Chuẩn bị thịt cơm tấm',         '','mon,fri',          '',0,''),
    (2,'Marinate chicken',               'Ướp gà',                        '','mon,tue,wed,sat,sun','',0,''),
    (2,'Prepare crispy roast pork',      'Chuẩn bị heo quay',              '',ALL_DAYS,            '',0,''),
    (2,'Slice pork / beef',              'Cắt thịt heo / thịt bò',        '',ALL_DAYS,            '',0,''),
    (2,'Prepare tofu',                   'Chuẩn bị đậu hũ',               '','mon,thu',          '',0,''),
    (2,'Slice brown onion',              'Cắt hành tây nâu',              '',ALL_DAYS,            '',0,''),
    (3,'Prepare black coffee base',      'Chuẩn bị cà phê đen base',      '',ALL_DAYS,            '',                      0,''),
    (3,'Cut fruit',                      'Cắt trái cây',                  '',ALL_DAYS,            '',                      0,''),
    (4,'Cut spring onion',               'Cắt hành lá',                   '',ALL_DAYS,            '',0,''),
    (4,'Soak the glutinous rice',        'Ngâm gạo nếp',                  '',ALL_DAYS,            '',0,''),
    (4,'Soak the rice noodles',          'Ngâm bún',                      '',ALL_DAYS,            '',0,''),
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

def _form_active_days():
    return ','.join([d for d in request.form.getlist('active_days') if d in DAYS])

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
            CREATE TABLE IF NOT EXISTS prep_migrations (
                key        TEXT PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS prep_stations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name_en    TEXT NOT NULL,
                name_vi    TEXT NOT NULL DEFAULT '',
                color      TEXT NOT NULL DEFAULT '#607D8B',
                sort_order INTEGER NOT NULL DEFAULT 0,
                active     INTEGER NOT NULL DEFAULT 1
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

        # Seed prep_stations from the legacy hardcoded list on first init so
        # existing data keeps working with the same station ids.
        if conn.execute('SELECT COUNT(*) as c FROM prep_stations').fetchone()['c'] == 0:
            for i, s in enumerate(PREP_STATIONS_SEED):
                conn.execute('''INSERT INTO prep_stations
                    (id, name_en, name_vi, color, sort_order, active)
                    VALUES (?, ?, ?, ?, ?, 1)''',
                    (s['id'], s['name_en'], s['name_vi'], s['color'], i))
        if not conn.execute(
            'SELECT 1 FROM prep_migrations WHERE key=?',
            ('clear_seeded_prep_assignees_v1',)).fetchone():
            placeholders = ','.join('?' for _ in SEEDED_PREP_ASSIGNEES)
            conn.execute(
                f"UPDATE prep_task_templates SET default_assignee='' "
                f"WHERE default_assignee IN ({placeholders})",
                SEEDED_PREP_ASSIGNEES)
            conn.execute(
                f"UPDATE prep_weekly_tasks SET assigned_to='' "
                f"WHERE assigned_to IN ({placeholders})",
                SEEDED_PREP_ASSIGNEES)
            conn.execute(
                'INSERT INTO prep_migrations (key) VALUES (?)',
                ('clear_seeded_prep_assignees_v1',))
    _refresh_stations()

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

def _sync_week_from_templates(week_start_str, conn):
    """Apply current task templates to one weekly schedule without deleting completed work."""
    ws = get_week_start(datetime.strptime(week_start_str, '%Y-%m-%d').date())
    week_start_str = ws.isoformat()
    week_dates = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
    _ensure_schedule(week_start_str, conn)
    sched = conn.execute(
        'SELECT * FROM prep_weekly_schedules WHERE week_start=?',
        (week_start_str,)).fetchone()
    if not sched:
        return {'updated': 0, 'added': 0, 'inactive': 0}
    if sched['locked']:
        return {'updated': 0, 'added': 0, 'inactive': 0, 'locked': 1}

    schedule_id = sched['id']
    templates = [dict(r) for r in conn.execute('''
        SELECT * FROM prep_task_templates
        WHERE active=1
        ORDER BY station_id, sort_order, id
    ''').fetchall()]
    existing = {
        r['template_id']: dict(r)
        for r in conn.execute(
            'SELECT * FROM prep_weekly_tasks WHERE schedule_id=? AND template_id IS NOT NULL',
            (schedule_id,)).fetchall()
    }

    updated = added = 0
    active_template_ids = set()
    for order, t in enumerate(templates):
        active_template_ids.add(t['id'])
        active_days = [d for d in (t['active_days'] or '').split(',') if d in DAYS]
        current = existing.get(t['id'])
        if current:
            wt_id = current['id']
            conn.execute('''UPDATE prep_weekly_tasks
                SET task_name_en=?, task_name_vi=?, station_id=?, scheduled_time=?,
                    assigned_to=?, active_days=?, is_supplier=?, supplier_name=?, sort_order=?
                WHERE id=?''',
                (t['task_name_en'], t['task_name_vi'], t['station_id'], t['default_time'],
                 t['default_assignee'], ','.join(active_days), t['is_supplier'],
                 t['supplier_name'], order, wt_id))
            updated += 1
        else:
            cur = conn.execute('''INSERT INTO prep_weekly_tasks
                (schedule_id,template_id,task_name_en,task_name_vi,station_id,
                 scheduled_time,assigned_to,active_days,is_supplier,supplier_name,sort_order)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (schedule_id, t['id'], t['task_name_en'], t['task_name_vi'], t['station_id'],
                 t['default_time'], t['default_assignee'], ','.join(active_days),
                 t['is_supplier'], t['supplier_name'], order))
            wt_id = cur.lastrowid
            added += 1

        for idx, day in enumerate(DAYS):
            d = week_dates[idx]
            required = 1 if day in active_days else 0
            row = conn.execute('''
                SELECT id, status FROM prep_daily_status
                WHERE weekly_task_id=? AND date=?
            ''', (wt_id, d)).fetchone()
            if row:
                if required:
                    status = 'pending' if row['status'] == 'na' else row['status']
                    conn.execute('''UPDATE prep_daily_status
                        SET day_of_week=?, is_required=1, scheduled_time=?, status=?
                        WHERE id=?''',
                        (day, t['default_time'], status, row['id']))
                else:
                    conn.execute('''UPDATE prep_daily_status
                        SET day_of_week=?, is_required=0, scheduled_time=?, status='na',
                            moved_to_day=NULL, moved_to_date=NULL, moved_from_day=NULL,
                            moved_from_date=NULL, moved_reason=NULL
                        WHERE id=?''',
                        (day, t['default_time'], row['id']))
            else:
                conn.execute('''INSERT INTO prep_daily_status
                    (weekly_task_id,date,day_of_week,is_required,scheduled_time,status)
                    VALUES (?,?,?,?,?,?)''',
                    (wt_id, d, day, required, t['default_time'],
                     'pending' if required else 'na'))

            if t['is_supplier'] and required:
                conn.execute('INSERT OR IGNORE INTO prep_supplier_status (weekly_task_id,date) VALUES (?,?)',
                             (wt_id, d))

        if not t['is_supplier']:
            conn.execute('DELETE FROM prep_supplier_status WHERE weekly_task_id=?', (wt_id,))
        else:
            supplier_dates = [d for idx, d in enumerate(week_dates) if DAYS[idx] in active_days]
            if supplier_dates:
                conn.execute(f'''DELETE FROM prep_supplier_status
                    WHERE weekly_task_id=? AND date NOT IN ({','.join('?' for _ in supplier_dates)})''',
                    [wt_id] + supplier_dates)
            else:
                conn.execute('DELETE FROM prep_supplier_status WHERE weekly_task_id=?', (wt_id,))

    inactive = 0
    for wt in conn.execute(
        'SELECT * FROM prep_weekly_tasks WHERE schedule_id=? AND template_id IS NOT NULL',
        (schedule_id,)).fetchall():
        if wt['template_id'] in active_template_ids:
            continue
        activity = conn.execute('''
            SELECT
              (SELECT COUNT(*) FROM prep_daily_status
               WHERE weekly_task_id=? AND (status='done' OR issue_flag=1 OR COALESCE(note,'')!='')) +
              (SELECT COUNT(*) FROM prep_supplier_status
               WHERE weekly_task_id=? AND (ordered=1 OR received=1 OR issue_flag=1 OR COALESCE(note,'')!='')) as c
        ''', (wt['id'], wt['id'])).fetchone()['c']
        if activity:
            conn.execute('''UPDATE prep_weekly_tasks
                SET active_days='', sort_order=9999
                WHERE id=?''', (wt['id'],))
            conn.execute('''UPDATE prep_daily_status
                SET is_required=0, status=CASE WHEN status='done' THEN status ELSE 'na' END
                WHERE weekly_task_id=?''', (wt['id'],))
        else:
            conn.execute('DELETE FROM prep_weekly_tasks WHERE id=?', (wt['id'],))
        inactive += 1

    return {'updated': updated, 'added': added, 'inactive': inactive}

def _sync_all_unlocked_weeks_from_templates(conn):
    totals = {'weeks': 0, 'updated': 0, 'added': 0, 'inactive': 0}
    schedules = conn.execute('''
        SELECT week_start FROM prep_weekly_schedules
        WHERE locked=0
        ORDER BY week_start
    ''').fetchall()
    for sched in schedules:
        stats = _sync_week_from_templates(sched['week_start'], conn)
        if stats.get('locked'):
            continue
        totals['weeks'] += 1
        totals['updated'] += stats.get('updated', 0)
        totals['added'] += stats.get('added', 0)
        totals['inactive'] += stats.get('inactive', 0)
    return totals

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
    if station_filter and not station_filter.isdigit():
        station_filter = ''
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
        week_start_nav=get_week_start().isoformat(),
        days=DAYS, day_labels=DAY_LABELS,
        stations=PREP_STATIONS, station_filter=station_filter,
        is_admin=_is_admin(), today=today, staff_list=_get_staff(),
        today_day_code=today_day_code, today_day_label=today_day_label)

@prep.route('/weekly/<week_start>/create', methods=['POST'])
@_admin_required
def prep_create_schedule(week_start):
    with _get_db() as conn:
        _build_schedule(week_start, conn)
        task_count = conn.execute(
            'SELECT COUNT(*) c FROM prep_weekly_tasks WHERE schedule_id IN '
            '(SELECT id FROM prep_weekly_schedules WHERE week_start=?)',
            (week_start,)).fetchone()['c']
    if email_service:
        email_service.send_notification(
            'prep',
            subject=f'Weekly prep schedule created for {week_start}',
            lines=[
                f'Week start: {week_start}',
                f'Tasks scheduled: {task_count}',
                f'Created by: {session.get("role","admin")}',
            ],
            link_path=f'/prep/weekly/{week_start}',
            actor=session.get('role','admin'),
        )
    return redirect(url_for('prep.prep_weekly_view', week_start=week_start))

@prep.route('/weekly/<week_start>/apply-templates', methods=['POST'])
@_admin_required
def apply_templates_to_week(week_start):
    try:
        ws_date = datetime.strptime(week_start, '%Y-%m-%d').date()
    except Exception:
        ws_date = get_week_start()
    week_start = get_week_start(ws_date).isoformat()
    with _get_db() as conn:
        _ensure_schedule(week_start, conn)
        sched = conn.execute('SELECT locked FROM prep_weekly_schedules WHERE week_start=?', (week_start,)).fetchone()
        if sched and sched['locked']:
            return redirect(url_for('prep.prep_weekly_view', week_start=week_start, template_locked=1))
        stats = _sync_all_unlocked_weeks_from_templates(conn)
    return redirect(url_for('prep.prep_weekly_view',
        week_start=week_start,
        templates_applied=1,
        weeks=stats.get('weeks', 0),
        updated=stats.get('updated', 0),
        added=stats.get('added', 0),
        inactive=stats.get('inactive', 0)))

@prep.route('/weekly/<week_start>/template-add', methods=['POST'])
@_admin_required
def weekly_add_template(week_start):
    try:
        ws_date = datetime.strptime(week_start, '%Y-%m-%d').date()
    except Exception:
        ws_date = get_week_start()
    week_start = get_week_start(ws_date).isoformat()
    active_days = _form_active_days()
    station_id = int(request.form.get('station_id', 1) or 1)
    is_supplier = 1 if request.form.get('is_supplier') else 0
    with _get_db() as conn:
        next_order = conn.execute('''
            SELECT COALESCE(MAX(sort_order), -1) + 1
            FROM prep_task_templates
            WHERE station_id=?
        ''', (station_id,)).fetchone()[0]
        conn.execute('''INSERT INTO prep_task_templates
            (task_name_en,task_name_vi,station_id,default_time,active_days,
             default_assignee,is_supplier,supplier_name,sort_order,active)
            VALUES (?,?,?,?,?,?,?,?,?,1)''',
            (request.form.get('task_name_en','').strip(),
             request.form.get('task_name_vi','').strip(),
             station_id,
             request.form.get('default_time','').strip(),
             active_days,
             request.form.get('default_assignee','').strip(),
             is_supplier,
             request.form.get('supplier_name','').strip() if is_supplier else '',
             next_order))
        _ensure_schedule(week_start, conn)
        stats = _sync_all_unlocked_weeks_from_templates(conn)
    return redirect(url_for('prep.prep_weekly_view',
        week_start=week_start, template_added=1,
        weeks=stats.get('weeks', 0),
        updated=stats.get('updated', 0),
        added=stats.get('added', 0),
        inactive=stats.get('inactive', 0)))

@prep.route('/weekly-task/<int:task_id>/template-edit', methods=['POST'])
@_admin_required
def weekly_edit_task_template(task_id):
    active_days = _form_active_days()
    station_id = int(request.form.get('station_id', 1) or 1)
    is_supplier = 1 if request.form.get('is_supplier') else 0
    with _get_db() as conn:
        task = conn.execute('''SELECT wt.*, ws.week_start
            FROM prep_weekly_tasks wt
            JOIN prep_weekly_schedules ws ON ws.id=wt.schedule_id
            WHERE wt.id=?''', (task_id,)).fetchone()
        if not task:
            return redirect(url_for('prep.prep_weekly_view', week_start=get_week_start().isoformat()))
        template_id = task['template_id']
        if not template_id:
            next_order = conn.execute('''
                SELECT COALESCE(MAX(sort_order), -1) + 1
                FROM prep_task_templates WHERE station_id=?
            ''', (station_id,)).fetchone()[0]
            cur = conn.execute('''INSERT INTO prep_task_templates
                (task_name_en,task_name_vi,station_id,default_time,active_days,
                 default_assignee,is_supplier,supplier_name,sort_order,active)
                VALUES (?,?,?,?,?,?,?,?,?,1)''',
                (request.form.get('task_name_en','').strip(),
                 request.form.get('task_name_vi','').strip(),
                 station_id,
                 request.form.get('default_time','').strip(),
                 active_days,
                 request.form.get('default_assignee','').strip(),
                 is_supplier,
                 request.form.get('supplier_name','').strip() if is_supplier else '',
                 next_order))
            template_id = cur.lastrowid
            conn.execute('UPDATE prep_weekly_tasks SET template_id=? WHERE id=?', (template_id, task_id))
        else:
            conn.execute('''UPDATE prep_task_templates
                SET task_name_en=?, task_name_vi=?, station_id=?, default_time=?,
                    active_days=?, default_assignee=?, is_supplier=?, supplier_name=?, active=1
                WHERE id=?''',
                (request.form.get('task_name_en','').strip(),
                 request.form.get('task_name_vi','').strip(),
                 station_id,
                 request.form.get('default_time','').strip(),
                 active_days,
                 request.form.get('default_assignee','').strip(),
                 is_supplier,
                 request.form.get('supplier_name','').strip() if is_supplier else '',
                 template_id))
        stats = _sync_all_unlocked_weeks_from_templates(conn)
        week_start = task['week_start']
    return redirect(url_for('prep.prep_weekly_view',
        week_start=week_start, template_saved=1,
        weeks=stats.get('weeks', 0),
        updated=stats.get('updated', 0),
        added=stats.get('added', 0),
        inactive=stats.get('inactive', 0)))

@prep.route('/weekly-task/<int:task_id>/template-archive', methods=['POST'])
@_admin_required
def weekly_archive_task_template(task_id):
    with _get_db() as conn:
        task = conn.execute('''SELECT wt.*, ws.week_start
            FROM prep_weekly_tasks wt
            JOIN prep_weekly_schedules ws ON ws.id=wt.schedule_id
            WHERE wt.id=?''', (task_id,)).fetchone()
        if not task:
            return redirect(url_for('prep.prep_weekly_view', week_start=get_week_start().isoformat()))
        if task['template_id']:
            conn.execute('UPDATE prep_task_templates SET active=0 WHERE id=?', (task['template_id'],))
            stats = _sync_all_unlocked_weeks_from_templates(conn)
        else:
            conn.execute('DELETE FROM prep_weekly_tasks WHERE id=?', (task_id,))
            stats = {'weeks': 1, 'updated': 0, 'added': 0, 'inactive': 1}
        week_start = task['week_start']
    return redirect(url_for('prep.prep_weekly_view',
        week_start=week_start, template_archived=1,
        weeks=stats.get('weeks', 0),
        updated=stats.get('updated', 0),
        added=stats.get('added', 0),
        inactive=stats.get('inactive', 0)))

@prep.route('/weekly/<week_start>/tasks/bulk-delete', methods=['POST'])
@_admin_required
def weekly_bulk_delete_tasks(week_start):
    try:
        ws_date = datetime.strptime(week_start, '%Y-%m-%d').date()
    except Exception:
        ws_date = get_week_start()
    week_start = get_week_start(ws_date).isoformat()

    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get('task_ids') or request.form.getlist('task_ids')
    task_ids = []
    for raw in raw_ids:
        try:
            task_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    task_ids = sorted(set(task_ids))
    if not task_ids:
        return jsonify({'error': 'No tasks selected.'}), 400

    with _get_db() as conn:
        _ensure_schedule(week_start, conn)
        sched = conn.execute(
            'SELECT * FROM prep_weekly_schedules WHERE week_start=?',
            (week_start,)).fetchone()
        if not sched:
            return jsonify({'error': 'Weekly schedule not found.'}), 404
        if sched['locked']:
            return jsonify({'error': 'This week is locked. Unlock or choose an active week first.'}), 400

        placeholders = ','.join('?' for _ in task_ids)
        tasks = conn.execute(f'''
            SELECT id, template_id
            FROM prep_weekly_tasks
            WHERE schedule_id=? AND id IN ({placeholders})
        ''', [sched['id']] + task_ids).fetchall()
        if not tasks:
            return jsonify({'error': 'Selected tasks were not found in this week.'}), 404

        template_ids = sorted({t['template_id'] for t in tasks if t['template_id']})
        loose_ids = sorted({t['id'] for t in tasks if not t['template_id']})

        if template_ids:
            tpl_placeholders = ','.join('?' for _ in template_ids)
            conn.execute(
                f'UPDATE prep_task_templates SET active=0 WHERE id IN ({tpl_placeholders})',
                template_ids)
        if loose_ids:
            loose_placeholders = ','.join('?' for _ in loose_ids)
            conn.execute(
                f'DELETE FROM prep_weekly_tasks WHERE id IN ({loose_placeholders})',
                loose_ids)

        stats = _sync_all_unlocked_weeks_from_templates(conn)

    return jsonify({
        'ok': True,
        'deleted': len(tasks),
        'stats': stats,
        'redirect': url_for(
            'prep.prep_weekly_view',
            week_start=week_start,
            tasks_deleted=1,
            deleted=len(tasks),
            weeks=stats.get('weeks', 0),
            updated=stats.get('updated', 0),
            added=stats.get('added', 0),
            inactive=stats.get('inactive', 0),
        ),
    })

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

@prep.route('/weekly/<week_start>/export/pdf')
@_admin_required
def weekly_export_pdf(week_start):
    from html import escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        ws_date = datetime.strptime(week_start, '%Y-%m-%d').date()
    except Exception:
        ws_date = get_week_start()
    ws_date    = get_week_start(ws_date)
    week_start = ws_date.isoformat()
    week_dates = get_week_dates(week_start)

    # Optional ?station=<id> filter — export only that one section.
    try:
        station_filter = int(request.args.get('station', '') or 0)
    except (TypeError, ValueError):
        station_filter = 0
    filter_station_obj = STATIONS_MAP.get(station_filter) if station_filter else None

    with _get_db() as conn:
        _ensure_schedule(week_start, conn)
        sched = conn.execute('SELECT * FROM prep_weekly_schedules WHERE week_start=?', (week_start,)).fetchone()
        sched = dict(sched) if sched else None
        tasks = []
        if sched:
            sql = 'SELECT * FROM prep_weekly_tasks WHERE schedule_id=?'
            params = [sched['id']]
            if station_filter:
                sql += ' AND station_id=?'
                params.append(station_filter)
            sql += ' ORDER BY station_id, scheduled_time, id'
            for wt in conn.execute(sql, params).fetchall():
                wt = dict(wt)
                wt['station']  = STATIONS_MAP.get(wt['station_id'], {})
                wt['fmt_time'] = fmt_time(wt.get('scheduled_time'))
                ds_rows = conn.execute(
                    'SELECT * FROM prep_daily_status WHERE weekly_task_id=? ORDER BY date', (wt['id'],)).fetchall()
                wt['days'] = {r['day_of_week']: dict(r) for r in ds_rows}
                tasks.append(wt)

    try:
        from app import register_pdf_fonts
        font_name, bold_font = register_pdf_fonts()
    except Exception:
        font_name, bold_font = 'Helvetica', 'Helvetica-Bold'

    buf = BytesIO()
    page_size = landscape(A3)
    doc = SimpleDocTemplate(buf, pagesize=page_size,
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=10*mm, bottomMargin=9*mm)
    base = getSampleStyleSheet()
    styles = {
        'title': ParagraphStyle('title', parent=base['Title'], fontName=bold_font, fontSize=25,
                                leading=29, textColor=colors.HexColor('#143D2A'), alignment=TA_CENTER,
                                spaceAfter=2*mm),
        'sub': ParagraphStyle('sub', parent=base['Normal'], fontName=bold_font, fontSize=12.5,
                              leading=15, textColor=colors.HexColor('#333333'), alignment=TA_CENTER),
        'task': ParagraphStyle('task', parent=base['BodyText'], fontName=bold_font, fontSize=10.5,
                               leading=12.5, textColor=colors.HexColor('#111111')),
        'note': ParagraphStyle('note', parent=base['BodyText'], fontName=font_name, fontSize=8,
                               leading=9.5, textColor=colors.HexColor('#6D4C41')),
        'station': ParagraphStyle('station', parent=base['Normal'], fontName=bold_font, fontSize=12,
                                  leading=14, textColor=colors.white, alignment=TA_CENTER),
    }

    total_req  = sum(1 for t in tasks for d in DAYS if t['days'].get(d, {}).get('is_required') and t['days'][d].get('status') != 'moved')
    total_done = sum(1 for t in tasks for d in DAYS if t['days'].get(d, {}).get('status') == 'done')

    if filter_station_obj:
        title_text = f"MCQ MIRRABOOKA - {filter_station_obj.get('name_en', '').upper()}"
    else:
        title_text = 'MCQ MIRRABOOKA - WEEKLY PREP SCHEDULE'

    story = [
        Paragraph(title_text, styles['title']),
        Paragraph(f"Week: {week_start} to {week_dates[-1]}   |   Tasks: {len(tasks)}   |   "
                  f"Done: {total_done} / {total_req}   |   "
                  f"Status: {'LOCKED' if sched and sched.get('locked') else 'ACTIVE'}",
                  styles['sub']),
        Spacer(1, 5*mm),
    ]

    # Wall-print table: section header rows, task/time/staff/notes columns, then Mon-Sun checkboxes.
    day_vi = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN']
    headers = ['TASK', 'TIME', 'STAFF', 'NOTES']
    for i, d in enumerate(DAY_LABELS):
        headers.append(f'{d.upper()} / {day_vi[i]}\n{week_dates[i][5:]}')
    rows = [headers]

    cur_station = None
    station_row_indexes = []
    task_row_indexes = []
    for t in tasks:
        st = t['station']
        if t['station_id'] != cur_station:
            station_row_indexes.append(len(rows))
            rows.append([
                Paragraph(
                    f"{escape(st.get('name_en', 'SECTION'))}"
                    f"{' - ' + escape(st.get('name_vi', '')) if st.get('name_vi') else ''}",
                    styles['station']),
            ] + [''] * 10)
            cur_station = t['station_id']

        supplier = ''
        if t.get('is_supplier'):
            supplier = f"<br/><font color='#0D47A1'><b>Supplier:</b> {escape(t.get('supplier_name') or 'Supplier')}</font>"
        task_p = Paragraph(
            f"<b>{escape(t['task_name_en'] or '')}</b><br/>"
            f"<font color='#555555'>{escape(t['task_name_vi'] or '')}</font>"
            f"{supplier}",
            styles['task'])
        time_p = Paragraph(f"<b>{escape(t['fmt_time'] or '-')}</b>", styles['note'])
        staff_p = Paragraph(escape(t.get('assigned_to') or '-'), styles['note'])

        notes = []
        for i, d in enumerate(DAYS):
            ds = t['days'].get(d, {})
            note = (ds.get('note') or '').strip()
            if note:
                notes.append(f"<b>{DAY_LABELS[i]}:</b> {escape(note)}")
        notes_p = Paragraph('<br/>'.join(notes) if notes else '____________________', styles['note'])

        task_row_indexes.append(len(rows))
        row = [task_p, time_p, staff_p, notes_p]
        for d in DAYS:
            ds = t['days'].get(d, {})
            if not ds.get('is_required'):
                row.append('—')
            elif ds.get('status') == 'done':
                row.append('☑')
            elif ds.get('status') == 'moved':
                row.append('→ ' + (ds.get('moved_to_day','') or '').upper())
            else:
                row.append('☐')
        rows.append(row)

    col_widths = [76*mm, 22*mm, 36*mm, 52*mm] + [30*mm]*7
    table = Table(rows, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B4332')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), bold_font),
        ('FONTNAME', (0, 1), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, 0), 9.5),
        ('FONTSIZE', (4, 1), (-1, -1), 10.5),
        ('LEADING', (4, 1), (-1, -1), 12),
        ('ALIGN', (1, 0), (2, -1), 'CENTER'),
        ('ALIGN', (4, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (3, 0), (3, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.65, colors.HexColor('#9EAAA2')),
        ('LINEBELOW', (0, 0), (-1, 0), 1.2, colors.HexColor('#143D2A')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]

    for ri in station_row_indexes:
        style_cmds.extend([
            ('SPAN', (0, ri), (-1, ri)),
            ('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#1B4332')),
            ('TEXTCOLOR', (0, ri), (-1, ri), colors.white),
            ('FONTNAME', (0, ri), (-1, ri), bold_font),
            ('FONTSIZE', (0, ri), (-1, ri), 12),
            ('TOPPADDING', (0, ri), (-1, ri), 6),
            ('BOTTOMPADDING', (0, ri), (-1, ri), 6),
        ])

    for idx, t in enumerate(tasks):
        ri = task_row_indexes[idx]
        style_cmds.extend([
            ('BACKGROUND', (0, ri), (2, ri), colors.HexColor('#FAFBFC')),
            ('BACKGROUND', (3, ri), (3, ri), colors.HexColor('#FFFDE7')),
            ('FONTNAME', (0, ri), (0, ri), bold_font),
            ('TOPPADDING', (0, ri), (-1, ri), 6),
            ('BOTTOMPADDING', (0, ri), (-1, ri), 6),
        ])
        for di, d in enumerate(DAYS):
            ds = t['days'].get(d, {})
            col = 4 + di
            if not ds.get('is_required'):
                style_cmds.append(('BACKGROUND', (col, ri), (col, ri), colors.HexColor('#F5F5F5')))
                style_cmds.append(('TEXTCOLOR', (col, ri), (col, ri), colors.HexColor('#BBBBBB')))
            elif ds.get('status') == 'done':
                style_cmds.append(('BACKGROUND', (col, ri), (col, ri), colors.HexColor('#C8E6C9')))
                style_cmds.append(('TEXTCOLOR', (col, ri), (col, ri), colors.HexColor('#1B5E20')))
            elif ds.get('status') == 'moved':
                style_cmds.append(('BACKGROUND', (col, ri), (col, ri), colors.HexColor('#FFF3E0')))
                style_cmds.append(('TEXTCOLOR', (col, ri), (col, ri), colors.HexColor('#E65100')))
            elif t.get('is_supplier'):
                style_cmds.append(('BACKGROUND', (col, ri), (col, ri), colors.HexColor('#E3F2FD')))
                style_cmds.append(('TEXTCOLOR', (col, ri), (col, ri), colors.HexColor('#1565C0')))
            else:
                style_cmds.append(('BACKGROUND', (col, ri), (col, ri), colors.HexColor('#F8FFF8')))

    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    story.append(Spacer(1, 5*mm))

    legend = Paragraph(
        '<b>Legend:</b> '
        '<font color="#1B5E20">☑ Done</font> &nbsp;&nbsp; '
        '<font color="#222222">☐ To do / tick when complete</font> &nbsp;&nbsp; '
        '<font color="#1565C0">Supplier shown in task details</font> &nbsp;&nbsp; '
        '<font color="#E65100">→ moved earlier</font> &nbsp;&nbsp; '
        '<font color="#999999">— not required / no checkbox</font>',
        styles['sub'])
    story.append(legend)

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(font_name, 9)
        canvas.setFillColor(colors.HexColor('#666666'))
        canvas.drawString(10*mm, 6*mm, 'MCQ Mirrabooka Cafe - Weekly Prep Schedule - wall print')
        canvas.drawRightString(page_size[0] - 10*mm, 6*mm,
                               f'Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}   Page {doc_obj.page}')
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    buf.seek(0)
    if filter_station_obj:
        slug = re.sub(r'[^A-Za-z0-9]+', '_',
                      filter_station_obj.get('name_en', f'station{station_filter}')).strip('_')
        fname = f'MCQ_Weekly_Prep_{week_start}_{slug}.pdf'
    else:
        fname = f'MCQ_Weekly_Prep_{week_start}.pdf'
    return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=fname)


@prep.route('/weekly/<week_start>/lock', methods=['POST'])
@_admin_required
def lock_week(week_start):
    with _get_db() as conn:
        conn.execute('''UPDATE prep_weekly_schedules
            SET locked=1,locked_by=?,locked_at=datetime('now','localtime') WHERE week_start=?''',
            (session.get('role','admin'), week_start))
    if email_service:
        email_service.send_notification(
            'prep',
            subject=f'Weekly prep schedule LOCKED ({week_start})',
            lines=[
                f'Week: {week_start}',
                f'Locked by: {session.get("role","admin")}',
                'Staff can no longer modify this week.',
            ],
            link_path=f'/prep/weekly/{week_start}',
            actor=session.get('role','admin'),
        )
    return redirect(url_for('prep.prep_weekly_view', week_start=week_start))

@prep.route('/weekly/batch-save', methods=['POST'])
@_login_required
def weekly_batch_save():
    items = request.get_json() or []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    changed_by_week = {}
    with _get_db() as conn:
        for item in items:
            sid = item.get('id')
            done = item.get('done', False)
            if not sid:
                continue
            row = conn.execute('''
                SELECT status, date, day_of_week
                FROM prep_daily_status
                WHERE id=?
            ''', (sid,)).fetchone()
            if not row or row['status'] == 'moved':
                continue
            new_status = 'done' if done else 'pending'
            if row['status'] == new_status:
                continue
            done_at = now if done else None
            done_by = session.get('staff_name') or session.get('username') or session.get('role', '')
            conn.execute('UPDATE prep_daily_status SET status=?, done_by=?, done_at=? WHERE id=?',
                         (new_status, done_by if done else None, done_at, sid))
            try:
                day_idx = DAYS.index(row['day_of_week'])
                ws = (datetime.strptime(row['date'], '%Y-%m-%d').date()
                      - timedelta(days=day_idx)).isoformat()
                changed_by_week[ws] = changed_by_week.get(ws, 0) + 1
            except Exception:
                pass
    if email_service and hasattr(email_service, 'send_prep_weekly_schedule'):
        for ws, changed_count in changed_by_week.items():
            week_end = get_week_dates(ws)[-1]
            email_service.send_prep_weekly_schedule(
                ws,
                subject=f'Weekly prep schedule saved - {ws} to {week_end}',
                actor=session.get('role', ''),
                changed_count=changed_count,
            )
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
        if flag == 1 and email_service:
            ctx = conn.execute('''SELECT ds.date, ds.day_of_week, wt.task_name_en, wt.assigned_to
                FROM prep_daily_status ds JOIN prep_weekly_tasks wt ON wt.id=ds.weekly_task_id
                WHERE ds.id=?''', (status_id,)).fetchone()
            if ctx:
                email_service.send_notification(
                    'prep',
                    subject=f'Prep issue flagged: {ctx["task_name_en"]} ({ctx["date"]})',
                    lines=[
                        f'Task: {ctx["task_name_en"]}',
                        f'Date: {ctx["date"]} ({ctx["day_of_week"]})',
                        f'Assigned: {ctx["assigned_to"] or "-"}',
                        f'Note: {note or "-"}',
                    ],
                    link_path=f'/prep/weekly/{(datetime.strptime(ctx["date"],"%Y-%m-%d").date() - timedelta(days=DAYS.index(ctx["day_of_week"]))).isoformat()}',
                    actor=session.get('role',''),
                )
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
    active_days = _form_active_days()
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

# ── Stations CRUD ─────────────────────────────────────────────────────────────

def _clean_color(value, fallback='#607D8B'):
    v = (value or '').strip()
    if len(v) == 7 and v.startswith('#'):
        try:
            int(v[1:], 16)
            return v.upper()
        except ValueError:
            pass
    return fallback


@prep.route('/stations/add', methods=['POST'])
@_admin_required
def add_station():
    name_en = request.form.get('name_en', '').strip()
    name_vi = request.form.get('name_vi', '').strip()
    color   = _clean_color(request.form.get('color', ''))
    if not name_en:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'Section name required'}), 400
        return redirect(request.referrer or url_for('prep.prep_today'))
    with _get_db() as conn:
        next_order = conn.execute(
            'SELECT COALESCE(MAX(sort_order), -1) + 1 as n FROM prep_stations'
        ).fetchone()['n']
        cur = conn.execute('''INSERT INTO prep_stations
            (name_en, name_vi, color, sort_order, active)
            VALUES (?, ?, ?, ?, 1)''', (name_en, name_vi, color, next_order))
        new_id = cur.lastrowid
    _refresh_stations()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'id': new_id, 'name_en': name_en,
                        'name_vi': name_vi, 'color': color})
    return redirect(request.referrer or url_for('prep.prep_today'))


@prep.route('/stations/<int:sid>/edit', methods=['POST'])
@_admin_required
def edit_station(sid):
    name_en = request.form.get('name_en', '').strip()
    name_vi = request.form.get('name_vi', '').strip()
    color   = _clean_color(request.form.get('color', ''))
    if not name_en:
        return jsonify({'error': 'Section name required'}), 400
    with _get_db() as conn:
        row = conn.execute('SELECT 1 FROM prep_stations WHERE id=?', (sid,)).fetchone()
        if not row:
            return jsonify({'error': 'Section not found'}), 404
        conn.execute('UPDATE prep_stations SET name_en=?, name_vi=?, color=? WHERE id=?',
                     (name_en, name_vi, color, sid))
    _refresh_stations()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'id': sid, 'name_en': name_en,
                        'name_vi': name_vi, 'color': color})
    return redirect(request.referrer or url_for('prep.prep_today'))


@prep.route('/stations/<int:sid>/delete', methods=['POST'])
@_admin_required
def delete_station(sid):
    with _get_db() as conn:
        task_count = conn.execute(
            'SELECT COUNT(*) as c FROM prep_task_templates WHERE station_id=?',
            (sid,)).fetchone()['c']
        if task_count > 0:
            return jsonify({
                'error': f'Cannot delete: this section still has {task_count} task(s). '
                         f'Move or delete those tasks first.'
            }), 400
        # Soft delete: mark inactive so any historical data referencing the id
        # still resolves to a name. Hard delete would orphan past weeks.
        conn.execute('UPDATE prep_stations SET active=0 WHERE id=?', (sid,))
    _refresh_stations()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(request.referrer or url_for('prep.prep_today'))


@prep.route('/templates/<int:tid>/edit', methods=['POST'])
@_admin_required
def edit_template(tid):
    active_days = _form_active_days()
    is_supplier = 1 if request.form.get('is_supplier') else 0
    with _get_db() as conn:
        conn.execute('''UPDATE prep_task_templates SET
            task_name_en=?,task_name_vi=?,station_id=?,default_time=?,active_days=?,
            default_assignee=?,is_supplier=?,supplier_name=? WHERE id=?''',
            (request.form.get('task_name_en',''), request.form.get('task_name_vi',''),
             int(request.form.get('station_id',1)), request.form.get('default_time',''),
             active_days, request.form.get('default_assignee',''),
             is_supplier, request.form.get('supplier_name',''), tid))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('prep.prep_templates_view'))


@prep.route('/templates/<int:tid>/delete', methods=['POST'])
@_admin_required
def delete_template(tid):
    with _get_db() as conn:
        conn.execute('DELETE FROM prep_task_templates WHERE id=?', (tid,))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
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
