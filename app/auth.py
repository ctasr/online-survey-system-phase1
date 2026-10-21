from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .security import valid_csrf


auth_bp = Blueprint("auth", __name__)


def pending_survey_title():
    path = session.get("after_login", "")
    if not path.startswith("/survey/") or "/" in path[len("/survey/"):]:
        return None
    survey = get_db().surveys.find_one({"slug": path[len("/survey/"):]}, {"title": 1})
    return survey["title"] if survey else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(url_for("auth.dashboard"))

    error = None
    username = ""
    if request.method == "POST":
        if not valid_csrf():
            abort(400)

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")

        if not username:
            error = "用户名不能为空。"
        elif not 6 <= len(password) <= 12:
            error = "密码必须为 6～12 位。"
        elif password != confirmation:
            error = "两次输入的密码不一致。"
        else:
            try:
                get_db().users.insert_one(
                    {
                        "username": username,
                        "password_hash": generate_password_hash(password),
                        "created_at": datetime.now(timezone.utc),
                    }
                )
            except DuplicateKeyError:
                error = "用户名已被使用。"
            else:
                flash("注册成功，请登录。", "success")
                session["new_username"] = username
                return redirect(url_for("auth.login"))

    return render_template("register.html", error=error, username=username,
                           continue_survey_title=pending_survey_title())


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("auth.dashboard"))

    error = None
    username = session.pop("new_username", "")
    if request.method == "POST":
        if not valid_csrf():
            abort(400)

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().users.find_one({"username": username})

        if user is None or not check_password_hash(user["password_hash"], password):
            error = "用户名或密码错误。"
        else:
            after_login = session.get("after_login")
            session.clear()
            session["user_id"] = str(user["_id"])
            session.permanent = True
            return redirect(after_login or url_for("auth.dashboard"))

    return render_template("login.html", error=error, username=username,
                           continue_survey_title=pending_survey_title())


@auth_bp.get("/dashboard")
@login_required
def dashboard():
    from .surveys import active_questions, local_time, status_text, personal_answers_filter

    surveys = list(get_db().surveys.find({"owner_id": g.user["_id"]})
                   .sort("created_at", -1).limit(4))
    for survey in surveys:
        survey["visible_questions"] = active_questions(survey)
    return render_template("dashboard.html", surveys=surveys, status_text=status_text,
                           local_time=local_time,
                           response_count=get_db().responses.count_documents(personal_answers_filter()))


@auth_bp.post("/logout")
@login_required
def logout():
    if not valid_csrf():
        abort(400)
    session.clear()
    return redirect(url_for("index"))
