"""JSON interface for the first-stage questionnaire workflow."""

import secrets
from copy import deepcopy
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, abort, current_app, g, jsonify, request, session, url_for
from pymongo.errors import DuplicateKeyError
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .jumps import build_jump_rule, jump_warnings, next_question_index
from .numbering import option_style
from .questions import build_question, validate_answer
from .security import csrf_token, valid_csrf
from .sharing import survey_link
from .statistics import summarize_responses, survey_overview
from .surveys import (
    CHINA_TIME, active_questions, already_submitted, answer_history_key,
    fill_settings_snapshot, parse_deadline, personal_answers_filter,
    previous_version, response_key, save_questions, validated_answers,
    AnswerValidationError,
)


api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


def fail(message, status):
    return jsonify(error=message), status


@api_bp.errorhandler(HTTPException)
def api_http_error(error):
    return fail(error.description, error.code)


@api_bp.before_request
def protect_json_mutations():
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        if not valid_csrf():
            return fail("CSRF token 无效。", 400)
        if not request.is_json or not isinstance(request.get_json(silent=True), dict):
            return fail("请求体必须是 JSON 对象。", 415)


def payload():
    return request.get_json()


def must_login():
    if g.user is None:
        abort(401, description="请先登录。")


def owned(survey_id):
    must_login()
    try:
        object_id = ObjectId(survey_id)
    except (InvalidId, TypeError):
        abort(404, description="问卷不存在。")
    survey = get_db().surveys.find_one({"_id": object_id, "owner_id": g.user["_id"]})
    if survey is None:
        abort(404, description="问卷不存在。")
    return survey


def as_text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name}必须是文字。")
    return value.strip()


def optional_bool(data, key, default=False):
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key}必须是 true 或 false。")
    return value


def revision_check(survey, data):
    if "revision" in data and data["revision"] != survey.get("revision", 1):
        abort(409, description="问卷已更新，请重新读取后再修改。")


def iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()


def question_json(question):
    def encode(value):
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode(item) for item in value]
        return deepcopy(value)
    return encode(question)


def survey_json(survey, include_questions=True):
    path = url_for("surveys.public_survey", slug=survey["slug"])
    public_url, _ = survey_link(path)
    data = {
        "id": str(survey["_id"]), "slug": survey["slug"],
        "title": survey["title"], "description": survey.get("description", ""),
        "status": survey["status"], "revision": survey.get("revision", 1),
        "option_label_style": option_style(survey),
        "allow_anonymous": survey.get("allow_anonymous", False),
        "allow_multiple_submissions": survey.get("allow_multiple_submissions", True),
        "deadline": iso(survey.get("deadline")),
        "created_at": iso(survey.get("created_at")),
        "updated_at": iso(survey.get("updated_at")),
        "public_path": path,
        "public_url": public_url,
        "share_ready": public_url is not None,
    }
    if include_questions:
        data["questions"] = [question_json(q) for q in active_questions(survey)]
    return data


def form_for_question(data):
    fields = MultiDict()
    for key in ("type", "title", "min_select", "max_select", "exact_select",
                "min_length", "max_length", "min_value", "max_value", "number_statistics"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(f"{key}的格式不正确。")
            fields[key] = str(value)
    options = data.get("options", [])
    if options is not None:
        if not isinstance(options, list) or any(not isinstance(item, str) for item in options):
            raise ValueError("options 必须是文字数组。")
        fields["options"] = "\n".join(options)
    for key in ("required", "integer_only"):
        if optional_bool(data, key):
            fields[key] = "on"
    return fields


def form_for_jump(data):
    fields = MultiDict()
    for key in ("target_id", "option", "mode", "operator", "threshold"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(f"{key}的格式不正确。")
            fields[key] = str(value)
    matches = data.get("match_options", [])
    if not isinstance(matches, list) or any(not isinstance(item, str) for item in matches):
        raise ValueError("match_options 必须是文字数组。")
    fields.setlist("match_options", matches)
    return fields


def answer_form(data, questions=None):
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("answers 必须是以题目 ID 为键的对象。")
    definitions = {question['id']: question for question in questions or []}
    fields = MultiDict()
    for question_id, value in answers.items():
        if not isinstance(question_id, str):
            raise ValueError("题目 ID 格式不正确。")
        if value is None:
            continue
        question = definitions.get(question_id)
        if question is not None:
            multiple = question['type'] == 'multiple_choice'
            if multiple != isinstance(value, list):
                expected = '数组' if multiple else '单个文字或数字'
                raise ValueError(f"“{question['title']}”的答案必须是{expected}。")
        values = value if isinstance(value, list) else [value]
        if any(not isinstance(item, (str, int, float)) or isinstance(item, bool)
               for item in values):
            raise ValueError("答案必须是文字、数字或文字数组。")
        fields.setlist("q_" + question_id, [str(item) for item in values])
    return fields


@api_bp.get("/csrf")
def get_csrf():
    return jsonify(csrf_token=csrf_token())


@api_bp.post("/auth/register")
def register():
    data = payload()
    try:
        username = as_text(data.get("username"), "用户名")
        password = data.get("password")
        confirmation = data.get("confirmation")
        if not username:
            raise ValueError("用户名不能为空。")
        if not isinstance(password, str) or not 6 <= len(password) <= 12:
            raise ValueError("密码必须为 6 到 12 位。")
        if password != confirmation:
            raise ValueError("两次输入的密码不一致。")
    except ValueError as problem:
        return fail(str(problem), 422)
    try:
        result = get_db().users.insert_one({
            "username": username, "password_hash": generate_password_hash(password),
            "created_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return fail("用户名已被使用。", 409)
    return jsonify(id=str(result.inserted_id), username=username), 201


@api_bp.post("/auth/login")
def login():
    data = payload()
    if not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
        return fail("用户名和密码必须是文字。", 422)
    user = get_db().users.find_one({"username": data["username"].strip()})
    if user is None or not check_password_hash(user["password_hash"], data["password"]):
        return fail("用户名或密码错误。", 401)
    session.clear()
    session["user_id"] = str(user["_id"])
    session.permanent = True
    return jsonify(id=str(user["_id"]), username=user["username"], csrf_token=csrf_token())


@api_bp.get("/auth/me")
def me():
    must_login()
    return jsonify(id=str(g.user["_id"]), username=g.user["username"])


@api_bp.post("/auth/logout")
def logout():
    must_login()
    session.clear()
    return jsonify(ok=True)


@api_bp.route("/surveys", methods=["GET", "POST"])
def surveys_collection():
    must_login()
    if request.method == "GET":
        surveys = get_db().surveys.find({"owner_id": g.user["_id"]}).sort([("created_at", -1), ("_id", -1)])
        return jsonify(surveys=[survey_json(item, False) for item in surveys])
    data = payload()
    try:
        title = as_text(data.get("title"), "问卷标题")
        description = as_text(data.get("description", ""), "问卷说明")
        if not title:
            raise ValueError("问卷标题不能为空。")
        allow_anonymous = optional_bool(data, "allow_anonymous")
        allow_multiple = optional_bool(data, "allow_multiple_submissions", True)
        style = data.get("option_label_style", "numeric")
        if style not in ("numeric", "letters"):
            raise ValueError("选项编号只能是 numeric 或 letters。")
        raw_deadline = data.get("deadline")
        if raw_deadline is not None and not isinstance(raw_deadline, str):
            raise ValueError("截止时间格式不正确。")
        deadline = parse_deadline(raw_deadline or "")
    except ValueError as problem:
        return fail(str(problem), 422)
    now = datetime.now(timezone.utc)
    for _ in range(5):
        survey = {
            "owner_id": g.user["_id"], "slug": secrets.token_urlsafe(6),
            "title": title, "description": description,
            "option_label_style": style, "allow_anonymous": allow_anonymous,
            "allow_multiple_submissions": allow_multiple, "repeat_policy_epoch": 0,
            "status": "draft", "deadline": deadline, "questions": [],
            "question_history": [], "revision": 1,
            "created_at": now, "updated_at": now,
        }
        try:
            survey["_id"] = get_db().surveys.insert_one(survey).inserted_id
            return jsonify(survey=survey_json(survey)), 201
        except DuplicateKeyError:
            continue
    abort(503, description="暂时无法创建问卷。")


@api_bp.route("/surveys/<survey_id>", methods=["GET", "PATCH", "DELETE"])
def survey_item(survey_id):
    survey = owned(survey_id)
    if request.method == "GET":
        return jsonify(survey=survey_json(survey))
    data = payload()
    revision_check(survey, data)
    if request.method == "DELETE":
        result = get_db().surveys.delete_one({
            "_id": survey["_id"], "owner_id": g.user["_id"],
            "revision": survey.get("revision", 1), "status": survey["status"],
        })
        if not result.deleted_count:
            abort(409, description="问卷已更新，请重试。")
        get_db().responses.delete_many({"survey_id": survey["_id"]})
        get_db().response_drafts.delete_many({"survey_id": survey["_id"]})
        return jsonify(ok=True)
    allowed = {"title", "description", "allow_anonymous",
               "allow_multiple_submissions", "option_label_style", "deadline", "revision"}
    if set(data) - allowed:
        return fail("包含不支持修改的字段。", 422)
    try:
        changes = {}
        if "title" in data:
            changes["title"] = as_text(data["title"], "问卷标题")
            if not changes["title"]:
                raise ValueError("问卷标题不能为空。")
        if "description" in data:
            changes["description"] = as_text(data["description"], "问卷说明")
        for key in ("allow_anonymous", "allow_multiple_submissions"):
            if key in data:
                changes[key] = optional_bool(data, key)
        if "option_label_style" in data:
            if data["option_label_style"] not in ("numeric", "letters"):
                raise ValueError("选项编号只能是 numeric 或 letters。")
            changes["option_label_style"] = data["option_label_style"]
        if "deadline" in data:
            if survey["status"] == "closed":
                abort(409, description="已关闭的问卷不能修改截止时间。")
            if data["deadline"] is not None and not isinstance(data["deadline"], str):
                raise ValueError("截止时间格式不正确。")
            attempted_deadline = parse_deadline(data["deadline"] or "", allow_past=True)
            changes["deadline"] = (survey.get("deadline") if iso(attempted_deadline) == iso(survey.get("deadline"))
                                   else parse_deadline(data["deadline"] or ""))
    except ValueError as problem:
        return fail(str(problem), 422)
    defaults = {"description": "", "allow_anonymous": False,
                "allow_multiple_submissions": True, "option_label_style": option_style(survey)}
    changes = {key: value for key, value in changes.items()
               if (iso(value) != iso(survey.get(key)) if key == "deadline"
                   else value != survey.get(key, defaults.get(key)))}
    if not changes:
        return jsonify(survey=survey_json(survey))
    if (changes.get("allow_multiple_submissions") is False
            and survey.get("allow_multiple_submissions", True)):
        changes["repeat_policy_epoch"] = survey.get("repeat_policy_epoch", 0) + 1
    changes["updated_at"] = datetime.now(timezone.utc)
    result = get_db().surveys.update_one(
        {"_id": survey["_id"], "owner_id": g.user["_id"],
         "revision": survey.get("revision", 1)},
        {"$set": changes, "$inc": {"revision": 1}},
    )
    if not result.modified_count:
        abort(409, description="问卷已更新，请重试。")
    return jsonify(survey=survey_json(owned(survey_id)))


@api_bp.post("/surveys/<survey_id>/publish")
def publish_survey(survey_id):
    survey = owned(survey_id)
    revision_check(survey, payload())
    if survey["status"] != "draft":
        abort(409, description="只有草稿可以发布。")
    if not active_questions(survey):
        return fail("请先添加题目。", 422)
    deadline = survey.get("deadline")
    if deadline and deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        return fail("截止时间已过。", 422)
    now = datetime.now(timezone.utc)
    result = get_db().surveys.update_one(
        {"_id": survey["_id"], "owner_id": g.user["_id"], "status": "draft",
         "revision": survey.get("revision", 1)},
        {"$set": {"status": "published", "published_at": now, "updated_at": now}},
    )
    if not result.modified_count:
        abort(409, description="问卷已更新，请重试。")
    return jsonify(survey=survey_json(owned(survey_id)),
                   jump_warnings=jump_warnings(active_questions(survey)))


@api_bp.post("/surveys/<survey_id>/close")
def close_survey(survey_id):
    survey = owned(survey_id)
    revision_check(survey, payload())
    if survey["status"] != "published":
        abort(409, description="只有已发布问卷可以关闭。")
    now = datetime.now(timezone.utc)
    result = get_db().surveys.update_one(
        {"_id": survey["_id"], "owner_id": g.user["_id"], "status": "published",
         "revision": survey.get("revision", 1)},
        {"$set": {"status": "closed", "closed_at": now, "updated_at": now}},
    )
    if not result.modified_count:
        abort(409, description="问卷已更新，请重试。")
    return jsonify(survey=survey_json(owned(survey_id)))


@api_bp.route("/surveys/<survey_id>/questions", methods=["GET", "POST"])
def questions_collection(survey_id):
    survey = owned(survey_id)
    if request.method == "GET":
        return jsonify(questions=[question_json(q) for q in active_questions(survey)])
    if survey["status"] != "draft":
        abort(409, description="这份问卷不能修改题目。")
    data = payload()
    revision_check(survey, data)
    try:
        question = build_question(form_for_question(data))
    except ValueError as problem:
        return fail(str(problem), 422)
    question.update(version=1, active=True)
    questions = list(survey.get("questions", [])) + [question]
    save_questions(survey, questions)
    return jsonify(question=question_json(question),
                   revision=survey.get("revision", 1) + 1), 201


@api_bp.route("/surveys/<survey_id>/questions/<question_id>",
              methods=["GET", "PATCH", "DELETE"])
def question_item(survey_id, question_id):
    survey = owned(survey_id)
    questions = survey.get("questions", [])
    index = next((i for i, q in enumerate(questions)
                  if q["id"] == question_id and q.get("active", True)), None)
    if index is None:
        abort(404, description="题目不存在。")
    if request.method == "GET":
        return jsonify(question=question_json(questions[index]))
    if survey["status"] != "draft":
        abort(409, description="这份问卷不能修改题目。")
    data = payload()
    revision_check(survey, data)
    history = list(survey.get("question_history", []))
    if request.method == "DELETE":
        questions[index]["active"] = False
        for question in questions:
            if question.get("active", True):
                rules = question.get("jumps", [])
                remaining = [rule for rule in rules if rule["target_id"] != question_id]
                if len(remaining) != len(rules):
                    previous_version(question, history)
                    question["jumps"] = remaining
        save_questions(survey, questions, history)
        return jsonify(ok=True, revision=survey.get("revision", 1) + 1)
    try:
        merged = {**questions[index], **data}
        replacement = build_question(form_for_question(merged))
    except ValueError as problem:
        return fail(str(problem), 422)
    original = questions[index]
    old_content = {k: v for k, v in original.items()
                   if k not in ("id", "version", "active", "jumps", "library_question_id", "library_root_id", "library_version", "library_source_question_version")}
    new_content = {k: v for k, v in replacement.items() if k != "id"}
    if original["type"] == "number":
        old_content.setdefault("number_statistics", "distribution")
    if new_content == old_content:
        return jsonify(question=question_json(original), revision=survey.get("revision", 1))
    if (original["type"] == "number" and replacement["type"] == "number"
            and {k: v for k, v in new_content.items() if k != "number_statistics"}
            == {k: v for k, v in old_content.items() if k != "number_statistics"}):
        original["number_statistics"] = replacement["number_statistics"]
        save_questions(survey, questions)
        return jsonify(question=question_json(original), revision=survey.get("revision", 1) + 1)
    previous_version(original, history)
    replacement.update(id=question_id, version=original["version"], active=True,
                       jumps=original.get("jumps", []))
    questions[index] = replacement
    save_questions(survey, questions, history)
    return jsonify(question=question_json(replacement),
                   revision=survey.get("revision", 1) + 1,
                   jump_warnings=jump_warnings(active_questions({"questions": questions})))


@api_bp.post("/surveys/<survey_id>/questions/<question_id>/jumps")
def add_jump(survey_id, question_id):
    survey = owned(survey_id)
    if survey["status"] != "draft":
        abort(409, description="这份问卷不能修改跳转规则。")
    data = payload()
    revision_check(survey, data)
    questions = active_questions(survey)
    index = next((i for i, q in enumerate(questions) if q["id"] == question_id), None)
    if index is None:
        abort(404, description="题目不存在。")
    try:
        rule = build_jump_rule(questions, index, form_for_jump(data))
    except ValueError as problem:
        return fail(str(problem), 422)
    history = list(survey.get("question_history", []))
    previous_version(questions[index], history)
    questions[index].setdefault("jumps", []).append(rule)
    save_questions(survey, survey["questions"], history)
    return jsonify(rule=rule, revision=survey.get("revision", 1) + 1,
                   jump_warnings=jump_warnings(questions).get(question_id, [])), 201


@api_bp.route("/surveys/<survey_id>/questions/<question_id>/jumps/<rule_id>",
              methods=["PATCH", "DELETE"])
def jump_item(survey_id, question_id, rule_id):
    survey = owned(survey_id)
    if survey["status"] != "draft":
        abort(409, description="这份问卷不能修改跳转规则。")
    data = payload()
    revision_check(survey, data)
    source = next((q for q in active_questions(survey) if q["id"] == question_id), None)
    if source is None:
        abort(404, description="题目不存在。")
    rules = source.get("jumps", [])
    index = next((i for i, rule in enumerate(rules) if rule["id"] == rule_id), None)
    if index is None:
        abort(404, description="跳转规则不存在。")
    if request.method == "PATCH":
        direction = data.get("direction")
        if direction not in ("up", "down"):
            return fail("direction 只能是 up 或 down。", 422)
        target = index + (-1 if direction == "up" else 1)
        if target < 0 or target >= len(rules):
            return jsonify(rules=rules, revision=survey.get("revision", 1))
    history = list(survey.get("question_history", []))
    previous_version(source, history)
    if request.method == "DELETE":
        source["jumps"] = [rule for rule in rules if rule["id"] != rule_id]
    else:
        rules[index], rules[target] = rules[target], rules[index]
    save_questions(survey, survey["questions"], history)
    return jsonify(rules=source["jumps"], revision=survey.get("revision", 1) + 1)


@api_bp.get("/surveys/<survey_id>/statistics")
def survey_statistics(survey_id):
    survey = owned(survey_id)
    responses = get_db().responses.find({"survey_id": survey["_id"]},
                                        {"answers": 1, "respondent_id": 1, "submitted_at": 1})
    results, overview = summarize_responses(survey.get("questions", []), responses,
                                            survey.get("question_history", []), return_overview=True)
    overview["first_submission"] = iso(overview["first_submission"])
    overview["last_submission"] = iso(overview["last_submission"])
    return jsonify(survey_id=survey_id, response_count=overview["response_count"], overview=overview,
                   results=[{**row, "question": question_json(row["question"])} for row in results])


@api_bp.get("/surveys/<survey_id>/statistics/<question_id>")
def question_statistics(survey_id, question_id):
    survey = owned(survey_id)
    if not any(q["id"] == question_id for q in survey.get("questions", [])):
        abort(404, description="题目不存在。")
    responses = get_db().responses.find({"survey_id": survey["_id"]},
                                        {"answers": 1, "respondent_id": 1, "submitted_at": 1})
    results, overview = summarize_responses(survey.get("questions", []), responses,
                                            survey.get("question_history", []), return_overview=True)
    return jsonify(survey_id=survey_id, question_id=question_id,
                   response_count=overview["response_count"],
                   results=[{**row, "question": question_json(row["question"])} for row in results
                            if row["question"]["id"] == question_id])


def public_survey(slug):
    survey = get_db().surveys.find_one({"slug": slug})
    if survey is None or survey["status"] == "draft":
        abort(404, description="问卷不存在。")
    if survey["status"] != "published":
        abort(409, description="问卷已关闭。")
    deadline = survey.get("deadline")
    if deadline and deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        abort(409, description="问卷已截止。")
    return survey


@api_bp.get("/public/surveys/<slug>")
def public_survey_get(slug):
    survey = public_survey(slug)
    must_login()
    if already_submitted(survey):
        abort(409, description="你已提交过这份问卷。")
    return jsonify(survey=survey_json(survey))


@api_bp.post("/public/surveys/<slug>/assess")
def public_assess(slug):
    survey = public_survey(slug)
    must_login()
    if already_submitted(survey):
        abort(409, description="你已提交过这份问卷。")
    data = payload()
    try:
        form = answer_form(data, active_questions(survey))
    except ValueError as problem:
        return fail(str(problem), 422)
    questions = active_questions(survey)
    reachable = []
    answered = []
    errors = {}
    index = 0
    while index < len(questions):
        question = questions[index]
        question_id = question["id"]
        reachable.append(question_id)
        if any(value.strip() for value in form.getlist("q_" + question_id)):
            answered.append(question_id)
        try:
            value = validate_answer(question, form)
        except ValueError as problem:
            errors[question_id] = str(problem)
            value = None
        index = next_question_index(questions, index, value)
    return jsonify(reachable=reachable, answered=answered, errors=errors)


@api_bp.post("/public/surveys/<slug>/responses")
def submit_response(slug):
    survey = public_survey(slug)
    must_login()
    if already_submitted(survey):
        abort(409, description="你已提交过这份问卷。")
    data = payload()
    if data.get("revision") != survey.get("revision", 1):
        abort(409, description="问卷已修改，请重新读取题目后提交。")
    try:
        mode = data.get("identity_mode", "anonymous")
        if survey.get("allow_anonymous") and mode not in ("anonymous", "named"):
            raise ValueError("identity_mode 只能是 anonymous 或 named。")
        form = answer_form(data, active_questions(survey))
        answers = validated_answers(active_questions(survey), form)
    except AnswerValidationError as problem:
        return jsonify(error=str(problem), question_id=problem.question_id), 422
    except ValueError as problem:
        return fail(str(problem), 422)
    if not answers:
        return fail("这份问卷还没有题目。", 422)
    latest = get_db().surveys.find_one({"_id": survey["_id"]})
    if latest is None or latest["status"] != "published":
        abort(409, description="问卷当前无法填写。")
    if latest.get("revision", 1) != survey.get("revision", 1):
        abort(409, description="问卷已修改，请重新读取题目后提交。")
    deadline = latest.get("deadline")
    if deadline and deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        abort(409, description="问卷已截止。")
    response = {
        "survey_id": survey["_id"], "survey_title": survey["title"],
        "viewer_key": answer_history_key(g.user),
        "respondent_id": None if survey.get("allow_anonymous") and mode == "anonymous"
        else g.user["_id"],
        "answers": answers, "submitted_at": datetime.now(timezone.utc),
    }
    if not survey.get("allow_multiple_submissions", True):
        response["repeat_limited"] = True
        response["respondent_key"] = response_key(survey, g.user)
    try:
        inserted = get_db().responses.insert_one(response)
    except DuplicateKeyError:
        abort(409, description="你已提交过这份问卷。")
    after_insert = get_db().surveys.find_one({"_id": survey["_id"]})
    if (after_insert is None or after_insert["status"] != "published"
            or after_insert.get("revision", 1) != survey.get("revision", 1)
            or (after_insert.get("deadline")
                and after_insert["deadline"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc))):
        get_db().responses.delete_one({"_id": inserted.inserted_id})
        abort(409, description="问卷已修改、关闭或截止，请重新打开。")
    get_db().response_drafts.delete_one({"survey_id": survey["_id"], "user_id": g.user["_id"]})
    return jsonify(response_id=str(inserted.inserted_id),
                   submitted_at=iso(response["submitted_at"])), 201


@api_bp.get("/responses")
def my_responses():
    must_login()
    rows = get_db().responses.find(personal_answers_filter()).sort([("submitted_at", -1), ("_id", -1)])
    return jsonify(responses=[{
        "id": str(row["_id"]), "survey_id": str(row["survey_id"]),
        "survey_title": row.get("survey_title", ""),
        "submitted_at": iso(row["submitted_at"]),
    } for row in rows])


@api_bp.get("/responses/<response_id>")
def response_detail(response_id):
    must_login()
    try:
        object_id = ObjectId(response_id)
    except (InvalidId, TypeError):
        abort(404, description="答卷不存在。")
    row = get_db().responses.find_one({"_id": object_id, **personal_answers_filter()})
    if row is None:
        abort(404, description="答卷不存在。")
    survey = get_db().surveys.find_one({"_id": row["survey_id"]}) or {}
    definitions = {(q["id"], q.get("version", 1)): q for q in
                   survey.get("question_history", []) + survey.get("questions", [])}
    answers = [{
        **answer,
        "title": definitions.get((answer["question_id"],
                                  answer.get("question_version", 1)), {}).get("title"),
    } for answer in row.get("answers", [])]
    return jsonify(id=str(row["_id"]), survey_id=str(row["survey_id"]),
                   survey_title=row.get("survey_title", ""),
                   submitted_at=iso(row["submitted_at"]), answers=answers)
