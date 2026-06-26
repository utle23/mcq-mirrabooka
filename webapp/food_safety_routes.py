"""Food-safety operational logs — store-scoped HACCP records.

Two screens, mirroring the shop's paper forms:

* Defrosting Temperature Monitoring (/defrost) — one record per product being
  thawed (e.g. "Pate" from "MCQ Food") with a running list of daily core-temp
  checks. Core temp must stay in the fridge range 0–5 °C.
* Delivery Inspection Record (/delivery) — a flat log, one row per received
  product, with an ad-hoc (editable) receiving date.

Every table carries store_id so Mirrabooka / Morley / Subiaco data stay separate.
Reads/writes use the session store via store_scope; by-id mutations are guarded so
one store can never touch another store's row.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify)
import sqlite3
from datetime import datetime, date
from functools import wraps

from store_scope import current_store_id, store_guard_clause

try:
    import email_service
except Exception:
    email_service = None

defrost_bp  = Blueprint('defrost',  __name__, url_prefix='/defrost')
delivery_bp = Blueprint('delivery', __name__, url_prefix='/delivery')
DB_PATH = None

# Defrosting must happen in the fridge — safe core-temperature window.
DEFROST_LO, DEFROST_HI = 0.0, 5.0
DEFAULT_SUPPLIER = 'MCQ Food'

# Fixed food-safety notes shown on the defrosting screen (from the paper form).
DEFROST_NOTES = [
    'Use within 48 hours after fully defrosted.',
    'Do not refreeze after thawing.',
    'Always defrost food in the fridge (0–5 °C).',
    'Discard any unused portions according to food waste procedures.',
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
    """Active staff for the current store (used in the 'Checked by' pickers)."""
    try:
        with _get_db() as conn:
            rows = conn.execute(
                'SELECT name FROM staff_members WHERE active=1 AND store_id=? ORDER BY name',
                (current_store_id(),)).fetchall()
            return [r['name'] for r in rows]
    except Exception:
        return []


def _float_or_none(value):
    raw = (value or '').strip()
    if raw == '':
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _fmt_date(iso):
    """ISO date -> 'Tue 23/06/2026' for display; pass through if unparseable."""
    try:
        return datetime.strptime(iso, '%Y-%m-%d').strftime('%a %d/%m/%Y')
    except Exception:
        return iso or ''


def _notify(kind, subject, lines, link_path, actor):
    if not email_service:
        return
    try:
        email_service.send_notification(kind, subject=subject, lines=lines,
                                        link_path=link_path, actor=actor)
    except Exception:
        pass


def init_food_safety_tables(db_path):
    global DB_PATH
    DB_PATH = db_path
    with _get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS defrost_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id      INTEGER NOT NULL DEFAULT 1,
                product_name  TEXT NOT NULL,
                supplier_name TEXT NOT NULL DEFAULT 'MCQ Food',
                started_on    TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'active',   -- active | finished | discarded
                notes         TEXT NOT NULL DEFAULT '',
                created_by    TEXT NOT NULL DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS defrost_checks (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id          INTEGER NOT NULL REFERENCES defrost_records(id) ON DELETE CASCADE,
                checked_on         TEXT NOT NULL DEFAULT '',
                checked_time       TEXT NOT NULL DEFAULT '',
                core_temp          REAL,
                physical_condition TEXT NOT NULL DEFAULT 'Ready to use',
                checked_by         TEXT NOT NULL DEFAULT '',
                created_at         TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS delivery_inspections (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id            INTEGER NOT NULL DEFAULT 1,
                supplier_name       TEXT NOT NULL DEFAULT 'MCQ Food',
                receiving_date      TEXT NOT NULL DEFAULT '',
                receiving_time      TEXT NOT NULL DEFAULT '',
                product_name        TEXT NOT NULL DEFAULT '',
                quantity            TEXT NOT NULL DEFAULT '',
                delivery_temp       REAL,
                use_by_date         TEXT NOT NULL DEFAULT '',
                product_condition   TEXT NOT NULL DEFAULT '',
                packaging_condition TEXT NOT NULL DEFAULT '',
                accepted            TEXT NOT NULL DEFAULT 'accepted',  -- accepted | rejected
                note                TEXT NOT NULL DEFAULT '',
                checked_by          TEXT NOT NULL DEFAULT '',
                created_at          TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_defrost_store_status
                ON defrost_records(store_id, status);
            CREATE INDEX IF NOT EXISTS idx_defrost_checks_record
                ON defrost_checks(record_id);
            CREATE INDEX IF NOT EXISTS idx_delivery_store_date
                ON delivery_inspections(store_id, receiving_date);
        ''')


# ── Defrosting Temperature Monitoring ────────────────────────────────────────

def _defrost_temp_unsafe(temp):
    return temp is not None and (temp < DEFROST_LO or temp > DEFROST_HI)


@defrost_bp.route('/')
@_login_required
def defrost_home():
    sid = current_store_id()
    with _get_db() as conn:
        records = [dict(r) for r in conn.execute(
            '''SELECT * FROM defrost_records WHERE store_id=?
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                        started_on DESC, id DESC''', (sid,)).fetchall()]
        checks_by_record = {}
        for r in records:
            checks_by_record[r['id']] = [dict(c) for c in conn.execute(
                '''SELECT * FROM defrost_checks WHERE record_id=?
                   ORDER BY checked_on, checked_time, id''', (r['id'],)).fetchall()]
    for r in records:
        r['checks'] = checks_by_record.get(r['id'], [])
    active = [r for r in records if r['status'] == 'active']
    closed = [r for r in records if r['status'] != 'active']
    return render_template('defrost.html',
        active=active, closed=closed,
        notes=DEFROST_NOTES, staff=_get_staff(),
        today=date.today().isoformat(), now_time=datetime.now().strftime('%H:%M'),
        lo=DEFROST_LO, hi=DEFROST_HI, default_supplier=DEFAULT_SUPPLIER,
        is_admin=_is_admin())


@defrost_bp.route('/add', methods=['POST'])
@_login_required
def defrost_add():
    product = request.form.get('product_name', '').strip()
    if not product:
        return redirect(url_for('defrost.defrost_home'))
    supplier   = request.form.get('supplier_name', '').strip() or DEFAULT_SUPPLIER
    started_on = (request.form.get('started_on') or date.today().isoformat()).strip()
    created_by = request.form.get('created_by', '').strip()
    notes      = request.form.get('notes', '').strip()
    with _get_db() as conn:
        conn.execute('''INSERT INTO defrost_records
            (store_id, product_name, supplier_name, started_on, created_by, notes)
            VALUES (?,?,?,?,?,?)''',
            (current_store_id(), product, supplier, started_on, created_by, notes))
    return redirect(url_for('defrost.defrost_home'))


@defrost_bp.route('/<int:rid>/check', methods=['POST'])
@_login_required
def defrost_check(rid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        rec = conn.execute(
            f'SELECT * FROM defrost_records WHERE id=? AND {guard}', [rid] + gp).fetchone()
        if not rec:
            return redirect(url_for('defrost.defrost_home'))
        checked_on   = (request.form.get('checked_on') or date.today().isoformat()).strip()
        checked_time = request.form.get('checked_time', '').strip()
        core_temp    = _float_or_none(request.form.get('core_temp'))
        condition    = request.form.get('physical_condition', '').strip() or 'Ready to use'
        checked_by   = request.form.get('checked_by', '').strip()
        conn.execute('''INSERT INTO defrost_checks
            (record_id, checked_on, checked_time, core_temp, physical_condition, checked_by)
            VALUES (?,?,?,?,?,?)''',
            (rid, checked_on, checked_time, core_temp, condition, checked_by))
    if _defrost_temp_unsafe(core_temp):
        _notify('temperature',
            f'⚠️ Defrost temperature out of range — {rec["product_name"]} ({core_temp:g}°C)',
            [f'Product: {rec["product_name"]}',
             f'Supplier: {rec["supplier_name"]}',
             f'Core temperature: {core_temp:g}°C (safe {DEFROST_LO:g}–{DEFROST_HI:g}°C)',
             f'Checked: {checked_on} {checked_time}'.strip(),
             f'Physical condition: {condition}'],
            link_path='/defrost/', actor=checked_by)
    return redirect(url_for('defrost.defrost_home'))


@defrost_bp.route('/<int:rid>/status', methods=['POST'])
@_login_required
def defrost_status(rid):
    status = request.form.get('status', '').strip()
    if status not in ('active', 'finished', 'discarded'):
        return redirect(url_for('defrost.defrost_home'))
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'UPDATE defrost_records SET status=? WHERE id=? AND {guard}',
                     [status, rid] + gp)
    return redirect(url_for('defrost.defrost_home'))


@defrost_bp.route('/<int:rid>/delete', methods=['POST'])
@_admin_required
def defrost_delete(rid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'DELETE FROM defrost_records WHERE id=? AND {guard}', [rid] + gp)
    return redirect(url_for('defrost.defrost_home'))


@defrost_bp.route('/check/<int:cid>/delete', methods=['POST'])
@_admin_required
def defrost_check_delete(cid):
    guard, gp = store_guard_clause('r')
    with _get_db() as conn:
        conn.execute(
            f'''DELETE FROM defrost_checks WHERE id=? AND record_id IN (
                    SELECT r.id FROM defrost_records r WHERE {guard})''',
            [cid] + gp)
    return redirect(url_for('defrost.defrost_home'))


# ── Delivery Inspection Record ───────────────────────────────────────────────

def _delivery_form_values():
    return dict(
        supplier_name=request.form.get('supplier_name', '').strip() or DEFAULT_SUPPLIER,
        receiving_date=(request.form.get('receiving_date') or date.today().isoformat()).strip(),
        receiving_time=request.form.get('receiving_time', '').strip(),
        product_name=request.form.get('product_name', '').strip(),
        quantity=request.form.get('quantity', '').strip(),
        delivery_temp=_float_or_none(request.form.get('delivery_temp')),
        use_by_date=request.form.get('use_by_date', '').strip(),
        product_condition=request.form.get('product_condition', '').strip(),
        packaging_condition=request.form.get('packaging_condition', '').strip(),
        accepted='rejected' if request.form.get('accepted', '').strip().lower() == 'rejected' else 'accepted',
        note=request.form.get('note', '').strip(),
        checked_by=request.form.get('checked_by', '').strip(),
    )


@delivery_bp.route('/')
@_login_required
def delivery_home():
    sid = current_store_id()
    with _get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            '''SELECT * FROM delivery_inspections WHERE store_id=?
               ORDER BY receiving_date DESC, id DESC''', (sid,)).fetchall()]
    return render_template('delivery.html',
        rows=rows, staff=_get_staff(),
        today=date.today().isoformat(), now_time=datetime.now().strftime('%H:%M'),
        default_supplier=DEFAULT_SUPPLIER, hi=DEFROST_HI, is_admin=_is_admin())


@delivery_bp.route('/add', methods=['POST'])
@_login_required
def delivery_add():
    v = _delivery_form_values()
    if not v['product_name']:
        return redirect(url_for('delivery.delivery_home'))
    with _get_db() as conn:
        conn.execute('''INSERT INTO delivery_inspections
            (store_id, supplier_name, receiving_date, receiving_time, product_name,
             quantity, delivery_temp, use_by_date, product_condition,
             packaging_condition, accepted, note, checked_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (current_store_id(), v['supplier_name'], v['receiving_date'], v['receiving_time'],
             v['product_name'], v['quantity'], v['delivery_temp'], v['use_by_date'],
             v['product_condition'], v['packaging_condition'], v['accepted'],
             v['note'], v['checked_by']))
    if v['accepted'] == 'rejected' or (v['delivery_temp'] is not None and v['delivery_temp'] > DEFROST_HI):
        temp_txt = f"{v['delivery_temp']:g}°C" if v['delivery_temp'] is not None else '-'
        _notify('temperature',
            f'⚠️ Delivery flagged — {v["product_name"]} ({v["accepted"].upper()})',
            [f'Supplier: {v["supplier_name"]}',
             f'Product: {v["product_name"]}  ·  Qty: {v["quantity"] or "-"}',
             f'Received: {v["receiving_date"]} {v["receiving_time"]}'.strip(),
             f'Delivery temp: {temp_txt}',
             f'Product condition: {v["product_condition"] or "-"}',
             f'Packaging: {v["packaging_condition"] or "-"}',
             f'Decision: {v["accepted"].upper()}',
             f'Note: {v["note"] or "-"}'],
            link_path='/delivery/', actor=v['checked_by'])
    return redirect(url_for('delivery.delivery_home'))


@delivery_bp.route('/<int:iid>/edit', methods=['POST'])
@_login_required
def delivery_edit(iid):
    v = _delivery_form_values()
    if not v['product_name']:
        return redirect(url_for('delivery.delivery_home'))
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'''UPDATE delivery_inspections SET
            supplier_name=?, receiving_date=?, receiving_time=?, product_name=?,
            quantity=?, delivery_temp=?, use_by_date=?, product_condition=?,
            packaging_condition=?, accepted=?, note=?, checked_by=?
            WHERE id=? AND {guard}''',
            [v['supplier_name'], v['receiving_date'], v['receiving_time'], v['product_name'],
             v['quantity'], v['delivery_temp'], v['use_by_date'], v['product_condition'],
             v['packaging_condition'], v['accepted'], v['note'], v['checked_by'], iid] + gp)
    return redirect(url_for('delivery.delivery_home'))


@delivery_bp.route('/<int:iid>/delete', methods=['POST'])
@_admin_required
def delivery_delete(iid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'DELETE FROM delivery_inspections WHERE id=? AND {guard}', [iid] + gp)
    return redirect(url_for('delivery.delivery_home'))
