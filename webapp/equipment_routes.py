"""Equipment Temperature Record — weekly grid (Mon–Sun).

Tracks fridge / freezer / hot-unit temperatures for each piece of equipment,
one reading per day. Safe ranges (built into the headings + colour alerts):

    cold    fridges      0°C  to  5°C        (safe ≤ 5°C, ≥ 0°C)
    freezer freezers    -20°C to -15°C       (safe between -20 and -15)
    hot     hot holding  ≥ 60°C

Exposes helper `collect_equipment_for_date(conn, date_str)` used by the daily
WhatsApp / Gmail share to embed equipment temperatures.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify, flash)
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps

try:
    import email_service
except Exception:
    email_service = None

equipment = Blueprint('equipment', __name__, url_prefix='/equipment')
DB_PATH = None

DAYS_FULL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DAYS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

KIND_META = {
    'cold':    {'label': 'Fridge (Cold)', 'range': '0°C to 5°C',     'lo': 0.0,   'hi': 5.0,   'color': '#1565C0', 'icon': 'fa-snowflake'},
    'freezer': {'label': 'Freezer',       'range': '-20°C to -15°C', 'lo': -20.0, 'hi': -15.0, 'color': '#00838F', 'icon': 'fa-icicles'},
    'hot':     {'label': 'Hot Holding',   'range': '60°C or above',  'lo': 60.0,  'hi': None,  'color': '#C62828', 'icon': 'fa-fire'},
}

# (name, kind) — seeded once; admin can add/edit/delete afterwards.
UNITS_SEED = [
    ('Cold Unit 1 – Fruit Juice',       'cold'),
    ('Cold Unit 2 – Soft Drink Fridge', 'cold'),
    ('Cold Unit 3 – Rice Paper Roll',   'cold'),
    ('Cold Unit 4 – Banh Mi Fridge',    'cold'),
    ('Cold Unit 5 – Coffee Fridge',     'cold'),
    ('Cold Unit 6 – Soup & Rice',       'cold'),
    ('Cold Unit 7 – Food Prep Fridge',  'cold'),
    ('Freezer 1',                       'freezer'),
    ('Freezer 2',                       'freezer'),
    ('Hot Unit 1 – Pastry Display',     'hot'),
]

# ── Helpers ──────────────────────────────────────────────────────────────────

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

def _get_staff():
    try:
        with _get_db() as conn:
            rows = conn.execute(
                'SELECT name FROM staff_members WHERE active=1 ORDER BY name').fetchall()
            return [r['name'] for r in rows]
    except Exception:
        return []

def _monday(d):
    return d - timedelta(days=d.weekday())

def is_unsafe(kind, temp):
    """True if a reading is outside the safe range for its equipment kind."""
    if temp is None:
        return False
    m = KIND_META.get(kind, KIND_META['cold'])
    if m['hi'] is None:        # hot holding: only a lower bound
        return temp < m['lo']
    return temp < m['lo'] or temp > m['hi']

# ── DB init ──────────────────────────────────────────────────────────────────

def init_equipment_tables(db_path):
    global DB_PATH
    DB_PATH = db_path
    with _get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS equipment_units (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                kind       TEXT DEFAULT 'cold',
                sort_order INTEGER DEFAULT 0,
                active     INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS equipment_temp_readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id     INTEGER NOT NULL,
                date        TEXT NOT NULL,
                temp        REAL,
                recorded_by TEXT,
                recorded_at TEXT,
                UNIQUE(unit_id, date),
                FOREIGN KEY(unit_id) REFERENCES equipment_units(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_eqtemp_date ON equipment_temp_readings(date);
        ''')
        if conn.execute('SELECT COUNT(*) c FROM equipment_units').fetchone()['c'] == 0:
            for i, (name, kind) in enumerate(UNITS_SEED):
                conn.execute('INSERT INTO equipment_units(name, kind, sort_order) VALUES(?,?,?)',
                             (name, kind, i))

# ── Shared collector (used by the daily share) ───────────────────────────────

def collect_equipment_for_date(conn, date_str):
    """Return equipment readings for one date, with safety flags.
    Shape: {'units':[{name,kind,temp,unsafe}], 'recorded':n, 'alerts':n, 'recorded_by':..}"""
    units = conn.execute(
        'SELECT * FROM equipment_units WHERE active=1 ORDER BY sort_order, id').fetchall()
    rows = conn.execute(
        'SELECT * FROM equipment_temp_readings WHERE date=?', (date_str,)).fetchall()
    by_unit = {r['unit_id']: r for r in rows}
    out, recorded, alerts, recorded_by = [], 0, 0, ''
    for u in units:
        r = by_unit.get(u['id'])
        temp = r['temp'] if r else None
        unsafe = is_unsafe(u['kind'], temp)
        if temp is not None:
            recorded += 1
            if r['recorded_by']:
                recorded_by = r['recorded_by']
        if unsafe:
            alerts += 1
        out.append({'name': u['name'], 'kind': u['kind'], 'temp': temp,
                    'unsafe': unsafe, 'range': KIND_META.get(u['kind'], {}).get('range', '')})
    return {'units': out, 'recorded': recorded, 'alerts': alerts,
            'total': len(units), 'recorded_by': recorded_by}

# ── Routes ───────────────────────────────────────────────────────────────────

@equipment.route('/')
@_login_required
def index():
    ws = _monday(date.today()).isoformat()
    return redirect(url_for('equipment.week_view', week_start=ws))

@equipment.route('/week/<week_start>')
@_login_required
def week_view(week_start):
    try:
        ws = datetime.strptime(week_start, '%Y-%m-%d').date()
    except ValueError:
        ws = _monday(date.today())
    ws = _monday(ws)  # snap to Monday
    week_dates = [(ws + timedelta(days=i)) for i in range(7)]
    week_iso = [d.isoformat() for d in week_dates]
    today_iso = date.today().isoformat()

    with _get_db() as conn:
        units = [dict(r) for r in conn.execute(
            'SELECT * FROM equipment_units WHERE active=1 ORDER BY sort_order, id').fetchall()]
        readings = conn.execute(
            'SELECT unit_id, date, temp, recorded_by FROM equipment_temp_readings '
            'WHERE date >= ? AND date <= ?', (week_iso[0], week_iso[6])).fetchall()

    # map[unit_id][date] = temp
    rmap = {}
    for r in readings:
        rmap.setdefault(r['unit_id'], {})[r['date']] = r['temp']

    grid = []
    for u in units:
        cells = []
        for diso in week_iso:
            temp = rmap.get(u['id'], {}).get(diso)
            cells.append({'date': diso, 'temp': temp,
                          'unsafe': is_unsafe(u['kind'], temp)})
        grid.append({'unit': u, 'cells': cells,
                     'meta': KIND_META.get(u['kind'], KIND_META['cold'])})

    prev_ws = (ws - timedelta(days=7)).isoformat()
    next_ws = (ws + timedelta(days=7)).isoformat()

    return render_template('equipment_temp.html',
        grid=grid, units=units, kind_meta=KIND_META,
        week_dates=week_dates, week_iso=week_iso, days_short=DAYS_SHORT,
        week_start=ws.isoformat(), prev_ws=prev_ws, next_ws=next_ws,
        today_iso=today_iso, staff=_get_staff(), is_admin=_is_admin())

@equipment.route('/save', methods=['POST'])
@_login_required
def save_week():
    week_start = request.form.get('week_start', '').strip()
    recorded_by = request.form.get('recorded_by', '').strip()
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        ws = _monday(datetime.strptime(week_start, '%Y-%m-%d').date())
    except ValueError:
        ws = _monday(date.today())
    week_iso = [(ws + timedelta(days=i)).isoformat() for i in range(7)]

    alerts = []
    with _get_db() as conn:
        units = {r['id']: r for r in conn.execute(
            'SELECT * FROM equipment_units WHERE active=1').fetchall()}
        for uid in units:
            for diso in week_iso:
                raw = request.form.get(f'temp_{uid}_{diso}', '').strip()
                if raw == '':
                    # blank → delete any existing reading for that cell
                    conn.execute('DELETE FROM equipment_temp_readings WHERE unit_id=? AND date=?',
                                 (uid, diso))
                    continue
                try:
                    temp = float(raw)
                except ValueError:
                    continue
                conn.execute('''INSERT INTO equipment_temp_readings
                    (unit_id, date, temp, recorded_by, recorded_at) VALUES (?,?,?,?,?)
                    ON CONFLICT(unit_id, date) DO UPDATE SET
                      temp=excluded.temp, recorded_by=excluded.recorded_by,
                      recorded_at=excluded.recorded_at''',
                    (uid, diso, temp, recorded_by, now))
                if is_unsafe(units[uid]['kind'], temp):
                    alerts.append(f"{units[uid]['name']} {diso}: {temp}°C")

    if email_service:
        try:
            email_service.send_notification(
                'temperature',
                subject=f'Equipment Temperature Record saved (week {week_iso[0]})',
                lines=[
                    f'Week: {week_iso[0]} → {week_iso[6]}',
                    f'Recorded by: {recorded_by or "-"}',
                    f'Out-of-range readings: {len(alerts)}'
                    + (' — ' + '; '.join(alerts[:6]) if alerts else ''),
                ],
                link_path=f'/equipment/week/{ws.isoformat()}',
                actor=recorded_by,
            )
        except Exception:
            pass

    flash(f'Equipment temperatures saved for week of {week_iso[0]}.'
          + (f' ⚠️ {len(alerts)} reading(s) out of range.' if alerts else ''),
          'warning' if alerts else 'success')
    return redirect(url_for('equipment.week_view', week_start=ws.isoformat()))

# ── Admin: manage units ──────────────────────────────────────────────────────

@equipment.route('/unit/add', methods=['POST'])
@_admin_required
def unit_add():
    name = request.form.get('name', '').strip()
    kind = request.form.get('kind', 'cold').strip()
    if kind not in KIND_META:
        kind = 'cold'
    if name:
        with _get_db() as conn:
            nxt = conn.execute('SELECT COALESCE(MAX(sort_order),0)+1 n FROM equipment_units').fetchone()['n']
            conn.execute('INSERT INTO equipment_units(name, kind, sort_order) VALUES(?,?,?)',
                         (name, kind, nxt))
        flash(f'Equipment "{name}" added.', 'success')
    return redirect(request.referrer or url_for('equipment.index'))

@equipment.route('/unit/<int:uid>/update', methods=['POST'])
@_admin_required
def unit_update(uid):
    name = request.form.get('name', '').strip()
    kind = request.form.get('kind', 'cold').strip()
    active = 1 if request.form.get('active', '1') == '1' else 0
    if kind not in KIND_META:
        kind = 'cold'
    with _get_db() as conn:
        conn.execute('UPDATE equipment_units SET name=?, kind=?, active=? WHERE id=?',
                     (name, kind, active, uid))
    flash('Equipment updated.', 'success')
    return redirect(request.referrer or url_for('equipment.index'))

@equipment.route('/unit/<int:uid>/delete', methods=['POST'])
@_admin_required
def unit_delete(uid):
    with _get_db() as conn:
        conn.execute('DELETE FROM equipment_units WHERE id=?', (uid,))
    flash('Equipment deleted.', 'warning')
    return redirect(request.referrer or url_for('equipment.index'))
