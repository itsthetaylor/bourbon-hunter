"""
auth.py — authentication primitives: bcrypt password hashing + the Flask-Login
user model and loader. No hand-rolled session logic; Flask-Login manages the
signed session cookie.

Passwords are NEVER stored or logged in plaintext — only bcrypt hashes, produced
here and stored via db.create_user.
"""

import bcrypt
from flask_login import LoginManager, UserMixin

import db

login_manager = LoginManager()
login_manager.login_view = "login"  # where @login_required sends anonymous users
login_manager.login_message = "Please log in to view your collection."
login_manager.login_message_category = "error"

# bcrypt silently truncates input beyond 72 bytes; reject longer to avoid surprise.
BCRYPT_MAX_BYTES = 72


def hash_password(password):
    """Return a bcrypt hash string for `password`. Raises ValueError if too long."""
    pw = (password or "").encode("utf-8")
    if len(pw) == 0:
        raise ValueError("password is required")
    if len(pw) > BCRYPT_MAX_BYTES:
        raise ValueError("password too long (max 72 bytes)")
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    """Constant-time bcrypt check. Returns False on any malformed input."""
    try:
        return bcrypt.checkpw(
            (password or "").encode("utf-8"),
            (password_hash or "").encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


class User(UserMixin):
    """Minimal Flask-Login user. `id` is the integer users.id."""

    def __init__(self, id, email, is_admin=False):
        self.id = int(id)
        self.email = email
        self.is_admin = bool(is_admin)


@login_manager.user_loader
def load_user(user_id):
    row = db.get_user_by_id(int(user_id))
    return User(row["id"], row["email"], row["is_admin"]) if row else None
