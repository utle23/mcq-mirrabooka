"""Packaging Order — supplier-aware order composer.

Each supplier has a fixed item catalogue (codes, names, units) and contact
details. The main page lets the staff member tick / set quantities, the
"Compose" step builds a clean email body, and the action buttons either:

* open a pre-filled Gmail compose window (https://mail.google.com/...),
* fall back to a mailto: link, or
* send through Brevo using the same API key already configured for
  email notifications.

Admin can edit supplier contact details, add / edit / delete catalogue
items, and tweak default quantities directly from the same screen.
"""
from __future__ import annotations

import sqlite3
import urllib.parse
from datetime import datetime, date
from functools import wraps

from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify)

packaging_bp = Blueprint('packaging', __name__, url_prefix='/packaging')
DB_PATH: str | None = None


# ── Catalogue (sourced from the Word order template) ─────────────────────────

JACCUS_ITEMS_SEED = [
    # (product_code, name_en, name_vi, unit, default_qty)
    ('NapQBES',             'Quilted Brown Express (Tork Xpress Dispenser) Napkin', 'Khăn giấy dispenser Tork Xpress', 'carton', 0),
    ('1WFB',                'Tui banh ngot',                              'Túi bánh ngọt',                                    'bag',    4),
    ('KCB-M',               'Hop nho — catering box',                     'Hộp nhỏ — catering box',                           'carton', 0),
    ('KCBWLid-M',           'Nap hop nho — catering box',                 'Nắp hộp nhỏ — catering box',                       'carton', 0),
    ('KCB-L',               'Hop lon — catering box',                     'Hộp lớn — catering box',                           'carton', 0),
    ('KCBWLid-L',           'Nap hop lon — catering box',                 'Nắp hộp lớn — catering box',                       'carton', 0),
    ('KDTR-4-PLA',          'Hop goi cuon',                               'Hộp gỏi cuốn',                                     'carton', 0),
    ('KDTR-4Lid',           'Nap hop goi cuon',                           'Nắp hộp gỏi cuốn',                                 'carton', 0),
    ('P200',                'Hop nuoc mam',                               'Hộp nước mắm',                                     'carton', 0),
    ('P200Lid',             'Nap hop nuoc mam',                           'Nắp hộp nước mắm',                                 'carton', 0),
    ('Rec1000-PLA-K',       'Hop vuong take away',                        'Hộp vuông take away',                              'carton', 0),
    ('RecPaper-PPLid',      'Nap hop vuong take away',                    'Nắp hộp vuông take away',                          'carton', 0),
    ('Rd24-PLA-W',          'Hop soup trang',                             'Hộp soup trắng',                                   'carton', 0),
    ('RdPPLid-115-F',       'Nap hop soup trang',                         'Nắp hộp soup trắng',                               'carton', 0),
    ('PaperBowl-Extra Large','Hop bun heo quay',                          'Hộp bún heo quay',                                 'carton', 2),
    ('BPB-PETLid184',       'Nap hop bun heo quay',                       'Nắp hộp bún heo quay',                             'carton', 1),
    ('EC-DCC390',           'Ly cafe',                                    'Ly cà phê',                                        'carton', 0),
    ('EC-DCC500',           'Ly juice',                                   'Ly juice',                                         'carton', 0),
    ('BioBCL-90C-Pulp-F',   'Nap ly',                                     'Nắp ly',                                           'carton', 0),
    ('DSPaperBlk',          'Ong hut',                                    'Ống hút',                                          'carton', 0),
    ('ChopstickBam',        'Dua',                                        'Đũa',                                              'box',    0),
    ('WoodenFrk',           'Nia',                                        'Nĩa',                                              'box',    1),
    ('WoodenKnf',           'Dao',                                        'Dao',                                              'box',    0),
    ('WoodenSpn',           'Muong',                                      'Muỗng',                                            'box',    0),
    ('PulpCSpn',            'Muong soup',                                 'Muỗng soup',                                       'box',    1),
    ('NitrileBluPF-Md',     'Glove M — bao tay',                          'Găng tay M',                                       'carton', 2),
    ('NitrileBluPF-Lg',     'Glove L — bao tay',                          'Găng tay L',                                       'carton', 2),
    ('BL82/35',             'Bao rac den',                                'Bao rác đen',                                      'box',    0),
    ('HTSlimline',          'Khan giay lau tay',                          'Khăn giấy lau tay',                                'carton', 0),
    ('CUP-HOLDER',          'Cup holder',                                 'Khay đựng ly',                                     'carton', 0),
    ('Surplus20',           'Nuoc rua chen 20L',                          'Nước rửa chén 20L',                                'can',    0),
    ('Oven5',               'Nuoc rua lo nuong 5L',                       'Nước rửa lò nướng 5L',                             'can',    0),
    ('AF44/150',            'Foil — giay bac',                            'Giấy bạc',                                         'each',   0),
    ('CW45/600 Pro',        'Clingwrap 45cm x 600m',                      'Màng bọc thực phẩm 45cm x 600m',                   'each',   0),
    ('BioR-500Y',           '500ml Clear Biocup',                         'Cốc Biocup 500ml trong',                           'each',   0),
    ('BioC-96D(N)',         'Dome Lid (no hole) for 300-700ml BioCup',    'Nắp vòm BioCup 300-700ml',                         'each',   0),
    ('LWGP33x40',           'Wrap banh mi',                               'Túi gói bánh mì',                                  'bag',    0),
    ('Blitz5',              'Blitz Multi-Purpose Floor Cleaner & Degreaser','Blitz — chất tẩy đa năng',                       'can',    0),
]

JACCUS_SUPPLIER_SEED = {
    'name':           'Jaccus Trading',
    'email':          'Orders@jaccus.com.au',
    'phone':          '08-9248 9668',
    'cc_emails':      'Mirrabooka@mcqinternational.com',
    'delivery_days':  'WED,FRI',
    'cafe_name':      'MCQ Vietnamese Street Food — MIRRABOOKA',
    'cafe_address':   'Shop MM4/43 Yirrigan Dr, Mirrabooka WA 6061',
    'cafe_contacts':  '0433 916 386 — Kate\n0449 624 146 — Tommy',
    'notes':          '',
}

PACKAGING_ITEM_UNIT_FIXES = {
    'AF44/150': 'each',
    'CW45/600 Pro': 'each',
}

JACCUS_PRICE_DATA = {
    # product_code: (unit_of_measure from Jaccus price list, price per order unit)
    'NapQBES': ('6000', 49.90),
    '1WFB': ('1000', 19.90),
    'KCB-M': ('100', 60.90),
    'KCBWLid-M': ('100', 49.00),
    'KCB-L': ('50', 44.90),
    'KCBWLid-L': ('50', 37.00),
    'KDTR-4-PLA': ('400', 69.90),
    'KDTR-4Lid': ('400', 53.00),
    'P200': ('3000', 87.90),
    'P200Lid': ('3000', 74.00),
    'Rec1000-PLA-K': ('300', 51.90),
    'RecPaper-PPLid': ('300', 25.00),
    'Rd24-PLA-W': ('500', 76.90),
    'RdPPLid-115-F': ('500', 31.00),
    'PaperBowl-Extra Large': ('300', 78.90),
    'BPB-PETLid184': ('300', 47.00),
    'EC-DCC390': ('1000', 109.90),
    'EC-DCC500': ('1000', 124.90),
    'BioBCL-90C-Pulp-F': ('1000', 66.00),
    'DSPaperBlk': ('2500', 36.90),
    'ChopstickBam': ('3000', 49.90),
    'WoodenFrk': ('1000', 21.90),
    'WoodenKnf': ('1000', 18.90),
    'WoodenSpn': ('1000', 22.90),
    'PulpCSpn': ('1000', 49.90),
    'NitrileBluPF-Md': ('10 Box', 69.90),
    'NitrileBluPF-Lg': ('10 Box', 69.90),
    'BL82/35': ('200', 38.90),
    'HTSlimline': ('4000', 44.90),
    'Surplus20': ('20lt', 33.90),
    'Oven5': ('5lt', 29.90),
    'AF44/150': ('Each', 22.90),
    'CW45/600 Pro': ('1 Roll', 23.90),
    'BioR-500Y': ('1000', 219.90),
    'BioC-96D(N)': ('1000', 94.90),
    'LWGP33x40': ('800', 19.90),
    'Blitz5': ('5lt', 22.90),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _money(amount) -> str:
    try:
        return f"${float(amount or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _float_form(value, default=0.0):
    try:
        return max(0.0, float(value or default))
    except (TypeError, ValueError):
        return default


def _sync_jaccus_catalog_prices(c):
    supplier = c.execute(
        "SELECT id FROM packaging_suppliers WHERE name=? AND active=1 ORDER BY id LIMIT 1",
        (JACCUS_SUPPLIER_SEED['name'],)
    ).fetchone()
    if not supplier:
        return
    sid = supplier['id']
    existing_codes = {
        r['product_code']
        for r in c.execute(
            "SELECT product_code FROM packaging_items WHERE supplier_id=? AND active=1",
            (sid,)
        ).fetchall()
    }
    next_sort = c.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM packaging_items WHERE supplier_id=?",
        (sid,)
    ).fetchone()['n']

    for code, en, vi, unit, qty in JACCUS_ITEMS_SEED:
        if code in existing_codes:
            continue
        c.execute(
            '''INSERT INTO packaging_items
               (supplier_id, product_code, name_en, name_vi, unit, default_qty, sort_order)
               VALUES (?,?,?,?,?,?,?)''',
            (sid, code, en, vi, unit, qty, next_sort)
        )
        existing_codes.add(code)
        next_sort += 1

    for product_code, (unit_measure, unit_price) in JACCUS_PRICE_DATA.items():
        c.execute(
            '''UPDATE packaging_items
               SET unit_measure=?, unit_price=?
               WHERE supplier_id=? AND product_code=? AND active=1''',
            (unit_measure, unit_price, sid, product_code)
        )


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


def init_packaging(db_path: str):
    global DB_PATH
    DB_PATH = db_path
    with _conn() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS packaging_suppliers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL DEFAULT '',
                phone         TEXT NOT NULL DEFAULT '',
                cc_emails     TEXT NOT NULL DEFAULT '',
                delivery_days TEXT NOT NULL DEFAULT '',
                cafe_name     TEXT NOT NULL DEFAULT 'MCQ Vietnamese Street Food — MIRRABOOKA',
                cafe_address  TEXT NOT NULL DEFAULT '',
                cafe_contacts TEXT NOT NULL DEFAULT '',
                notes         TEXT NOT NULL DEFAULT '',
                active        INTEGER NOT NULL DEFAULT 1,
                sort_order    INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS packaging_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id   INTEGER NOT NULL REFERENCES packaging_suppliers(id) ON DELETE CASCADE,
                product_code  TEXT NOT NULL DEFAULT '',
                name_en       TEXT NOT NULL,
                name_vi       TEXT NOT NULL DEFAULT '',
                unit          TEXT NOT NULL DEFAULT 'carton',
                unit_measure  TEXT NOT NULL DEFAULT '',
                unit_price    REAL NOT NULL DEFAULT 0,
                default_qty   INTEGER NOT NULL DEFAULT 0,
                sort_order    INTEGER NOT NULL DEFAULT 0,
                active        INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS packaging_orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id   INTEGER NOT NULL REFERENCES packaging_suppliers(id),
                delivery_date TEXT NOT NULL DEFAULT '',
                composed_by   TEXT NOT NULL DEFAULT '',
                sent_at       TEXT DEFAULT (datetime('now','localtime')),
                send_channel  TEXT NOT NULL DEFAULT '',   -- gmail / brevo / mailto / preview
                subject       TEXT NOT NULL DEFAULT '',
                body          TEXT NOT NULL DEFAULT '',
                payload_json  TEXT NOT NULL DEFAULT ''    -- chosen items snapshot
            );
        ''')

        # Migration: add cafe_address to pre-existing supplier tables.
        cols = [r['name'] for r in c.execute("PRAGMA table_info(packaging_suppliers)").fetchall()]
        if 'cafe_address' not in cols:
            c.execute("ALTER TABLE packaging_suppliers ADD COLUMN cafe_address TEXT NOT NULL DEFAULT ''")
        item_cols = [r['name'] for r in c.execute("PRAGMA table_info(packaging_items)").fetchall()]
        if 'unit_measure' not in item_cols:
            c.execute("ALTER TABLE packaging_items ADD COLUMN unit_measure TEXT NOT NULL DEFAULT ''")
        if 'unit_price' not in item_cols:
            c.execute("ALTER TABLE packaging_items ADD COLUMN unit_price REAL NOT NULL DEFAULT 0")
        # Backfill the MCQ Mirrabooka delivery address where it's still blank.
        c.execute("UPDATE packaging_suppliers SET cafe_address=? WHERE COALESCE(cafe_address,'')=''",
                  (JACCUS_SUPPLIER_SEED['cafe_address'],))
        for product_code, unit in PACKAGING_ITEM_UNIT_FIXES.items():
            c.execute(
                "UPDATE packaging_items SET unit=? WHERE product_code=? AND active=1",
                (unit, product_code)
            )

        # Seed Jaccus if no suppliers exist yet
        if c.execute('SELECT COUNT(*) c FROM packaging_suppliers').fetchone()['c'] == 0:
            cur = c.execute('''INSERT INTO packaging_suppliers
                (name, email, phone, cc_emails, delivery_days, cafe_name, cafe_address, cafe_contacts)
                VALUES (?,?,?,?,?,?,?,?)''',
                (JACCUS_SUPPLIER_SEED['name'],
                 JACCUS_SUPPLIER_SEED['email'],
                 JACCUS_SUPPLIER_SEED['phone'],
                 JACCUS_SUPPLIER_SEED['cc_emails'],
                 JACCUS_SUPPLIER_SEED['delivery_days'],
                 JACCUS_SUPPLIER_SEED['cafe_name'],
                 JACCUS_SUPPLIER_SEED['cafe_address'],
                 JACCUS_SUPPLIER_SEED['cafe_contacts']))
            sid = cur.lastrowid
            for i, (code, en, vi, unit, qty) in enumerate(JACCUS_ITEMS_SEED):
                c.execute('''INSERT INTO packaging_items
                    (supplier_id, product_code, name_en, name_vi, unit, default_qty, sort_order)
                    VALUES (?,?,?,?,?,?,?)''',
                    (sid, code, en, vi, unit, qty, i))
        _sync_jaccus_catalog_prices(c)


# ── Data helpers ─────────────────────────────────────────────────────────────

def _suppliers():
    with _conn() as c:
        rows = c.execute(
            'SELECT * FROM packaging_suppliers WHERE active=1 '
            'ORDER BY sort_order, name').fetchall()
    return [dict(r) for r in rows]


def _supplier(sid):
    with _conn() as c:
        row = c.execute('SELECT * FROM packaging_suppliers WHERE id=?', (sid,)).fetchone()
    return dict(row) if row else None


def _items(sid):
    with _conn() as c:
        rows = c.execute(
            'SELECT * FROM packaging_items WHERE supplier_id=? AND active=1 '
            'ORDER BY sort_order, id', (sid,)).fetchall()
    return [dict(r) for r in rows]


# ── Email composition ────────────────────────────────────────────────────────

def _format_delivery_date(s: str) -> str:
    try:
        return datetime.strptime(s, '%Y-%m-%d').strftime('%A, %d/%m/%Y')
    except Exception:
        return s or '-'


def _compose_order(supplier: dict, items_with_qty: list[dict],
                    delivery_date: str, extra_note: str = '') -> tuple[str, str]:
    """Return (subject, plain text body) for the order email.
    Body uses a clean bulleted list (no fixed-width columns) because Gmail's
    compose window collapses runs of spaces, which destroyed the old
    ASCII-table layout when the user opened the draft."""
    today_pretty = datetime.now().strftime('%d/%m/%Y')
    deliv_pretty = _format_delivery_date(delivery_date)
    subject = f"MCQ Restaurant Mirrabooka — Packaging order — delivery {deliv_pretty}"

    total_units = sum(i['qty'] for i in items_with_qty)
    total_money = sum(i['qty'] * float(i.get('unit_price') or 0) for i in items_with_qty)
    line_count  = len(items_with_qty)
    s = 's' if total_units != 1 else ''
    ls = 's' if line_count != 1 else ''

    cafe_address = (supplier.get('cafe_address') or '').strip()
    parts = [
        f"Hello {supplier['name']},",
        "",
        f"Please prepare the following packaging order for delivery on {deliv_pretty}.",
    ]
    if cafe_address:
        parts.append(f"Deliver to: {supplier.get('cafe_name') or 'MCQ Restaurant Mirrabooka'} — {cafe_address}")
    parts += [
        "",
        f"ORDER LIST ({line_count} item{ls}, {total_units} unit{s}):",
        "",
    ]
    for it in items_with_qty:
        code = it.get('product_code') or ''
        unit_price = float(it.get('unit_price') or 0)
        line_total = it['qty'] * unit_price
        bullet = f"• {it['qty']} × {it['unit']} — {it['name_en']}"
        if code:
            bullet += f"  (code: {code})"
        bullet += f"  @ {_money(unit_price)} = {_money(line_total)}"
        parts.append(bullet)

    parts.extend([
        "",
        f"TOTAL: {total_units} unit{s} across {line_count} line{ls}.",
        f"TOTAL MONEY: {_money(total_money)}",
    ])

    if extra_note.strip():
        parts += ["", "Note:", extra_note.strip()]

    parts += [
        "",
        f"CC: {supplier.get('cc_emails') or '-'}",
        "",
        f"— {supplier.get('cafe_name') or 'MCQ Restaurant Mirrabooka'}",
    ]
    if cafe_address:
        parts.append(cafe_address)
    parts += [
        "Contact:",
        supplier.get('cafe_contacts') or '-',
        "",
        f"Order date: {today_pretty}",
    ]
    return subject, '\n'.join(parts)


def _gmail_compose_url(to: str, subject: str, body: str, cc: str = '') -> str:
    """Build a https://mail.google.com compose-window URL."""
    qs = {
        'view': 'cm', 'fs': '1', 'tf': '1',
        'to': to or '',
        'su': subject,
        'body': body,
    }
    if cc:
        qs['cc'] = cc
    return 'https://mail.google.com/mail/?' + urllib.parse.urlencode(qs, quote_via=urllib.parse.quote)


def _mailto_url(to: str, subject: str, body: str, cc: str = '') -> str:
    qs = {'subject': subject, 'body': body}
    if cc:
        qs['cc'] = cc
    return f'mailto:{urllib.parse.quote(to or "")}?' + urllib.parse.urlencode(qs, quote_via=urllib.parse.quote)


def _gmail_app_url(to: str, subject: str, body: str, cc: str = '') -> str:
    """Deep-link to the Gmail iOS / Android app: googlegmail:///co?…
    On iOS this opens the Gmail app directly; on Android the same scheme is
    also recognised when Gmail is installed."""
    qs = {'to': to or '', 'subject': subject, 'body': body}
    if cc:
        qs['cc'] = cc
    return 'googlegmail:///co?' + urllib.parse.urlencode(qs, quote_via=urllib.parse.quote)


# ── Routes ───────────────────────────────────────────────────────────────────

@packaging_bp.route('/')
@_login_required
def packaging_home():
    suppliers = _suppliers()
    if not suppliers:
        return render_template('packaging.html', supplier=None, suppliers=[], items=[])

    sid_str = request.args.get('supplier', '')
    try:
        sid = int(sid_str) if sid_str else suppliers[0]['id']
    except ValueError:
        sid = suppliers[0]['id']
    supplier = _supplier(sid) or suppliers[0]
    items    = _items(supplier['id'])

    # Pick a sensible default delivery date — the next configured day
    deliv_default = date.today().isoformat()
    try:
        days = [d.strip().upper() for d in (supplier['delivery_days'] or '').split(',') if d.strip()]
        day_map = {'MON': 0, 'TUE': 1, 'WED': 2, 'THU': 3, 'FRI': 4, 'SAT': 5, 'SUN': 6}
        if days:
            today_dt = date.today()
            best = None
            for d_ in days:
                target = day_map.get(d_)
                if target is None: continue
                delta = (target - today_dt.weekday()) % 7
                if delta == 0: delta = 7
                from datetime import timedelta as _td
                cand = today_dt + _td(days=delta)
                if best is None or cand < best:
                    best = cand
            if best:
                deliv_default = best.isoformat()
    except Exception:
        pass

    return render_template('packaging.html',
        supplier=supplier, suppliers=suppliers, items=items,
        delivery_default=deliv_default, is_admin=session.get('role')=='admin')


@packaging_bp.route('/compose', methods=['POST'])
@_login_required
def packaging_compose():
    """Build email subject + body from posted quantities and return JSON.
    Used by the page's JS to fill the preview textarea and the Gmail URL."""
    try:
        sid = int(request.form.get('supplier_id', 0))
    except ValueError:
        return jsonify({'error': 'invalid supplier'}), 400
    supplier = _supplier(sid)
    if not supplier:
        return jsonify({'error': 'supplier not found'}), 404

    delivery_date = request.form.get('delivery_date', '').strip()
    extra_note    = request.form.get('extra_note', '').strip()

    all_items = _items(sid)
    chosen = []
    for it in all_items:
        raw = request.form.get(f'qty_{it["id"]}', '').strip()
        try:
            qty = int(raw or 0)
        except ValueError:
            qty = 0
        if qty > 0:
            chosen.append({**it, 'qty': qty})

    if not chosen:
        return jsonify({'error': 'No items selected. Set a quantity > 0 on at least one row.'}), 400

    subject, body = _compose_order(supplier, chosen, delivery_date, extra_note)
    cc = supplier.get('cc_emails', '')
    return jsonify({
        'ok': True,
        'subject':       subject,
        'body':          body,
        'gmail_url':     _gmail_compose_url(supplier['email'], subject, body, cc),
        'gmail_app_url': _gmail_app_url   (supplier['email'], subject, body, cc),
        'mailto':        _mailto_url      (supplier['email'], subject, body, cc),
        'to':            supplier['email'],
        'cc':            cc,
        'item_count':    len(chosen),
        'total_qty':     sum(i['qty'] for i in chosen),
        'total_money':   sum(i['qty'] * float(i.get('unit_price') or 0) for i in chosen),
    })


@packaging_bp.route('/send', methods=['POST'])
@_admin_required
def packaging_send():
    """Send order via Brevo HTTP API using the existing email_service config."""
    try:
        sid = int(request.form.get('supplier_id', 0))
    except ValueError:
        return jsonify({'error': 'invalid supplier'}), 400
    supplier = _supplier(sid)
    if not supplier:
        return jsonify({'error': 'supplier not found'}), 404

    delivery_date = request.form.get('delivery_date', '').strip()
    extra_note    = request.form.get('extra_note', '').strip()
    composed_by   = request.form.get('composed_by', session.get('role','')).strip()

    all_items = _items(sid)
    chosen = []
    for it in all_items:
        raw = request.form.get(f'qty_{it["id"]}', '').strip()
        try:
            qty = int(raw or 0)
        except ValueError:
            qty = 0
        if qty > 0:
            chosen.append({**it, 'qty': qty})

    if not chosen:
        return jsonify({'error': 'No items selected.'}), 400

    subject, body = _compose_order(supplier, chosen, delivery_date, extra_note)

    # Send via Brevo
    try:
        import email_service
    except Exception:
        return jsonify({'error': 'email_service not available'}), 500

    settings = email_service.get_settings()
    if not email_service._is_configured(settings):
        return jsonify({'error':
            'Brevo not configured. Fill in the API key + sender email under Email Notifications first.'}), 400

    to_email = (supplier.get('email') or '').strip()
    if not to_email:
        return jsonify({'error': 'Supplier has no email address. Edit supplier info first.'}), 400

    # HTML version — keep it simple: <pre> with the same body for compatibility
    from html import escape as _esc
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5;color:#222">'
        + '<pre style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;'
        + 'background:#f7f9fb;border:1px solid #eef0f3;border-radius:8px;padding:14px;white-space:pre-wrap">'
        + _esc(body)
        + '</pre></div>'
    )

    cc_list = [c.strip() for c in (supplier.get('cc_emails') or '').split(',') if c.strip()]
    payload = {
        'sender':      {'name': settings.get('from_name') or 'MCQ Mirrabooka',
                         'email': settings['sender_email']},
        'to':          [{'email': to_email}],
        'subject':     subject,
        'htmlContent': html_body,
        'textContent': body,
    }
    if cc_list:
        payload['cc'] = [{'email': e} for e in cc_list]

    ok, msg = email_service._brevo_post(payload, settings['brevo_api_key'])

    import json as _json
    with _conn() as c:
        c.execute('''INSERT INTO packaging_orders
            (supplier_id, delivery_date, composed_by, send_channel, subject, body, payload_json)
            VALUES (?,?,?,?,?,?,?)''',
            (sid, delivery_date, composed_by,
             'brevo' if ok else f'brevo_failed',
             subject, body, _json.dumps(chosen)))

    if not ok:
        # Friendlier hint when Brevo bounces
        return jsonify({'error': f'Brevo send failed: {msg}'}), 500
    return jsonify({'ok': True, 'message': f'Order emailed to {to_email}.'})


@packaging_bp.route('/log-action', methods=['POST'])
@_login_required
def packaging_log_action():
    """Record that the admin clicked the Gmail or mailto link so the order
    history shows it. No actual mail send happens here — Gmail/mailto opens
    in a new tab and the user clicks Send manually."""
    try:
        sid = int(request.form.get('supplier_id', 0))
    except ValueError:
        return jsonify({'error': 'invalid'}), 400
    channel = (request.form.get('channel', '') or '').strip().lower()
    if channel not in ('gmail', 'mailto', 'preview'):
        channel = 'preview'
    supplier = _supplier(sid)
    if not supplier:
        return jsonify({'error': 'supplier not found'}), 404
    subject = request.form.get('subject', '').strip()
    body    = request.form.get('body', '').strip()
    delivery_date = request.form.get('delivery_date', '').strip()
    composed_by = request.form.get('composed_by', session.get('role','')).strip()
    with _conn() as c:
        c.execute('''INSERT INTO packaging_orders
            (supplier_id, delivery_date, composed_by, send_channel, subject, body)
            VALUES (?,?,?,?,?,?)''',
            (sid, delivery_date, composed_by, channel, subject, body))
    return jsonify({'ok': True})


# ── Admin CRUD ───────────────────────────────────────────────────────────────

@packaging_bp.route('/supplier/<int:sid>/edit', methods=['POST'])
@_admin_required
def supplier_edit(sid):
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Supplier name required'}), 400
    with _conn() as c:
        c.execute('''UPDATE packaging_suppliers SET
            name=?, email=?, phone=?, cc_emails=?, delivery_days=?,
            cafe_name=?, cafe_address=?, cafe_contacts=?, notes=?
            WHERE id=?''',
            (name,
             request.form.get('email', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('cc_emails', '').strip(),
             request.form.get('delivery_days', '').strip().upper(),
             request.form.get('cafe_name', '').strip(),
             request.form.get('cafe_address', '').strip(),
             request.form.get('cafe_contacts', '').strip(),
             request.form.get('notes', '').strip(),
             sid))
    return jsonify({'ok': True})


@packaging_bp.route('/supplier/add', methods=['POST'])
@_admin_required
def supplier_add():
    name = request.form.get('name', '').strip()
    if not name:
        return redirect(url_for('packaging.packaging_home'))
    with _conn() as c:
        n = c.execute(
            'SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM packaging_suppliers'
        ).fetchone()['n']
        cur = c.execute('''INSERT INTO packaging_suppliers
            (name, email, phone, cc_emails, delivery_days, cafe_name, cafe_contacts, sort_order)
            VALUES (?,?,?,?,?,?,?,?)''',
            (name,
             request.form.get('email', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('cc_emails', '').strip(),
             request.form.get('delivery_days', '').strip().upper(),
             request.form.get('cafe_name', '').strip() or 'MCQ Vietnamese Street Food — MIRRABOOKA',
             request.form.get('cafe_contacts', '').strip(),
             n))
        new_id = cur.lastrowid
    return redirect(url_for('packaging.packaging_home', supplier=new_id))


@packaging_bp.route('/supplier/<int:sid>/delete', methods=['POST'])
@_admin_required
def supplier_delete(sid):
    with _conn() as c:
        c.execute('DELETE FROM packaging_suppliers WHERE id=?', (sid,))
    return redirect(url_for('packaging.packaging_home'))


@packaging_bp.route('/item/add', methods=['POST'])
@_admin_required
def item_add():
    try:
        sid = int(request.form.get('supplier_id', 0))
    except ValueError:
        return jsonify({'error': 'invalid supplier'}), 400
    name_en = request.form.get('name_en', '').strip()
    if not name_en:
        return jsonify({'error': 'Item name required'}), 400
    with _conn() as c:
        n = c.execute(
            'SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM packaging_items WHERE supplier_id=?',
            (sid,)).fetchone()['n']
        try:
            qty = max(0, int(request.form.get('default_qty', '0') or 0))
        except ValueError:
            qty = 0
        cur = c.execute('''INSERT INTO packaging_items
            (supplier_id, product_code, name_en, name_vi, unit, unit_measure,
             unit_price, default_qty, sort_order)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (sid,
             request.form.get('product_code', '').strip(),
             name_en,
             request.form.get('name_vi', '').strip(),
             request.form.get('unit', 'carton').strip() or 'carton',
             request.form.get('unit_measure', '').strip(),
             _float_form(request.form.get('unit_price'), 0.0),
             qty, n))
        new_id = cur.lastrowid
    return jsonify({'ok': True, 'id': new_id})


@packaging_bp.route('/item/<int:iid>/edit', methods=['POST'])
@_admin_required
def item_edit(iid):
    name_en = request.form.get('name_en', '').strip()
    if not name_en:
        return jsonify({'error': 'Item name required'}), 400
    try:
        qty = max(0, int(request.form.get('default_qty', '0') or 0))
    except ValueError:
        qty = 0
    with _conn() as c:
        c.execute('''UPDATE packaging_items SET
            product_code=?, name_en=?, name_vi=?, unit=?, unit_measure=?,
            unit_price=?, default_qty=?
            WHERE id=?''',
            (request.form.get('product_code', '').strip(),
             name_en,
             request.form.get('name_vi', '').strip(),
             request.form.get('unit', 'carton').strip() or 'carton',
             request.form.get('unit_measure', '').strip(),
             _float_form(request.form.get('unit_price'), 0.0),
             qty, iid))
    return jsonify({'ok': True})


@packaging_bp.route('/item/<int:iid>/delete', methods=['POST'])
@_admin_required
def item_delete(iid):
    with _conn() as c:
        c.execute('DELETE FROM packaging_items WHERE id=?', (iid,))
    return jsonify({'ok': True})


@packaging_bp.route('/history')
@_login_required
def packaging_history():
    sid = request.args.get('supplier', '')
    with _conn() as c:
        q = ('SELECT o.*, s.name AS supplier_name FROM packaging_orders o '
             'LEFT JOIN packaging_suppliers s ON s.id=o.supplier_id')
        params = []
        if sid:
            q += ' WHERE o.supplier_id=?'; params.append(int(sid))
        q += ' ORDER BY o.id DESC LIMIT 50'
        rows = [dict(r) for r in c.execute(q, params).fetchall()]
    return render_template('packaging_history.html', orders=rows)
