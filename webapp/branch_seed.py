"""One-time branch data importer.

Loads the Subiaco setup workbook into store_id=3 without touching Mirrabooka.
The importer is idempotent through an audit_log marker so app restarts do not
overwrite later admin edits made in the web UI.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

from openpyxl import load_workbook

from packaging_routes import JACCUS_PRICE_DATA


SUBIACO_STORE_ID = 3
MARKER = 'subiaco_branch_seed_v1'
PRICE_MARKER = 'subiaco_packaging_prices_from_pdf_v1'
CONTACT_FIX_MARKER = 'subiaco_contact_fix_v1'

# Subiaco's Jaccus packaging contact (from branch.xlsx). A historic global
# UPDATE in init_packaging clobbered this with Mirrabooka's "Khoi: 0449819235"
# on every startup; _fix_subiaco_contact restores it once.
SUBIACO_PACKAGING_CONTACT = 'Kenny Ho — 0406552462 — mcqsubiaco@mcqinternational.com'
MIRRABOOKA_PACKAGING_CONTACT = 'Khoi: 0449819235'

DAYS = {
    'mon': 'mon', 'monday': 'mon',
    'tue': 'tue', 'tues': 'tue', 'tuesday': 'tue',
    'wed': 'wed', 'wednesday': 'wed',
    'thu': 'thu', 'thur': 'thu', 'thurs': 'thu', 'thursday': 'thu',
    'fri': 'fri', 'friday': 'fri',
    'sat': 'sat', 'saturday': 'sat',
    'sun': 'sun', 'sunday': 'sun',
}

VALID_CHECKLISTS = {'take_order', 'banh_mi', 'chef', 'grill_beef', 'serve_order'}
VALID_TEMP_TYPES = {'banh_mi', 'chef', 'pastry'}
VALID_KINDS = {'cold', 'room', 'hot', 'freezer'}

PREP_STATION_COLORS = {
    'banh mi station': '#FF9800',
    'pho / kitchen station': '#F44336',
    'drink station': '#00BCD4',
    'chef / general prep': '#4CAF50',
}


def _s(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value)).strip()
    return str(value).strip()


def _phone(value: Any) -> str:
    text = _s(value)
    if not text:
        return ''
    if re.fullmatch(r'\d+', text) and len(text) == 9 and text.startswith('4'):
        return '0' + text
    return text


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == '':
        return default
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r'-?\d+(?:\.\d+)?', str(value))
    return float(m.group(0)) if m else default


def _qty(value: Any) -> int:
    try:
        return max(0, int(round(_num(value, 0))))
    except Exception:
        return 0


def _bool(value: Any) -> int:
    return 1 if _s(value).lower() in {'yes', 'y', 'true', '1', 'on'} else 0


def _kind(value: Any, default: str = 'cold') -> str:
    k = _s(value).lower()
    return k if k in VALID_KINDS else default


def _days(value: Any) -> str:
    raw = _s(value).lower()
    if not raw:
        return ''
    out: list[str] = []
    for part in re.split(r'[,/; ]+', raw):
        d = DAYS.get(part.strip().lower())
        if d and d not in out:
            out.append(d)
    return ','.join(out)


def _rows(ws, header_label: str) -> list[list[Any]]:
    start = None
    for idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if _s(row[0]).lower() == header_label.lower():
            start = idx + 1
            break
    if start is None:
        return []
    data = []
    for row in ws.iter_rows(min_row=start, values_only=True):
        vals = list(row)
        if not any(_s(v) for v in vals):
            continue
        data.append(vals)
    return data


def _prep_station_id(conn: sqlite3.Connection, station_name: str) -> int:
    name = station_name.strip()
    row = conn.execute(
        'SELECT id FROM prep_stations WHERE lower(name_en)=lower(?) AND active=1',
        (name,)).fetchone()
    if row:
        return row['id']
    next_order = conn.execute(
        'SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM prep_stations'
    ).fetchone()['n']
    color = PREP_STATION_COLORS.get(name.lower(), '#607D8B')
    cur = conn.execute('''INSERT INTO prep_stations
        (name_en, name_vi, color, sort_order, active)
        VALUES (?, '', ?, ?, 1)''', (name, color, next_order))
    return cur.lastrowid


def _seed_store(conn: sqlite3.Connection) -> None:
    conn.execute('''UPDATE stores
        SET name=?, address=?, phone=?, active=1,
            user_password=?, admin_password=?, kitchen_password=?
        WHERE id=?''',
        ('Subiaco',
         '4A Seddon St, Subiaco WA 6008',
         '0406552462',
         '8888', '8888', 'banhmivietnam',
         SUBIACO_STORE_ID))


def _seed_staff(conn: sqlite3.Connection, wb) -> None:
    for row in _rows(wb['2. Staff Members'], 'Full name'):
        name = _s(row[0])
        if not name:
            continue
        conn.execute('''INSERT INTO staff_members
            (name, role, phone, email, emergency_contact, active, store_id)
            VALUES (?,?,?,?,?,1,?)
            ON CONFLICT(name, store_id) DO UPDATE SET
                role=excluded.role,
                phone=excluded.phone,
                email=excluded.email,
                emergency_contact=excluded.emergency_contact,
                active=1''',
            (name, _s(row[1]), _phone(row[2]), _s(row[3]).strip(),
             _phone(row[4]) if len(row) > 4 else '', SUBIACO_STORE_ID))


def _seed_checklists(conn: sqlite3.Connection, wb) -> None:
    conn.execute('DELETE FROM checklist_task_templates WHERE store_id=?', (SUBIACO_STORE_ID,))
    order: dict[tuple[str, str], int] = {}
    for row in _rows(wb['4. Daily Checklists'], 'Station (chk_type)'):
        chk_type, section, task = _s(row[0]), _s(row[1]).lower(), _s(row[2])
        if chk_type not in VALID_CHECKLISTS or section not in {'opening', 'closing'} or not task:
            continue
        key = (chk_type, section)
        idx = order.get(key, 0)
        conn.execute('''INSERT INTO checklist_task_templates
            (chk_type, section, task_order, task_name, store_id)
            VALUES (?,?,?,?,?)''', (chk_type, section, idx, task, SUBIACO_STORE_ID))
        order[key] = idx + 1


def _seed_temperatures(conn: sqlite3.Connection, wb) -> None:
    conn.execute('DELETE FROM temp_food_templates WHERE store_id=?', (SUBIACO_STORE_ID,))
    order: dict[str, int] = {}
    for row in _rows(wb['5. Food Temperature Items'], 'Record (banh_mi/chef/pastry)'):
        temp_type, food = _s(row[0]), _s(row[1])
        if temp_type not in VALID_TEMP_TYPES or not food:
            continue
        idx = order.get(temp_type, 0)
        conn.execute('''INSERT INTO temp_food_templates
            (temp_type, food_order, food_name, food_kind, store_id)
            VALUES (?,?,?,?,?)''',
            (temp_type, idx, food, _kind(row[2] if len(row) > 2 else None), SUBIACO_STORE_ID))
        order[temp_type] = idx + 1


def _seed_equipment(conn: sqlite3.Connection, wb) -> None:
    conn.execute('DELETE FROM equipment_temp_readings WHERE store_id=?', (SUBIACO_STORE_ID,))
    conn.execute('DELETE FROM equipment_units WHERE store_id=?', (SUBIACO_STORE_ID,))
    for idx, row in enumerate(_rows(wb['6. Equipment (Fridges)'], 'Equipment name')):
        name = _s(row[0])
        if not name:
            continue
        conn.execute('''INSERT INTO equipment_units
            (name, kind, sort_order, active, store_id)
            VALUES (?,?,?,1,?)''',
            (name, _kind(row[1] if len(row) > 1 else None), idx, SUBIACO_STORE_ID))


def _seed_prep(conn: sqlite3.Connection, wb) -> None:
    conn.execute('DELETE FROM prep_task_templates WHERE store_id=?', (SUBIACO_STORE_ID,))
    orders: dict[int, int] = {}
    for row in _rows(wb['7. Prep Timetable'], 'Station'):
        station, task = _s(row[0]), _s(row[1])
        if not station or not task:
            continue
        station_id = _prep_station_id(conn, station)
        idx = orders.get(station_id, 0)
        conn.execute('''INSERT INTO prep_task_templates
            (task_name_en, task_name_vi, station_id, default_time, active_days,
             default_assignee, is_supplier, supplier_name, sort_order, active, store_id)
            VALUES (?, '', ?, ?, ?, '', 0, '', ?, 1, ?)''',
            (task, station_id, _s(row[3]) if len(row) > 3 else '',
             _days(row[2] if len(row) > 2 else None), idx, SUBIACO_STORE_ID))
        orders[station_id] = idx + 1


def _seed_packaging(conn: sqlite3.Connection, wb) -> None:
    old_suppliers = [r['id'] for r in conn.execute(
        'SELECT id FROM packaging_suppliers WHERE store_id=?', (SUBIACO_STORE_ID,)).fetchall()]
    for sid in old_suppliers:
        conn.execute('DELETE FROM packaging_items WHERE supplier_id=?', (sid,))
    conn.execute('DELETE FROM packaging_suppliers WHERE store_id=?', (SUBIACO_STORE_ID,))

    fields = {_s(r[0]).lower(): _s(r[1]) for r in _rows(wb['8. Packaging Supplier'], 'Field') if _s(r[0])}
    cur = conn.execute('''INSERT INTO packaging_suppliers
        (name, email, phone, cc_emails, delivery_days, cafe_name, cafe_address,
         cafe_contacts, notes, active, sort_order, store_id)
        VALUES (?,?,?,?,?,?,?,?,'',1,0,?)''',
        (fields.get('supplier name', 'Jaccus Trading'),
         fields.get('order email', ''),
         fields.get('phone', ''),
         fields.get('cc email', ''),
         fields.get('delivery days', '').upper(),
         'Saigon Alley by MCQ Street Food — SUBIACO',
         fields.get('deliver-to address', '4A Seddon St, Subiaco WA 6008'),
         SUBIACO_PACKAGING_CONTACT,
         SUBIACO_STORE_ID))
    supplier_id = cur.lastrowid
    for idx, row in enumerate(_rows(wb['9. Packaging Items'], 'Product code')):
        code, en, vi = _s(row[0]), _s(row[1]), _s(row[2])
        if not (code or en):
            continue
        unit = _s(row[3] if len(row) > 3 else '') or 'carton'
        qty_val = row[4] if len(row) > 4 else None
        if isinstance(row[3] if len(row) > 3 else None, (int, float)) and not qty_val:
            qty_val, unit = row[3], 'carton'
        unit_measure, unit_price = JACCUS_PRICE_DATA.get(code, ('', 0))
        conn.execute('''INSERT INTO packaging_items
            (supplier_id, product_code, name_en, name_vi, unit, unit_measure,
             unit_price, default_qty, sort_order, active)
            VALUES (?,?,?,?,?,?,?,?,?,1)''',
            (supplier_id, code, en, vi, unit, unit_measure, unit_price, _qty(qty_val), idx))


def _sync_packaging_prices(conn: sqlite3.Connection) -> None:
    """Apply the PDF price list to Subiaco's existing Jaccus catalogue once."""
    exists = conn.execute('SELECT 1 FROM audit_log WHERE action=? LIMIT 1', (PRICE_MARKER,)).fetchone()
    if exists:
        return
    updated = 0
    for product_code, (unit_measure, unit_price) in JACCUS_PRICE_DATA.items():
        cur = conn.execute('''UPDATE packaging_items
            SET unit_measure=?, unit_price=?
            WHERE product_code=? AND active=1 AND supplier_id IN (
                SELECT id FROM packaging_suppliers
                WHERE store_id=? AND lower(name)=lower('Jaccus Trading') AND active=1
            )''', (unit_measure, unit_price, product_code, SUBIACO_STORE_ID))
        updated += cur.rowcount or 0
    conn.execute('''INSERT INTO audit_log(action, record_type, user_name, details)
        VALUES (?, 'migration', 'system', ?)''',
        (PRICE_MARKER, f'Applied Jaccus PDF price list to {updated} Subiaco packaging item(s).'))


def _fix_subiaco_contact(conn: sqlite3.Connection) -> None:
    """Restore Subiaco's Jaccus packaging contact once.

    Earlier builds clobbered every branch's contact with Mirrabooka's on each
    startup. Now that init_packaging only touches store_id=1, repair the live
    Subiaco row — but only if it is still blank or holds the clobbered Mirrabooka
    value, so genuine admin edits are never overwritten. Guarded by an audit
    marker so it runs at most once.
    """
    if conn.execute('SELECT 1 FROM audit_log WHERE action=? LIMIT 1', (CONTACT_FIX_MARKER,)).fetchone():
        return
    cur = conn.execute('''UPDATE packaging_suppliers
        SET cafe_contacts=?
        WHERE store_id=? AND lower(name)=lower('Jaccus Trading') AND active=1
          AND (COALESCE(cafe_contacts,'')='' OR cafe_contacts=?)''',
        (SUBIACO_PACKAGING_CONTACT, SUBIACO_STORE_ID, MIRRABOOKA_PACKAGING_CONTACT))
    conn.execute('''INSERT INTO audit_log(action, record_type, user_name, details)
        VALUES (?, 'migration', 'system', ?)''',
        (CONTACT_FIX_MARKER, f'Restored Subiaco Jaccus contact on {cur.rowcount or 0} row(s).'))


def _seed_pastry(conn: sqlite3.Connection, wb) -> None:
    conn.execute('DELETE FROM pastry_items WHERE store_id=?', (SUBIACO_STORE_ID,))
    conn.execute('DELETE FROM pastry_suppliers WHERE store_id=?', (SUBIACO_STORE_ID,))
    suppliers: dict[str, int] = {}

    def supplier_id(name: str) -> int | None:
        name = name.strip()
        if not name:
            return None
        key = name.lower()
        if key in suppliers:
            return suppliers[key]
        cur = conn.execute('''INSERT INTO pastry_suppliers (name, phone, active, store_id)
            VALUES (?, '', 1, ?)''', (name, SUBIACO_STORE_ID))
        suppliers[key] = cur.lastrowid
        return cur.lastrowid

    for idx, row in enumerate(_rows(wb['10. Pastry Items'], 'Name (EN)')):
        name_en, name_vi, sup = _s(row[0]), _s(row[1]), _s(row[2])
        if not name_en:
            continue
        conn.execute('''INSERT INTO pastry_items
            (name_en, name_vi, supplier_id, selling_price, cost_price, delivery_days,
             returnable, on_order_only, active, sort_order, store_id)
            VALUES (?,?,?,?,?,?,?,?,1,?,?)''',
            (name_en, name_vi, supplier_id(sup), _num(row[3] if len(row) > 3 else None),
             _num(row[4] if len(row) > 4 else None), _days(row[5] if len(row) > 5 else None),
             _bool(row[6] if len(row) > 6 else None), _bool(row[7] if len(row) > 7 else None),
             idx, SUBIACO_STORE_ID))


def _seed_structure(conn: sqlite3.Connection, wb) -> None:
    conn.execute('DELETE FROM structure_members WHERE store_id=?', (SUBIACO_STORE_ID,))
    conn.execute('DELETE FROM structure_departments WHERE store_id=?', (SUBIACO_STORE_ID,))
    conn.execute('DELETE FROM structure_meta WHERE store_id=?', (SUBIACO_STORE_ID,))
    conn.execute('''INSERT INTO structure_meta(key, value, store_id) VALUES
        ('manager_name', 'Kenny', ?),
        ('manager_title', 'MANAGER', ?),
        ('live_updated_by', 'subiaco-seed', ?),
        ('live_updated_at', datetime('now','localtime'), ?)''',
        (SUBIACO_STORE_ID, SUBIACO_STORE_ID, SUBIACO_STORE_ID, SUBIACO_STORE_ID))

    colors = ['#2F83C2', '#CF850D', '#D75435', '#1AA17E', '#96394E', '#704C31']
    departments: dict[str, int] = {}
    for row in _rows(wb['3. Staff Structure'], 'Department'):
        dept, person, level = _s(row[0]).upper(), _s(row[1]), _s(row[2]).upper()
        if not dept or dept in {'MANAGER', 'SUPERVISOR'}:
            continue
        if dept not in departments:
            cur = conn.execute('''INSERT INTO structure_departments
                (name, color, lead_name, lead_badge, sort_order, active, store_id)
                VALUES (?,?,?,?,?,1,?)''',
                (dept, colors[len(departments) % len(colors)], person,
                 level or 'LEAD', len(departments), SUBIACO_STORE_ID))
            departments[dept] = cur.lastrowid
        elif person:
            dept_id = departments[dept]
            sort_order = conn.execute('''SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
                FROM structure_members WHERE department_id=?''', (dept_id,)).fetchone()['n']
            conn.execute('''INSERT INTO structure_members
                (department_id, level_label, staff_name, sort_order, active, store_id)
                VALUES (?,?,?,?,1,?)''',
                (dept_id, level, person, sort_order, SUBIACO_STORE_ID))


def _seed_email(conn: sqlite3.Connection) -> None:
    conn.execute('INSERT OR IGNORE INTO email_settings (store_id, from_name) VALUES (?, ?)',
                 (SUBIACO_STORE_ID, 'MCQ Subiaco Notification'))
    conn.execute('''UPDATE email_settings
        SET from_name=CASE WHEN COALESCE(from_name,'')='' THEN 'MCQ Subiaco Notification' ELSE from_name END
        WHERE store_id=?''', (SUBIACO_STORE_ID,))
    conn.execute('''INSERT INTO email_recipients
        (email, name, active, notify_checklist, notify_temperature, notify_violation,
         notify_issue, notify_prep, notify_training, notify_pastry, notify_jobs, store_id)
        VALUES ('mcqsubiaco@mcqinternational.com','Subiaco Manager',1,1,1,1,1,1,1,1,1,?)
        ON CONFLICT(email, store_id) DO UPDATE SET active=1, name=excluded.name''',
        (SUBIACO_STORE_ID,))


def _annotate_cert_notes(conn: sqlite3.Connection, wb) -> None:
    cert_names = [_s(r[0]) for r in _rows(wb['11. Food Safety Certs'], 'Staff name') if _s(r[0])]
    for name in cert_names:
        conn.execute('''UPDATE staff_members
            SET staff_notes=TRIM(COALESCE(staff_notes,'') || ' Food safety certificate listed in Subiaco workbook.')
            WHERE lower(name)=lower(?) AND store_id=? AND COALESCE(staff_notes,'') NOT LIKE '%Subiaco workbook%' ''',
            (name, SUBIACO_STORE_ID))


def seed_subiaco_branch(db_path: str, workbook_path: str | None = None) -> None:
    workbook_path = workbook_path or os.path.join(os.path.dirname(__file__), 'templates', 'branch.xlsx')
    if not os.path.exists(workbook_path):
        return
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        _seed_store(conn)
        exists = conn.execute('SELECT 1 FROM audit_log WHERE action=? LIMIT 1', (MARKER,)).fetchone()
        if exists:
            _sync_packaging_prices(conn)
            _fix_subiaco_contact(conn)
            conn.commit()
            return
        wb = load_workbook(workbook_path, data_only=True)
        _seed_staff(conn, wb)
        _seed_checklists(conn, wb)
        _seed_temperatures(conn, wb)
        _seed_equipment(conn, wb)
        _seed_prep(conn, wb)
        _seed_packaging(conn, wb)
        _sync_packaging_prices(conn)
        _fix_subiaco_contact(conn)
        _seed_pastry(conn, wb)
        _seed_structure(conn, wb)
        _seed_email(conn)
        _annotate_cert_notes(conn, wb)
        conn.execute('''INSERT INTO audit_log(action, record_type, user_name, details)
            VALUES (?, 'migration', 'system', ?)''',
            (MARKER, f'Seeded Subiaco branch data from {os.path.basename(workbook_path)}.'))
        conn.commit()
    finally:
        conn.close()
