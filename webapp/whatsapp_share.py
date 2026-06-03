"""Daily WhatsApp share — composes a single PNG image summarising the day's
checklists (with photo thumbnails) and temperature records, ready to be
shared straight to a WhatsApp group via the browser's native share sheet.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from io import BytesIO

from flask import Blueprint, render_template, request, send_file, redirect, url_for, session
from functools import wraps

whatsapp_bp = Blueprint('whatsapp', __name__, url_prefix='/whatsapp')

DB_PATH: str | None = None
STATIC_DIR: str | None = None
UPLOAD_DIR: str | None = None
CHECKLISTS_META: dict = {}
TEMPERATURES_META: dict = {}


def init_whatsapp(db_path: str, static_dir: str, upload_dir: str,
                  checklists: dict, temperatures: dict) -> None:
    global DB_PATH, STATIC_DIR, UPLOAD_DIR, CHECKLISTS_META, TEMPERATURES_META
    DB_PATH           = db_path
    STATIC_DIR        = static_dir
    UPLOAD_DIR        = upload_dir
    CHECKLISTS_META   = checklists
    TEMPERATURES_META = temperatures


def _login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('logged_in'):
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ── Data collection ──────────────────────────────────────────────────────────

def _collect_today(date_str: str) -> dict:
    """Pull today's checklist + temperature submissions with photo paths."""
    out = {'date': date_str, 'checklists': [], 'temperatures': []}
    with _conn() as conn:
        # Checklists — one entry per (type, section) combination submitted today
        chk_rows = conn.execute('''
            SELECT cs.*,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id) AS total_tasks,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id AND done=1) AS done_tasks
            FROM checklist_sessions cs
            WHERE cs.date=?
            ORDER BY cs.type, cs.section
        ''', (date_str,)).fetchall()

        for r in chk_rows:
            r = dict(r)
            meta = CHECKLISTS_META.get(r['type'], {})
            photos = [dict(p) for p in conn.execute(
                'SELECT filename, photo_number FROM checklist_photos '
                'WHERE session_id=? ORDER BY photo_number LIMIT 4',
                (r['id'],)).fetchall()]
            r['photos'] = photos
            r['meta']   = meta
            out['checklists'].append(r)

        # Temperature
        temp_rows = conn.execute('''
            SELECT ts.*,
                   (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id) AS reading_count,
                   (SELECT COUNT(*) FROM temp_readings WHERE session_id=ts.id AND discarded='Y') AS discarded
            FROM temp_sessions ts WHERE ts.date=?
            ORDER BY ts.type
        ''', (date_str,)).fetchall()
        for r in temp_rows:
            r = dict(r)
            meta = TEMPERATURES_META.get(r['type'], {})
            # Out-of-zone count uses food_kind: cold→unsafe if >5, hot→unsafe if <60
            reading_rows = conn.execute('''
                SELECT tr.food_name, tr.c1_temp, tr.c2_temp, tr.c3_temp, tr.c4_temp, tr.c5_temp,
                       COALESCE(ft.food_kind, 'cold') AS kind
                FROM temp_readings tr
                LEFT JOIN temp_food_templates ft
                  ON ft.temp_type = ? AND ft.food_name = tr.food_name
                WHERE tr.session_id = ?''', (r['type'], r['id'])).fetchall()
            bad = 0
            for rr in reading_rows:
                kind = rr['kind'] or 'cold'
                for col in ('c1_temp', 'c2_temp', 'c3_temp', 'c4_temp', 'c5_temp'):
                    v = rr[col]
                    if v is None:
                        continue
                    if (kind == 'cold' and v > 5) or (kind == 'hot' and v < 60):
                        bad += 1
            r['out_of_zone'] = bad
            r['meta']        = meta
            out['temperatures'].append(r)
    return out


# ── PNG composition (Pillow) ────────────────────────────────────────────────

def _font(size: int, bold: bool = False):
    """Try a few system fonts; fall back to PIL default if all are missing."""
    from PIL import ImageFont
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(s: str, fb=(96, 125, 139)) -> tuple:
    s = (s or '').lstrip('#')
    if len(s) != 6:
        return fb
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return fb


def _rounded(draw, xy, radius, fill, outline=None, width=1):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill,
                            outline=outline, width=width)


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines = []
    cur = ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def build_daily_png(date_str: str) -> bytes:
    from PIL import Image, ImageDraw

    data = _collect_today(date_str)

    # Canvas — 1080 width is the WhatsApp share sweet spot
    W = 1080
    PAD = 32
    THUMB = 220     # photo thumbnail size
    NAVY = (26, 26, 46)
    BRAND = (192, 57, 43)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)

    # ── First measure how tall we need ────────────────────────────────────
    f_title  = _font(46, bold=True)
    f_sub    = _font(22)
    f_h2     = _font(30, bold=True)
    f_body   = _font(22)
    f_body_b = _font(22, bold=True)
    f_small  = _font(18)
    f_chip   = _font(16, bold=True)

    # Header block
    h_header = 220
    # Summary KPI strip
    h_kpi = 160
    # Each checklist card
    h_chk_card_base = 260   # title + meta rows
    h_chk_photos    = THUMB + 24
    # Each temperature card
    h_temp_card     = 150
    # Footer
    h_footer = 100

    total_h = h_header + h_kpi
    # Each section has: 46px title space + (cards | 90px empty placeholder)
    total_h += 46
    if data['checklists']:
        for c in data['checklists']:
            total_h += h_chk_card_base + (h_chk_photos if c['photos'] else 0) + 24
    else:
        total_h += 90
    total_h += 46
    if data['temperatures']:
        for _ in data['temperatures']:
            total_h += h_temp_card + 16
    else:
        total_h += 90
    total_h += h_footer + 30

    img = Image.new('RGB', (W, total_h), LIGHT_BG)
    draw = ImageDraw.Draw(img)

    # ── HEADER (gradient-ish solid + brand) ───────────────────────────────
    draw.rectangle((0, 0, W, h_header), fill=NAVY)
    draw.rectangle((0, h_header - 6, W, h_header), fill=BRAND)

    logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((130, 130), Image.LANCZOS)
            # White rounded bg behind logo
            lx, ly = PAD, 40
            _rounded(draw, (lx - 8, ly - 8, lx + logo.width + 8, ly + logo.height + 8),
                     radius=14, fill=(255, 255, 255))
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass

    # Date + branding text
    text_x = PAD + 160
    try:
        date_pretty = datetime.strptime(date_str, '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = date_str
    draw.text((text_x, 36), 'MCQ MIRRABOOKA CAFE', fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 96), 'DAILY OPERATIONS SUMMARY',
              fill=(255, 255, 255, 200), font=f_sub)
    draw.text((text_x, 134), date_pretty, fill=(255, 200, 200), font=f_body_b)

    y = h_header + 24

    # ── KPI strip ─────────────────────────────────────────────────────────
    chk_done = sum(1 for c in data['checklists'] if c['done_tasks'] == c['total_tasks'] and c['total_tasks'] > 0)
    chk_late = sum(1 for c in data['checklists'] if c.get('is_late'))
    temp_done = len(data['temperatures'])
    temp_alerts = sum(c.get('out_of_zone', 0) for c in data['temperatures'])

    kpi_cards = [
        ('CHECKLISTS', f'{len(data["checklists"])}', (46, 125, 50)),
        ('FULLY DONE', f'{chk_done}', (33, 150, 83)),
        ('TEMPERATURE', f'{temp_done}', (216, 67, 21)),
        ('ALERTS', f'{temp_alerts + chk_late}', (198, 40, 40)),
    ]
    card_w = (W - PAD * 2 - 36) // 4
    for i, (label, val, col) in enumerate(kpi_cards):
        cx = PAD + i * (card_w + 12)
        _rounded(draw, (cx, y, cx + card_w, y + h_kpi - 12), radius=14,
                 fill=(255, 255, 255))
        draw.rectangle((cx, y, cx + card_w, y + 6), fill=col)
        draw.text((cx + 18, y + 24), label, fill=MUTED, font=f_chip)
        draw.text((cx + 18, y + 56), val, fill=col, font=_font(56, bold=True))
    y += h_kpi + 12

    # ── CHECKLISTS section ────────────────────────────────────────────────
    draw.text((PAD, y), 'CHECKLISTS', fill=NAVY, font=f_h2)
    y += 46

    if not data['checklists']:
        _rounded(draw, (PAD, y, W - PAD, y + 70), radius=12, fill=(255, 255, 255))
        draw.text((PAD + 24, y + 22),
                  'No checklists submitted yet today.',
                  fill=MUTED, font=f_body)
        y += 90
    else:
        for c in data['checklists']:
            meta_color = _hex_to_rgb(c['meta'].get('color'))
            card_h = h_chk_card_base + (h_chk_photos if c['photos'] else 0)
            _rounded(draw, (PAD, y, W - PAD, y + card_h), radius=14,
                     fill=(255, 255, 255))
            # Left colour stripe
            draw.rectangle((PAD, y, PAD + 8, y + card_h), fill=meta_color)

            title = (c['meta'].get('title') or c['type']).upper()
            section_label = (c['section'] or '').upper()
            draw.text((PAD + 28, y + 18),
                      f"{title}", fill=NAVY, font=f_h2)
            # Section pill
            sec_color = (231, 76, 60) if section_label == 'CLOSING' else (243, 156, 18)
            sec_w = 130
            sec_x = PAD + 28 + draw.textbbox((0, 0), title, font=f_h2)[2] + 20
            _rounded(draw, (sec_x, y + 26, sec_x + sec_w, y + 56),
                     radius=14, fill=sec_color)
            sec_tx = sec_x + (sec_w - draw.textbbox((0, 0), section_label, font=f_chip)[2]) // 2
            draw.text((sec_tx, y + 33), section_label,
                      fill=(255, 255, 255), font=f_chip)

            # Status pills row
            pill_y = y + 72
            pct = round(c['done_tasks'] / c['total_tasks'] * 100) if c['total_tasks'] else 0
            pills = [
                (f'{c["done_tasks"]}/{c["total_tasks"]} TASKS', (76, 175, 80) if pct >= 90 else (255, 152, 0)),
                (f'{pct}% COMPLETE',                            (33, 150, 243)),
            ]
            if c.get('is_late'):
                pills.append(('LATE', (198, 40, 40)))
            else:
                pills.append(('ON TIME', (46, 125, 50)))
            if c.get('verified'):
                pills.append(('VERIFIED', (26, 26, 46)))

            px = PAD + 28
            for txt, col in pills:
                tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
                _rounded(draw, (px, pill_y, px + tw, pill_y + 34),
                         radius=14, fill=col)
                draw.text((px + 12, pill_y + 8), txt,
                          fill=(255, 255, 255), font=f_chip)
                px += tw + 8

            # Meta lines
            meta_lines = [
                f"Submitted by: {c.get('submitted_by') or '-'}",
                f"Responsible: {c.get('responsible') or '-'}    Submitted at: {(c.get('submitted_at') or '-')[:16]}",
            ]
            if c.get('general_note'):
                meta_lines.append(f"Note: {c['general_note']}")
            my = y + 122
            for line in meta_lines:
                for sub in _wrap_text(draw, line, f_body, W - PAD * 2 - 56)[:1]:
                    draw.text((PAD + 28, my), sub, fill=(51, 51, 51), font=f_body)
                    my += 30

            # Photo strip
            if c['photos']:
                px = PAD + 28
                py = y + card_h - h_chk_photos + 6
                for p in c['photos'][:4]:
                    src = os.path.join(UPLOAD_DIR, p['filename'])
                    if not os.path.exists(src):
                        _rounded(draw, (px, py, px + THUMB, py + THUMB),
                                 radius=10, fill=(245, 245, 245))
                        draw.text((px + 50, py + THUMB // 2 - 12),
                                  'photo missing', fill=MUTED, font=f_small)
                    else:
                        try:
                            thumb = Image.open(src).convert('RGB')
                            # Center-crop to square
                            tw, th = thumb.size
                            sz = min(tw, th)
                            left = (tw - sz) // 2
                            top  = (th - sz) // 2
                            thumb = thumb.crop((left, top, left + sz, top + sz))
                            thumb = thumb.resize((THUMB, THUMB), Image.LANCZOS)
                            # Rounded mask
                            mask = Image.new('L', (THUMB, THUMB), 0)
                            ImageDraw.Draw(mask).rounded_rectangle(
                                (0, 0, THUMB, THUMB), radius=10, fill=255)
                            img.paste(thumb, (px, py), mask)
                        except Exception:
                            _rounded(draw, (px, py, px + THUMB, py + THUMB),
                                     radius=10, fill=(245, 245, 245))
                    px += THUMB + 12
            y += card_h + 18

    # ── TEMPERATURE section ──────────────────────────────────────────────
    draw.text((PAD, y), 'TEMPERATURE RECORDS', fill=NAVY, font=f_h2)
    y += 46

    if not data['temperatures']:
        _rounded(draw, (PAD, y, W - PAD, y + 70), radius=12, fill=(255, 255, 255))
        draw.text((PAD + 24, y + 22),
                  'No temperature records submitted yet today.',
                  fill=MUTED, font=f_body)
        y += 90
    else:
        for t in data['temperatures']:
            meta_color = _hex_to_rgb(t['meta'].get('color'))
            _rounded(draw, (PAD, y, W - PAD, y + h_temp_card), radius=14,
                     fill=(255, 255, 255))
            draw.rectangle((PAD, y, PAD + 8, y + h_temp_card), fill=meta_color)
            title = (t['meta'].get('title') or t['type']).upper()
            draw.text((PAD + 28, y + 18), title, fill=NAVY, font=f_h2)

            pills = [
                (f'{t["reading_count"]} FOODS', (76, 175, 80)),
            ]
            if t.get('discarded'):
                pills.append((f'{t["discarded"]} DISCARDED', (198, 40, 40)))
            if t.get('out_of_zone'):
                pills.append((f'{t["out_of_zone"]} OUT OF ZONE', (198, 40, 40)))
            else:
                pills.append(('ALL SAFE', (46, 125, 50)))

            px = PAD + 28
            pill_y = y + 64
            for txt, col in pills:
                tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
                _rounded(draw, (px, pill_y, px + tw, pill_y + 34),
                         radius=14, fill=col)
                draw.text((px + 12, pill_y + 8), txt,
                          fill=(255, 255, 255), font=f_chip)
                px += tw + 8

            draw.text((PAD + 28, y + 108),
                      f"Recorded by: {t.get('recorded_by') or '-'}    Checked by: {t.get('checked_by') or '-'}",
                      fill=(51, 51, 51), font=f_body)
            y += h_temp_card + 16

    # ── Footer ────────────────────────────────────────────────────────────
    fy = total_h - h_footer
    draw.rectangle((0, fy, W, total_h), fill=NAVY)
    draw.text((PAD, fy + 22),
              f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}',
              fill=(255, 255, 255, 180), font=f_body)
    draw.text((PAD, fy + 56),
              'MCQ Mirrabooka Cafe — Vietnamese Street Food',
              fill=(255, 200, 200), font=f_small)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Per-checklist detail PNG ─────────────────────────────────────────────────

def _checklist_detail(session_id: int) -> dict | None:
    with _conn() as conn:
        sess = conn.execute('''
            SELECT cs.*,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id) AS total_tasks,
                   (SELECT COUNT(*) FROM checklist_tasks WHERE session_id=cs.id AND done=1) AS done_tasks
            FROM checklist_sessions cs WHERE cs.id=?''', (session_id,)).fetchone()
        if not sess:
            return None
        sess = dict(sess)
        sess['tasks'] = [dict(r) for r in conn.execute(
            'SELECT task_order, task_name, done, note FROM checklist_tasks '
            'WHERE session_id=? ORDER BY task_order', (session_id,)).fetchall()]
        sess['photos'] = [dict(r) for r in conn.execute(
            'SELECT filename, photo_number FROM checklist_photos '
            'WHERE session_id=? ORDER BY photo_number', (session_id,)).fetchall()]
        sess['meta'] = CHECKLISTS_META.get(sess['type'], {})
    return sess


def build_checklist_png(session_id: int) -> bytes:
    from PIL import Image, ImageChops, ImageDraw, ImageOps

    c = _checklist_detail(session_id)
    if c is None:
        raise ValueError(f'Checklist session {session_id} not found')

    W = 1800
    PAD = 60
    CARD_PAD = 42
    NAVY = (26, 26, 46)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)
    TEXT = (38, 43, 52)
    LINE = (226, 230, 238)
    OK = (46, 125, 50)
    BAD = (198, 40, 40)

    color = _hex_to_rgb(c['meta'].get('color'))

    f_title  = _font(54, bold=True)
    f_sub    = _font(28)
    f_h2     = _font(34, bold=True)
    f_body   = _font(26)
    f_body_b = _font(26, bold=True)
    f_small  = _font(22)
    f_note   = _font(24)
    f_note_b = _font(24, bold=True)
    f_task   = _font(26)
    f_task_b = _font(26, bold=True)
    f_chip   = _font(20, bold=True)

    measure = ImageDraw.Draw(Image.new('RGB', (W, 100), LIGHT_BG))
    content_w = W - PAD * 2
    inner_w = content_w - CARD_PAD * 2

    def line_h(font) -> int:
        b = measure.textbbox((0, 0), 'Ag', font=font)
        return b[3] - b[1]

    def wrap(text, font, max_width) -> list[str]:
        raw = str(text or '').replace('\r', '')
        if not raw.strip():
            return []
        lines: list[str] = []
        for para in raw.split('\n'):
            if not para.strip():
                lines.append('')
                continue
            lines.extend(_wrap_text(measure, para, font, max_width))
        return lines

    def wrapped_h(lines: list[str], font, gap=6) -> int:
        if not lines:
            return 0
        return len(lines) * line_h(font) + (len(lines) - 1) * gap

    general_note_lines = wrap(c.get('general_note') or '', f_note, inner_w - 40)
    manager_note_lines = wrap(c.get('manager_notes') or '', f_note, inner_w - 40)
    issues_lines = wrap(c.get('issues_found') or '', f_note, inner_w - 40)
    action_lines = wrap(c.get('action_responsible') or '', f_note, inner_w - 40)
    detail_text = [
        f"Submitted by: {c.get('submitted_by') or '-'}    Responsible: {c.get('responsible') or '-'}",
        f"Submitted at: {(c.get('submitted_at') or '-')[:16]}",
        f"Photos attached: {len(c['photos'])}",
    ]
    detail_lines = []
    for line in detail_text:
        detail_lines.extend(wrap(line, f_body, inner_w))

    def note_box_h(note_lines: list[str], font=f_note) -> int:
        return 34 + line_h(f_note_b) + 10 + wrapped_h(note_lines, font)

    h_header = 250
    h_footer = 110
    h_meta = 88 + wrapped_h(detail_lines, f_body, 8) + CARD_PAD
    if general_note_lines:
        h_meta += 8 + note_box_h(general_note_lines) + 14
    if c.get('verified') or manager_note_lines or issues_lines or action_lines:
        verification_lines = []
        if c.get('verified'):
            verified_by = c.get('verified_by') or '-'
            verified_at = (c.get('verified_at') or '-')[:16]
            verification_lines.extend(wrap(f"Verified by: {verified_by}    Verified at: {verified_at}", f_note, inner_w - 40))
        if c.get('overall_result'):
            verification_lines.extend(wrap(f"Result: {c.get('overall_result')}", f_note, inner_w - 40))
        if issues_lines:
            verification_lines.extend(['Issues found:'] + issues_lines)
        if action_lines:
            verification_lines.extend(['Action responsible:'] + action_lines)
        if manager_note_lines:
            verification_lines.extend(['Manager notes:'] + manager_note_lines)
        h_meta += note_box_h(verification_lines or ['Verified']) + 14

    task_rows = []
    task_text_w = inner_w - 76
    for i, t in enumerate(c['tasks'], start=1):
        name_lines = wrap(f"{i}. {t['task_name']}", f_task, task_text_w)
        note_lines = wrap(t.get('note') or '', f_note, task_text_w - 26)
        row_h = 28 + wrapped_h(name_lines, f_task, 7)
        if note_lines:
            row_h += 18 + line_h(f_note_b) + 8 + wrapped_h(note_lines, f_note, 7)
        row_h = max(row_h, 74)
        task_rows.append((t, name_lines, note_lines, row_h))
    h_tasks = 92 + sum(row_h for _, _, _, row_h in task_rows) + CARD_PAD

    photo_gap = 24
    photo_cols = 1 if len(c['photos']) <= 8 else 2
    photo_w = (inner_w - (photo_cols - 1) * photo_gap) // photo_cols
    photo_label_h = 42
    fallback_photo_h = int(photo_w * 0.68)

    def trim_photo_borders(photo):
        """Remove large same-colour margins from screenshots/scans without
        cropping normal camera photos aggressively."""
        try:
            bg = Image.new(photo.mode, photo.size, photo.getpixel((0, 0)))
            diff = ImageChops.difference(photo, bg)
            diff = ImageOps.grayscale(diff).point(lambda p: 255 if p > 18 else 0)
            bbox = diff.getbbox()
            if not bbox:
                return photo
            iw, ih = photo.size
            margin = max(10, int(min(iw, ih) * 0.025))
            left, top, right, bottom = bbox
            bbox = (
                max(0, left - margin),
                max(0, top - margin),
                min(iw, right + margin),
                min(ih, bottom + margin),
            )
            cropped_w = bbox[2] - bbox[0]
            cropped_h = bbox[3] - bbox[1]
            if cropped_w < iw * 0.95 or cropped_h < ih * 0.95:
                return photo.crop(bbox)
        except Exception:
            pass
        return photo

    def photo_box_height(p: dict) -> int:
        src = os.path.join(UPLOAD_DIR, p['filename'])
        if not os.path.exists(src):
            return fallback_photo_h
        try:
            with Image.open(src) as src_img:
                src_img = trim_photo_borders(ImageOps.exif_transpose(src_img).convert('RGB'))
                iw, ih = src_img.size
            if iw > 0 and ih > 0:
                return min(max(int(photo_w * ih / iw), 360), 1280)
        except Exception:
            pass
        return fallback_photo_h
    photo_heights = [photo_box_height(p) for p in c['photos']]
    photo_layout = []
    photo_row_heights = []
    for row_start in range(0, len(c['photos']), photo_cols):
        row_items = []
        row_h = 0
        for col, ix in enumerate(range(row_start, min(row_start + photo_cols, len(c['photos'])))):
            tile_h = photo_label_h + photo_heights[ix]
            row_h = max(row_h, tile_h)
            row_items.append((ix, col))
        row_y = sum(photo_row_heights) + len(photo_row_heights) * photo_gap
        photo_row_heights.append(row_h)
        for ix, col in row_items:
            photo_layout.append({
                'index': ix,
                'col': col,
                'x': PAD + CARD_PAD + col * (photo_w + photo_gap),
                'y': row_y,
                'w': photo_w,
                'h': photo_heights[ix],
            })
    h_photos = 0
    if c['photos']:
        h_photos = 94 + sum(photo_row_heights) + max(0, len(photo_row_heights) - 1) * photo_gap + CARD_PAD

    total_h = h_header + 28 + h_meta + 24 + h_tasks + (h_photos + 24 if h_photos else 0) + h_footer + 30

    img = Image.new('RGB', (W, total_h), LIGHT_BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle((0, 0, W, h_header), fill=NAVY)
    draw.rectangle((0, h_header - 8, W, h_header), fill=color)
    logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((145, 145), Image.LANCZOS)
            lx, ly = PAD, 44
            _rounded(draw, (lx - 10, ly - 10, lx + logo.width + 10, ly + logo.height + 10),
                     radius=18, fill=(255, 255, 255))
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass
    text_x = PAD + 180
    title = (c['meta'].get('title') or c['type']).upper()
    section_label = (c['section'] or '').upper()
    draw.text((text_x, 42), title, fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 112), f"{section_label} CHECKLIST",
              fill=(255, 255, 255, 220), font=f_sub)
    try:
        date_pretty = datetime.strptime(c['date'], '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = c['date']
    draw.text((text_x, 158), date_pretty, fill=(255, 216, 216), font=f_body_b)

    y = h_header + 28

    # Meta card
    _rounded(draw, (PAD, y, W - PAD, y + h_meta), radius=18, fill=(255, 255, 255))
    draw.rectangle((PAD, y, PAD + 10, y + h_meta), fill=color)
    pct = round(c['done_tasks'] / c['total_tasks'] * 100) if c['total_tasks'] else 0
    pills = [
        (f'{c["done_tasks"]}/{c["total_tasks"]} TASKS', OK if pct >= 90 else (255, 152, 0)),
        (f'{pct}% COMPLETE', (33, 150, 243)),
        ('LATE' if c.get('is_late') else 'ON TIME', BAD if c.get('is_late') else OK),
    ]
    if c.get('verified'):
        pills.append(('VERIFIED', NAVY))

    px = PAD + CARD_PAD
    for txt, col in pills:
        tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 30
        _rounded(draw, (px, y + 26, px + tw, y + 66), radius=18, fill=col)
        draw.text((px + 15, y + 36), txt, fill=(255, 255, 255), font=f_chip)
        px += tw + 10

    my = y + 88
    for line in detail_lines:
        draw.text((PAD + CARD_PAD, my), line, fill=TEXT, font=f_body)
        my += line_h(f_body) + 8

    def draw_note_box(box_y: int, label: str, note_lines: list[str],
                      fill=(255, 249, 232), accent=(229, 156, 40)) -> int:
        box_h = note_box_h(note_lines)
        bx1, bx2 = PAD + CARD_PAD, W - PAD - CARD_PAD
        _rounded(draw, (bx1, box_y, bx2, box_y + box_h), radius=14, fill=fill,
                 outline=(245, 223, 166), width=1)
        draw.rectangle((bx1, box_y, bx1 + 7, box_y + box_h), fill=accent)
        draw.text((bx1 + 24, box_y + 18), label, fill=(103, 72, 21), font=f_note_b)
        ly = box_y + 18 + line_h(f_note_b) + 12
        for line in note_lines:
            draw.text((bx1 + 24, ly), line, fill=TEXT, font=f_note)
            ly += line_h(f_note) + 7
        return box_h

    if general_note_lines:
        my += 8
        my += draw_note_box(my, 'GENERAL NOTE', general_note_lines) + 14

    if c.get('verified') or manager_note_lines or issues_lines or action_lines:
        verification_lines = []
        if c.get('verified'):
            verified_by = c.get('verified_by') or '-'
            verified_at = (c.get('verified_at') or '-')[:16]
            verification_lines.extend(wrap(f"Verified by: {verified_by}    Verified at: {verified_at}", f_note, inner_w - 40))
        if c.get('overall_result'):
            verification_lines.extend(wrap(f"Result: {c.get('overall_result')}", f_note, inner_w - 40))
        if issues_lines:
            verification_lines.extend(['Issues found:'] + issues_lines)
        if action_lines:
            verification_lines.extend(['Action responsible:'] + action_lines)
        if manager_note_lines:
            verification_lines.extend(['Manager notes:'] + manager_note_lines)
        my += draw_note_box(my, 'MANAGER REVIEW', verification_lines or ['Verified'],
                            fill=(238, 246, 255), accent=(33, 150, 243)) + 14

    y += h_meta + 24

    # Tasks card
    _rounded(draw, (PAD, y, W - PAD, y + h_tasks), radius=18, fill=(255, 255, 255))
    draw.text((PAD + CARD_PAD, y + 24), 'TASKS', fill=NAVY, font=f_h2)
    ty = y + 78
    for i, (t, name_lines, note_lines, row_h) in enumerate(task_rows):
        # Status icon
        row_fill = (255, 255, 255) if i % 2 == 0 else (248, 250, 253)
        if not t['done']:
            row_fill = (255, 246, 246)
        _rounded(draw, (PAD + CARD_PAD - 10, ty, W - PAD - CARD_PAD + 10, ty + row_h - 10),
                 radius=12, fill=row_fill)
        icon_x = PAD + CARD_PAD
        if t['done']:
            _rounded(draw, (icon_x, ty + 19, icon_x + 32, ty + 51), radius=7, fill=OK)
            draw.text((icon_x + 8, ty + 22), '✓', fill=(255, 255, 255), font=f_chip)
            txt_color = TEXT
        else:
            _rounded(draw, (icon_x, ty + 19, icon_x + 32, ty + 51), radius=7, fill=BAD)
            draw.text((icon_x + 10, ty + 22), '×', fill=(255, 255, 255), font=f_chip)
            txt_color = BAD
        text_x = icon_x + 50
        line_y = ty + 18
        for line in name_lines:
            draw.text((text_x, line_y), line, fill=txt_color, font=f_task_b if not t['done'] else f_task)
            line_y += line_h(f_task) + 7
        if note_lines:
            note_y = line_y + 8
            draw.text((text_x, note_y), 'Note:', fill=(129, 91, 25), font=f_note_b)
            note_y += line_h(f_note_b) + 8
            for line in note_lines:
                draw.text((text_x + 26, note_y), line, fill=MUTED, font=f_note)
                note_y += line_h(f_note) + 7
        ty += row_h
    y += h_tasks + 24

    # Photos card
    if c['photos']:
        _rounded(draw, (PAD, y, W - PAD, y + h_photos), radius=18, fill=(255, 255, 255))
        draw.text((PAD + CARD_PAD, y + 24), f"PHOTO EVIDENCE ({len(c['photos'])})", fill=NAVY, font=f_h2)
        py = y + 82
        for item in photo_layout:
            ix = item['index']
            p = c['photos'][ix]
            px = item['x']
            ppy = py + item['y']
            tile_w = item['w']
            photo_h = item['h']
            src = os.path.join(UPLOAD_DIR, p['filename'])
            label = f"Photo {int(p.get('photo_number') or 0) + 1}"
            _rounded(draw, (px, ppy, px + 128, ppy + 34),
                     radius=16, fill=(0, 0, 0))
            draw.text((px + 18, ppy + 8), label, fill=(255, 255, 255), font=f_chip)
            ppy += photo_label_h
            if not os.path.exists(src):
                _rounded(draw, (px, ppy, px + tile_w, ppy + photo_h),
                         radius=16, fill=(245, 245, 245))
                draw.text((px + 150, ppy + photo_h // 2 - 12),
                          'photo missing', fill=MUTED, font=f_small)
                continue
            try:
                photo = trim_photo_borders(ImageOps.exif_transpose(Image.open(src)).convert('RGB'))
                frame = Image.new('RGB', (tile_w, photo_h), (235, 238, 244))
                photo.thumbnail((tile_w, photo_h), Image.LANCZOS)
                frame.paste(photo, ((tile_w - photo.width) // 2, (photo_h - photo.height) // 2))
                mask = Image.new('L', (tile_w, photo_h), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, tile_w, photo_h), radius=16, fill=255)
                img.paste(frame, (px, ppy), mask)
            except Exception:
                _rounded(draw, (px, ppy, px + tile_w, ppy + photo_h),
                         radius=16, fill=(245, 245, 245))
        y += h_photos + 24

    # Footer
    fy = total_h - h_footer
    draw.rectangle((0, fy, W, total_h), fill=NAVY)
    draw.text((PAD, fy + 22),
              f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}',
              fill=(255, 255, 255, 180), font=f_body)
    draw.text((PAD, fy + 54),
              'MCQ Mirrabooka Cafe — Vietnamese Street Food',
              fill=(255, 200, 200), font=f_small)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Per-temperature detail PNG ──────────────────────────────────────────────

def _temperature_detail(session_id: int) -> dict | None:
    with _conn() as conn:
        sess = conn.execute(
            'SELECT * FROM temp_sessions WHERE id=?', (session_id,)).fetchone()
        if not sess:
            return None
        sess = dict(sess)
        rows = conn.execute('''
            SELECT tr.*, COALESCE(ft.food_kind, 'cold') AS food_kind
            FROM temp_readings tr
            LEFT JOIN temp_food_templates ft
              ON ft.temp_type=? AND ft.food_name=tr.food_name
            WHERE tr.session_id=?
            ORDER BY tr.food_order''', (sess['type'], session_id)).fetchall()
        sess['readings'] = [dict(r) for r in rows]
        sess['meta']     = TEMPERATURES_META.get(sess['type'], {})
    return sess


def _temp_unsafe(kind: str, v) -> bool:
    if v is None:
        return False
    if kind == 'hot':
        return v < 60
    return v > 5


def build_temperature_png(session_id: int) -> bytes:
    from PIL import Image, ImageDraw

    t = _temperature_detail(session_id)
    if t is None:
        raise ValueError(f'Temperature session {session_id} not found')

    W = 1080
    PAD = 32
    NAVY = (26, 26, 46)
    LIGHT_BG = (244, 246, 250)
    MUTED = (110, 117, 125)
    OK = (46, 125, 50)
    BAD = (198, 40, 40)

    color = _hex_to_rgb(t['meta'].get('color'))

    f_title  = _font(38, bold=True)
    f_sub    = _font(22)
    f_h2     = _font(28, bold=True)
    f_body   = _font(22)
    f_body_b = _font(22, bold=True)
    f_small  = _font(17)
    f_food   = _font(22, bold=True)
    f_chip   = _font(15, bold=True)
    f_temp   = _font(20, bold=True)

    h_header = 200
    h_meta   = 130
    row_h    = 70
    h_table  = 90 + len(t['readings']) * row_h
    h_footer = 90
    total_h  = h_header + h_meta + h_table + h_footer + 30

    img = Image.new('RGB', (W, total_h), LIGHT_BG)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rectangle((0, 0, W, h_header), fill=NAVY)
    draw.rectangle((0, h_header - 8, W, h_header), fill=color)
    logo_path = os.path.join(STATIC_DIR, 'logo.png') if STATIC_DIR else ''
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((120, 120), Image.LANCZOS)
            lx, ly = PAD, 36
            _rounded(draw, (lx - 8, ly - 8, lx + logo.width + 8, ly + logo.height + 8),
                     radius=14, fill=(255, 255, 255))
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass
    text_x = PAD + 150
    draw.text((text_x, 36),
              (t['meta'].get('title') or t['type']).upper(),
              fill=(255, 255, 255), font=f_title)
    draw.text((text_x, 90), 'TEMPERATURE RECORD',
              fill=(255, 255, 255, 220), font=f_sub)
    try:
        date_pretty = datetime.strptime(t['date'], '%Y-%m-%d').strftime('%A, %d %b %Y').upper()
    except Exception:
        date_pretty = t['date']
    draw.text((text_x, 124), date_pretty, fill=(255, 200, 200), font=f_body_b)

    y = h_header + 24

    # Meta card
    _rounded(draw, (PAD, y, W - PAD, y + h_meta - 24), radius=14, fill=(255, 255, 255))
    unsafe_count = sum(
        1 for r in t['readings']
        for col in ('c1_temp', 'c2_temp', 'c3_temp', 'c4_temp', 'c5_temp')
        if _temp_unsafe(r['food_kind'], r[col])
    )
    discarded = sum(1 for r in t['readings'] if (r.get('discarded') or 'N').upper() == 'Y')
    pills = [
        (f'{len(t["readings"])} FOODS', OK),
        (f'{unsafe_count} OUT-OF-ZONE', BAD if unsafe_count else OK),
    ]
    if discarded:
        pills.append((f'{discarded} DISCARDED', BAD))

    px = PAD + 24
    for txt, col in pills:
        tw = draw.textbbox((0, 0), txt, font=f_chip)[2] + 24
        _rounded(draw, (px, y + 18, px + tw, y + 50), radius=14, fill=col)
        draw.text((px + 12, y + 26), txt, fill=(255, 255, 255), font=f_chip)
        px += tw + 8

    draw.text((PAD + 24, y + 70),
              f"Recorded by: {t.get('recorded_by') or '-'}    Checked by: {t.get('checked_by') or '-'}",
              fill=(51, 51, 51), font=f_body)
    y += h_meta

    # Table card
    _rounded(draw, (PAD, y, W - PAD, y + h_table - 12), radius=14, fill=(255, 255, 255))
    # Header row
    hx = PAD + 24
    draw.text((hx, y + 18), 'FOOD ITEM', fill=NAVY, font=f_h2)
    # Column labels
    col_xs = [W - PAD - 24 - i * 90 for i in reversed(range(5))]
    for ci, cx in enumerate(col_xs):
        draw.text((cx - 24, y + 24), f'C{ci+1}', fill=MUTED, font=f_small)
    ty = y + 62
    for r in t['readings']:
        kind = r['food_kind'] or 'cold'
        # Kind badge
        kind_label = 'HOT' if kind == 'hot' else 'COLD'
        kind_col   = (192, 57, 43) if kind == 'hot' else (21, 101, 192)
        kind_bg    = (255, 224, 178) if kind == 'hot' else (225, 245, 254)
        kw = draw.textbbox((0, 0), kind_label, font=f_chip)[2] + 18
        _rounded(draw, (PAD + 24, ty + 14, PAD + 24 + kw, ty + 42), radius=10, fill=kind_bg)
        draw.text((PAD + 32, ty + 20), kind_label, fill=kind_col, font=f_chip)
        # Food name
        name = r['food_name']
        name_line = _wrap_text(draw, name, f_food, 380)[0]
        draw.text((PAD + 24 + kw + 12, ty + 18), name_line, fill=(40, 40, 40), font=f_food)
        # Notes line
        if r.get('notes'):
            nx = PAD + 24 + kw + 12
            note_line = _wrap_text(draw, f"Note: {r['notes']}", f_small, 480)[0]
            draw.text((nx, ty + 46), note_line, fill=MUTED, font=f_small)
        # Discarded flag
        if (r.get('discarded') or 'N').upper() == 'Y':
            draw.text((PAD + 24 + kw + 12 + 380, ty + 18),
                      'DISCARDED', fill=BAD, font=f_chip)
        # Temp columns
        for ci, cx in enumerate(col_xs):
            v = r[f'c{ci+1}_temp']
            if v is None:
                draw.text((cx - 18, ty + 22), '—', fill=MUTED, font=f_temp)
                continue
            unsafe = _temp_unsafe(kind, v)
            col = BAD if unsafe else OK
            txt = f'{v:g}°'
            tw_ = draw.textbbox((0, 0), txt, font=f_temp)[2]
            draw.text((cx - tw_, ty + 22), txt, fill=col, font=f_temp)
        # Row separator
        draw.line((PAD + 24, ty + row_h, W - PAD - 24, ty + row_h),
                  fill=(238, 240, 243), width=1)
        ty += row_h
    y += h_table

    # Footer
    fy = total_h - h_footer
    draw.rectangle((0, fy, W, total_h), fill=NAVY)
    draw.text((PAD, fy + 22),
              f'Generated {datetime.now().strftime("%a %d %b %Y · %H:%M")}',
              fill=(255, 255, 255, 180), font=f_body)
    draw.text((PAD, fy + 54),
              'MCQ Mirrabooka Cafe — Vietnamese Street Food',
              fill=(255, 200, 200), font=f_small)

    out = BytesIO()
    img.save(out, 'PNG', optimize=True)
    out.seek(0)
    return out.getvalue()


# ── Routes ───────────────────────────────────────────────────────────────────

@whatsapp_bp.route('/')
@_login_required
def whatsapp_today():
    from datetime import date
    today_str = date.today().isoformat()
    data = _collect_today(today_str)
    return render_template('whatsapp_share.html',
        date=today_str, data=data,
        checklists_meta=CHECKLISTS_META,
        temperatures_meta=TEMPERATURES_META)


@whatsapp_bp.route('/png')
@_login_required
def whatsapp_png():
    from datetime import date
    date_str = request.args.get('date') or date.today().isoformat()
    try:
        png_bytes = build_daily_png(date_str)
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes)
    buf.seek(0)
    return send_file(buf, mimetype='image/png',
                     as_attachment=False,
                     download_name=f'MCQ_Daily_{date_str}.png')


@whatsapp_bp.route('/checklist/<int:session_id>.png')
@_login_required
def whatsapp_checklist_png(session_id):
    try:
        png_bytes = build_checklist_png(session_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes); buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=False,
                     download_name=f'MCQ_Checklist_{session_id}.png')


@whatsapp_bp.route('/temperature/<int:session_id>.png')
@_login_required
def whatsapp_temperature_png(session_id):
    try:
        png_bytes = build_temperature_png(session_id)
    except ValueError as e:
        return str(e), 404
    except Exception as e:
        return f'PNG generation failed: {type(e).__name__}: {e}', 500
    buf = BytesIO(png_bytes); buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=False,
                     download_name=f'MCQ_Temperature_{session_id}.png')
