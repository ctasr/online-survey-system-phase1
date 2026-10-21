import secrets
import hashlib
import hmac
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeSerializer
from pymongo.errors import DuplicateKeyError
from werkzeug.datastructures import MultiDict

from .auth import login_required
from .db import get_db
from .jumps import MULTI_MODES, NUMBER_OPERATORS, build_jump_rule, jump_warnings, next_question_index
from .numbering import option_style
from .questions import QUESTION_TYPES, build_question, validate_answer
from .security import valid_csrf
from .sharing import survey_link
from .statistics import display_percent, summarize_responses, survey_overview


surveys_bp = Blueprint("surveys", __name__)
CHINA_TIME = timezone(timedelta(hours=8))


def local_time(value):
    if value is None:
        return "未设置"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHINA_TIME).strftime("%Y-%m-%d %H:%M")


def status_text(survey):
    deadline = survey.get("deadline")
    if survey["status"] == "published" and deadline:
        if deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
            return "已截止"
    return {"draft": "草稿", "published": "已发布", "closed": "已关闭"}[survey["status"]]


def parse_deadline(raw):
    if not raw:
        return None
    try:
        chosen = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError("截止时间格式不正确。") from error
    if chosen.tzinfo is not None:
        raise ValueError("截止时间格式不正确。")
    deadline = chosen.replace(tzinfo=CHINA_TIME).astimezone(timezone.utc)
    if deadline <= datetime.now(timezone.utc):
        raise ValueError("截止时间必须晚于当前时间。")
    return deadline


def owned_survey(survey_id):
    try:
        object_id = ObjectId(survey_id)
    except (InvalidId, TypeError):
        abort(404)
    survey = get_db().surveys.find_one({"_id": object_id, "owner_id": g.user["_id"]})
    if survey is None:
        abort(404)
    return survey


def active_questions(survey):
    return [question for question in survey.get("questions", []) if question.get("active", True)]


def save_questions(survey, questions, history=None, option_label_style=None):
    revision = survey.get("revision", 1)
    revision_filter = revision if "revision" in survey else {"$exists": False}
    changes = {"questions": questions, "updated_at": datetime.now(timezone.utc)}
    if history is not None:
        changes["question_history"] = history
    if option_label_style is not None:
        changes["option_label_style"] = option_label_style
    result = get_db().surveys.update_one(
        {"_id": survey["_id"], "owner_id": g.user["_id"],
         "status": survey["status"], "revision": revision_filter},
        {"$set": changes, "$inc": {"revision": 1}},
    )
    if not result.modified_count:
        abort(409)


def previous_version(question, history):
    history.append(deepcopy(question))
    question["version"] = question.get("version", 1) + 1


def survey_snapshot(survey):
    payload = {
        "slug": survey["slug"],
        "revision": survey.get("revision", 1),
        "versions": {q["id"]: q.get("version", 1) for q in active_questions(survey)},
        "fill_settings": fill_settings_snapshot(survey),
    }
    return URLSafeSerializer(current_app.secret_key, salt="survey-question-snapshot").dumps(payload)


def fill_settings_snapshot(survey):
    deadline = survey.get("deadline")
    return {
        "allow_anonymous": survey.get("allow_anonymous", False),
        "allow_multiple_submissions": survey.get("allow_multiple_submissions", True),
        "deadline": deadline.isoformat() if deadline else None,
    }


def submitted_snapshot(survey):
    try:
        payload = URLSafeSerializer(current_app.secret_key, salt="survey-question-snapshot").loads(
            request.form.get("question_snapshot", "")
        )
    except BadSignature:
        abort(400)
    if (not isinstance(payload, dict) or payload.get("slug") != survey["slug"]
            or not isinstance(payload.get("versions"), dict)):
        abort(400)
    return payload


def questions_from_snapshot(survey, versions):
    definitions = {
        (question["id"], question.get("version", 1)): question
        for question in survey.get("question_history", []) + survey.get("questions", [])
    }
    questions = []
    for question_id, version in versions.items():
        question = definitions.get((question_id, version))
        if question is None:
            return None
        questions.append(question)
    return questions


def preserved_form(form, questions, seen_versions):
    kept = MultiDict()
    kept.setlist("identity_mode", form.getlist("identity_mode"))
    changed = []
    for question in questions:
        question_id = question["id"]
        old_version = seen_versions.get(question_id)
        if old_version == question.get("version", 1):
            kept.setlist("q_" + question_id, form.getlist("q_" + question_id))
        else:
            changed.append(question_id)
    return kept, changed


def question_form_values(question):
    fields = {"type": question["type"], "title": question["title"],
              "required": "on" if question.get("required") else "",
              "options": "\n".join(question.get("options", [])),
              "integer_only": "on" if question.get("integer_only") else ""}
    fields["number_statistics"] = question.get("number_statistics", "distribution")
    for name in ("min_select", "max_select", "exact_select", "min_length", "max_length",
                 "min_value", "max_value"):
        value = question.get(name)
        fields[name] = "" if value is None else str(value)
    return fields


def changed_survey_page(survey, snapshot, preview=False):
    visible = active_questions(survey)
    seen_versions = snapshot.get("versions", {})
    kept, changed = preserved_form(request.form, visible, seen_versions)
    current = dict(survey)
    current["questions"] = visible
    same_questions = seen_versions == {
        question["id"]: question.get("version", 1) for question in visible
    }
    can_confirm_unchanged = (
        not preview
        and same_questions
        and snapshot.get("fill_settings") == fill_settings_snapshot(survey)
    )
    if not same_questions:
        message = "问卷在你填写期间发生改动。未改动题目的答案已保留；请核对当前顺序，改动或新增的题目需重新作答。"
    else:
        message = "问卷信息在你填写期间发生改动。原有答案已保留，请核对后重新提交。"
    return render_template(
        "surveys/public.html", survey=current, unavailable=False, error=message,
        error_question_id=None,
        form=kept, question_types=QUESTION_TYPES, question_snapshot=survey_snapshot(survey),
        preview=preview, survey_id=survey["_id"], changed_question_ids=changed,
        can_confirm_unchanged=can_confirm_unchanged,
    ), 409


class AnswerValidationError(ValueError):
    def __init__(self, message, question_id):
        super().__init__(message)
        self.question_id = question_id


def validated_answers(questions, form):
    answers = []
    index = 0
    while index < len(questions):
        question = questions[index]
        try:
            value = validate_answer(question, form)
        except ValueError as problem:
            raise AnswerValidationError(str(problem), question["id"]) from problem
        answers.append({
            "question_id": question["id"],
            "question_version": question.get("version", 1),
            "value": value,
        })
        index = next_question_index(questions, index, value)
    return answers


def assessed_answers(questions, form):
    reachable = []
    errors = {}
    answered = []
    index = 0
    while index < len(questions):
        question = questions[index]
        question_id = question["id"]
        reachable.append(question_id)
        raw = form.getlist("q_" + question_id)
        if any(str(value).strip() for value in raw):
            answered.append(question_id)
        try:
            value = validate_answer(question, form)
        except ValueError as problem:
            errors[question_id] = str(problem)
            value = None
        index = next_question_index(questions, index, value)
    return {"reachable": reachable, "answered": answered, "errors": errors}


def assess_response(survey):
    snapshot = submitted_snapshot(survey)
    questions = questions_from_snapshot(survey, snapshot["versions"])
    if questions is None:
        return jsonify(error="无法读取当前题目，请重新打开问卷。"), 409
    return jsonify(assessed_answers(questions, request.form))


def response_key(survey, user):
    epoch = survey.get("repeat_policy_epoch", 0)
    identity = f"{survey['_id']}:{user['_id']}"
    message = (identity if epoch == 0 else f"{identity}:{epoch}").encode("utf-8")
    secret = current_app.secret_key.encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def answer_history_key(user):
    return hmac.new(current_app.secret_key.encode("utf-8"),
                    f"answer-history:{user['_id']}".encode("utf-8"), hashlib.sha256).hexdigest()


def personal_answers_filter():
    return {"$or": [{"viewer_key": answer_history_key(g.user)}, {"respondent_id": g.user["_id"]}]}


@surveys_bp.get("/responses")
@login_required
def my_responses():
    page = max(1, request.args.get("page", 1, type=int))
    title_query = request.args.get("q", "").strip()[:100]
    submitted_on = request.args.get("date", "").strip()
    clauses = [personal_answers_filter()]
    if title_query:
        matching_ids = [survey["_id"] for survey in get_db().surveys.find(
            {"title": {"$regex": re.escape(title_query), "$options": "i"}}, {"_id": 1})]
        clauses.append({"$or": [{"survey_title": {"$regex": re.escape(title_query), "$options": "i"}},
                                {"survey_id": {"$in": matching_ids}}]})
    if submitted_on:
        try:
            day = datetime.strptime(submitted_on, "%Y-%m-%d").replace(tzinfo=CHINA_TIME)
        except ValueError:
            submitted_on = ""
        else:
            clauses.append({"submitted_at": {"$gte": day.astimezone(timezone.utc),
                                             "$lt": (day + timedelta(days=1)).astimezone(timezone.utc)}})
    query = {"$and": clauses} if len(clauses) > 1 else clauses[0]
    total = get_db().responses.count_documents(query)
    pages = max(1, (total + 19) // 20)
    page = min(page, pages)
    responses = list(get_db().responses.find(query).sort([("submitted_at", -1), ("_id", -1)])
                     .skip((page - 1) * 20).limit(20))
    surveys = {s["_id"]: s for s in get_db().surveys.find(
        {"_id": {"$in": [r["survey_id"] for r in responses]}}, {"title": 1})}
    return render_template("surveys/my_responses.html", responses=responses, surveys=surveys,
                           total=total, page=page, pages=pages, local_time=local_time,
                           title_query=title_query, submitted_on=submitted_on,
                           date_input_value=submitted_on or datetime.now(CHINA_TIME).date().isoformat())


@surveys_bp.get("/responses/<response_id>")
@login_required
def response_detail(response_id):
    try:
        object_id = ObjectId(response_id)
    except (InvalidId, TypeError):
        abort(404)
    query = {"_id": object_id, **personal_answers_filter()}
    response = get_db().responses.find_one(query)
    if response is None:
        abort(404)
    survey = get_db().surveys.find_one({"_id": response["survey_id"]}) or {}
    return render_response_detail(response, survey)


def render_response_detail(response, survey, *, owner_view=False, respondent_name=None):
    definitions = {(q["id"], q.get("version", 1)): q for q in
                   survey.get("question_history", []) + survey.get("questions", [])}
    question_numbers = {question["id"]: index for index, question in
                        enumerate(survey.get("questions", []), start=1)}
    rows = []
    for answer in response.get("answers", []):
        question = definitions.get((answer["question_id"], answer.get("question_version", 1)), {})
        rows.append({
            "number": question_numbers.get(answer["question_id"], len(rows) + 1),
            "title": question.get("title", "题目内容已不可用"),
            "type": question.get("type"),
            "options": question.get("options", []),
            "value": answer.get("value"),
        })
    page_size = 20
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page = min(max(request.args.get("page", 1, type=int) or 1, 1), page_count)
    return render_template("surveys/response_detail.html", response=response, survey=survey,
                           rows=rows[(page - 1) * page_size:page * page_size],
                           total_questions=len(rows), page=page, page_count=page_count,
                           page_size=page_size, local_time=local_time,
                           owner_view=owner_view, respondent_name=respondent_name)


def already_submitted(survey):
    if survey.get("allow_multiple_submissions", True):
        return False
    return get_db().responses.find_one({
        "survey_id": survey["_id"], "respondent_key": response_key(survey, g.user),
    }, {"_id": 1}) is not None


@surveys_bp.get("/surveys")
@login_required
def my_surveys():
    title_query = request.args.get("q", "").strip()[:100]
    status_filter = request.args.get("status", "")
    now = datetime.now(timezone.utc)
    query = {"owner_id": g.user["_id"]}
    if title_query:
        query["title"] = {"$regex": re.escape(title_query), "$options": "i"}
    if status_filter == "草稿":
        query["status"] = "draft"
    elif status_filter == "已关闭":
        query["status"] = "closed"
    elif status_filter == "已截止":
        query.update(status="published", deadline={"$lte": now})
    elif status_filter == "已发布":
        query.update(status="published", **{"$or": [{"deadline": None}, {"deadline": {"$gt": now}}]})
    else:
        status_filter = ""
    total = get_db().surveys.count_documents(query)
    pages = max(1, (total + 19) // 20)
    page = min(max(request.args.get("page", 1, type=int), 1), pages)
    surveys = list(get_db().surveys.find(query).sort("created_at", -1).skip((page - 1) * 20).limit(20))
    for survey in surveys:
        survey["visible_question_count"] = len(active_questions(survey))
    return render_template(
        "surveys/list.html", surveys=surveys, total=total, page=page, pages=pages,
        title_query=title_query, status_filter=status_filter,
        local_time=local_time, status_text=status_text,
    )


@surveys_bp.route("/surveys/new", methods=["GET", "POST"])
@login_required
def create_survey():
    error = None
    title = ""
    description = ""
    deadline_text = ""
    allow_anonymous = False
    repeat_policy = "multiple"
    option_label_style = "numeric"

    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        deadline_text = request.form.get("deadline", "").strip()
        allow_anonymous = request.form.get("allow_anonymous") == "on"
        repeat_policy = request.form.get("repeat_policy", "multiple")
        option_label_style = request.form.get("option_label_style", "numeric")

        if not title:
            error = "问卷标题不能为空。"
        elif repeat_policy not in ("multiple", "once"):
            error = "请选择正确的重复填写规则。"
        elif option_label_style not in ("numeric", "letters"):
            error = "请选择正确的选项编号样式。"
        else:
            try:
                deadline = parse_deadline(deadline_text)
            except ValueError as problem:
                error = str(problem)
            else:
                now = datetime.now(timezone.utc)
                for _ in range(5):
                    try:
                        result = get_db().surveys.insert_one({
                            "owner_id": g.user["_id"],
                            "slug": secrets.token_urlsafe(6),
                            "title": title,
                            "description": description,
                            "option_label_style": option_label_style,
                            "allow_anonymous": allow_anonymous,
                            "allow_multiple_submissions": repeat_policy == "multiple",
                            "repeat_policy_epoch": 0,
                            "status": "draft",
                            "deadline": deadline,
                            "questions": [],
                            "question_history": [],
                            "revision": 1,
                            "created_at": now,
                            "updated_at": now,
                        })
                        return redirect(url_for("surveys.survey_detail", survey_id=result.inserted_id))
                    except DuplicateKeyError:
                        continue
                abort(503)

    return render_template(
        "surveys/new.html", error=error, title=title, description=description,
        deadline_text=deadline_text, allow_anonymous=allow_anonymous,
        repeat_policy=repeat_policy,
        option_label_style=option_label_style,
    )


@surveys_bp.get("/surveys/<survey_id>")
@login_required
def survey_detail(survey_id):
    survey = owned_survey(survey_id)
    visible = active_questions(survey)
    reviews = jump_warnings(visible)
    public_path = url_for("surveys.public_survey", slug=survey["slug"])
    share_url, share_scope = survey_link(public_path)
    return render_template(
        "surveys/detail.html", survey=survey,
        share_url=share_url, share_scope=share_scope, public_path=public_path,
        visible_questions=visible, jump_warnings=reviews,
        warning_count=sum(len(items) for items in reviews.values()),
        removed_questions=[q for q in survey.get("questions", []) if not q.get("active", True)],
        local_time=local_time, status_text=status_text, question_types=QUESTION_TYPES,
        multi_modes=MULTI_MODES, number_operators=NUMBER_OPERATORS,
        question_titles={question["id"]: question["title"] for question in survey.get("questions", [])},
    )


@surveys_bp.post("/surveys/<survey_id>/delete")
@login_required
def delete_survey(survey_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    removed = get_db().surveys.delete_one({"_id": survey["_id"], "owner_id": g.user["_id"]})
    if not removed.deleted_count:
        abort(409)
    get_db().responses.delete_many({"survey_id": survey["_id"]})
    get_db().response_drafts.delete_many({"survey_id": survey["_id"]})
    flash("问卷及其答卷已删除。", "success")
    return redirect(url_for("surveys.my_surveys"))


@surveys_bp.route("/surveys/<survey_id>/edit", methods=["GET", "POST"])
@login_required
def edit_survey(survey_id):
    survey = owned_survey(survey_id)
    title = survey["title"]
    description = survey.get("description", "")
    original_deadline_text = local_time(survey["deadline"]).replace(" ", "T") if survey.get("deadline") else ""
    deadline_text = original_deadline_text
    allow_anonymous = survey.get("allow_anonymous", False)
    repeat_policy = "multiple" if survey.get("allow_multiple_submissions", True) else "once"
    option_label_style = option_style(survey)
    error = None
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        deadline_text = request.form.get("deadline", original_deadline_text).strip()
        if "repeat_policy" in request.form:
            allow_anonymous = request.form.get("allow_anonymous") == "on"
        repeat_policy = request.form.get("repeat_policy", repeat_policy)
        option_label_style = request.form.get("option_label_style", option_label_style)
        setting_changed = (
            allow_anonymous != survey.get("allow_anonymous", False)
            or (repeat_policy == "multiple") != survey.get("allow_multiple_submissions", True)
            or option_label_style != option_style(survey)
        )
        deadline_changed = survey["status"] != "closed" and deadline_text != original_deadline_text
        if not title:
            error = "问卷标题不能为空。"
        elif repeat_policy not in ("multiple", "once"):
            error = "请选择正确的重复填写规则。"
        elif option_label_style not in ("numeric", "letters"):
            error = "请选择正确的选项编号样式。"
        elif title == survey["title"] and description == survey.get("description", "") and not setting_changed and not deadline_changed:
            flash("问卷信息没有变化。", "success")
            return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
        else:
            if deadline_changed:
                try:
                    deadline = parse_deadline(deadline_text)
                except ValueError as problem:
                    error = str(problem)
            if error:
                return render_template("surveys/edit.html", survey=survey, title=title,
                                       description=description, deadline_text=deadline_text,
                                       allow_anonymous=allow_anonymous,
                                       repeat_policy=repeat_policy, option_label_style=option_label_style, error=error)
            changes = {"title": title, "description": description,
                       "updated_at": datetime.now(timezone.utc)}
            if deadline_changed:
                changes["deadline"] = deadline
            update = {"$set": changes}
            query = {"_id": survey["_id"], "owner_id": g.user["_id"],
                     "revision": survey.get("revision", 1)}
            update["$inc"] = {"revision": 1}
            if setting_changed:
                changes["option_label_style"] = option_label_style
                changes["allow_anonymous"] = allow_anonymous
                changes["allow_multiple_submissions"] = repeat_policy == "multiple"
                if repeat_policy == "once" and survey.get("allow_multiple_submissions", True):
                    changes["repeat_policy_epoch"] = survey.get("repeat_policy_epoch", 0) + 1
            if not get_db().surveys.update_one(query, update).modified_count:
                abort(409)
            flash("问卷信息已保存。正在填写的人会在提交时收到变更提示。", "success")
            return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    return render_template("surveys/edit.html", survey=survey, title=title,
                           description=description, deadline_text=deadline_text,
                           allow_anonymous=allow_anonymous,
                            repeat_policy=repeat_policy, option_label_style=option_label_style, error=error)


@surveys_bp.route("/surveys/<survey_id>/preview", methods=["GET", "POST"])
@login_required
def preview_survey(survey_id):
    survey = owned_survey(survey_id)
    questions = active_questions(survey)
    if not questions:
        flash("请先添加题目，再预览问卷。", "error")
        return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    error = None
    error_question_id = None
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        snapshot = submitted_snapshot(survey)
        if snapshot.get("revision") != survey.get("revision", 1) or snapshot.get("versions") != {
            q["id"]: q.get("version", 1) for q in questions
        }:
            return changed_survey_page(survey, snapshot, preview=True)
        try:
            answers = validated_answers(questions, request.form)
        except AnswerValidationError as problem:
            error, error_question_id = str(problem), problem.question_id
        else:
            latest = owned_survey(survey_id)
            if latest.get("revision", 1) != survey.get("revision", 1):
                return changed_survey_page(latest, snapshot, preview=True)
            return render_template("surveys/preview_done.html", survey=survey,
                                   answered_count=sum(answer["value"] is not None for answer in answers))
    view = dict(survey)
    view["questions"] = questions
    return render_template(
        "surveys/public.html", survey=view, unavailable=False, preview=True,
        survey_id=survey["_id"], error=error, error_question_id=error_question_id,
        form=request.form, question_types=QUESTION_TYPES, question_snapshot=survey_snapshot(survey),
    )


@surveys_bp.post("/surveys/<survey_id>/preview/next")
@login_required
def preview_next_question(survey_id):
    if not valid_csrf():
        abort(400)
    return next_step_response(owned_survey(survey_id))


@surveys_bp.post("/surveys/<survey_id>/preview/assess")
@login_required
def preview_assess(survey_id):
    if not valid_csrf():
        abort(400)
    return assess_response(owned_survey(survey_id))


@surveys_bp.route("/surveys/<survey_id>/questions/new", methods=["GET", "POST"])
@login_required
def add_question(survey_id):
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    error = None
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        try:
            option_label_style = request.form.get("option_label_style", option_style(survey))
            if option_label_style not in ("numeric", "letters"):
                raise ValueError("请选择正确的选项编号样式。")
            question = build_question(request.form)
        except ValueError as problem:
            error = str(problem)
        else:
            question["version"] = 1
            question["active"] = True
            questions = survey.get("questions", [])
            questions.append(question)
            save_questions(survey, questions, option_label_style=option_label_style)
            flash("题目已添加。", "success")
            if request.form.get("after_save") == "another":
                return redirect(url_for("surveys.add_question", survey_id=survey_id))
            return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    return render_template(
        "surveys/question_new.html", survey=survey, error=error,
        form=request.form, question_types=QUESTION_TYPES,
    )


@surveys_bp.route("/surveys/<survey_id>/questions/<question_id>/edit", methods=["GET", "POST"])
@login_required
def edit_question(survey_id, question_id):
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    questions = survey.get("questions", [])
    index = next((i for i, q in enumerate(questions) if q["id"] == question_id and q.get("active", True)), None)
    if index is None:
        abort(404)
    original = questions[index]
    error = None
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        try:
            option_label_style = request.form.get("option_label_style", option_style(survey))
            if option_label_style not in ("numeric", "letters"):
                raise ValueError("请选择正确的选项编号样式。")
            replacement = build_question(request.form)
        except ValueError as problem:
            error = str(problem)
        else:
            old_content = {key: value for key, value in original.items()
                           if key not in ("id", "version", "active", "jumps")}
            new_content = {key: value for key, value in replacement.items() if key != "id"}
            if original["type"] == "number":
                old_content.setdefault("number_statistics", "distribution")
            option_style_changed = option_label_style != option_style(survey)
            if new_content == old_content and not option_style_changed:
                flash("题目内容没有变化。", "success")
                return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
            if new_content == old_content:
                save_questions(survey, questions, option_label_style=option_label_style)
                flash("选项编号已更新。", "success")
                return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
            if (original["type"] == "number" and replacement["type"] == "number"
                    and {k: v for k, v in new_content.items() if k != "number_statistics"}
                    == {k: v for k, v in old_content.items() if k != "number_statistics"}):
                original["number_statistics"] = replacement["number_statistics"]
                save_questions(survey, questions, option_label_style=option_label_style)
                flash("统计方式已更新，已有答卷会按新方式显示。", "success")
                return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
            history = list(survey.get("question_history", []))
            previous_version(original, history)
            replacement["id"] = question_id
            replacement["version"] = original["version"]
            replacement["active"] = True
            replacement["jumps"] = original.get("jumps", [])
            questions[index] = replacement
            save_questions(survey, questions, history, option_label_style=option_label_style)
            message = "题目已更新；正在填写的人会在提交时收到提示，并重新回答这道题。" if survey["status"] == "published" else "题目已更新。"
            warning_count = sum(map(len, jump_warnings(active_questions({"questions": questions})).values()))
            if warning_count:
                message += f" 当前还有 {warning_count} 条跳转提醒，请在问卷详情中查看。"
            flash(message, "success")
            return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    return render_template("surveys/question_new.html", survey=survey, question=original,
                           form=request.form if request.method == "POST" else question_form_values(original),
                           error=error, question_types=QUESTION_TYPES)


@surveys_bp.post("/surveys/<survey_id>/questions/<question_id>/delete")
@login_required
def delete_question(survey_id, question_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    questions = survey.get("questions", [])
    target = next((q for q in questions if q["id"] == question_id and q.get("active", True)), None)
    if target is None:
        abort(404)
    if len(active_questions(survey)) <= 1 and survey["status"] == "published":
        flash("已发布问卷至少需要保留一道题目。", "error")
        return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    history = list(survey.get("question_history", []))
    target["active"] = False
    affected = 0
    for question in questions:
        if not question.get("active", True):
            continue
        rules = question.get("jumps", [])
        remaining = [rule for rule in rules if rule.get("target_id") != question_id]
        if len(remaining) != len(rules):
            previous_version(question, history)
            question["jumps"] = remaining
            affected += len(rules) - len(remaining)
    save_questions(survey, questions, history)
    message = "题目已从后续填写中移除，旧答卷与历史统计仍保留。"
    if affected:
        message += f" 指向该题的 {affected} 条跳转规则已移除。"
    flash(message, "success")
    return redirect(url_for("surveys.survey_detail", survey_id=survey_id))


@surveys_bp.get("/surveys/<survey_id>/questions/<question_id>/jumps/new")
@login_required
def new_jump(survey_id, question_id):
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    questions = active_questions(survey)
    source_index = next((i for i, question in enumerate(questions) if question["id"] == question_id), None)
    if source_index is None:
        abort(404)
    question = questions[source_index]
    targets = questions[source_index + 1:]
    if question["type"] not in ("single_choice", "multiple_choice", "number") or not targets:
        abort(404)
    return render_template("surveys/jump_new.html", survey=survey, question=question,
                           question_number=source_index + 1,
                           targets=list(enumerate(targets, start=source_index + 2)),
                           multi_modes=MULTI_MODES, number_operators=NUMBER_OPERATORS,
                           question_titles={item["id"]: item["title"] for item in questions})


@surveys_bp.post("/surveys/<survey_id>/questions/<question_id>/jumps")
@login_required
def add_jump(survey_id, question_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    questions = active_questions(survey)
    source_index = next((i for i, question in enumerate(questions) if question["id"] == question_id), None)
    if source_index is None:
        abort(404)
    try:
        rule = build_jump_rule(questions, source_index, request.form)
    except ValueError as problem:
        flash(str(problem), "error")
    else:
        history = list(survey.get("question_history", []))
        source = questions[source_index]
        previous_version(source, history)
        source.setdefault("jumps", []).append(rule)
        save_questions(survey, survey["questions"], history)
        count = len(jump_warnings(questions).get(question_id, []))
        flash("跳转规则已添加。" + (f" 本题有 {count} 条跳转提醒，请查看优先级。" if count else ""), "success")
    return redirect(jump_return_url(survey_id, question_id))


def jump_return_url(survey_id, question_id):
    if request.form.get("return_to") == "editor":
        return url_for("surveys.new_jump", survey_id=survey_id, question_id=question_id)
    return url_for("surveys.survey_detail", survey_id=survey_id)


@surveys_bp.post("/surveys/<survey_id>/questions/<question_id>/jumps/<rule_id>/delete")
@login_required
def delete_jump(survey_id, question_id, rule_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    source = next((q for q in active_questions(survey) if q["id"] == question_id), None)
    if source is None or not any(rule["id"] == rule_id for rule in source.get("jumps", [])):
        abort(404)
    history = list(survey.get("question_history", []))
    previous_version(source, history)
    source["jumps"] = [rule for rule in source["jumps"] if rule["id"] != rule_id]
    save_questions(survey, survey["questions"], history)
    flash("跳转规则已删除。", "success")
    return redirect(jump_return_url(survey_id, question_id))


@surveys_bp.post("/surveys/<survey_id>/questions/<question_id>/jumps/<rule_id>/move")
@login_required
def move_jump(survey_id, question_id, rule_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] not in ("draft", "published"):
        abort(409)
    source = next((q for q in active_questions(survey) if q["id"] == question_id), None)
    if source is None:
        abort(404)
    rules = source.get("jumps", [])
    index = next((i for i, rule in enumerate(rules) if rule["id"] == rule_id), None)
    if index is None:
        abort(404)
    direction = request.form.get("direction")
    if direction not in ("up", "down"):
        abort(400)
    target = index + (-1 if direction == "up" else 1)
    if target < 0 or target >= len(rules):
        return redirect(jump_return_url(survey_id, question_id))
    history = list(survey.get("question_history", []))
    previous_version(source, history)
    rules[index], rules[target] = rules[target], rules[index]
    save_questions(survey, survey["questions"], history)
    flash("跳转优先级已调整。", "success")
    return redirect(jump_return_url(survey_id, question_id))


@surveys_bp.post("/surveys/<survey_id>/deadline")
@login_required
def set_deadline(survey_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    try:
        deadline = parse_deadline(request.form.get("deadline", "").strip())
    except ValueError as problem:
        flash(str(problem), "error")
    else:
        old_deadline = survey.get("deadline")
        if old_deadline is not None:
            old_deadline = old_deadline.replace(tzinfo=timezone.utc)
        if deadline == old_deadline:
            flash("截止时间没有变化。", "success")
        else:
            updated = get_db().surveys.update_one(
                {"_id": survey["_id"], "owner_id": g.user["_id"],
                 "revision": survey.get("revision", 1)},
                {"$set": {"deadline": deadline, "updated_at": datetime.now(timezone.utc)},
                 "$inc": {"revision": 1}},
            )
            if not updated.modified_count:
                abort(409)
            flash("截止时间已更新。正在填写的人会在提交时收到变更提示。", "success")
    return redirect(url_for("surveys.survey_detail", survey_id=survey_id))


@surveys_bp.post("/surveys/<survey_id>/publish")
@login_required
def publish(survey_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] != "draft":
        abort(409)
    if not active_questions(survey):
        flash("请先添加题目，再发布问卷。", "error")
        return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    now = datetime.now(timezone.utc)
    deadline = survey.get("deadline")
    if deadline and deadline.replace(tzinfo=timezone.utc) <= now:
        flash("截止时间已过，请先设置新的截止时间。", "error")
    else:
        get_db().surveys.update_one(
            {"_id": survey["_id"], "owner_id": g.user["_id"], "status": "draft"},
            {"$set": {"status": "published", "published_at": now, "updated_at": now}},
        )
        count = sum(len(items) for items in jump_warnings(active_questions(survey)).values())
        flash("问卷已发布。" + (f" 有 {count} 条跳转提醒，建议在详情页检查优先级。" if count else ""), "success")
    return redirect(url_for("surveys.survey_detail", survey_id=survey_id))


@surveys_bp.post("/surveys/<survey_id>/close")
@login_required
def close(survey_id):
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] != "published":
        abort(409)
    now = datetime.now(timezone.utc)
    get_db().surveys.update_one(
        {"_id": survey["_id"], "owner_id": g.user["_id"], "status": "published"},
        {"$set": {"status": "closed", "closed_at": now, "updated_at": now}},
    )
    flash("问卷已关闭。", "success")
    return redirect(url_for("surveys.survey_detail", survey_id=survey_id))


@surveys_bp.get("/surveys/<survey_id>/statistics")
@login_required
def statistics(survey_id):
    survey = owned_survey(survey_id)
    responses = get_db().responses.find(
        {"survey_id": survey["_id"]}, {"answers": 1, "respondent_id": 1, "submitted_at": 1})
    results, overview = summarize_responses(survey.get("questions", []), responses,
                                            survey.get("question_history", []), return_overview=True)
    current_results = [item for item in results if item["current_definition"] and not item["historical"]]
    view = request.args.get("view")
    if view not in ("table", "cards"):
        view = "table" if len(current_results) > 5 else "cards"
    query = request.args.get("q", "").strip()
    if query:
        current_results = [item for item in current_results if (query.isdecimal() and str(item["index"]) == query)
                           or query.casefold() in item["question"]["title"].casefold()]
    for item in current_results:
        kind = item["question"]["type"]
        if kind in ("single_choice", "multiple_choice") and item["answered_count"] and item["option_counts"]:
            option, count = max(item["option_counts"].items(), key=lambda pair: pair[1])
            item["compact_summary"] = f"{option} · {count} {'份' if kind == 'single_choice' else '次'}"
        elif kind == "number" and item["number_frequencies"]:
            if item["question"].get("number_statistics") == "measure":
                item["compact_summary"] = f"平均值 {item['average']}"
            else:
                value, count = item["number_frequencies"][0]
                item["compact_summary"] = f"最常见回答 {value} · {count} 份"
        elif kind == "text" and item["answered_count"]:
            item["compact_summary"] = f"{item['distinct_count']} 种回答"
        else:
            item["compact_summary"] = "暂无回答"
    page_size = 25 if view == "table" else 10
    page_count = max(1, (len(current_results) + page_size - 1) // page_size)
    page = min(max(request.args.get("page", 1, type=int) or 1, 1), page_count)
    historical_results = [item for item in results if item["historical"]]
    historical_pages = max(1, (len(historical_results) + 9) // 10)
    historical_page = min(max(request.args.get("hpage", 1, type=int) or 1, 1), historical_pages)
    return render_template(
        "surveys/statistics.html", survey=survey, response_count=overview["response_count"],
        overview=overview, local_time=local_time,
        current_results=current_results[(page - 1) * page_size:page * page_size],
        historical_results=historical_results[(historical_page - 1) * 10:historical_page * 10],
        historical_total=len(historical_results), historical_page=historical_page,
        historical_pages=historical_pages,
        total_questions=len(current_results), all_question_count=sum(
            item["current_definition"] and not item["historical"] for item in results),
        page=page, page_count=page_count, page_size=page_size, view=view, query=query,
        question_types=QUESTION_TYPES, display_percent=display_percent,
    )


@surveys_bp.get("/surveys/<survey_id>/statistics/<question_id>")
@login_required
def question_statistics(survey_id, question_id):
    survey = owned_survey(survey_id)
    if not any(question["id"] == question_id for question in survey.get("questions", [])):
        abort(404)
    responses = get_db().responses.find(
        {"survey_id": survey["_id"]}, {"answers": 1, "respondent_id": 1, "submitted_at": 1})
    results, overview = summarize_responses(survey.get("questions", []), responses,
                                            survey.get("question_history", []), return_overview=True)
    results = [item for item in results if item["question"]["id"] == question_id]
    answer_page = max(1, request.args.get("answer_page", 1, type=int) or 1)
    return render_template(
        "surveys/question_statistics.html", survey=survey, results=results,
        response_count=overview["response_count"], answer_page=answer_page,
        current_question=next(q for q in survey["questions"] if q["id"] == question_id),
        display_percent=display_percent,
    )


@surveys_bp.get("/surveys/<survey_id>/responses")
@login_required
def survey_account_responses(survey_id):
    survey = owned_survey(survey_id)
    username_query = request.args.get("q", "").strip()[:80]
    filters = {"survey_id": survey["_id"], "respondent_id": {"$exists": True, "$ne": None}}
    if username_query:
        user_ids = [user["_id"] for user in get_db().users.find(
            {"username": {"$regex": re.escape(username_query), "$options": "i"}}, {"_id": 1})]
        filters["respondent_id"] = {"$in": user_ids}
    total = get_db().responses.count_documents(filters)
    page_size = 20
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(request.args.get("page", 1, type=int) or 1, 1), page_count)
    responses = list(get_db().responses.find(filters, {
        "respondent_id": 1, "submitted_at": 1, "answers": 1,
    }).sort([("submitted_at", -1), ("_id", -1)]).skip((page - 1) * page_size).limit(page_size))
    for row in responses:
        row["answered_count"] = sum(answer.get("value") not in (None, "", [])
                                    for answer in row.get("answers", []))
    names = {user["_id"]: user["username"] for user in get_db().users.find(
        {"_id": {"$in": [row["respondent_id"] for row in responses]}},
        {"username": 1})}
    return render_template("surveys/account_responses.html", survey=survey,
                           responses=responses, names=names, total=total,
                           page=page, page_count=page_count, query=username_query,
                           local_time=local_time)


@surveys_bp.get("/surveys/<survey_id>/responses/<response_id>")
@login_required
def survey_account_response_detail(survey_id, response_id):
    survey = owned_survey(survey_id)
    try:
        response_id = ObjectId(response_id)
    except (InvalidId, TypeError):
        abort(404)
    response = get_db().responses.find_one({
        "_id": response_id, "survey_id": survey["_id"],
        "respondent_id": {"$exists": True, "$ne": None},
    })
    if response is None:
        abort(404)
    user = get_db().users.find_one({"_id": response["respondent_id"]}, {"username": 1})
    return render_response_detail(response, survey, owner_view=True,
                                  respondent_name=user["username"] if user else "账号已删除")


@surveys_bp.get("/surveys/<survey_id>/statistics/<question_id>/accounts")
@login_required
def question_account_responses(survey_id, question_id):
    survey = owned_survey(survey_id)
    try:
        version = int(request.args.get("version", "1"))
    except ValueError:
        abort(404)
    question = next((item for item in survey.get("question_history", []) + survey.get("questions", [])
                     if item["id"] == question_id and item.get("version", 1) == version), None)
    if question is None:
        abort(404)
    option_text = None
    option_index = request.args.get("option", type=int)
    if option_index is not None:
        options = question.get("options", [])
        if question["type"] not in ("single_choice", "multiple_choice") or not 0 <= option_index < len(options):
            abort(404)
        option_text = options[option_index]
    answer_query = request.args.get("q", "").strip()[:1000]
    if option_text is not None:
        answer_query = ""
    exact = request.args.get("exact") == "1"
    answer_filter = {"question_id": question_id}
    answer_filter["question_version"] = {"$in": [1, None]} if version == 1 else version
    if option_text is not None:
        answer_filter["value"] = option_text
    elif answer_query:
        answer_filter["value"] = answer_query if exact else {"$regex": re.escape(answer_query), "$options": "i"}
    filters = {"survey_id": survey["_id"], "respondent_id": {"$exists": True, "$ne": None},
               "answers": {"$elemMatch": answer_filter}}
    total = get_db().responses.count_documents(filters)
    page_size = 20
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(request.args.get("page", 1, type=int) or 1, 1), page_count)
    responses = list(get_db().responses.find(filters, {"respondent_id": 1, "submitted_at": 1, "answers": 1})
                     .sort([("submitted_at", -1), ("_id", -1)])
                     .skip((page - 1) * page_size).limit(page_size))
    names = {user["_id"]: user["username"] for user in get_db().users.find(
        {"_id": {"$in": [row["respondent_id"] for row in responses]}}, {"username": 1})}
    for row in responses:
        row["answer"] = next((answer.get("value") for answer in row.get("answers", [])
                              if answer.get("question_id") == question_id
                              and answer.get("question_version", 1) == version), None)
    return render_template("surveys/question_account_responses.html", survey=survey,
                           question=question, responses=responses, names=names,
                           total=total, page=page, page_count=page_count,
                           version=version, option_index=option_index, option_text=option_text,
                           query=answer_query, exact=exact, local_time=local_time)


@surveys_bp.route("/survey/<slug>", methods=["GET", "POST"])
def public_survey(slug):
    survey = get_db().surveys.find_one({"slug": slug})
    if survey is None or survey["status"] == "draft":
        abort(404)
    deadline = survey.get("deadline")
    expired = bool(deadline and deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc))
    if survey["status"] == "closed" or expired:
        if request.method == "POST":
            return render_template("surveys/public.html", survey=survey, unavailable=True), 409
        return render_template("surveys/public.html", survey=survey, unavailable=True)
    if g.user is None:
        session["after_login"] = request.path
        return redirect(url_for("auth.login"))
    if already_submitted(survey):
        if request.method == "POST":
            return render_template("surveys/public.html", survey=survey, already_submitted=True), 409
        return render_template("surveys/public.html", survey=survey, already_submitted=True)
    visible = active_questions(survey)
    public_view = dict(survey)
    public_view["questions"] = visible
    error = None
    error_question_id = None
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        snapshot = submitted_snapshot(survey)
        if snapshot.get("revision") != survey.get("revision", 1) or snapshot.get("versions") != {
            q["id"]: q.get("version", 1) for q in visible
        }:
            return changed_survey_page(survey, snapshot)
        questions = visible
        identity_mode = request.form.get("identity_mode", "anonymous")
        try:
            if survey["allow_anonymous"] and identity_mode not in ("anonymous", "named"):
                raise ValueError("请选择正确的填写身份方式。")
            answers = validated_answers(questions, request.form)
        except AnswerValidationError as problem:
            error, error_question_id = str(problem), problem.question_id
        except ValueError as problem:
            error = str(problem)
        else:
            if not answers:
                error = "这份问卷还没有题目。"
            else:
                latest = get_db().surveys.find_one({"_id": survey["_id"]})
                if latest is None:
                    abort(404)
                if latest["status"] != "published":
                    return render_template("surveys/public.html", survey=latest, unavailable=True), 409
                if latest.get("revision", 1) != survey.get("revision", 1):
                    return changed_survey_page(latest, snapshot)
                latest_deadline = latest.get("deadline")
                if latest_deadline and latest_deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
                    return render_template("surveys/public.html", survey=latest, unavailable=True), 409
                response_data = {
                    "survey_id": survey["_id"],
                    "survey_title": survey["title"],
                    "viewer_key": answer_history_key(g.user),
                    "respondent_id": None if survey["allow_anonymous"] and identity_mode == "anonymous" else g.user["_id"],
                    "answers": answers,
                    "submitted_at": datetime.now(timezone.utc),
                }
                if not survey.get("allow_multiple_submissions", True):
                    response_data["repeat_limited"] = True
                    response_data["respondent_key"] = response_key(survey, g.user)
                try:
                    inserted = get_db().responses.insert_one(response_data)
                except DuplicateKeyError:
                    return "你已提交过这份问卷，不能重复填写。", 409
                after_insert = get_db().surveys.find_one({"_id": survey["_id"]})
                if after_insert is None or after_insert["status"] != "published":
                    get_db().responses.delete_one({"_id": inserted.inserted_id})
                    if after_insert is None:
                        abort(404)
                    return render_template("surveys/public.html", survey=after_insert, unavailable=True), 409
                after_deadline = after_insert.get("deadline")
                if after_deadline and after_deadline.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
                    get_db().responses.delete_one({"_id": inserted.inserted_id})
                    return render_template("surveys/public.html", survey=after_insert, unavailable=True), 409
                if after_insert.get("revision", 1) != survey.get("revision", 1):
                    get_db().responses.delete_one({"_id": inserted.inserted_id})
                    return changed_survey_page(after_insert, snapshot)
                get_db().response_drafts.delete_one({"survey_id": survey["_id"], "user_id": g.user["_id"]})
                session["submitted_slug"] = slug
                return redirect(url_for("surveys.submitted", slug=slug))
    return render_template(
        "surveys/public.html", survey=public_view, unavailable=False,
        error=error, error_question_id=error_question_id if error else None,
        form=request.form, question_types=QUESTION_TYPES, question_snapshot=survey_snapshot(survey),
    )


@surveys_bp.route("/survey/<slug>/draft", methods=["GET", "POST", "DELETE"])
def response_draft(slug):
    if g.user is None:
        return jsonify(error="请先登录。"), 401
    survey = get_db().surveys.find_one({"slug": slug, "status": "published"}, {"_id": 1, "questions": 1, "revision": 1})
    if survey is None:
        abort(404)
    key = {"survey_id": survey["_id"], "user_id": g.user["_id"]}
    if request.method == "GET":
        saved = get_db().response_drafts.find_one(key, {"_id": 0, "answers": 1, "identity_mode": 1, "revision": 1})
        return jsonify(saved or {})
    if not valid_csrf():
        abort(400)
    if request.method == "DELETE":
        get_db().response_drafts.delete_one(key)
        return jsonify(saved=False)
    if request.content_length and request.content_length > 100_000:
        return jsonify(error="暂存内容过大。"), 413
    data = request.get_json(silent=True) or {}
    raw = data.get("answers", {})
    if not isinstance(raw, dict):
        return jsonify(error="答案格式不正确。"), 400
    allowed = {f"q_{question['id']}" for question in active_questions(survey)}
    answers = {}
    for key_name, value in raw.items():
        if key_name not in allowed:
            continue
        if isinstance(value, str) and len(value) <= 10_000:
            answers[key_name] = value
        elif isinstance(value, list) and len(value) <= 100 and all(isinstance(item, str) and len(item) <= 1000 for item in value):
            answers[key_name] = value
    identity_mode = data.get("identity_mode", "anonymous")
    if identity_mode not in ("anonymous", "named"):
        identity_mode = "anonymous"
    get_db().response_drafts.update_one(
        key, {"$set": {"answers": answers, "identity_mode": identity_mode,
                       "revision": survey.get("revision", 1), "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    return jsonify(saved=True)


@surveys_bp.post("/survey/<slug>/next")
def next_question(slug):
    if not valid_csrf():
        abort(400)
    if g.user is None:
        return jsonify(error="请先登录。"), 401
    survey = get_db().surveys.find_one({"slug": slug, "status": {"$ne": "draft"}})
    if survey is None:
        return jsonify(error="问卷当前无法填写。"), 409
    return next_step_response(survey)


@surveys_bp.post("/survey/<slug>/assess")
def public_assess(slug):
    if not valid_csrf():
        abort(400)
    if g.user is None:
        return jsonify(error="请先登录。"), 401
    survey = get_db().surveys.find_one({"slug": slug, "status": "published"})
    if survey is None:
        return jsonify(error="问卷当前无法填写。"), 409
    return assess_response(survey)


def next_step_response(survey):
    snapshot = submitted_snapshot(survey)
    questions = questions_from_snapshot(survey, snapshot["versions"])
    if questions is None:
        return jsonify(error="无法读取当前题目，请重新打开问卷。"), 409
    question_id = request.form.get("question_id", "")
    index = next((i for i, question in enumerate(questions) if question["id"] == question_id), None)
    if index is None:
        return jsonify(error="题目不存在。"), 404
    try:
        value = validate_answer(questions[index], request.form)
    except ValueError as problem:
        return jsonify(error=str(problem)), 422
    following = next_question_index(questions, index, value)
    return jsonify(next_question_id=questions[following]["id"] if following < len(questions) else None)


@surveys_bp.get("/survey/<slug>/submitted")
@login_required
def submitted(slug):
    if session.get("submitted_slug") != slug:
        abort(404)
    survey = get_db().surveys.find_one({"slug": slug, "status": {"$ne": "draft"}})
    if survey is None:
        abort(404)
    can_repeat = survey.get("allow_multiple_submissions", True) and status_text(survey) == "已发布"
    return render_template("surveys/submitted.html", survey=survey, can_repeat=can_repeat)
