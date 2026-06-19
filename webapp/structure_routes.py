"""Staff Structure — live editable org chart with legacy upload fallback.

Admins can edit the restaurant structure directly in the page: manager name,
department columns, department leads, and lower-level staff rows. The same
structured data is used for the web view, PNG download, and PDF export so the
template stays consistent.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from functools import wraps
from io import BytesIO

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)

structure = Blueprint('structure', __name__, url_prefix='/structure')
DB_PATH = None
UPLOAD_DIR = None

CURRENT_BASENAME = 'staff_structure_current'
IMG_EXTS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED = IMG_EXTS | {'pdf'}

DEFAULT_DEPARTMENTS = [
    ('DRINK',        '#2F83C2', 'Phúc',   'LEAD',
     [('LEVEL 2', 'Vũ'), ('LEVEL 3', 'Hương')]),
    ('BANH MI',     '#CF850D', 'Phúc',   'LEAD',
     [('LEVEL 2', 'Ni'), ('', 'Quỳnh'), ('', 'Trí')]),
    ('CHEF',        '#D75435', 'Phụng',  'LEAD',
     [('LEVEL 2', 'Long')]),
    ('KITCHEN HAND','#1AA17E', 'Tân',    'LEAD',
     [('LEVEL 2', 'Khiêm')]),
    ('CASHIER',     '#96394E', 'Nguyễn', 'LEAD', []),
    ('OFFICE',      '#704C31', 'Khôi',   'LEAD', []),
]

SEED_NAME_FIXES = {
    'Phuc': 'Phúc',
    'Vu': 'Vũ',
    'Huong': 'Hương',
    'Phung': 'Phụng',
    'Tan': 'Tân',
    'Khiem': 'Khiêm',
    'Nguyen': 'Nguyễn',
    'Khoi': 'Khôi',
    'Quynh': 'Quỳnh',
    'Tri': 'Trí',
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def _actor():
    return session.get('staff_name') or session.get('role') or 'admin'


def _get_meta(conn, key, default=None):
    row = conn.execute('SELECT value FROM structure_meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _set_meta(conn, key, value):
    conn.execute('''INSERT INTO structure_meta(key, value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
                 (key, str(value)))


def _touch(conn, actor=None):
    _set_meta(conn, 'live_updated_at', _now())
    _set_meta(conn, 'live_updated_by', actor or _actor())


def _current_filename():
    with _get_db() as conn:
        return _get_meta(conn, 'filename')


def _normalize_color(value, fallback='#1A3A5C'):
    value = (value or '').strip()
    if len(value) == 7 and value.startswith('#'):
        try:
            int(value[1:], 16)
            return value.upper()
        except ValueError:
            pass
    return fallback


def _live_chart(conn):
    settings = {
        'manager_name': _get_meta(conn, 'manager_name', 'Phụng'),
        'manager_title': _get_meta(conn, 'manager_title', 'MANAGER'),
        'live_updated_at': _get_meta(conn, 'live_updated_at', ''),
        'live_updated_by': _get_meta(conn, 'live_updated_by', ''),
    }
    departments = [dict(r) for r in conn.execute('''
        SELECT * FROM structure_departments
        WHERE active=1
        ORDER BY sort_order, id
    ''').fetchall()]
    for dept in departments:
        dept['members'] = [dict(r) for r in conn.execute('''
            SELECT * FROM structure_members
            WHERE department_id=? AND active=1
            ORDER BY sort_order, id
        ''', (dept['id'],)).fetchall()]
    return {'settings': settings, 'departments': departments}


def _staff_options(conn):
    try:
        return [r['name'] for r in conn.execute('''
            SELECT name FROM staff_members WHERE active=1 ORDER BY name
        ''').fetchall()]
    except Exception:
        return []


def _seed_live_structure(conn):
    count = conn.execute('SELECT COUNT(*) AS c FROM structure_departments').fetchone()['c']
    if count:
        return
    _set_meta(conn, 'manager_name', 'Phụng')
    _set_meta(conn, 'manager_title', 'MANAGER')
    for i, (name, color, lead_name, lead_badge, members) in enumerate(DEFAULT_DEPARTMENTS):
        cur = conn.execute('''
            INSERT INTO structure_departments
                (name, color, lead_name, lead_badge, sort_order, active)
            VALUES (?,?,?,?,?,1)
        ''', (name, color, lead_name, lead_badge, i))
        dept_id = cur.lastrowid
        for j, (level_label, staff_name) in enumerate(members):
            conn.execute('''
                INSERT INTO structure_members
                    (department_id, level_label, staff_name, sort_order, active)
                VALUES (?,?,?,?,1)
            ''', (dept_id, level_label, staff_name, j))
    _touch(conn, 'seed')


def _upgrade_seed_names(conn):
    """Bring early live-editor seed rows in line with the original chart image."""
    seed_actor = _get_meta(conn, 'live_updated_by')
    if seed_actor not in (None, 'seed', 'seed-upgrade'):
        return

    changed = False
    manager_name = _get_meta(conn, 'manager_name')
    if manager_name in SEED_NAME_FIXES:
        _set_meta(conn, 'manager_name', SEED_NAME_FIXES[manager_name])
        changed = True

    for old_name, new_name in SEED_NAME_FIXES.items():
        cur = conn.execute('''
            UPDATE structure_departments
            SET lead_name=?
            WHERE lead_name=?
        ''', (new_name, old_name))
        changed = changed or cur.rowcount > 0
        cur = conn.execute('''
            UPDATE structure_members
            SET staff_name=?
            WHERE staff_name=?
        ''', (new_name, old_name))
        changed = changed or cur.rowcount > 0

    if changed:
        _touch(conn, 'seed')
    elif seed_actor == 'seed-upgrade':
        _set_meta(conn, 'live_updated_by', 'seed')


def _active_department_exists(conn, dept_id):
    row = conn.execute('''
        SELECT 1 FROM structure_departments
        WHERE id=? AND active=1
    ''', (dept_id,)).fetchone()
    return bool(row)


def _move_id(ids, item_id, direction):
    if item_id not in ids:
        return ids, False
    index = ids.index(item_id)
    new_index = index - 1 if direction == 'up' else index + 1
    if new_index < 0 or new_index >= len(ids):
        return ids, False
    moved = ids[:]
    moved[index], moved[new_index] = moved[new_index], moved[index]
    return moved, True


def _write_department_order(conn, ids):
    for i, dept_id in enumerate(ids):
        conn.execute('UPDATE structure_departments SET sort_order=? WHERE id=?',
                     (i, dept_id))


def _write_member_order(conn, ids):
    for i, member_id in enumerate(ids):
        conn.execute('UPDATE structure_members SET sort_order=? WHERE id=?',
                     (i, member_id))


# ── Init ─────────────────────────────────────────────────────────────────────

def init_structure_tables(db_path, upload_dir):
    global DB_PATH, UPLOAD_DIR
    DB_PATH = db_path
    UPLOAD_DIR = upload_dir
    with _get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS structure_meta (
            key TEXT PRIMARY KEY, value TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS structure_departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#1A3A5C',
            lead_name TEXT NOT NULL DEFAULT '',
            lead_badge TEXT NOT NULL DEFAULT 'LEAD',
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS structure_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department_id INTEGER NOT NULL REFERENCES structure_departments(id) ON DELETE CASCADE,
            level_label TEXT NOT NULL DEFAULT '',
            staff_name TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        )''')
        _seed_live_structure(conn)
        _upgrade_seed_names(conn)

        # Legacy uploaded chart fallback. Kept so existing uploads are not lost.
        if _get_meta(conn, 'filename') is None:
            seed = f'{CURRENT_BASENAME}.png'
            seed_path = os.path.join(upload_dir, seed)
            if not os.path.exists(seed_path):
                static_seed = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'static', 'staff_structure_seed.png')
                if os.path.exists(static_seed):
                    try:
                        import shutil
                        os.makedirs(upload_dir, exist_ok=True)
                        shutil.copyfile(static_seed, seed_path)
                    except Exception:
                        pass
            if os.path.exists(seed_path):
                _set_meta(conn, 'filename', seed)
                _set_meta(conn, 'updated_at', _now())
                _set_meta(conn, 'updated_by', 'seed')


# ── Live chart rendering ─────────────────────────────────────────────────────

def _font(size, bold=False, family='sans', italic=False):
    from PIL import ImageFont
    if family == 'serif':
        candidates = [
            '/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf' if bold and italic else None,
            '/System/Library/Fonts/Supplemental/Georgia Italic.ttf' if italic else None,
            '/System/Library/Fonts/Supplemental/Georgia Bold.ttf' if bold else None,
            '/System/Library/Fonts/Supplemental/Georgia.ttf',
            '/System/Library/Fonts/Supplemental/Times New Roman Bold Italic.ttf' if bold and italic else None,
            '/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf' if italic else None,
            '/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf' if bold else None,
            '/System/Library/Fonts/Supplemental/Times New Roman.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf' if bold and italic else None,
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf' if italic else None,
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf' if bold else None,
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
        ]
    else:
        candidates = [
            '/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf' if bold and italic else None,
            '/System/Library/Fonts/Supplemental/Arial Italic.ttf' if italic else None,
            '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else None,
            '/System/Library/Fonts/Supplemental/Arial.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf' if bold and italic else None,
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf' if italic else None,
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else None,
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold else None,
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            '/Library/Fonts/Arial.ttf',
        ]
    for path in candidates:
        if not path:
            continue
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_rgb(value, fallback=(26, 58, 92)):
    value = (value or '').strip().lstrip('#')
    if len(value) != 6:
        return fallback
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text or '', font=font)
    return box[2] - box[0]


def _center_text(draw, box, text, font, fill):
    x1, y1, x2, y2 = box
    bbox = draw.textbbox((0, 0), text or '', font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((x1 + (x2 - x1 - tw) / 2 - bbox[0],
               y1 + (y2 - y1 - th) / 2 - bbox[1] - 1),
              text or '', font=font, fill=fill)


def _fit_font(draw, text, max_width, size, bold=False, family='sans',
              italic=False, min_size=14):
    for font_size in range(size, min_size - 1, -1):
        font = _font(font_size, bold=bold, family=family, italic=italic)
        if _text_width(draw, text, font) <= max_width:
            return font
    return _font(min_size, bold=bold, family=family, italic=italic)


def _render_live_png(chart) -> bytes:
    from PIL import Image, ImageDraw

    W, H = 2339, 1656
    CREAM = (250, 246, 236)
    NAVY = (26, 39, 64)
    BLUE = (45, 65, 95)
    GOLD = (189, 151, 92)
    MUTED = (143, 134, 120)
    WHITE = (255, 255, 255)

    img = Image.new('RGB', (W, H), CREAM)
    draw = ImageDraw.Draw(img)

    f_title = _font(72, True, family='serif')
    f_banner = _font(42, True)
    f_sub = _font(27, family='serif', italic=True)
    f_label = _font(20, True)
    f_small = _font(18, True)
    f_footer = _font(18, family='serif')

    # Border and decorative corners.
    draw.rectangle((48, 48, W - 48, H - 48), outline=GOLD, width=3)
    draw.rectangle((58, 58, W - 58, H - 58), outline=(216, 194, 154), width=1)
    corner = 20
    for sx, sy in ((38, 38), (W - 38, 38), (38, H - 38), (W - 38, H - 38)):
        xdir = 1 if sx < W / 2 else -1
        ydir = 1 if sy < H / 2 else -1
        draw.line((sx, sy, sx + xdir * corner, sy), fill=GOLD, width=3)
        draw.line((sx, sy, sx, sy + ydir * corner), fill=GOLD, width=3)

    # Logo.
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'logo.png')
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((118, 118), Image.LANCZOS)
            lx, ly = (W - logo.width) // 2, 105
            draw.rounded_rectangle((lx - 12, ly - 12, lx + logo.width + 12, ly + logo.height + 12),
                                   radius=18, fill=WHITE, outline=GOLD, width=2)
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass

    _center_text(draw, (0, 285, W, 365), 'MCQ Mirrabooka Restaurant', f_title, NAVY)
    banner_w, banner_h = 810, 78
    bx1, by1 = (W - banner_w) // 2, 388
    draw.rectangle((bx1, by1, bx1 + banner_w, by1 + banner_h), fill=BLUE, outline=GOLD, width=4)
    _center_text(draw, (bx1, by1, bx1 + banner_w, by1 + banner_h),
                 'S T A F F   S T R U C T U R E', f_banner, WHITE)
    _center_text(draw, (0, 493, W, 535), 'Reporting lines & staff levels by department',
                 f_sub, MUTED)

    # Manager.
    mgr_name = chart['settings'].get('manager_name') or ''
    mgr_title = (chart['settings'].get('manager_title') or 'MANAGER').upper()
    mgr_w, mgr_h = 480, 126
    mx1, my1 = (W - mgr_w) // 2, 660
    draw.rectangle((mx1, my1, mx1 + mgr_w, my1 + mgr_h), fill=BLUE, outline=GOLD, width=4)
    _center_text(draw, (mx1, my1 + 20, mx1 + mgr_w, my1 + 46), ' '.join(mgr_title), f_small, GOLD)
    mgr_font = _fit_font(draw, mgr_name, mgr_w - 32, 48, bold=True,
                         family='serif', min_size=28)
    _center_text(draw, (mx1, my1 + 52, mx1 + mgr_w, my1 + 112), mgr_name, mgr_font, WHITE)

    line_y = 845
    draw.line((W // 2, my1 + mgr_h, W // 2, line_y), fill=GOLD, width=3)
    draw.line((285, line_y, W - 285, line_y), fill=GOLD, width=3)

    departments = chart['departments'] or []
    max_cols = max(1, min(len(departments), 6))
    left, right = 120, W - 120
    gap = 24
    col_w = (right - left - gap * (max_cols - 1)) / max_cols
    top_y = 900

    for i, dept in enumerate(departments):
        col = i % max_cols
        row = i // max_cols
        x = int(left + col * (col_w + gap))
        y = int(top_y + row * 430)
        w = int(col_w)
        accent = _hex_rgb(dept.get('color'))

        card_h = 205
        dept_title = ' '.join((dept.get('name') or '').upper())
        dept_font = _fit_font(draw, dept_title, w - 20, 20, bold=True,
                              family='sans', min_size=13)
        lead_name = dept.get('lead_name') or ''
        lead_font = _fit_font(draw, lead_name, w - 28, 34, bold=True,
                              family='serif', min_size=22)
        draw.rectangle((x, y, x + w, y + card_h), fill=WHITE, outline=accent, width=3)
        draw.rectangle((x, y, x + w, y + 58), fill=accent)
        _center_text(draw, (x, y + 5, x + w, y + 56), dept_title, dept_font, WHITE)
        _center_text(draw, (x, y + 76, x + w, y + 130), lead_name, lead_font, accent)
        badge = (dept.get('lead_badge') or 'LEAD').upper()
        badge_w, badge_h = 120, 32
        badge_x = x + (w - badge_w) // 2
        draw.rectangle((badge_x, y + 142, badge_x + badge_w, y + 142 + badge_h), fill=accent)
        _center_text(draw, (badge_x, y + 142, badge_x + badge_w, y + 142 + badge_h),
                     ' '.join(badge), f_small, WHITE)

        members = dept.get('members') or []
        if members:
            cx = x + w // 2
            draw.line((cx, y + card_h, cx, y + card_h + 34), fill=accent, width=3)
            my = y + card_h + 34
            for member in members:
                level = (member.get('level_label') or '').upper()
                if level:
                    tag_w, tag_h = min(170, w - 80), 34
                    tx = x + (w - tag_w) // 2
                    draw.rectangle((tx, my, tx + tag_w, my + tag_h),
                                   fill=(238, 246, 255), outline=accent, width=2)
                    _center_text(draw, (tx, my, tx + tag_w, my + tag_h),
                                 ' '.join(level), f_small, accent)
                    draw.line((cx, my - 34, cx, my), fill=accent, width=3)
                    my += tag_h + 18
                box_w, box_h = min(180, w - 80), 58
                bx = x + (w - box_w) // 2
                staff_name = member.get('staff_name') or ''
                staff_font = _fit_font(draw, staff_name, box_w - 16, 34,
                                       bold=True, family='serif', min_size=20)
                draw.rectangle((bx, my, bx + box_w, my + box_h), fill=WHITE, outline=accent, width=2)
                _center_text(draw, (bx, my, bx + box_w, my + box_h),
                             staff_name, staff_font, accent)
                draw.line((cx, my - 18, cx, my), fill=accent, width=3)
                my += box_h + 16

    draw.text((110, H - 112), 'M C Q  M I R R A B O O K A  R E S T A U R A N T',
              fill=MUTED, font=f_footer)
    draw.text((W - 310, H - 112), 'C O N F I D E N T I A L', fill=MUTED, font=f_footer)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Routes ───────────────────────────────────────────────────────────────────

@structure.route('/')
@_login_required
def view():
    with _get_db() as conn:
        fname = _get_meta(conn, 'filename')
        updated_at = _get_meta(conn, 'updated_at', '')
        updated_by = _get_meta(conn, 'updated_by', '')
        chart = _live_chart(conn)
        staff_options = _staff_options(conn)
    has_image = bool(fname and os.path.exists(os.path.join(UPLOAD_DIR, fname)))
    ver = (updated_at or '').replace(' ', '').replace(':', '').replace('-', '')
    return render_template('staff_structure.html',
        has_image=has_image, img_url=url_for('structure.uploaded_image', v=ver),
        updated_at=updated_at, updated_by=updated_by, is_admin=_is_admin(),
        chart=chart, staff_options=staff_options)


@structure.route('/image')
@_login_required
def image():
    with _get_db() as conn:
        png = _render_live_png(_live_chart(conn))
    buf = BytesIO(png)
    buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=True,
                     download_name='MCQ_Staff_Structure.png')


@structure.route('/uploaded-image')
@_login_required
def uploaded_image():
    fname = _current_filename()
    if not fname:
        abort(404)
    return send_from_directory(UPLOAD_DIR, os.path.basename(fname))


@structure.route('/settings', methods=['POST'])
@_admin_required
def update_settings():
    manager_name = request.form.get('manager_name', '').strip()
    manager_title = request.form.get('manager_title', '').strip() or 'MANAGER'
    with _get_db() as conn:
        _set_meta(conn, 'manager_name', manager_name)
        _set_meta(conn, 'manager_title', manager_title.upper())
        _touch(conn)
    flash('Manager block updated.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/department/add', methods=['POST'])
@_admin_required
def department_add():
    name = request.form.get('name', '').strip().upper()
    if not name:
        flash('Department name is required.', 'warning')
        return redirect(url_for('structure.view'))
    color = _normalize_color(request.form.get('color'), '#1A3A5C')
    lead_name = request.form.get('lead_name', '').strip()
    lead_badge = request.form.get('lead_badge', '').strip().upper() or 'LEAD'
    with _get_db() as conn:
        sort_order = conn.execute(
            'SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM structure_departments'
        ).fetchone()['n']
        conn.execute('''INSERT INTO structure_departments
            (name, color, lead_name, lead_badge, sort_order, active)
            VALUES (?,?,?,?,?,1)''',
            (name, color, lead_name, lead_badge, sort_order))
        _touch(conn)
    flash('Department added.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/department/<int:dept_id>/update', methods=['POST'])
@_admin_required
def department_update(dept_id):
    name = request.form.get('name', '').strip().upper()
    if not name:
        flash('Department name is required.', 'warning')
        return redirect(url_for('structure.view'))
    with _get_db() as conn:
        conn.execute('''UPDATE structure_departments
            SET name=?, color=?, lead_name=?, lead_badge=?
            WHERE id=?''',
            (name, _normalize_color(request.form.get('color'), '#1A3A5C'),
             request.form.get('lead_name', '').strip(),
             request.form.get('lead_badge', '').strip().upper() or 'LEAD',
             dept_id))
        _touch(conn)
    flash('Department updated.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/department/<int:dept_id>/delete', methods=['POST'])
@_admin_required
def department_delete(dept_id):
    with _get_db() as conn:
        conn.execute('UPDATE structure_departments SET active=0 WHERE id=?', (dept_id,))
        _touch(conn)
    flash('Department removed from live chart.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/department/<int:dept_id>/move/<direction>', methods=['POST'])
@_admin_required
def department_move(dept_id, direction):
    if direction not in ('up', 'down'):
        abort(404)
    with _get_db() as conn:
        ids = [r['id'] for r in conn.execute('''
            SELECT id FROM structure_departments
            WHERE active=1
            ORDER BY sort_order, id
        ''').fetchall()]
        ids, changed = _move_id(ids, dept_id, direction)
        if changed:
            _write_department_order(conn, ids)
            _touch(conn)
    return redirect(url_for('structure.view'))


@structure.route('/member/add', methods=['POST'])
@_admin_required
def member_add():
    try:
        dept_id = int(request.form.get('department_id', '0'))
    except ValueError:
        dept_id = 0
    staff_name = request.form.get('staff_name', '').strip()
    if not dept_id or not staff_name:
        flash('Choose a department and staff name.', 'warning')
        return redirect(url_for('structure.view'))
    level_label = request.form.get('level_label', '').strip().upper()
    with _get_db() as conn:
        if not _active_department_exists(conn, dept_id):
            flash('Choose an active department.', 'warning')
            return redirect(url_for('structure.view'))
        sort_order = conn.execute('''
            SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
            FROM structure_members WHERE department_id=?
        ''', (dept_id,)).fetchone()['n']
        conn.execute('''INSERT INTO structure_members
            (department_id, level_label, staff_name, sort_order, active)
            VALUES (?,?,?,?,1)''', (dept_id, level_label, staff_name, sort_order))
        _touch(conn)
    flash('Level/staff row added.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/member/<int:member_id>/update', methods=['POST'])
@_admin_required
def member_update(member_id):
    staff_name = request.form.get('staff_name', '').strip()
    if not staff_name:
        flash('Staff name is required.', 'warning')
        return redirect(url_for('structure.view'))
    try:
        dept_id = int(request.form.get('department_id', '0'))
    except ValueError:
        dept_id = 0
    with _get_db() as conn:
        current = conn.execute('''
            SELECT department_id FROM structure_members
            WHERE id=? AND active=1
        ''', (member_id,)).fetchone()
        if not current:
            flash('Level/staff row was not found.', 'warning')
            return redirect(url_for('structure.view'))
        if not _active_department_exists(conn, dept_id):
            flash('Choose an active department.', 'warning')
            return redirect(url_for('structure.view'))
        level_label = request.form.get('level_label', '').strip().upper()
        if dept_id == current['department_id']:
            conn.execute('''UPDATE structure_members
                SET level_label=?, staff_name=?
                WHERE id=?''', (level_label, staff_name, member_id))
        else:
            sort_order = conn.execute('''
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS n
                FROM structure_members WHERE department_id=? AND active=1
            ''', (dept_id,)).fetchone()['n']
            conn.execute('''UPDATE structure_members
                SET department_id=?, level_label=?, staff_name=?, sort_order=?
                WHERE id=?''',
                (dept_id, level_label, staff_name, sort_order, member_id))
        _touch(conn)
    flash('Level/staff row updated.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/member/<int:member_id>/delete', methods=['POST'])
@_admin_required
def member_delete(member_id):
    with _get_db() as conn:
        conn.execute('UPDATE structure_members SET active=0 WHERE id=?', (member_id,))
        _touch(conn)
    flash('Level/staff row removed.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/member/<int:member_id>/move/<direction>', methods=['POST'])
@_admin_required
def member_move(member_id, direction):
    if direction not in ('up', 'down'):
        abort(404)
    with _get_db() as conn:
        row = conn.execute('''
            SELECT department_id FROM structure_members
            WHERE id=? AND active=1
        ''', (member_id,)).fetchone()
        if row:
            ids = [r['id'] for r in conn.execute('''
                SELECT id FROM structure_members
                WHERE department_id=? AND active=1
                ORDER BY sort_order, id
            ''', (row['department_id'],)).fetchall()]
            ids, changed = _move_id(ids, member_id, direction)
            if changed:
                _write_member_order(conn, ids)
                _touch(conn)
    return redirect(url_for('structure.view'))


@structure.route('/upload', methods=['POST'])
@_admin_required
def upload():
    f = request.files.get('image')
    if not f or not f.filename:
        flash('Please choose an image or PDF to upload.', 'warning')
        return redirect(url_for('structure.view'))
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED:
        flash('Unsupported file type. Use PNG, JPG, WEBP or PDF.', 'danger')
        return redirect(url_for('structure.view'))

    dest_name = f'{CURRENT_BASENAME}.png'
    dest_path = os.path.join(UPLOAD_DIR, dest_name)
    try:
        if ext == 'pdf':
            import fitz
            data = f.read()
            doc = fitz.open(stream=data, filetype='pdf')
            doc[0].get_pixmap(dpi=200).save(dest_path)
        elif ext == 'png':
            f.save(dest_path)
        else:
            try:
                from PIL import Image
                img = Image.open(f.stream).convert('RGB')
                img.save(dest_path, 'PNG')
            except Exception:
                dest_name = f'{CURRENT_BASENAME}.{ext}'
                dest_path = os.path.join(UPLOAD_DIR, dest_name)
                f.save(dest_path)
    except Exception as e:
        flash(f'Could not process the file: {type(e).__name__}.', 'danger')
        return redirect(url_for('structure.view'))

    with _get_db() as conn:
        _set_meta(conn, 'filename', dest_name)
        _set_meta(conn, 'updated_at', _now())
        _set_meta(conn, 'updated_by', _actor())
    flash('Legacy uploaded chart updated.', 'success')
    return redirect(url_for('structure.view'))


@structure.route('/pdf')
@_login_required
def pdf():
    """Export the live chart as a polished A4-landscape PDF."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.lib import colors

    with _get_db() as conn:
        png_bytes = _render_live_png(_live_chart(conn))

    buf = BytesIO()
    page_w, page_h = landscape(A4)
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setFillColor(colors.HexColor('#FAF6EC'))
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    img = ImageReader(BytesIO(png_bytes))
    iw, ih = img.getSize()
    margin = 5 * mm
    avail_w = page_w - 2 * margin
    avail_h = page_h - 2 * margin
    scale = min(avail_w / iw, avail_h / ih)
    w, h = iw * scale, ih * scale
    x = (page_w - w) / 2
    y = (page_h - h) / 2
    c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask='auto')
    c.showPage()
    c.save()
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=False,
                     download_name='MCQ_Staff_Structure.pdf')
