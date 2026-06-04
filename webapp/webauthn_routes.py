"""Passkey / Face ID sign-in for MCQ Mirrabooka (WebAuthn).

Goal: "skip the password on a trusted device". A logged-in person registers a
passkey once on their phone/tablet/laptop; after that the device's Face ID /
Touch ID / Windows Hello signs them straight in. The shared passwords stay as a
fallback.

Design:
- One row per registered passkey in `webauthn_credentials`, carrying the role +
  branch of the session that created it, so a passkey login restores exactly the
  same access the password would have given.
- Discoverable credentials (resident keys) so the login button needs no username.
- rp_id / origin are derived from the live request, so it works on both
  http://localhost:5050 and the https PythonAnywhere domain without config.

Requires the `webauthn` package (see requirements.txt). The browser side uses
@simplewebauthn/browser from a CDN to handle the ArrayBuffer<->base64url plumbing.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, session, url_for)

# Import the WebAuthn library defensively: if it isn't installed yet (e.g. the
# code was deployed before `pip install -r requirements.txt`), the app must
# still boot and the password login must keep working — passkeys just stay off.
try:
    from webauthn import (generate_authentication_options,
                          generate_registration_options, options_to_json,
                          verify_authentication_response,
                          verify_registration_response)
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    from webauthn.helpers.structs import (AuthenticatorAttachment,
                                          AuthenticatorSelectionCriteria,
                                          PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement,
                                          UserVerificationRequirement)
    WEBAUTHN_AVAILABLE = True
except Exception:
    WEBAUTHN_AVAILABLE = False

webauthn_bp = Blueprint('webauthn', __name__, url_prefix='/webauthn')

DB_PATH: str | None = None
RP_NAME = 'MCQ Mirrabooka Cafe'

ROLE_LABELS = {'admin': 'Admin', 'kitchen': 'Kitchen', 'user': 'Staff'}


def _unavailable():
    return jsonify({'ok': False,
                    'error': 'Passkey support is not installed on the server yet.'}), 503


def init_webauthn(db_path: str) -> None:
    global DB_PATH
    DB_PATH = db_path
    with _conn() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS webauthn_credentials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            credential_id TEXT NOT NULL UNIQUE,
            public_key    TEXT NOT NULL,
            sign_count    INTEGER NOT NULL DEFAULT 0,
            user_handle   TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user',
            branch        TEXT NOT NULL DEFAULT '',
            label         TEXT NOT NULL DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now','localtime')),
            last_used_at  TEXT
        )''')


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('logged_in'):
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d


# ── Relying-party identity derived from the live request ─────────────────────

def _rp_id() -> str:
    """The registrable domain — host without port (e.g. 'localhost' or the
    PythonAnywhere hostname)."""
    return request.host.split(':')[0]


def _origin() -> str:
    """The exact page origin the browser will send, honouring the proxy's
    forwarded scheme so https is detected behind PythonAnywhere."""
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    return f'{proto}://{request.host}'


# ── Registration (must already be signed in — binds passkey to that role) ────

@webauthn_bp.route('/manage')
@_login_required
def manage():
    with _conn() as conn:
        creds = [dict(r) for r in conn.execute(
            'SELECT * FROM webauthn_credentials ORDER BY created_at DESC').fetchall()]
    for c in creds:
        c['role_label'] = ROLE_LABELS.get(c['role'], c['role'].title())
    return render_template('passkeys.html', creds=creds,
                           current_role=session.get('role', 'user'))


@webauthn_bp.route('/register/options', methods=['POST'])
@_login_required
def register_options():
    if not WEBAUTHN_AVAILABLE:
        return _unavailable()
    # Read the device label from a JSON body or a form field.
    label = ''
    if request.is_json:
        label = (request.get_json(silent=True) or {}).get('label', '')
    label = (label or request.form.get('label') or '').strip()
    if not label:
        label = f"{ROLE_LABELS.get(session.get('role'), 'Staff')} device"

    user_handle = secrets.token_bytes(16)

    with _conn() as conn:
        existing = [r['credential_id'] for r in conn.execute(
            'SELECT credential_id FROM webauthn_credentials').fetchall()]

    opts = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=RP_NAME,
        user_id=user_handle,
        user_name=label,
        user_display_name=label,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c)) for c in existing
        ],
    )
    session['wa_reg_challenge'] = bytes_to_base64url(opts.challenge)
    session['wa_reg_handle'] = bytes_to_base64url(user_handle)
    session['wa_reg_label'] = label
    return Response(options_to_json(opts), mimetype='application/json')


@webauthn_bp.route('/register/verify', methods=['POST'])
@_login_required
def register_verify():
    if not WEBAUTHN_AVAILABLE:
        return _unavailable()
    challenge = session.pop('wa_reg_challenge', None)
    handle = session.pop('wa_reg_handle', None)
    label = session.pop('wa_reg_label', '') or 'Device'
    if not challenge:
        return jsonify({'ok': False, 'error': 'Registration expired — please try again.'}), 400
    try:
        verification = verify_registration_response(
            credential=request.get_data(as_text=True),
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Could not register this device: {e}'}), 400

    cred_id = bytes_to_base64url(verification.credential_id)
    pub_key = bytes_to_base64url(verification.credential_public_key)
    try:
        with _conn() as conn:
            conn.execute('''INSERT INTO webauthn_credentials
                (credential_id, public_key, sign_count, user_handle, role, branch, label)
                VALUES (?,?,?,?,?,?,?)''',
                (cred_id, pub_key, verification.sign_count, handle or '',
                 session.get('role', 'user'), session.get('branch', ''), label))
    except sqlite3.IntegrityError:
        return jsonify({'ok': False, 'error': 'This device already has a passkey.'}), 400
    return jsonify({'ok': True, 'label': label})


@webauthn_bp.route('/delete/<int:cred_id>', methods=['POST'])
@_login_required
def delete_credential(cred_id):
    with _conn() as conn:
        conn.execute('DELETE FROM webauthn_credentials WHERE id=?', (cred_id,))
    return redirect(url_for('webauthn.manage'))


# ── Passwordless login ───────────────────────────────────────────────────────

@webauthn_bp.route('/login/options', methods=['POST'])
def login_options():
    if not WEBAUTHN_AVAILABLE:
        return _unavailable()
    opts = generate_authentication_options(
        rp_id=_rp_id(),
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session['wa_auth_challenge'] = bytes_to_base64url(opts.challenge)
    return Response(options_to_json(opts), mimetype='application/json')


@webauthn_bp.route('/login/verify', methods=['POST'])
def login_verify():
    if not WEBAUTHN_AVAILABLE:
        return _unavailable()
    challenge = session.pop('wa_auth_challenge', None)
    if not challenge:
        return jsonify({'ok': False, 'error': 'Sign-in expired — please try again.'}), 400

    body = request.get_json(silent=True) or {}
    cred_id = body.get('id') or body.get('rawId')
    if not cred_id:
        return jsonify({'ok': False, 'error': 'Malformed sign-in response.'}), 400

    with _conn() as conn:
        row = conn.execute(
            'SELECT * FROM webauthn_credentials WHERE credential_id=?', (cred_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': 'This passkey is not registered. Use your password.'}), 400

    try:
        verification = verify_authentication_response(
            credential=request.get_data(as_text=True),
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(),
            expected_origin=_origin(),
            credential_public_key=base64url_to_bytes(row['public_key']),
            credential_current_sign_count=row['sign_count'],
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Face ID sign-in failed: {e}'}), 400

    now = datetime.now()
    with _conn() as conn:
        conn.execute(
            'UPDATE webauthn_credentials SET sign_count=?, last_used_at=? WHERE id=?',
            (verification.new_sign_count, now.isoformat(timespec='seconds'), row['id']))

    session.clear()
    session.update({
        'logged_in':     True,
        'role':          row['role'],
        'branch':        row['branch'],
        'login_time':    now.strftime('%Y-%m-%d %H:%M'),
        'login_ts':      now.isoformat(timespec='seconds'),
        'last_activity': now.isoformat(timespec='seconds'),
    })
    session.permanent = True
    target = url_for('orders.kitchen') if row['role'] == 'kitchen' else url_for('dashboard')
    return jsonify({'ok': True, 'redirect': target})
