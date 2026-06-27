"""Equipment Temperature Record — weekly grid (Mon–Sun).

Tracks fridge / freezer / hot-unit temperatures for each piece of equipment,
with a morning and closing reading per day. Safe ranges (built into the
headings + colour alerts):

    cold    fridges      0°C  to  5°C        (safe ≤ 5°C, ≥ 0°C)
    freezer freezers    -20°C to -15°C       (safe between -20 and -15)
    hot     hot holding  ≥ 60°C

Exposes helper `collect_equipment_for_date(conn, date_str)` used by the daily
WhatsApp / Gmail share to embed equipment temperatures.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify, flash, send_from_directory, abort)
import sqlite3
import os
import re
import json
import base64
import secrets
import urllib.request
from datetime import datetime, date, timedelta
from functools import wraps

from store_scope import current_store_id, store_guard_clause


def _ensure_units_for_store(conn, store_id):
    """Each store has its own equipment list. Seed the standard units the first
    time a store opens the equipment screen (so new branches aren't blank)."""
    n = conn.execute('SELECT COUNT(*) c FROM equipment_units WHERE store_id=?',
                     (store_id,)).fetchone()['c']
    if n == 0:
        for i, (name, kind) in enumerate(UNITS_SEED):
            conn.execute('INSERT INTO equipment_units(name, kind, sort_order, store_id) '
                         'VALUES(?,?,?,?)', (name, kind, i, store_id))

try:
    import email_service
except Exception:
    email_service = None

equipment = Blueprint('equipment', __name__, url_prefix='/equipment')
DB_PATH = None

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
ALLOWED_IMG_EXT = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif'}
OPENAI_URL = 'https://api.openai.com/v1/chat/completions'
OPENAI_MODEL = 'gpt-4o'

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
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def _is_admin():
    return session.get('role') in ('admin', 'super_admin')

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
        if session.get('role') not in ('admin', 'super_admin'):
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

CHECK_TYPES = [
    {'key': 'morning', 'label': 'Morning', 'short': 'AM',
     'deadline': 'Before 10:00 AM', 'due_hour': 10, 'due_minute': 0},
    {'key': 'closing', 'label': 'Closing', 'short': 'Close',
     'deadline': 'Before 6:00 PM', 'due_hour': 18, 'due_minute': 0},
]
CHECK_META = {c['key']: c for c in CHECK_TYPES}


def _check_columns(check_type):
    if check_type not in CHECK_META:
        check_type = 'morning'
    return (f'{check_type}_temp', f'{check_type}_recorded_by',
            f'{check_type}_recorded_at')


def _reading(temp, kind, recorded_by='', recorded_at='', defrosted=False, photo=''):
    # A unit being defrosted is expected to run warm — never flag it unsafe.
    return {
        'temp': temp,
        'unsafe': False if defrosted else is_unsafe(kind, temp),
        'defrosted': bool(defrosted),
        'recorded_by': recorded_by or '',
        'recorded_at': recorded_at or '',
        'photo': photo or '',
    }


def _due_check_keys(date_str, now=None):
    """Checks due for a report date. Future closing checks are not alerts yet."""
    now = now or datetime.now()
    try:
        target = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return {c['key'] for c in CHECK_TYPES}
    if target < now.date():
        return {c['key'] for c in CHECK_TYPES}
    if target > now.date():
        return set()
    due = set()
    for check in CHECK_TYPES:
        deadline = datetime(target.year, target.month, target.day,
                            check.get('due_hour', 0), check.get('due_minute', 0))
        if now >= deadline:
            due.add(check['key'])
    return due


def _combined_status(unit):
    checks = unit.get('checks') or {}
    missing = sum(1 for c in CHECK_TYPES if checks.get(c['key'], {}).get('temp') is None)
    unsafe = sum(1 for c in CHECK_TYPES if checks.get(c['key'], {}).get('unsafe'))
    if unsafe:
        return 'alert'
    if missing:
        return 'missing'
    return 'ok'

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
        for _col, ddl in [
            ('morning_temp', "ALTER TABLE equipment_temp_readings ADD COLUMN morning_temp REAL"),
            ('morning_recorded_by', "ALTER TABLE equipment_temp_readings ADD COLUMN morning_recorded_by TEXT"),
            ('morning_recorded_at', "ALTER TABLE equipment_temp_readings ADD COLUMN morning_recorded_at TEXT"),
            ('closing_temp', "ALTER TABLE equipment_temp_readings ADD COLUMN closing_temp REAL"),
            ('closing_recorded_by', "ALTER TABLE equipment_temp_readings ADD COLUMN closing_recorded_by TEXT"),
            ('closing_recorded_at', "ALTER TABLE equipment_temp_readings ADD COLUMN closing_recorded_at TEXT"),
            ('defrosted', "ALTER TABLE equipment_temp_readings ADD COLUMN defrosted TEXT DEFAULT 'N'"),
            ('morning_photo', "ALTER TABLE equipment_temp_readings ADD COLUMN morning_photo TEXT"),
            ('closing_photo', "ALTER TABLE equipment_temp_readings ADD COLUMN closing_photo TEXT"),
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # Legacy rows used one temp per day. Keep them as the morning check.
        conn.execute('''
            UPDATE equipment_temp_readings
            SET morning_temp=COALESCE(morning_temp, temp),
                morning_recorded_by=COALESCE(morning_recorded_by, recorded_by),
                morning_recorded_at=COALESCE(morning_recorded_at, recorded_at)
            WHERE temp IS NOT NULL
        ''')
        if conn.execute('SELECT COUNT(*) c FROM equipment_units').fetchone()['c'] == 0:
            for i, (name, kind) in enumerate(UNITS_SEED):
                conn.execute('INSERT INTO equipment_units(name, kind, sort_order) VALUES(?,?,?)',
                             (name, kind, i))

# ── Shared collector (used by the daily share) ───────────────────────────────

def collect_equipment_for_date(conn, date_str, store_id=None):
    """Return equipment readings for one date, with safety flags, for one store.
    Shape: {'units':[{name,kind,checks:{morning,closing},status}],
            'recorded':n, 'total_checks':n, 'alerts':n, 'missing':n}"""
    if store_id is None:
        store_id = current_store_id()
    units = conn.execute(
        'SELECT * FROM equipment_units WHERE active=1 AND store_id=? ORDER BY sort_order, id',
        (store_id,)).fetchall()
    rows = conn.execute(
        'SELECT * FROM equipment_temp_readings WHERE date=? AND store_id=?',
        (date_str, store_id)).fetchall()
    by_unit = {r['unit_id']: r for r in rows}
    due_keys = _due_check_keys(date_str)
    out, recorded, alerts, missing, missing_due, recorded_by = [], 0, 0, 0, 0, ''
    for u in units:
        r = by_unit.get(u['id'])
        defrosted = _row_defrosted(r)
        checks = {}
        for check in CHECK_TYPES:
            temp_col, by_col, at_col = _check_columns(check['key'])
            legacy_temp = r['temp'] if r and check['key'] == 'morning' else None
            temp = r[temp_col] if r and temp_col in r.keys() else None
            by = r[by_col] if r and by_col in r.keys() else ''
            at = r[at_col] if r and at_col in r.keys() else ''
            if check['key'] == 'morning':
                if temp is None:
                    temp = legacy_temp
                if not by and r:
                    by = r['recorded_by'] if 'recorded_by' in r.keys() else ''
                if not at and r:
                    at = r['recorded_at'] if 'recorded_at' in r.keys() else ''
            checks[check['key']] = _reading(temp, u['kind'], by, at, defrosted=defrosted)
            if temp is None:
                missing += 1
                if check['key'] in due_keys:
                    missing_due += 1
            else:
                recorded += 1
                if by:
                    recorded_by = by
                if checks[check['key']]['unsafe']:
                    alerts += 1
        unit = {'name': u['name'], 'kind': u['kind'], 'checks': checks,
                'defrosted': defrosted,
                'range': KIND_META.get(u['kind'], {}).get('range', '')}
        unit['status'] = _combined_status(unit)
        # Legacy convenience fields for callers that have not been updated yet.
        unit['temp'] = checks['morning']['temp']
        unit['unsafe'] = checks['morning']['unsafe']
        out.append(unit)
    return {'units': out, 'recorded': recorded, 'alerts': alerts,
            'missing': missing, 'total': len(units),
            'missing_due': missing_due,
            'total_due_checks': len(units) * len(due_keys),
            'due_check_keys': sorted(due_keys),
            'total_checks': len(units) * len(CHECK_TYPES),
            'recorded_by': recorded_by, 'check_types': CHECK_TYPES}

# ── Routes ───────────────────────────────────────────────────────────────────

def _row_defrosted(r):
    return bool(r and 'defrosted' in r.keys() and (r['defrosted'] or 'N').upper() == 'Y')


def _cell_checks(u, r):
    """Build the {morning,closing} reading dict for one unit on one date."""
    defrosted = _row_defrosted(r)
    checks = {}
    for check in CHECK_TYPES:
        temp_col, by_col, at_col = _check_columns(check['key'])
        legacy_temp = r['temp'] if r and check['key'] == 'morning' else None
        temp = r[temp_col] if r and temp_col in r.keys() else None
        by = r[by_col] if r and by_col in r.keys() else ''
        at = r[at_col] if r and at_col in r.keys() else ''
        if check['key'] == 'morning':
            if temp is None:
                temp = legacy_temp
            if not by and r:
                by = r['recorded_by'] if 'recorded_by' in r.keys() else ''
            if not at and r:
                at = r['recorded_at'] if 'recorded_at' in r.keys() else ''
        photo_col = f"{check['key']}_photo"
        photo = r[photo_col] if r and photo_col in r.keys() else ''
        checks[check['key']] = _reading(temp, u['kind'], by, at, defrosted=defrosted, photo=photo)
    return checks


@equipment.route('/')
@equipment.route('/today')
@_login_required
def today_view():
    """Daily entry screen — only TODAY's morning + closing readings."""
    today = date.today()
    today_iso = today.isoformat()
    sid = current_store_id()
    with _get_db() as conn:
        _ensure_units_for_store(conn, sid)
        units = [dict(r) for r in conn.execute(
            'SELECT * FROM equipment_units WHERE active=1 AND store_id=? ORDER BY sort_order, id',
            (sid,)).fetchall()]
        rows = conn.execute(
            'SELECT * FROM equipment_temp_readings WHERE date=? AND store_id=?',
            (today_iso, sid)).fetchall()
    rmap = {r['unit_id']: r for r in rows}
    grid = []
    for u in units:
        r = rmap.get(u['id'])
        grid.append({'unit': u, 'checks': _cell_checks(u, r),
                     'defrosted': _row_defrosted(r),
                     'meta': KIND_META.get(u['kind'], KIND_META['cold'])})
    return render_template('equipment_today.html',
        grid=grid, units=units, kind_meta=KIND_META,
        today=today, today_iso=today_iso, day_name=today.strftime('%A'),
        check_types=CHECK_TYPES, staff=_get_staff(), is_admin=_is_admin())


@equipment.route('/save-today', methods=['POST'])
@_login_required
def save_today():
    """Save only the posted date's readings (non-destructive to other days)."""
    today_iso = (request.form.get('date') or date.today().isoformat()).strip()
    recorded_by = request.form.get('recorded_by', '').strip()
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    alerts = []
    saved_count = 0
    sid = current_store_id()
    with _get_db() as conn:
        units = {r['id']: r for r in conn.execute(
            'SELECT * FROM equipment_units WHERE active=1 AND store_id=?', (sid,)).fetchall()}
        for uid in units:
            conn.execute('INSERT OR IGNORE INTO equipment_temp_readings (unit_id, date, store_id) VALUES (?,?,?)',
                         (uid, today_iso, sid))
            # Defrosting flag for this unit today (cold/freezer units only).
            defrosted = 'Y' if request.form.get(f'defrosted_{uid}', '') == 'Y' else 'N'
            conn.execute('UPDATE equipment_temp_readings SET defrosted=? WHERE unit_id=? AND date=?',
                         (defrosted, uid, today_iso))
            for check in CHECK_TYPES:
                raw = request.form.get(f'{check["key"]}_temp_{uid}', '').strip()
                temp_col, by_col, at_col = _check_columns(check['key'])
                if raw == '':
                    conn.execute(f'''UPDATE equipment_temp_readings
                        SET {temp_col}=NULL, {by_col}=NULL, {at_col}=NULL
                        WHERE unit_id=? AND date=?''', (uid, today_iso))
                    if check['key'] == 'morning':
                        conn.execute('''UPDATE equipment_temp_readings
                            SET temp=NULL, recorded_by=NULL, recorded_at=NULL
                            WHERE unit_id=? AND date=?''', (uid, today_iso))
                    continue
                try:
                    temp = float(raw)
                except ValueError:
                    continue
                conn.execute(f'''UPDATE equipment_temp_readings
                    SET {temp_col}=?, {by_col}=?, {at_col}=? WHERE unit_id=? AND date=?''',
                    (temp, recorded_by, now, uid, today_iso))
                saved_count += 1
                if check['key'] == 'morning':
                    conn.execute('''UPDATE equipment_temp_readings
                        SET temp=?, recorded_by=?, recorded_at=? WHERE unit_id=? AND date=?''',
                        (temp, recorded_by, now, uid, today_iso))
                if defrosted != 'Y' and is_unsafe(units[uid]['kind'], temp):
                    alerts.append(f"{units[uid]['name']} {check['label']}: {temp:g}°C")
            # Drop a fully-empty row unless it's flagged defrosting (keep the flag).
            conn.execute('''DELETE FROM equipment_temp_readings
                WHERE unit_id=? AND date=? AND morning_temp IS NULL AND closing_temp IS NULL
                  AND COALESCE(defrosted,'N')<>'Y' ''',
                (uid, today_iso))

    if email_service:
        try:
            email_service.send_notification('temperature',
                subject=f'Equipment Temperature saved ({today_iso})',
                lines=[f'Date: {today_iso}', f'Recorded by: {recorded_by or "-"}',
                       'Checks: Morning + Closing',
                       f'Out-of-range: {len(alerts)}' + (' — ' + '; '.join(alerts[:6]) if alerts else '')],
                link_path='/equipment/today', actor=recorded_by)
        except Exception:
            pass

    msg = (f'Saved {saved_count} reading(s) for {today_iso}.'
           + (f' ⚠️ {len(alerts)} out of range.' if alerts else ''))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'saved': saved_count, 'alerts': len(alerts),
                        'alert_list': alerts[:10], 'saved_at': now, 'message': msg})
    flash(msg, 'warning' if alerts else 'success')
    return redirect(url_for('equipment.today_view'))


# ── AI temperature reading (photo → GPT-4o → auto-save) ──────────────────────

def _app_config(key, default=''):
    try:
        with _get_db() as conn:
            row = conn.execute('SELECT value FROM app_config WHERE key=?', (key,)).fetchone()
            return (row['value'] if row and row['value'] else default)
    except Exception:
        return default


def _save_eq_photo(file_storage):
    """Downscale + save the thermometer photo as evidence. Returns (filename, jpeg_bytes)."""
    if not file_storage:
        return '', None
    name = (getattr(file_storage, 'filename', '') or '').strip()
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ALLOWED_IMG_EXT:
        return '', None
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    base = f"eqtemp_{current_store_id()}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"
    try:
        from PIL import Image, ImageOps  # type: ignore
        file_storage.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file_storage.stream))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        img.thumbnail((1024, 1024))
        dest = os.path.join(UPLOAD_FOLDER, base + '.jpg')
        img.save(dest, 'JPEG', quality=82)
        with open(dest, 'rb') as f:
            return os.path.basename(dest), f.read()
    except Exception:
        try:
            file_storage.stream.seek(0)
            data = file_storage.read()
            dest = os.path.join(UPLOAD_FOLDER, base + '.' + ext)
            with open(dest, 'wb') as f:
                f.write(data)
            return os.path.basename(dest), data
        except Exception:
            return '', None


def _delete_eq_photo(fname):
    if not fname:
        return
    try:
        os.remove(os.path.join(UPLOAD_FOLDER, os.path.basename(fname)))
    except Exception:
        pass


def _openai_extract_temp(jpeg_bytes, api_key):
    """Ask GPT-4o for ONLY the Celsius number on the thermometer. Returns float or None."""
    b64 = base64.b64encode(jpeg_bytes).decode('ascii')
    payload = {
        'model': OPENAI_MODEL,
        'temperature': 0,
        'max_tokens': 10,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': (
                "This photo shows a fridge/freezer thermometer or temperature display. "
                "Reply with ONLY the temperature as a number in Celsius (e.g. 3.2 or -18.5). "
                "No words, no degree sign. If you cannot read a number, reply NULL.")},
            {'type': 'image_url', 'image_url': {
                'url': f'data:image/jpeg;base64,{b64}', 'detail': 'low'}},
        ]}],
    }
    ctx = email_service._ssl_context() if email_service else None
    req = urllib.request.Request(
        OPENAI_URL, data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'})
    with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
        data = json.loads(resp.read().decode('utf-8', 'replace'))
    text = ((data.get('choices') or [{}])[0].get('message', {}).get('content') or '').strip()
    if 'null' in text.lower():
        return None
    m = re.search(r'-?\d+(?:\.\d+)?', text)
    return float(m.group(0)) if m else None


@equipment.route('/ai-temp', methods=['POST'])
@_login_required
def ai_temp():
    """Photo of the thermometer → GPT-4o reads the temp → auto-saved (no confirm)."""
    api_key = _app_config('openai_api_key')
    if not api_key:
        return jsonify({'ok': False, 'error': 'AI temperature reading is not set up yet (no OpenAI key).'}), 400
    try:
        uid = int(request.form.get('unit_id', 0))
    except ValueError:
        return jsonify({'ok': False, 'error': 'invalid unit'}), 400
    check = (request.form.get('check') or 'morning').strip().lower()
    if check not in CHECK_META:
        check = 'morning'
    date_iso = (request.form.get('date') or date.today().isoformat()).strip()
    recorded_by = request.form.get('recorded_by', '').strip()
    sid = current_store_id()

    with _get_db() as conn:
        unit = conn.execute(
            'SELECT * FROM equipment_units WHERE id=? AND store_id=? AND active=1',
            (uid, sid)).fetchone()
    if not unit:
        return jsonify({'ok': False, 'error': 'unit not found'}), 404

    fname, jpeg = _save_eq_photo(request.files.get('photo'))
    if not jpeg:
        return jsonify({'ok': False, 'error': 'No photo received. Please take a clear photo.'}), 400

    try:
        temp = _openai_extract_temp(jpeg, api_key)
    except Exception as e:
        _delete_eq_photo(fname)
        return jsonify({'ok': False, 'error': f'AI read failed ({type(e).__name__}). Try again or type it.'}), 502
    if temp is None:
        _delete_eq_photo(fname)
        return jsonify({'ok': False, 'error': 'Could not read a number from the photo. Retake or type it.'}), 422

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    temp_col, by_col, at_col = _check_columns(check)
    photo_col = f'{check}_photo'
    with _get_db() as conn:
        conn.execute('INSERT OR IGNORE INTO equipment_temp_readings (unit_id, date, store_id) VALUES (?,?,?)',
                     (uid, date_iso, sid))
        conn.execute(f'''UPDATE equipment_temp_readings
            SET {temp_col}=?, {by_col}=?, {at_col}=?, {photo_col}=?
            WHERE unit_id=? AND date=? AND store_id=?''',
            (temp, recorded_by, now, fname, uid, date_iso, sid))
        if check == 'morning':
            conn.execute('''UPDATE equipment_temp_readings
                SET temp=?, recorded_by=?, recorded_at=? WHERE unit_id=? AND date=? AND store_id=?''',
                (temp, recorded_by, now, uid, date_iso, sid))
        row = conn.execute(
            'SELECT defrosted FROM equipment_temp_readings WHERE unit_id=? AND date=? AND store_id=?',
            (uid, date_iso, sid)).fetchone()

    defrosted = bool(row and row['defrosted'] == 'Y')
    unsafe = (not defrosted) and is_unsafe(unit['kind'], temp)
    if unsafe and email_service:
        try:
            meta = KIND_META.get(unit['kind'], KIND_META['cold'])
            email_service.send_notification('temperature',
                subject=f'⚠️ Equipment temp out of range — {unit["name"]} ({temp:g}°C)',
                lines=[f'Unit: {unit["name"]}',
                       f'Check: {CHECK_META[check]["label"]}',
                       f'Temp: {temp:g}°C  (safe {meta["range"]})',
                       f'Date: {date_iso}', 'Read by AI from a photo'],
                link_path='/equipment/today', actor=recorded_by)
        except Exception:
            pass

    return jsonify({'ok': True, 'temp': temp, 'unsafe': unsafe, 'saved_at': now,
                    'check': check, 'unit_id': uid,
                    'photo_url': (url_for('equipment.equipment_photo', filename=fname) if fname else '')})


@equipment.route('/photo/<path:filename>')
@_login_required
def equipment_photo(filename):
    """Serve a thermometer photo, but only if it belongs to a reading in the caller's store."""
    safe = os.path.basename(filename)
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        row = conn.execute(
            f'''SELECT 1 FROM equipment_temp_readings
                WHERE (morning_photo=? OR closing_photo=?) AND {guard}''',
            [safe, safe] + gp).fetchone()
    if not row:
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, safe, max_age=604800)


@equipment.route('/report')
@_login_required
def report_view():
    """Read-only history — view by week or by month."""
    period = request.args.get('period', 'week')
    ref = request.args.get('ref', date.today().isoformat())
    try:
        refd = datetime.strptime(ref, '%Y-%m-%d').date()
    except ValueError:
        refd = date.today()

    if period == 'month':
        start = refd.replace(day=1)
        nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1))
        end = nxt - timedelta(days=1)
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        prev_ref = (start - timedelta(days=1)).replace(day=1).isoformat()
        next_ref = nxt.isoformat()
        label = start.strftime('%B %Y')
    else:
        period = 'week'
        ws = _monday(refd)
        dates = [ws + timedelta(days=i) for i in range(7)]
        prev_ref = (ws - timedelta(days=7)).isoformat()
        next_ref = (ws + timedelta(days=7)).isoformat()
        label = f"{dates[0].strftime('%d %b')} – {dates[6].strftime('%d %b %Y')}"

    date_iso = [d.isoformat() for d in dates]
    sid = current_store_id()
    with _get_db() as conn:
        _ensure_units_for_store(conn, sid)
        units = [dict(r) for r in conn.execute(
            'SELECT * FROM equipment_units WHERE active=1 AND store_id=? ORDER BY sort_order, id',
            (sid,)).fetchall()]
        readings = conn.execute(
            'SELECT * FROM equipment_temp_readings WHERE date >= ? AND date <= ? AND store_id=?',
            (date_iso[0], date_iso[-1], sid)).fetchall()
    rmap = {}
    for r in readings:
        rmap.setdefault(r['unit_id'], {})[r['date']] = r

    grid, total_alerts, total_recorded = [], 0, 0
    for u in units:
        cells = []
        for diso in date_iso:
            checks = _cell_checks(u, rmap.get(u['id'], {}).get(diso))
            for c in CHECK_TYPES:
                rd = checks[c['key']]
                if rd['temp'] is not None:
                    total_recorded += 1
                    if rd['unsafe']:
                        total_alerts += 1
            cells.append({'date': diso, 'checks': checks,
                          'status': _combined_status({'checks': checks})})
        grid.append({'unit': u, 'cells': cells,
                     'meta': KIND_META.get(u['kind'], KIND_META['cold'])})

    return render_template('equipment_report.html',
        grid=grid, dates=dates, date_iso=date_iso, period=period, ref=refd.isoformat(),
        prev_ref=prev_ref, next_ref=next_ref, label=label, today_iso=date.today().isoformat(),
        kind_meta=KIND_META, check_types=CHECK_TYPES, days_short=DAYS_SHORT,
        total_alerts=total_alerts, total_recorded=total_recorded, is_admin=_is_admin())


@equipment.route('/index-legacy')
@_login_required
def index():
    return redirect(url_for('equipment.today_view'))

@equipment.route('/week/<week_start>')
@_login_required
def week_view(week_start):
    """Backwards-compatible alias — the weekly grid is now the read-only report."""
    return redirect(url_for('equipment.report_view', period='week', ref=week_start))

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
    sid = current_store_id()
    with _get_db() as conn:
        units = {r['id']: r for r in conn.execute(
            'SELECT * FROM equipment_units WHERE active=1 AND store_id=?', (sid,)).fetchall()}
        for uid in units:
            for diso in week_iso:
                conn.execute('''INSERT OR IGNORE INTO equipment_temp_readings
                    (unit_id, date, store_id) VALUES (?,?,?)''', (uid, diso, sid))
                for check in CHECK_TYPES:
                    raw = request.form.get(
                        f'{check["key"]}_temp_{uid}_{diso}', '').strip()
                    temp_col, by_col, at_col = _check_columns(check['key'])
                    if raw == '':
                        conn.execute(f'''UPDATE equipment_temp_readings
                            SET {temp_col}=NULL, {by_col}=NULL, {at_col}=NULL
                            WHERE unit_id=? AND date=?''', (uid, diso))
                        if check['key'] == 'morning':
                            conn.execute('''UPDATE equipment_temp_readings
                                SET temp=NULL, recorded_by=NULL, recorded_at=NULL
                                WHERE unit_id=? AND date=?''', (uid, diso))
                        continue
                    try:
                        temp = float(raw)
                    except ValueError:
                        continue
                    conn.execute(f'''UPDATE equipment_temp_readings
                        SET {temp_col}=?, {by_col}=?, {at_col}=?
                        WHERE unit_id=? AND date=?''',
                        (temp, recorded_by, now, uid, diso))
                    if check['key'] == 'morning':
                        conn.execute('''UPDATE equipment_temp_readings
                            SET temp=?, recorded_by=?, recorded_at=?
                            WHERE unit_id=? AND date=?''',
                            (temp, recorded_by, now, uid, diso))
                    if is_unsafe(units[uid]['kind'], temp):
                        alerts.append(
                            f"{units[uid]['name']} {diso} {check['label']}: {temp:g}°C")
                conn.execute('''DELETE FROM equipment_temp_readings
                    WHERE unit_id=? AND date=?
                      AND morning_temp IS NULL AND closing_temp IS NULL''',
                    (uid, diso))

    if email_service:
        try:
            email_service.send_notification(
                'temperature',
                subject=f'Equipment Temperature Record saved (week {week_iso[0]})',
                lines=[
                    f'Week: {week_iso[0]} → {week_iso[6]}',
                    f'Recorded by: {recorded_by or "-"}',
                    'Checks: Morning + Closing',
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
        sid = current_store_id()
        with _get_db() as conn:
            nxt = conn.execute('SELECT COALESCE(MAX(sort_order),0)+1 n FROM equipment_units '
                               'WHERE store_id=?', (sid,)).fetchone()['n']
            conn.execute('INSERT INTO equipment_units(name, kind, sort_order, store_id) VALUES(?,?,?,?)',
                         (name, kind, nxt, sid))
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
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'UPDATE equipment_units SET name=?, kind=?, active=? WHERE id=? AND {guard}',
                     [name, kind, active, uid] + gp)
    flash('Equipment updated.', 'success')
    return redirect(request.referrer or url_for('equipment.index'))

@equipment.route('/unit/<int:uid>/delete', methods=['POST'])
@_admin_required
def unit_delete(uid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'DELETE FROM equipment_units WHERE id=? AND {guard}', [uid] + gp)
    flash('Equipment deleted.', 'warning')
    return redirect(request.referrer or url_for('equipment.index'))
