"""Restaurant Internal Order Management.

Phone-order taking + Kitchen Display System, integrated as a blueprint into the
existing MCQ app. Reuses the shared login/session, branches and staff list.

Roles (from session['role']):
  admin   – full control (manage menu, prices, cancel/restore, reports)
  user    – "order staff": create/edit orders, view kitchen + history
  kitchen – kitchen display only: change status, mark completed
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify, flash)
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps

from store_scope import current_store_id, store_filter_clause, store_guard_clause

try:
    import email_service
except Exception:
    email_service = None

orders = Blueprint('orders', __name__, url_prefix='/orders')
DB_PATH = None

STATUSES = ['confirmed', 'preparing', 'ready', 'completed', 'cancelled']
STATUS_LABELS = {
    'confirmed': 'Confirmed', 'preparing': 'Preparing', 'ready': 'Ready',
    'completed': 'Completed', 'cancelled': 'Cancelled',
}
STATUS_COLORS = {
    'confirmed': '#2196F3',   # blue
    'preparing': '#FB8C00',   # orange
    'ready':     '#43A047',   # green
    'completed': '#9E9E9E',   # grey
    'cancelled': '#E53935',   # red
}
# Active statuses that belong on the kitchen board
KITCHEN_STATUSES = ('confirmed', 'preparing', 'ready')

PAYMENT_METHODS = {
    'bank':   'Pay via Bank Transfer',
    'pickup': 'Pay when Pickup',
}

# (category, item_name, price)  — seeded from Menu_Full.docx
MENU_SEED = [
    # Pho Noodle Soup
    ('Pho Noodle Soup', 'Raw Beef Pho', 14.0),
    ('Pho Noodle Soup', 'Raw Beef and Beef Balls Pho', 16.0),
    ('Pho Noodle Soup', 'Beef Brisket Pho', 16.0),
    ('Pho Noodle Soup', 'Slow-Cooked Beef Rib Pho', 17.0),
    ('Pho Noodle Soup', 'MCQ Special Pho', 17.0),
    ('Pho Noodle Soup', 'Chicken Pho', 15.0),
    ('Pho Noodle Soup', 'Beef Balls Pho', 15.0),
    ('Pho Noodle Soup', 'Bun Bo Hue', 16.0),
    # Pho Cups
    ('Pho Cups', 'Beef Pho Cup', 12.0),
    ('Pho Cups', 'Chicken Pho Cup', 12.0),
    ('Pho Cups', 'Bun Bo Hue Cup', 13.0),
    # Pho Combo
    ('Pho Combo', 'Pho Combo (Pho + Coffee + Juice)', 20.0),
    # Rice Dishes
    ('Rice Dishes', 'Grilled Pork Chop with Broken Rice', 17.0),
    ('Rice Dishes', 'Roast Pork Rice', 16.0),
    ('Rice Dishes', 'Grilled Chicken Rice', 15.0),
    ('Rice Dishes', 'Grilled Beef Rice', 15.0),
    ('Rice Dishes', 'Stir-Fried Tofu Rice', 15.0),
    # Sizzling Hot Plates
    ('Sizzling Hot Plates', 'Chicken Sizzling Hot Plate', 15.0),
    ('Sizzling Hot Plates', 'Beef Sizzling Hot Plate', 16.0),
    ('Sizzling Hot Plates', 'Pork Sizzling Hot Plate', 15.0),
    ('Sizzling Hot Plates', 'Tofu Sizzling Hot Plate', 15.0),
    ('Sizzling Hot Plates', 'MCQ Sizzling Beef with Bread', 17.0),
    # Dry Noodles
    ('Dry Noodles', 'Roast Pork Dry Noodles', 15.0),
    ('Dry Noodles', 'Grilled Lemongrass Chicken Dry Noodles', 15.0),
    ('Dry Noodles', 'Grilled Lemongrass Beef Dry Noodles', 15.0),
    ('Dry Noodles', 'Grilled Lemongrass Pork Dry Noodles', 15.0),
    ('Dry Noodles', 'Stir-Fried Tofu Dry Noodles', 15.0),
    # Banh Mi
    ('Banh Mi', 'Roast Pork Banh Mi', 11.0),
    ('Banh Mi', 'Traditional Pork Banh Mi', 10.0),
    ('Banh Mi', 'Grilled Chicken Banh Mi', 11.0),
    ('Banh Mi', 'Grilled Pork Banh Mi', 11.0),
    ('Banh Mi', 'Grilled Beef Banh Mi', 12.0),
    ('Banh Mi', 'Banh Mi with Coffee', 10.0),
    ('Banh Mi', 'Banh Mi with Juice', 8.0),
    # Rice Paper Rolls
    ('Rice Paper Rolls', 'Chicken Rice Paper Roll', 7.0),
    ('Rice Paper Rolls', 'Prawn and Pork Rice Paper Roll', 7.0),
    ('Rice Paper Rolls', 'Grilled Beef Rice Paper Roll', 8.0),
    # Mixed Juices
    ('Mixed Juices', 'Detox Juice', 7.0),
    ('Mixed Juices', 'Immunity Juice', 7.0),
    ('Mixed Juices', 'Sweet Beets Juice', 7.0),
    ('Mixed Juices', 'Green Glow Juice', 7.0),
    ('Mixed Juices', 'Tropical Juice', 7.0),
    ('Mixed Juices', 'Sugarcane Juice', 7.0),
    # Smoothies
    ('Smoothies', 'Avocado Smoothie', 9.0),
    ('Smoothies', 'Strawberry Smoothie', 9.0),
    ('Smoothies', 'Mango Smoothie', 9.0),
    ('Smoothies', 'Mixed Berry Smoothie', 9.0),
    ('Smoothies', 'Coconut Smoothie', 9.0),
    # Vietnamese Coffee
    ('Vietnamese Coffee', 'Black Coffee', 7.0),
    ('Vietnamese Coffee', 'Milk Coffee', 8.0),
    # Lemonade Drinks
    ('Lemonade Drinks', 'Kiwi Lemonade', 8.0),
    ('Lemonade Drinks', 'Strawberry Lemonade', 8.0),
    ('Lemonade Drinks', 'Watermelon Lemonade', 8.0),
    ('Lemonade Drinks', 'Coconut Lemonade', 8.0),
    ('Lemonade Drinks', 'Pineapple Lemonade', 8.0),
    ('Lemonade Drinks', 'Aloe Vera Lemonade', 8.0),
    # Desserts
    ('Desserts', 'Thai Dessert (Che Thai)', 8.0),
    ('Desserts', 'Red Bean Dessert', 7.0),
    ('Desserts', 'Coconut Milk Dessert', 6.0),
    # Pastries  (prices per current Pastry Menu)
    ('Pastries', 'Banh Tieu', 3.0),
    ('Pastries', 'Chao Quay', 3.0),
    ('Pastries', 'Mung Bean Sesame Ball', 3.0),
    ('Pastries', 'Red Bean Sesame Ball', 3.0),
    ('Pastries', 'Banh Bao', 5.5),
    ('Pastries', 'Sticky Rice with Chicken', 8.0),
    ('Pastries', 'Banh Tai Yen', 2.5),
    ('Pastries', 'Spring Roll', 2.5),
    ('Pastries', 'Beef Samosa', 3.0),
    ('Pastries', 'Chicken Curry Puff', 3.0),
    ('Pastries', 'Fried Pork Dumpling', 3.0),
    ('Pastries', 'Fried Banana', 3.0),
    ('Pastries', 'Pork Pateso', 3.0),
]
CATEGORY_ORDER = [
    'Pho Noodle Soup', 'Pho Cups', 'Pho Combo', 'Rice Dishes',
    'Sizzling Hot Plates', 'Dry Noodles', 'Banh Mi', 'Rice Paper Rolls',
    'Mixed Juices', 'Smoothies', 'Vietnamese Coffee', 'Lemonade Drinks',
    'Desserts', 'Pastries', 'Catering',
]
DEFAULT_CATERING_PRICE = 3.0

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

def _staff_required(f):
    """Order-taking screens: admin or order staff (not kitchen-only)."""
    @wraps(f)
    def d(*a, **kw):
        if not session.get('logged_in'):
            return redirect(url_for('login_page'))
        if session.get('role') == 'kitchen':
            return render_template('access_denied.html'), 403
        return f(*a, **kw)
    return d

def _get_staff():
    try:
        with _get_db() as conn:
            rows = conn.execute(
                'SELECT name FROM staff_members WHERE active=1 AND store_id=? ORDER BY name',
                (current_store_id(),)).fetchall()
            return [r['name'] for r in rows]
    except Exception:
        return []

def _actor():
    return (session.get('staff_name') or session.get('role') or 'staff')

def _get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM order_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default

def _set_setting(conn, key, value):
    conn.execute('''INSERT INTO order_settings(key, value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                 (key, str(value)))

def _catering_price(conn):
    try:
        return float(_get_setting(conn, 'catering_box_price', DEFAULT_CATERING_PRICE))
    except (TypeError, ValueError):
        return DEFAULT_CATERING_PRICE

def _grace_minutes(conn):
    try:
        return int(_get_setting(conn, 'kitchen_grace_minutes', 60))
    except (TypeError, ValueError):
        return 60

def _order_code(order_id):
    return f'#{1000 + int(order_id)}'

def _pickup_dt(o):
    """Parse pickup_date + pickup_time into a datetime; None if unparseable."""
    try:
        return datetime.strptime(f"{o['pickup_date']} {o['pickup_time']}", '%Y-%m-%d %H:%M')
    except Exception:
        return None

# ── DB Init ──────────────────────────────────────────────────────────────────

def init_order_tables(db_path):
    global DB_PATH
    DB_PATH = db_path
    with _get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS order_categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                active     INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS order_menu_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                name        TEXT NOT NULL,
                price       REAL DEFAULT 0,
                is_catering INTEGER DEFAULT 0,
                active      INTEGER DEFAULT 1,
                sort_order  INTEGER DEFAULT 0,
                FOREIGN KEY(category_id) REFERENCES order_categories(id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name   TEXT,
                phone           TEXT,
                branch          TEXT,
                order_type      TEXT DEFAULT 'pickup',
                payment_method  TEXT DEFAULT '',
                pickup_date     TEXT,
                pickup_time     TEXT,
                notes           TEXT DEFAULT '',
                status          TEXT DEFAULT 'confirmed',
                subtotal        REAL DEFAULT 0,
                discount_type   TEXT DEFAULT '',
                discount_value  REAL DEFAULT 0,
                discount_amount REAL DEFAULT 0,
                total           REAL DEFAULT 0,
                created_by      TEXT,
                created_at      TEXT,
                updated_at      TEXT,
                completed_at    TEXT,
                cancelled_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS order_line_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id         INTEGER,
                item_id          INTEGER,
                name             TEXT,
                unit_price       REAL DEFAULT 0,
                qty              INTEGER DEFAULT 1,
                notes            TEXT DEFAULT '',
                line_discount_pct REAL DEFAULT 0,
                line_total       REAL DEFAULT 0,
                is_catering      INTEGER DEFAULT 0,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS order_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_pickup ON orders(pickup_date);
            CREATE INDEX IF NOT EXISTS idx_lineitems_order ON order_line_items(order_id);
        ''')

        # Migration: add payment_method to pre-existing orders tables.
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if 'payment_method' not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT ''")

        # Seed categories + menu once
        existing = conn.execute('SELECT COUNT(*) c FROM order_categories').fetchone()['c']
        if existing == 0:
            cat_ids = {}
            for i, cname in enumerate(CATEGORY_ORDER):
                cur = conn.execute(
                    'INSERT INTO order_categories(name, sort_order) VALUES(?,?)',
                    (cname, i))
                cat_ids[cname] = cur.lastrowid
            for j, (cat, name, price) in enumerate(MENU_SEED):
                conn.execute('''INSERT INTO order_menu_items
                    (category_id, name, price, is_catering, sort_order)
                    VALUES (?,?,?,0,?)''', (cat_ids[cat], name, price, j))
            # Catering boxes
            conn.execute('''INSERT INTO order_menu_items
                (category_id, name, price, is_catering, sort_order)
                VALUES (?,?,?,1,0)''',
                (cat_ids['Catering'], 'Large Catering Box', DEFAULT_CATERING_PRICE))
            conn.execute('''INSERT INTO order_menu_items
                (category_id, name, price, is_catering, sort_order)
                VALUES (?,?,?,1,1)''',
                (cat_ids['Catering'], 'Banh Mi Catering Box (Large)', DEFAULT_CATERING_PRICE))

        if _get_setting(conn, 'catering_box_price') is None:
            _set_setting(conn, 'catering_box_price', DEFAULT_CATERING_PRICE)
        if _get_setting(conn, 'kitchen_grace_minutes') is None:
            _set_setting(conn, 'kitchen_grace_minutes', 60)

# ── Menu data ────────────────────────────────────────────────────────────────

def _load_menu(conn, active_only=True):
    """Return list of categories, each with its items."""
    cq = 'SELECT * FROM order_categories'
    if active_only:
        cq += ' WHERE active=1'
    cq += ' ORDER BY sort_order, id'
    cats = [dict(r) for r in conn.execute(cq).fetchall()]
    iq = 'SELECT * FROM order_menu_items'
    if active_only:
        iq += ' WHERE active=1'
    iq += ' ORDER BY sort_order, id'
    items = [dict(r) for r in conn.execute(iq).fetchall()]
    by_cat = {}
    for it in items:
        by_cat.setdefault(it['category_id'], []).append(it)
    for c in cats:
        c['items'] = by_cat.get(c['id'], [])
    return cats

# ── Order maths (server-authoritative) ───────────────────────────────────────

def _compute_totals(lines, discount_type, discount_value):
    """lines: list of dicts with unit_price, qty, line_discount_pct.
    Returns (subtotal, discount_amount, total) and mutates each line's line_total."""
    subtotal = 0.0
    for ln in lines:
        gross = float(ln['unit_price']) * int(ln['qty'])
        pct = max(0.0, min(100.0, float(ln.get('line_discount_pct') or 0)))
        net = round(gross * (1 - pct / 100.0), 2)
        ln['line_total'] = net
        subtotal += net
    subtotal = round(subtotal, 2)

    discount_amount = 0.0
    if discount_type == 'percent':
        pct = max(0.0, min(100.0, float(discount_value or 0)))
        discount_amount = round(subtotal * pct / 100.0, 2)
    elif discount_type == 'amount':
        discount_amount = max(0.0, min(subtotal, round(float(discount_value or 0), 2)))
    total = round(subtotal - discount_amount, 2)
    return subtotal, discount_amount, total

# ── Routes: order taking ─────────────────────────────────────────────────────

@orders.route('/')
@_login_required
def index():
    # Kitchen role lands on the display; everyone else on the orders list.
    if session.get('role') == 'kitchen':
        return redirect(url_for('orders.kitchen'))
    return redirect(url_for('orders.order_list'))

@orders.route('/list')
@_staff_required
def order_list():
    """Upcoming / active orders for order staff."""
    status_f = request.args.get('status', '').strip()
    scope, sp = store_filter_clause()
    with _get_db() as conn:
        q = f'SELECT * FROM orders WHERE {scope}'
        params = list(sp)
        if status_f and status_f in STATUSES:
            q += ' AND status=?'
            params.append(status_f)
        else:
            q += " AND status IN ('confirmed','preparing','ready')"
        q += ' ORDER BY pickup_date, pickup_time'
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
        for o in rows:
            o['code'] = _order_code(o['id'])
            o['n_items'] = conn.execute(
                'SELECT COALESCE(SUM(qty),0) n FROM order_line_items WHERE order_id=?',
                (o['id'],)).fetchone()['n']
    return render_template('orders_list.html',
        rows=rows, status_f=status_f, statuses=STATUSES,
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
        is_admin=_is_admin())

@orders.route('/new')
@_staff_required
def new_order():
    with _get_db() as conn:
        cats = _load_menu(conn)
        catering_price = _catering_price(conn)
    return render_template('orders_form.html',
        mode='new', order=None, lines=[], cats=cats,
        catering_price=catering_price, branches=['Mirrabooka', 'Subiaco', 'Morley'],
        staff_list=_get_staff(), today=date.today().isoformat(),
        payment_methods=PAYMENT_METHODS,
        status_labels=STATUS_LABELS, is_admin=_is_admin())

@orders.route('/<int:oid>/edit')
@_staff_required
def edit_order(oid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        o = conn.execute(f'SELECT * FROM orders WHERE id=? AND {guard}', [oid] + gp).fetchone()
        if not o:
            flash('Order not found.', 'danger')
            return redirect(url_for('orders.order_list'))
        o = dict(o)
        lines = [dict(r) for r in conn.execute(
            'SELECT * FROM order_line_items WHERE order_id=? ORDER BY id', (oid,)).fetchall()]
        cats = _load_menu(conn)
        catering_price = _catering_price(conn)
    o['code'] = _order_code(o['id'])
    return render_template('orders_form.html',
        mode='edit', order=o, lines=lines, cats=cats,
        catering_price=catering_price, branches=['Mirrabooka', 'Subiaco', 'Morley'],
        staff_list=_get_staff(), today=date.today().isoformat(),
        payment_methods=PAYMENT_METHODS,
        status_labels=STATUS_LABELS, is_admin=_is_admin())

def _parse_lines_from_form():
    """Read repeating line-item fields into a list of dicts."""
    names   = request.form.getlist('li_name[]')
    iids    = request.form.getlist('li_item_id[]')
    prices  = request.form.getlist('li_price[]')
    qtys    = request.form.getlist('li_qty[]')
    notes   = request.form.getlist('li_notes[]')
    discs   = request.form.getlist('li_disc[]')
    caters  = request.form.getlist('li_catering[]')
    lines = []
    for i in range(len(names)):
        name = (names[i] or '').strip()
        if not name:
            continue
        try:
            qty = max(1, int(qtys[i] or 1))
        except (ValueError, IndexError):
            qty = 1
        try:
            price = max(0.0, float(prices[i] or 0))
        except (ValueError, IndexError):
            price = 0.0
        try:
            disc = max(0.0, min(100.0, float(discs[i] or 0)))
        except (ValueError, IndexError):
            disc = 0.0
        try:
            iid = int(iids[i]) if iids[i] else None
        except (ValueError, IndexError):
            iid = None
        lines.append({
            'item_id': iid,
            'name': name,
            'unit_price': price,
            'qty': qty,
            'notes': (notes[i] if i < len(notes) else '').strip(),
            'line_discount_pct': disc,
            'is_catering': 1 if (i < len(caters) and caters[i] == '1') else 0,
        })
    return lines

@orders.route('/save', methods=['POST'])
@_staff_required
def save_order():
    oid = request.form.get('order_id', '').strip()
    customer = request.form.get('customer_name', '').strip()
    phone    = request.form.get('phone', '').strip()
    branch   = request.form.get('branch', '').strip() or session.get('branch', '')
    otype    = request.form.get('order_type', 'pickup').strip()
    pay      = request.form.get('payment_method', '').strip()
    if pay not in PAYMENT_METHODS:
        pay = ''
    pdate    = request.form.get('pickup_date', '').strip()
    ptime    = request.form.get('pickup_time', '').strip()
    notes    = request.form.get('notes', '').strip()
    created_by = request.form.get('created_by', '').strip() or _actor()
    disc_type  = request.form.get('discount_type', '').strip()
    if disc_type not in ('percent', 'amount'):
        disc_type = ''
    try:
        disc_val = float(request.form.get('discount_value', 0) or 0)
    except ValueError:
        disc_val = 0.0

    lines = _parse_lines_from_form()
    if not customer:
        flash('Customer name is required.', 'warning')
        return redirect(request.referrer or url_for('orders.new_order'))
    if not lines:
        flash('Add at least one item to the order.', 'warning')
        return redirect(request.referrer or url_for('orders.new_order'))

    subtotal, disc_amt, total = _compute_totals(lines, disc_type, disc_val)
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    guard, gp = store_guard_clause()
    with _get_db() as conn:
        if oid:
            conn.execute(f'''UPDATE orders SET customer_name=?, phone=?, branch=?,
                order_type=?, payment_method=?, pickup_date=?, pickup_time=?, notes=?,
                subtotal=?, discount_type=?, discount_value=?, discount_amount=?,
                total=?, updated_at=? WHERE id=? AND {guard}''',
                [customer, phone, branch, otype, pay, pdate, ptime, notes,
                 subtotal, disc_type, disc_val, disc_amt, total, now, int(oid)] + gp)
            order_id = int(oid)
            conn.execute('DELETE FROM order_line_items WHERE order_id=?', (order_id,))
        else:
            cur = conn.execute('''INSERT INTO orders
                (customer_name, phone, branch, order_type, payment_method, pickup_date, pickup_time,
                 notes, status, subtotal, discount_type, discount_value,
                 discount_amount, total, created_by, created_at, updated_at, store_id)
                VALUES (?,?,?,?,?,?,?,?,'confirmed',?,?,?,?,?,?,?,?,?)''',
                (customer, phone, branch, otype, pay, pdate, ptime, notes,
                 subtotal, disc_type, disc_val, disc_amt, total, created_by, now, now,
                 current_store_id()))
            order_id = cur.lastrowid
        for ln in lines:
            conn.execute('''INSERT INTO order_line_items
                (order_id, item_id, name, unit_price, qty, notes,
                 line_discount_pct, line_total, is_catering)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (order_id, ln['item_id'], ln['name'], ln['unit_price'], ln['qty'],
                 ln['notes'], ln['line_discount_pct'], ln['line_total'], ln['is_catering']))

    flash(f'Order {_order_code(order_id)} saved — {customer} · ${total:.2f}', 'success')
    return redirect(url_for('orders.order_detail', oid=order_id))

@orders.route('/<int:oid>')
@_login_required
def order_detail(oid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        o = conn.execute(f'SELECT * FROM orders WHERE id=? AND {guard}', [oid] + gp).fetchone()
        if not o:
            flash('Order not found.', 'danger')
            return redirect(url_for('orders.order_list'))
        o = dict(o)
        lines = [dict(r) for r in conn.execute(
            'SELECT * FROM order_line_items WHERE order_id=? ORDER BY id', (oid,)).fetchall()]
    o['code'] = _order_code(o['id'])
    return render_template('orders_detail.html',
        order=o, lines=lines, status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS, statuses=STATUSES,
        payment_methods=PAYMENT_METHODS, is_admin=_is_admin())

# ── Status changes ───────────────────────────────────────────────────────────

@orders.route('/<int:oid>/status', methods=['POST'])
@_login_required
def set_status(oid):
    new_status = request.form.get('status', '').strip()
    if new_status not in STATUSES:
        return jsonify({'ok': False, 'error': 'bad status'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        extra, params = '', []
        if new_status == 'completed':
            extra = ', completed_at=?'
            params.append(now)
        elif new_status == 'cancelled':
            extra = ', cancelled_at=?'
            params.append(now)
        conn.execute(f'UPDATE orders SET status=?, updated_at=?{extra} WHERE id=? AND {guard}',
                     [new_status, now] + params + [oid] + gp)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'status': new_status})
    flash(f'Order {_order_code(oid)} → {STATUS_LABELS[new_status]}', 'success')
    return redirect(request.referrer or url_for('orders.order_detail', oid=oid))

@orders.route('/<int:oid>/cancel', methods=['POST'])
@_login_required
def cancel_order(oid):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'UPDATE orders SET status=?, cancelled_at=?, updated_at=? WHERE id=? AND {guard}',
                     ['cancelled', now, now, oid] + gp)
    flash(f'Order {_order_code(oid)} cancelled.', 'warning')
    return redirect(request.referrer or url_for('orders.order_detail', oid=oid))

@orders.route('/<int:oid>/restore', methods=['POST'])
@_admin_required
def restore_order(oid):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'''UPDATE orders SET status='confirmed', cancelled_at=NULL,
                        updated_at=? WHERE id=? AND {guard}''', [now, oid] + gp)
    flash(f'Order {_order_code(oid)} restored.', 'success')
    return redirect(request.referrer or url_for('orders.order_detail', oid=oid))

@orders.route('/<int:oid>/delete', methods=['POST'])
@_admin_required
def delete_order(oid):
    guard, gp = store_guard_clause()
    with _get_db() as conn:
        conn.execute(f'DELETE FROM orders WHERE id=? AND {guard}', [oid] + gp)
    flash(f'Order {_order_code(oid)} permanently deleted.', 'danger')
    return redirect(url_for('orders.order_list'))

# ── Kitchen display ──────────────────────────────────────────────────────────

def _kitchen_orders(conn):
    """Active orders still within their pickup window, sorted by pickup time.
    Scoped to the current store's kitchen."""
    grace = _grace_minutes(conn)
    cutoff = datetime.now() - timedelta(minutes=grace)
    scope, sp = store_filter_clause()
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM orders WHERE status IN ('confirmed','preparing','ready') AND {scope} "
        "ORDER BY pickup_date, pickup_time", sp).fetchall()]
    out = []
    for o in rows:
        pdt = _pickup_dt(o)
        # Auto-expiry: drop orders whose pickup time passed more than `grace` ago.
        if pdt is not None and pdt < cutoff:
            continue
        o['code'] = _order_code(o['id'])
        o['pickup_dt'] = pdt.isoformat() if pdt else ''
        o['items'] = [dict(r) for r in conn.execute(
            'SELECT name, qty, notes FROM order_line_items WHERE order_id=? ORDER BY id',
            (o['id'],)).fetchall()]
        out.append(o)
    return out

@orders.route('/kitchen')
@_login_required
def kitchen():
    with _get_db() as conn:
        kos = _kitchen_orders(conn)
    return render_template('orders_kitchen.html',
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
        is_admin=_is_admin(), user_role=session.get('role', ''))

@orders.route('/kitchen/data')
@_login_required
def kitchen_data():
    with _get_db() as conn:
        kos = _kitchen_orders(conn)
    return jsonify({
        'now': datetime.now().isoformat(),
        'orders': kos,
        'status_colors': STATUS_COLORS,
        'status_labels': STATUS_LABELS,
    })

# ── History ──────────────────────────────────────────────────────────────────

@orders.route('/history')
@_staff_required
def history():
    f = {
        'date_from': request.args.get('date_from', '').strip(),
        'date_to':   request.args.get('date_to', '').strip(),
        'branch':    request.args.get('branch', '').strip(),
        'status':    request.args.get('status', '').strip(),
        'staff':     request.args.get('staff', '').strip(),
        'q':         request.args.get('q', '').strip(),
    }
    scope, sp = store_filter_clause()
    q = f'SELECT * FROM orders WHERE {scope}'
    params = list(sp)
    if f['date_from']:
        q += ' AND pickup_date >= ?'; params.append(f['date_from'])
    if f['date_to']:
        q += ' AND pickup_date <= ?'; params.append(f['date_to'])
    if f['branch']:
        q += ' AND branch = ?'; params.append(f['branch'])
    if f['status'] and f['status'] in STATUSES:
        q += ' AND status = ?'; params.append(f['status'])
    if f['staff']:
        q += ' AND created_by = ?'; params.append(f['staff'])
    if f['q']:
        like = f"%{f['q']}%"
        # Allow searching by the displayed order code (#1001) or raw id.
        digits = f['q'].lstrip('#').strip()
        raw_id = ''
        if digits.isdigit():
            n = int(digits)
            raw_id = str(n - 1000) if n >= 1000 else digits
        q += ' AND (customer_name LIKE ? OR phone LIKE ? OR CAST(id AS TEXT) = ?)'
        params += [like, like, raw_id]
    q += ' ORDER BY pickup_date DESC, pickup_time DESC, id DESC LIMIT 500'
    with _get_db() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
        for o in rows:
            o['code'] = _order_code(o['id'])
            o['n_items'] = conn.execute(
                'SELECT COALESCE(SUM(qty),0) n FROM order_line_items WHERE order_id=?',
                (o['id'],)).fetchone()['n']
        staff_opts = [r['created_by'] for r in conn.execute(
            'SELECT DISTINCT created_by FROM orders WHERE created_by<>"" ORDER BY created_by').fetchall()]
    return render_template('orders_history.html',
        rows=rows, f=f, statuses=STATUSES, status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS, branches=['Mirrabooka', 'Subiaco', 'Morley'],
        staff_opts=staff_opts, is_admin=_is_admin())

# ── Reports / Analytics (admin) ──────────────────────────────────────────────

@orders.route('/reports')
@_admin_required
def reports():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    with _get_db() as conn:
        def scalar(sql, params=()):
            r = conn.execute(sql, params).fetchone()
            return r[0] if r and r[0] is not None else 0

        today_s = today.isoformat()
        week_s = week_start.isoformat()
        scope, sp = store_filter_clause()
        oscope, osp = store_filter_clause('o')
        not_cancelled = f"status <> 'cancelled' AND {scope}"

        today_orders = scalar(f"SELECT COUNT(*) FROM orders WHERE pickup_date=? AND {not_cancelled}", [today_s] + sp)
        week_orders  = scalar(f"SELECT COUNT(*) FROM orders WHERE pickup_date>=? AND {not_cancelled}", [week_s] + sp)
        today_rev = scalar(f"SELECT SUM(total) FROM orders WHERE pickup_date=? AND {not_cancelled}", [today_s] + sp)
        week_rev  = scalar(f"SELECT SUM(total) FROM orders WHERE pickup_date>=? AND {not_cancelled}", [week_s] + sp)
        total_rev = scalar(f"SELECT SUM(total) FROM orders WHERE {not_cancelled}", sp)
        cancelled = scalar(f"SELECT COUNT(*) FROM orders WHERE status='cancelled' AND {scope}", sp)

        popular = [dict(r) for r in conn.execute(f'''
            SELECT li.name, SUM(li.qty) qty, SUM(li.line_total) revenue
            FROM order_line_items li JOIN orders o ON o.id=li.order_id
            WHERE o.status <> 'cancelled' AND {oscope}
            GROUP BY li.name ORDER BY qty DESC LIMIT 10''', osp).fetchall()]

        # Per-branch revenue: super_admin sees every store; a normal admin sees
        # just their own. Join to stores so the real store name shows.
        by_store = [dict(r) for r in conn.execute(f'''
            SELECT COALESCE(s.name, o.branch) AS branch, COUNT(*) orders,
                   COALESCE(SUM(o.total),0) revenue
            FROM orders o LEFT JOIN stores s ON s.id=o.store_id
            WHERE o.status <> 'cancelled' AND {oscope}
            GROUP BY o.store_id ORDER BY revenue DESC''', osp).fetchall()]

        # last 7 days revenue trend
        trend = []
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            rev = scalar(f"SELECT SUM(total) FROM orders WHERE pickup_date=? AND {not_cancelled}", [d] + sp)
            trend.append({'date': d, 'revenue': round(rev, 2)})

    return render_template('orders_reports.html',
        today_orders=today_orders, week_orders=week_orders,
        today_rev=round(today_rev, 2), week_rev=round(week_rev, 2),
        total_rev=round(total_rev, 2), cancelled=cancelled,
        popular=popular, by_store=by_store, trend=trend, is_admin=_is_admin())

# ── Menu management (admin) ──────────────────────────────────────────────────

@orders.route('/menu')
@_admin_required
def menu_manage():
    with _get_db() as conn:
        cats = _load_menu(conn, active_only=False)
        catering_price = _catering_price(conn)
        grace = _grace_minutes(conn)
    return render_template('orders_menu.html',
        cats=cats, catering_price=catering_price, grace=grace, is_admin=_is_admin())

@orders.route('/menu/settings', methods=['POST'])
@_admin_required
def menu_settings():
    with _get_db() as conn:
        try:
            _set_setting(conn, 'catering_box_price',
                         max(0.0, float(request.form.get('catering_box_price', DEFAULT_CATERING_PRICE))))
        except ValueError:
            pass
        try:
            _set_setting(conn, 'kitchen_grace_minutes',
                         max(0, int(request.form.get('kitchen_grace_minutes', 60))))
        except ValueError:
            pass
        # keep the catering menu item price in sync
        cp = _catering_price(conn)
        conn.execute('UPDATE order_menu_items SET price=? WHERE is_catering=1', (cp,))
    flash('Settings saved.', 'success')
    return redirect(url_for('orders.menu_manage'))

@orders.route('/menu/category/add', methods=['POST'])
@_admin_required
def category_add():
    name = request.form.get('name', '').strip()
    if name:
        with _get_db() as conn:
            nxt = conn.execute('SELECT COALESCE(MAX(sort_order),0)+1 n FROM order_categories').fetchone()['n']
            conn.execute('INSERT INTO order_categories(name, sort_order) VALUES(?,?)', (name, nxt))
        flash(f'Category "{name}" added.', 'success')
    return redirect(url_for('orders.menu_manage'))

@orders.route('/menu/category/<int:cid>/update', methods=['POST'])
@_admin_required
def category_update(cid):
    name = request.form.get('name', '').strip()
    active = 1 if request.form.get('active') == '1' else 0
    with _get_db() as conn:
        conn.execute('UPDATE order_categories SET name=?, active=? WHERE id=?', (name, active, cid))
    flash('Category updated.', 'success')
    return redirect(url_for('orders.menu_manage'))

@orders.route('/menu/category/<int:cid>/delete', methods=['POST'])
@_admin_required
def category_delete(cid):
    with _get_db() as conn:
        conn.execute('UPDATE order_menu_items SET active=0 WHERE category_id=?', (cid,))
        conn.execute('DELETE FROM order_categories WHERE id=?', (cid,))
    flash('Category deleted.', 'warning')
    return redirect(url_for('orders.menu_manage'))

@orders.route('/menu/item/add', methods=['POST'])
@_admin_required
def item_add():
    name = request.form.get('name', '').strip()
    cat_id = request.form.get('category_id', '').strip()
    try:
        price = max(0.0, float(request.form.get('price', 0) or 0))
    except ValueError:
        price = 0.0
    if name and cat_id:
        with _get_db() as conn:
            conn.execute('INSERT INTO order_menu_items(category_id, name, price) VALUES(?,?,?)',
                         (int(cat_id), name, price))
        flash(f'Item "{name}" added.', 'success')
    return redirect(url_for('orders.menu_manage'))

@orders.route('/menu/item/<int:iid>/update', methods=['POST'])
@_admin_required
def item_update(iid):
    name = request.form.get('name', '').strip()
    cat_id = request.form.get('category_id', '').strip()
    active = 1 if request.form.get('active') == '1' else 0
    try:
        price = max(0.0, float(request.form.get('price', 0) or 0))
    except ValueError:
        price = 0.0
    with _get_db() as conn:
        conn.execute('''UPDATE order_menu_items SET name=?, price=?, category_id=?, active=?
                        WHERE id=?''', (name, price, int(cat_id) if cat_id else None, active, iid))
    flash('Item updated.', 'success')
    return redirect(url_for('orders.menu_manage'))

@orders.route('/menu/item/<int:iid>/delete', methods=['POST'])
@_admin_required
def item_delete(iid):
    with _get_db() as conn:
        conn.execute('DELETE FROM order_menu_items WHERE id=?', (iid,))
    flash('Item deleted.', 'warning')
    return redirect(url_for('orders.menu_manage'))
