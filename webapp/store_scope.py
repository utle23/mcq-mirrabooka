"""Shared multi-store scoping helpers.

Kept in its own module (no DB import) so both app.py and the blueprints
(packaging_routes, orders_routes, equipment_routes, ...) can import the same
logic without circular imports. These only read flask.session / flask.request.

Canonical key is stores.id, carried in session['store_id']. The legacy
session['branch'] text is display/back-compat only.
"""
from flask import session, request


def is_super_admin():
    """super_admin can view/report across every store."""
    return session.get('role') == 'super_admin'


def current_store_id():
    """The store the logged-in session operates within (writes target this)."""
    return session.get('store_id', 1)


def selected_store_scope():
    """Resolve the store scope for the CURRENT request (reads).
    Returns an int store_id, or None meaning 'all stores' (super_admin only).
    Normal users are always pinned to their own session store."""
    if is_super_admin():
        sel = (request.args.get('store', 'all') or 'all').strip()
        if sel in ('', 'all'):
            return None
        try:
            return int(sel)
        except (TypeError, ValueError):
            return None
    return current_store_id()


def store_filter_clause(alias=''):
    """SQL fragment + params to scope a SELECT to the right store.
    Returns ('1=1', []) for the super_admin all-stores view so callers can
    always safely append ' AND ' + clause."""
    sid = selected_store_scope()
    if sid is None:
        return ('1=1', [])
    col = (f'{alias}.store_id' if alias else 'store_id')
    return (f'{col} = ?', [sid])


def store_guard_clause(alias=''):
    """Like store_filter_clause but for by-id UPDATE/DELETE/SELECT: super_admin
    is unrestricted (ignores the ?store= view arg); everyone else is pinned to
    their own session store so they cannot touch another store's row.
    Returns (fragment, params), always safe to AND onto a WHERE."""
    if is_super_admin():
        return ('1=1', [])
    col = (f'{alias}.store_id' if alias else 'store_id')
    return (f'{col} = ?', [current_store_id()])
