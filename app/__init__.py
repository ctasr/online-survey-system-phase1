import os
import secrets
from datetime import timedelta
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from .auth import auth_bp
from .api import api_bp
from .db import init_db
from .security import csrf_token
from .surveys import surveys_bp


def _secret_key():
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured

    key_file = Path(__file__).resolve().parent.parent / "instance" / "secret_key"
    key_file.parent.mkdir(exist_ok=True)
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()


def create_app():
    load_dotenv()

    app = Flask(__name__)
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
    app.config["MONGO_DB_NAME"] = os.getenv("MONGO_DB_NAME", "survey_phase1")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_base_url and os.getenv("RENDER") == "true":
        public_base_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    app.config["PUBLIC_BASE_URL"] = public_base_url
    app.config["SECRET_KEY"] = _secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(os.getenv("SESSION_DAYS", "30")))
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = public_base_url.startswith("https://")

    init_db(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(surveys_bp)
    app.register_blueprint(api_bp)

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/v1/"):
            return jsonify(error=error.description), error.code
        if error.code == 404:
            return render_template("error.html", error_title="页面不存在",
                                   error_message="链接可能有误，或者这份问卷已经无法访问。"), 404
        return error

    @app.errorhandler(500)
    def server_error(error):
        if request.path.startswith("/api/v1/"):
            return jsonify(error="服务暂时无法完成请求，请稍后重试。"), 500
        return render_template("error.html", error_title="页面暂时打不开",
                               error_message="请稍后重试；如果问题持续，可以返回个人首页继续操作。"), 500
    app.jinja_env.globals["csrf_token"] = csrf_token
    from .numbering import question_label, option_label, option_style
    app.jinja_env.globals["question_label"] = question_label
    app.jinja_env.globals["option_label"] = option_label
    app.jinja_env.globals["option_style"] = option_style

    @app.before_request
    def load_user():
        g.user = None
        user_id = session.get("user_id")
        if not user_id:
            return
        try:
            object_id = ObjectId(user_id)
        except (InvalidId, TypeError):
            session.clear()
            return
        g.user = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]].users.find_one(
            {"_id": object_id}, {"username": 1}
        )
        if g.user is None:
            session.clear()

    @app.get("/")
    def index():
        if g.user is not None:
            return redirect(url_for("auth.dashboard"))
        return render_template("index.html")

    return app
