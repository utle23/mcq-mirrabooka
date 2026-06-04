"""Staff Structure — displays the restaurant org-chart image, lets admins
upload a replacement when the structure changes, and exports a clean A4 PDF.

The current chart is kept as a single image file in the uploads folder; its
filename + last-update metadata live in the `structure_meta` table.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, send_file, send_from_directory, flash, abort)
import os
import sqlite3
from io import BytesIO
from datetime import datetime
from functools import wraps

structure = Blueprint('structure', __name__, url_prefix='/structure')
DB_PATH = None
UPLOAD_DIR = None

CURRENT_BASENAME = 'staff_structure_current'           # extension added on save
IMG_EXTS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED  = IMG_EXTS | {'pdf'}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_db():
    conn = sqlite3.connect(DB_PATH)
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

def _get_meta(conn, key, default=None):
    row = conn.execute('SELECT value FROM structure_meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default

def _set_meta(conn, key, value):
    conn.execute('''INSERT INTO structure_meta(key, value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, str(value)))

def _current_filename():
    with _get_db() as conn:
        return _get_meta(conn, 'filename')

# ── Init ─────────────────────────────────────────────────────────────────────

def init_structure_tables(db_path, upload_dir):
    global DB_PATH, UPLOAD_DIR
    DB_PATH = db_path
    UPLOAD_DIR = upload_dir
    with _get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS structure_meta (
            key TEXT PRIMARY KEY, value TEXT)''')
        # Seed the initial chart. On a fresh deploy the uploads copy won't exist
        # (uploads/ is gitignored), so fall back to the tracked static seed.
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
                _set_meta(conn, 'updated_at', datetime.now().strftime('%Y-%m-%d %H:%M'))
                _set_meta(conn, 'updated_by', 'seed')

# ── Routes ───────────────────────────────────────────────────────────────────

@structure.route('/')
@_login_required
def view():
    with _get_db() as conn:
        fname = _get_meta(conn, 'filename')
        updated_at = _get_meta(conn, 'updated_at', '')
        updated_by = _get_meta(conn, 'updated_by', '')
    has_image = bool(fname and os.path.exists(os.path.join(UPLOAD_DIR, fname)))
    # cache-buster so a freshly uploaded image shows immediately
    ver = (updated_at or '').replace(' ', '').replace(':', '').replace('-', '')
    return render_template('staff_structure.html',
        has_image=has_image, img_url=url_for('structure.image', v=ver),
        updated_at=updated_at, updated_by=updated_by, is_admin=_is_admin())

@structure.route('/image')
@_login_required
def image():
    fname = _current_filename()
    if not fname:
        abort(404)
    return send_from_directory(UPLOAD_DIR, os.path.basename(fname))

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
            # Render the first PDF page to PNG so the web view is an image.
            import fitz
            data = f.read()
            doc = fitz.open(stream=data, filetype='pdf')
            doc[0].get_pixmap(dpi=200).save(dest_path)
        elif ext == 'png':
            f.save(dest_path)
        else:
            # Convert other image formats to PNG for a single, predictable file.
            try:
                from PIL import Image
                img = Image.open(f.stream).convert('RGB')
                img.save(dest_path, 'PNG')
            except Exception:
                # Fall back to saving with the original extension
                dest_name = f'{CURRENT_BASENAME}.{ext}'
                dest_path = os.path.join(UPLOAD_DIR, dest_name)
                f.save(dest_path)
    except Exception as e:
        flash(f'Could not process the file: {type(e).__name__}.', 'danger')
        return redirect(url_for('structure.view'))

    with _get_db() as conn:
        _set_meta(conn, 'filename', dest_name)
        _set_meta(conn, 'updated_at', datetime.now().strftime('%Y-%m-%d %H:%M'))
        _set_meta(conn, 'updated_by', session.get('staff_name') or session.get('role') or 'admin')
    flash('Staff structure updated.', 'success')
    return redirect(url_for('structure.view'))

@structure.route('/pdf')
@_login_required
def pdf():
    """Export the current chart as a polished A4-landscape PDF."""
    fname = _current_filename()
    if not fname:
        abort(404)
    src = os.path.join(UPLOAD_DIR, os.path.basename(fname))
    if not os.path.exists(src):
        abort(404)

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader

    buf = BytesIO()
    PAGE_W, PAGE_H = landscape(A4)
    c = rl_canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    # Cream background to match the template feel
    c.setFillColor(colors.HexColor('#FAF6EC'))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    try:
        img = ImageReader(src)
        iw, ih = img.getSize()
        margin = 10 * mm
        avail_w = PAGE_W - 2 * margin
        avail_h = PAGE_H - 2 * margin
        scale = min(avail_w / iw, avail_h / ih)
        w, h = iw * scale, ih * scale
        x = (PAGE_W - w) / 2
        y = (PAGE_H - h) / 2
        c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask='auto')
    except Exception:
        c.setFillColor(colors.black)
        c.drawString(20 * mm, PAGE_H / 2, 'Staff structure image could not be embedded.')
    c.showPage()
    c.save()
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf', as_attachment=False,
                     download_name='MCQ_Staff_Structure.pdf')
