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
from .question_bank import bank_bp, bank_api_bp


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

    from .navigation import init_navigation
    init_navigation(app)
    init_db(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(surveys_bp)
    app.register_blueprint(bank_bp)
    app.register_blueprint(bank_api_bp)
    app.register_blueprint(api_bp)

    @app.errorhandler(HTTPException)
    def http_error(error):
        response = error.get_response()
        if request.path.startswith("/api/v1/"):
            response.set_data(app.json.dumps({"error": error.description}))
            response.mimetype = "application/json"
            return response
        messages = {
            400: ("请求未能完成", "页面可能已过期，请返回原页面重新操作。"),
            401: ("请先登录", "登录后可以继续操作。"),
            403: ("暂时无法访问", "当前账号无法执行这项操作。"),
            404: ("页面不存在", "链接可能有误，或者内容已被移除、无法访问。"),
            405: ("此页面不支持该操作", "请返回原页面，使用页面提供的操作入口。"),
            409: ("操作状态已变化", "内容可能已在其他页面修改，请返回核对后重试。"),
            413: ("内容超出请求限制", "请返回原页面检查内容大小后重试。"),
            415: ("请求格式不正确", "请返回原页面重新操作。"),
            422: ("输入内容需要修改", "请返回原页面检查输入内容后重试。"),
        }
        title, message = messages.get(error.code, ("操作未完成", "请返回原页面检查后重试。"))
        if error.code != 404 and error.description != type(error).description:
            message = error.description
        response.set_data(render_template("error.html", error_title=title, error_message=message))
        response.mimetype = "text/html"
        return response

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
