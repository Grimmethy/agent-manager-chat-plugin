"""Routes for the multi-chat account registry (HUB0006, piece 2/7).

GET  /api/chat/accounts                  -> {'accounts': public_view+usage, 'live': label|null}
POST /api/chat/accounts/<label>/live     -> make that account live, same body as GET
(404 JSON for an unknown label, 400 for an invalid one)

The registry lives in accounts.py (piece 1). Neither endpoint accepts nor returns
a token or a token path: only accounts.public_view's fields (label, host, live)
are ever echoed back, and nothing here logs registry contents.

Each account also carries a 'usage' key (HUB0007 3/4): the per-account snapshot
read by usage.read_usage from <AM_STATUSLINE_DIR or statusline_writer.DEFAULT_DIR>/
limits.<label>.json, or None when no (valid) snapshot exists. read_usage returns
only percent/resetsAt/reset/stale fields -- never a token, token path, or file
path -- so the body stays token-free.
"""
import os
import time

from flask import Blueprint, jsonify

import accounts
from statusline_writer import DEFAULT_DIR
from usage import read_usage

# The app.py helper these views call (CHAT_STORAGE_DIR) is imported lazily inside
# each view: app.py imports THIS module to register the blueprint, so a top-level
# `from app import ...` would be a circular import (same rationale as routes/chat.py).
# accounts.py itself imports nothing from app, so it is safe at top level.

accounts_bp = Blueprint("accounts-bp", __name__)


def _accounts_body():
    """The single body shape both endpoints return -- public_view output (with a
    per-account 'usage' snapshot, or None when none exists) plus the live label
    (or None). Never contains tokenFile, a token, or a file path."""
    from app import CHAT_STORAGE_DIR
    usage_dir = os.environ.get('AM_STATUSLINE_DIR', DEFAULT_DIR)
    now = time.time()
    body_accounts = accounts.public_view(accounts.load(CHAT_STORAGE_DIR))
    for account in body_accounts:
        account["usage"] = read_usage(usage_dir, account["label"], now)
    return jsonify({
        "accounts": body_accounts,
        "live": accounts.live_label(CHAT_STORAGE_DIR),
    })


@accounts_bp.route("/api/chat/accounts", methods=["GET"])
def api_chat_accounts():
    """Every registered account (label, host, live, usage) plus which one is live --
    the account strip's data source. Empty list and null live when none exist;
    usage is null for an account with no limits snapshot."""
    return _accounts_body()


@accounts_bp.route("/api/chat/accounts/<label>/live", methods=["POST"])
def api_chat_accounts_make_live(label):
    """Switch the single live account to `label`. 400 for a malformed label,
    404 for a well-formed one that is not registered. Returns the same body
    as GET so the strip can re-render from one response."""
    if not accounts._valid_label(label):
        return jsonify({"error": "invalid account label"}), 400
    try:
        from app import CHAT_STORAGE_DIR
        accounts.make_live(CHAT_STORAGE_DIR, label)
    except KeyError:
        return jsonify({"error": "unknown account label"}), 404
    return _accounts_body()
