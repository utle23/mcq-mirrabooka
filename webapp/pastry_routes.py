from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps

pastry = Blueprint('pastry', __name__, url_prefix='/pastry')
DB_PATH = None

DAYS       = ['mon','tue','wed','thu','fri','sat','sun']
DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
DAY_FULL   = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

SUPPLIERS_SEED = [
    {'name': 'Mai Em',         'phone': '0411 652 199'},
    {'name': 'Tú Anh',         'phone': '0424 165 560'},
    {'name': 'Golden Bites',   'phone': '0435 156 847'},
    {'name': 'Chú Tai Yến',    'phone': '0407 153 059'},
    {'name': 'MCQ Supermarket','phone': ''},
    {'name': 'Rubi Bakery',    'phone': '0424 546 488'},
    {'name': 'Chị Kim',        'phone': '0401 792 495'},
    {'name': 'Chị Linh',       'phone': '0416 524 045'},
    {'name': 'Cha Lua VINHAM', 'phone': '0422 785 126'},
]

# (name_en, name_vi, supplier_name, sell_price, cost_price, delivery_days, returnable, on_order_only)
ITEMS_SEED = [
    ('Banh Tieu (Hollow Sesame Donut)', 'Bánh Tiêu Mè',           'Mai Em',          2.50, 2.50, 'mon,tue,wed',               0, 0),
    ('Banh Tieu Dau (Sesame Donut)',    'Bánh Tiêu Đậu',           'Mai Em',          3.50, 2.50, 'thu,fri',                   0, 0),
    ('Chao Quay (Chinese Doughnut)',    'Cháo Quẩy',               'Mai Em',          3.00, 2.00, 'mon,tue,wed,thu,fri',       0, 0),
    ('Mung Bean Sesame Ball',           'Bánh Cam Đậu Xanh',       'Tú Anh',          2.50, 1.50, 'mon,tue,wed,thu,fri,sat,sun', 1, 0),
    ('Red Bean Sesame Ball',            'Bánh Cam Đậu Đỏ',         'Golden Bites',    2.50, 1.50, 'mon,tue,wed,thu,fri,sat,sun', 1, 0),
    ('Banh Bao (Steam Bun)',            'Bánh Bao',                'Tú Anh',          5.00, 3.50, 'mon,tue,wed,thu,fri,sat,sun', 1, 0),
    ('Banh Tai Yen (Bird Nest Cake)',   'Bánh Tai Yến',            'Chú Tai Yến',     2.50, 1.40, 'thu,fri,sat,sun',           1, 0),
    ('Batiso',                          'Bánh Batiso',             'Tú Anh',          3.00, 1.50, 'mon,tue,wed,thu,fri,sat,sun', 1, 0),
    ('Fried Pork Dumpling',             'Há Cảo Chiên',            'Golden Bites',    2.50, 1.50, 'mon,tue,wed,thu,fri,sat,sun', 1, 0),
    ('Fried Banana',                    'Chuối Chiên',             'MCQ Supermarket', 3.50, 1.10, 'mon,tue,wed,thu,fri,sat,sun', 0, 0),
    ('Chao Quay (Rubi Bakery)',         'Cháo Quẩy (Rubi)',        'Rubi Bakery',     2.50, 1.50, 'sat,sun',                   0, 0),
    ('Banh Tieu (Rubi Bakery)',         'Bánh Tiêu (Rubi)',        'Rubi Bakery',     2.50, 1.50, 'thu,fri,sat,sun',           0, 0),
    ('Banh Cam (Rubi Bakery)',          'Bánh Cam (Rubi)',         'Rubi Bakery',     2.50, 1.50, 'sat,sun',                   0, 0),
    ('Meat Spring Roll',                'Chả Giò (đặt trước)',     'Chị Kim',         2.50, 1.00, '',                          0, 1),
    ('Chicken Curry Puffs (min. 300)',  'Bánh Cà Ri Gà (tối thiểu 300)', 'Chị Linh', 3.50, 1.80, '',                          0, 1),
    ('Cha Lua VINHAM (3.5kg x 10 pcs)','Chả Lụa VINHAM (3.5kg x 10 pcs)', 'Cha Lua VINHAM', 0, 0, '',                        0, 1),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

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

def _today_day():
    return DAYS[date.today().weekday()]

def _form_bool(name):
    value = request.form.get(name)
    if value is None:
        return 0
    return 1 if str(value).strip().lower() in ('1', 'true', 'yes', 'on') else 0

# ── DB Init ────────────────────────────────────────────────────────────────────

def init_pastry_tables(db_path):
    global DB_PATH
    DB_PATH = db_path
    with _get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS pastry_suppliers (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name   TEXT NOT NULL,
                phone  TEXT,
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS pastry_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name_en       TEXT NOT NULL,
                name_vi       TEXT,
                supplier_id   INTEGER REFERENCES pastry_suppliers(id),
                selling_price REAL DEFAULT 0,
                cost_price    REAL DEFAULT 0,
                delivery_days TEXT DEFAULT '',
                returnable    INTEGER DEFAULT 0,
                on_order_only INTEGER DEFAULT 0,
                active        INTEGER DEFAULT 1,
                sort_order    INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pastry_delivery (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id      INTEGER NOT NULL REFERENCES pastry_items(id),
                date         TEXT NOT NULL,
                qty_received INTEGER DEFAULT 0,
                received_by  TEXT,
                received_at  TEXT,
                condition    TEXT DEFAULT 'good',
                notes        TEXT,
                UNIQUE(item_id, date)
            );
            CREATE TABLE IF NOT EXISTS pastry_sales (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     INTEGER NOT NULL REFERENCES pastry_items(id),
                date        TEXT NOT NULL,
                qty_sold    INTEGER DEFAULT 0,
                qty_returned INTEGER DEFAULT 0,
                qty_wasted  INTEGER DEFAULT 0,
                recorded_by TEXT,
                recorded_at TEXT,
                notes       TEXT,
                UNIQUE(item_id, date)
            );
        ''')

        # Seed suppliers if empty
        if conn.execute('SELECT COUNT(*) as c FROM pastry_suppliers').fetchone()['c'] == 0:
            for s in SUPPLIERS_SEED:
                conn.execute('INSERT INTO pastry_suppliers (name, phone) VALUES (?,?)',
                             (s['name'], s['phone']))

        # Migrations: update/add specific suppliers
        conn.execute("UPDATE pastry_suppliers SET phone='0407 153 059' WHERE name='Chú Tai Yến' AND (phone='' OR phone IS NULL)")
        if not conn.execute("SELECT 1 FROM pastry_suppliers WHERE name='Cha Lua VINHAM'").fetchone():
            conn.execute("INSERT INTO pastry_suppliers (name, phone) VALUES ('Cha Lua VINHAM','0422 785 126')")

        # Seed items if empty
        if conn.execute('SELECT COUNT(*) as c FROM pastry_items').fetchone()['c'] == 0:
            for i, item in enumerate(ITEMS_SEED):
                sup = conn.execute('SELECT id FROM pastry_suppliers WHERE name=?', (item[2],)).fetchone()
                sup_id = sup['id'] if sup else None
                conn.execute('''INSERT INTO pastry_items
                    (name_en, name_vi, supplier_id, selling_price, cost_price,
                     delivery_days, returnable, on_order_only, sort_order)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (item[0], item[1], sup_id, item[3], item[4], item[5], item[6], item[7], i))

        # Migration: add Cha Lua VINHAM item if not exists (for existing DBs)
        if not conn.execute("SELECT 1 FROM pastry_items WHERE name_en LIKE 'Cha Lua VINHAM%'").fetchone():
            sup = conn.execute("SELECT id FROM pastry_suppliers WHERE name='Cha Lua VINHAM'").fetchone()
            if sup:
                max_order = conn.execute('SELECT COALESCE(MAX(sort_order),0) FROM pastry_items').fetchone()[0]
                conn.execute('''INSERT INTO pastry_items
                    (name_en, name_vi, supplier_id, selling_price, cost_price,
                     delivery_days, returnable, on_order_only, sort_order)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    ('Cha Lua VINHAM (3.5kg x 10 pcs)', 'Chả Lụa VINHAM (3.5kg x 10 pcs)',
                     sup['id'], 0, 0, '', 0, 1, max_order + 1))

def _get_items_with_supplier(conn, active_only=True):
    q = '''SELECT i.*, s.name as supplier_name, s.phone as supplier_phone
           FROM pastry_items i LEFT JOIN pastry_suppliers s ON s.id=i.supplier_id'''
    if active_only:
        q += ' WHERE i.active=1'
    q += ' ORDER BY i.sort_order, i.id'
    rows = [dict(r) for r in conn.execute(q).fetchall()]
    for r in rows:
        r['margin']   = round((r['selling_price'] or 0) - (r['cost_price'] or 0), 2)
        r['margin_pct']= round(r['margin'] / r['selling_price'] * 100, 1) if r['selling_price'] else 0
        r['days_list'] = (r['delivery_days'] or '').split(',') if r['delivery_days'] else []
    return rows

# ── Routes ─────────────────────────────────────────────────────────────────────

@pastry.route('/')
@_login_required
def pastry_index():
    return redirect(url_for('pastry.pastry_today'))

@pastry.route('/today')
@_login_required
def pastry_today():
    today     = date.today().isoformat()
    today_day = _today_day()
    with _get_db() as conn:
        items = _get_items_with_supplier(conn)
        for item in items:
            item['today_scheduled'] = today_day in item['days_list']
        # Group by supplier (all items)
        by_supplier = {}
        for item in items:
            sname = item['supplier_name'] or 'Other'
            by_supplier.setdefault(sname, {'phone': item['supplier_phone'] or '', 'items': []})
            by_supplier[sname]['items'].append(item)

    today_label = DAY_FULL[DAYS.index(today_day)]
    today_count = sum(1 for i in items if i['today_scheduled'])
    return render_template('pastry_today.html',
        today=today, today_day=today_day,
        today_day_label=today_label,
        by_supplier=by_supplier, items=items,
        today_count=today_count,
        days=DAYS, day_labels=DAY_LABELS,
        is_admin=_is_admin())

@pastry.route('/delivery/<int:item_id>/receive', methods=['POST'])
@_login_required
def record_delivery(item_id):
    today       = date.today().isoformat()
    qty         = int(request.form.get('qty_received', 0) or 0)
    condition   = request.form.get('condition', 'good')
    notes       = request.form.get('notes', '').strip()
    received_by = request.form.get('received_by', '').strip()
    now         = datetime.now().strftime('%Y-%m-%d %H:%M')
    with _get_db() as conn:
        conn.execute('''INSERT INTO pastry_delivery
            (item_id, date, qty_received, received_by, received_at, condition, notes)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(item_id, date) DO UPDATE SET
            qty_received=excluded.qty_received,
            received_by=excluded.received_by,
            received_at=excluded.received_at,
            condition=excluded.condition,
            notes=excluded.notes''',
            (item_id, today, qty, received_by, now, condition, notes))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'qty': qty})
    return redirect(url_for('pastry.pastry_today'))

@pastry.route('/sales/<int:item_id>/record', methods=['POST'])
@_login_required
def record_sales(item_id):
    today       = date.today().isoformat()
    qty_sold    = int(request.form.get('qty_sold', 0) or 0)
    qty_returned= int(request.form.get('qty_returned', 0) or 0)
    qty_wasted  = int(request.form.get('qty_wasted', 0) or 0)
    notes       = request.form.get('notes', '').strip()
    recorded_by = request.form.get('recorded_by', '').strip()
    now         = datetime.now().strftime('%Y-%m-%d %H:%M')
    with _get_db() as conn:
        conn.execute('''INSERT INTO pastry_sales
            (item_id, date, qty_sold, qty_returned, qty_wasted, recorded_by, recorded_at, notes)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id, date) DO UPDATE SET
            qty_sold=excluded.qty_sold,
            qty_returned=excluded.qty_returned,
            qty_wasted=excluded.qty_wasted,
            recorded_by=excluded.recorded_by,
            recorded_at=excluded.recorded_at,
            notes=excluded.notes''',
            (item_id, today, qty_sold, qty_returned, qty_wasted, recorded_by, now, notes))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('pastry.pastry_today'))

@pastry.route('/weekly')
@_login_required
def pastry_weekly():
    today = date.today()
    with _get_db() as conn:
        items = _get_items_with_supplier(conn)
        by_supplier = {}
        for item in items:
            sname = item['supplier_name'] or 'Other'
            by_supplier.setdefault(sname, {'phone': item['supplier_phone'] or '', 'items': []})
            by_supplier[sname]['items'].append(item)

    return render_template('pastry_weekly.html',
        items=items, by_supplier=by_supplier,
        days=DAYS, day_labels=DAY_LABELS, day_full=DAY_FULL,
        today=today.isoformat(), today_day=_today_day(),
        is_admin=_is_admin())

@pastry.route('/dashboard')
@_admin_required
def pastry_dashboard():
    today     = date.today().isoformat()
    today_day = _today_day()
    ws        = date.today() - timedelta(days=date.today().weekday())
    week_dates= [(ws + timedelta(days=i)).isoformat() for i in range(7)]

    with _get_db() as conn:
        items = _get_items_with_supplier(conn)
        # Today stats
        today_del = conn.execute('''
            SELECT SUM(d.qty_received) as total_recv,
                   COUNT(DISTINCT d.item_id) as items_recv
            FROM pastry_delivery d WHERE d.date=?''', (today,)).fetchone()
        today_sales = conn.execute('''
            SELECT SUM(s.qty_sold) as total_sold,
                   SUM(s.qty_sold * i.selling_price) as revenue,
                   SUM(s.qty_sold * i.margin_calc) as profit
            FROM pastry_sales s
            JOIN (SELECT id, selling_price, (selling_price-cost_price) as margin_calc FROM pastry_items) i
            ON i.id=s.item_id WHERE s.date=?''', (today,)).fetchone()

        # Week stats
        week_sales = conn.execute('''
            SELECT SUM(s.qty_sold) as total_sold,
                   SUM(s.qty_sold * i.selling_price) as revenue,
                   SUM(s.qty_sold * i.margin_calc) as profit
            FROM pastry_sales s
            JOIN (SELECT id, selling_price, (selling_price-cost_price) as margin_calc FROM pastry_items) i
            ON i.id=s.item_id WHERE s.date>=? AND s.date<=?''',
            (week_dates[0], week_dates[-1])).fetchone()

        # Per-item performance this week
        item_perf = conn.execute('''
            SELECT i.id, i.name_en, i.selling_price, i.cost_price,
                   (i.selling_price-i.cost_price) as margin,
                   s.name as supplier_name,
                   COALESCE(SUM(sl.qty_sold),0) as week_sold,
                   COALESCE(SUM(sl.qty_returned),0) as week_returned,
                   COALESCE(SUM(sl.qty_sold*(i.selling_price-i.cost_price)),0) as week_profit,
                   COALESCE(SUM(sl.qty_sold*i.selling_price),0) as week_revenue
            FROM pastry_items i
            LEFT JOIN pastry_suppliers s ON s.id=i.supplier_id
            LEFT JOIN pastry_sales sl ON sl.item_id=i.id AND sl.date>=? AND sl.date<=?
            WHERE i.active=1
            GROUP BY i.id ORDER BY week_revenue DESC''',
            (week_dates[0], week_dates[-1])).fetchall()
        item_perf = [dict(r) for r in item_perf]

        # Recent deliveries
        recent_del = conn.execute('''
            SELECT d.*, i.name_en, s.name as supplier_name
            FROM pastry_delivery d
            JOIN pastry_items i ON i.id=d.item_id
            LEFT JOIN pastry_suppliers s ON s.id=i.supplier_id
            WHERE d.date>=? ORDER BY d.received_at DESC LIMIT 20''',
            (week_dates[0],)).fetchall()

    return render_template('pastry_dashboard.html',
        today=today, today_day=today_day,
        today_del=dict(today_del) if today_del else {},
        today_sales=dict(today_sales) if today_sales else {},
        week_sales=dict(week_sales) if week_sales else {},
        item_perf=item_perf,
        recent_del=[dict(r) for r in recent_del],
        week_start=week_dates[0], week_end=week_dates[-1],
        is_admin=True)

@pastry.route('/items')
@_admin_required
def pastry_items_view():
    with _get_db() as conn:
        items     = _get_items_with_supplier(conn, active_only=False)
        suppliers = [dict(r) for r in conn.execute(
            'SELECT * FROM pastry_suppliers WHERE active=1 ORDER BY name').fetchall()]
    return render_template('pastry_items.html',
        items=items, suppliers=suppliers,
        days=DAYS, day_labels=DAY_LABELS, is_admin=True)

@pastry.route('/items/add', methods=['POST'])
@_admin_required
def add_item():
    delivery_days = ','.join(request.form.getlist('delivery_days'))
    with _get_db() as conn:
        conn.execute('''INSERT INTO pastry_items
            (name_en, name_vi, supplier_id, selling_price, cost_price,
             delivery_days, returnable, on_order_only)
            VALUES (?,?,?,?,?,?,?,?)''',
            (request.form.get('name_en','').strip(),
             request.form.get('name_vi','').strip(),
             request.form.get('supplier_id') or None,
             float(request.form.get('selling_price',0) or 0),
             float(request.form.get('cost_price',0) or 0),
             delivery_days,
             _form_bool('returnable'),
             _form_bool('on_order_only')))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('pastry.pastry_items_view'))

@pastry.route('/items/<int:iid>/edit', methods=['POST'])
@_admin_required
def edit_item(iid):
    delivery_days = ','.join(request.form.getlist('delivery_days'))
    with _get_db() as conn:
        conn.execute('''UPDATE pastry_items SET
            name_en=?, name_vi=?, supplier_id=?, selling_price=?, cost_price=?,
            delivery_days=?, returnable=?, on_order_only=? WHERE id=?''',
            (request.form.get('name_en','').strip(),
             request.form.get('name_vi','').strip(),
             request.form.get('supplier_id') or None,
             float(request.form.get('selling_price',0) or 0),
             float(request.form.get('cost_price',0) or 0),
             delivery_days,
             _form_bool('returnable'),
             _form_bool('on_order_only'), iid))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('pastry.pastry_items_view'))

@pastry.route('/items/<int:iid>/toggle', methods=['POST'])
@_admin_required
def toggle_item(iid):
    with _get_db() as conn:
        row = conn.execute('SELECT active FROM pastry_items WHERE id=?', (iid,)).fetchone()
        if row:
            conn.execute('UPDATE pastry_items SET active=? WHERE id=?',
                         (0 if row['active'] else 1, iid))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('pastry.pastry_items_view'))

@pastry.route('/suppliers/add', methods=['POST'])
@_admin_required
def add_supplier():
    with _get_db() as conn:
        conn.execute('INSERT INTO pastry_suppliers (name, phone) VALUES (?,?)',
                     (request.form.get('name','').strip(),
                      request.form.get('phone','').strip()))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('pastry.pastry_items_view'))

@pastry.route('/suppliers/<int:sid>/edit', methods=['POST'])
@_admin_required
def edit_supplier(sid):
    with _get_db() as conn:
        conn.execute('UPDATE pastry_suppliers SET name=?, phone=? WHERE id=?',
                     (request.form.get('name','').strip(),
                      request.form.get('phone','').strip(), sid))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(url_for('pastry.pastry_items_view'))
