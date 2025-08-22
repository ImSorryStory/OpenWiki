import os
from functools import wraps
from flask import session, redirect, url_for, flash, request
from typing import Dict, Tuple

def parse_users_file(path: str) -> Dict[str, dict]:
    users = {}
    if not os.path.exists(path):
        return users
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip().strip('"') for p in s.split(":")]
            if len(parts) < 4:
                continue
            login, password, first_name, last_name = parts[:4]
            users[login] = {
                "password": password,
                "first_name": first_name,
                "last_name": last_name,
                "is_admin": login == "Admin"
            }
    return users

def current_user():
    if "user" in session:
        return session["user"]
    return None

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or not u.get("is_admin"):
            flash("Требуются права администратора.", "error")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper
