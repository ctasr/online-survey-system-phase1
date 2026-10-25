import hmac
import secrets

from flask import request, session


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf():
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if (not isinstance(supplied, str) or not isinstance(expected, str)
            or not supplied or not expected or not supplied.isascii() or not expected.isascii()):
        return False
    return hmac.compare_digest(supplied, expected)
