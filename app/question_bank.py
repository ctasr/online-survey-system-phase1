import hashlib
import hmac
import re
import secrets
from copy import deepcopy
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from pymongo.errors import DuplicateKeyError
from werkzeug.exceptions import HTTPException

from .auth import login_required
from .db import get_db
from .numbering import option_style
from .questions import QUESTION_TYPES, build_question, definition_content
from .security import valid_csrf
from .statistics import display_percent, summarize_responses


bank_bp = Blueprint("bank", __name__)
bank_api_bp = Blueprint("bank_api", __name__, url_prefix="/api/v1")
DEFINITION_KEYS = ("type", "title", "required", "options", "min_select", "max_select",
                   "exact_select", "min_length", "max_length", "min_value", "max_value",
                   "integer_only", "number_statistics")


def object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        abort(404)


def accessible_question(question_id, *, allow_used=False):
    item = get_db().question_library.find_one({"_id": object_id(question_id)})
    if item is None:
        abort(404)
    if item["owner_id"] != g.user["_id"] and not item.get("shared"):
        used = allow_used and get_db().surveys.find_one({"owner_id": g.user["_id"], "$or": [
            {"status": {"$ne": "draft"}, "questions.library_question_id": item["_id"]},
            {"questions": {"$elemMatch": {"library_question_id": item["_id"], "active": {"$ne": False}}}}
        ]}, {"_id": 1})
        if not used:
            abort(404)
    return item


def owned_question(question_id):
    item = accessible_question(question_id)
    if item["owner_id"] != g.user["_id"]:
        abort(404)
    return item


def question_fields(item):
    return {key: deepcopy(item[key]) for key in DEFINITION_KEYS if key in item}


def make_version(source, form, *, restoring=False):
    parsed = build_question(form)
    if source and not restoring:
        original = definition_content({key: source.get(key) for key in DEFINITION_KEYS})
        changed = definition_content({key: parsed.get(key) for key in DEFINITION_KEYS})
        if original == changed:
            raise ValueError("题目内容没有变化，请修改后再保存。")
    root_id = source["root_id"] if source else ObjectId()
    latest = get_db().question_library.find_one({"root_id": root_id}, sort=[("version", -1)]) if source else None
    document = {
        "root_id": root_id, "version": latest["version"] + 1 if latest else 1,
        "parent_id": source["_id"] if source else None,
        "owner_id": g.user["_id"], "shared": False,
        "created_at": datetime.now(timezone.utc),
        **question_fields(parsed),
    }
    if restoring:
        document["restored_from_id"] = source["_id"]
    result = get_db().question_library.insert_one(document)
    document["_id"] = result.inserted_id
    return document


def form_values(item):
    values = question_fields(item)
    values["options"] = "\n".join(values.get("options", []))
    values["required"] = "on" if values.get("required") else ""
    values["integer_only"] = "on" if values.get("integer_only") else ""
    for key in ("min_select", "max_select", "exact_select", "min_length", "max_length", "min_value", "max_value"):
        values[key] = "" if values.get(key) is None else str(values[key])
    return values


def version_groups(items):
    """Keep one visible entry per question while preserving every usable version."""
    groups = {}
    for item in items:
        groups.setdefault(item["root_id"], []).append(item)
    return [{"latest": versions[0], "versions": versions}
            for versions in (sorted(group, key=lambda row: row["version"], reverse=True)
                             for group in groups.values())]


LIBRARY_PAGE_SIZE = 12
LIBRARY_VALUES_PAGE_SIZE = 20


def page_number(value):
    try:
        return min(100000, max(1, int(value)))
    except (TypeError, ValueError):
        return 1


def navigation_context():
    """Carry only internal navigation fields through version and sharing actions."""
    context = {key: request.values[key][:100] for key in
               ("q", "bank", "return_bank", "survey", "from_survey", "from_question")
               if request.values.get(key)}
    for key in ("mine_page", "shared_page"):
        if request.values.get(key):
            context[key] = page_number(request.values[key])
    return context


def optional_owned_survey(survey_id):
    """Resolve a return destination without making an independent library item depend on it."""
    try:
        key = ObjectId(survey_id)
    except (InvalidId, TypeError):
        abort(404)
    survey = get_db().surveys.find_one({"_id": key})
    if survey is not None and survey["owner_id"] != g.user["_id"]:
        abort(404)
    return survey


def paged_version_groups(base_filter, text_query, page, *, refreshes=1):
    """Page question roots in MongoDB, then load only the selected roots' versions."""
    match = dict(base_filter)
    if text_query:
        match["title"] = {"$regex": re.escape(text_query), "$options": "i"}
    result = list(get_db().question_library.aggregate([
        {"$match": match},
        {"$sort": {"version": -1}},
        {"$group": {"_id": "$root_id", "match": {"$first": "$$ROOT"}}},
        {"$sort": {"match.created_at": -1, "_id": -1}},
        {"$facet": {"rows": [{"$skip": (page - 1) * LIBRARY_PAGE_SIZE},
                             {"$limit": LIBRARY_PAGE_SIZE}],
                    "count": [{"$count": "total"}]}}
    ]))[0]
    total = result["count"][0]["total"] if result["count"] else 0
    last_page = max(1, (total + LIBRARY_PAGE_SIZE - 1) // LIBRARY_PAGE_SIZE)
    if page > last_page:
        return paged_version_groups(base_filter, text_query, last_page, refreshes=refreshes)
    roots = [row["_id"] for row in result["rows"]]
    if not roots:
        return [], total, page
    version_filter = {"$and": [base_filter, {"root_id": {"$in": roots}}]}
    items = list(get_db().question_library.find(version_filter))
    available_ids = {item["_id"] for item in items}
    visible_rows = [row for row in result["rows"] if row["match"]["_id"] in available_ids]
    if len(visible_rows) != len(result["rows"]):
        if refreshes:
            return paged_version_groups(base_filter, text_query, page, refreshes=refreshes - 1)
        total -= len(result["rows"]) - len(visible_rows)
    grouped = {group["latest"]["root_id"]: group for group in version_groups(items)}
    groups = [grouped[row["_id"]] for row in visible_rows]
    for group, row in zip(groups, visible_rows):
        if text_query and text_query.casefold() not in group["latest"]["title"].casefold():
            group["matched_old_title"] = row["match"]["title"]
    return groups, total, page


def definition_changes(old, new):
    labels = {"type": "题型", "title": "题目内容", "required": "是否必答", "options": "选项",
              "min_select": "最少选择数", "max_select": "最多选择数", "exact_select": "固定选择数",
              "min_length": "最少字数", "max_length": "最多字数", "min_value": "最小值",
              "max_value": "最大值", "integer_only": "整数要求", "number_statistics": "统计方式"}
    return [label for key, label in labels.items() if old.get(key) != new.get(key)]


def creation_context(source):
    """Validate the destination before creating a reusable question."""
    from .surveys import owned_survey
    survey_id = source.get("survey", "")
    bank_id = source.get("bank", "")
    if survey_id and bank_id:
        abort(400)
    survey = owned_survey(survey_id) if survey_id else None
    if survey and survey["status"] != "draft":
        abort(409)
    bank = None
    if bank_id:
        bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
        if bank is None:
            abort(404)
    return survey, bank


def add_to_survey(survey, item):
    from .surveys import save_questions
    question = {"id": secrets.token_hex(8), "version": 1, "active": True,
                "jumps": [], "library_question_id": item["_id"],
                "library_root_id": item["root_id"], "library_version": item["version"],
                "library_source_question_version": 1,
                **question_fields(item)}
    save_questions(survey, list(survey.get("questions", [])) + [question])
    return question


@bank_bp.get("/question-library")
@login_required
def library(*, bank_form_name="", bank_error=None):
    db = get_db()
    query = request.args.get("q", "").strip()[:100]
    mine_page = page_number(request.args.get("mine_page"))
    shared_page = page_number(request.args.get("shared_page"))
    banks = list(db.question_banks.find({"owner_id": g.user["_id"]}).sort("name", 1))
    selected_bank = next((row for row in banks if str(row["_id"]) == request.args.get("bank")), None)
    if request.args.get("bank") and selected_bank is None:
        flash("该题库已不存在或无法访问，已显示全部可用题目。", "error")
        return redirect(url_for("bank.library", q=query, mine_page=mine_page, shared_page=shared_page))
    mine_groups, mine_total, mine_page = paged_version_groups({"owner_id": g.user["_id"]}, query, mine_page)
    shared_groups, shared_total, shared_page = paged_version_groups(
        {"shared": True, "owner_id": {"$ne": g.user["_id"]}}, query, shared_page)
    owner_names = {row["_id"]: row["username"] for row in db.users.find(
        {"_id": {"$in": list({group["latest"]["owner_id"] for group in shared_groups})}},
        {"username": 1})}
    context_args = {"q": query, "mine_page": mine_page, "shared_page": shared_page}
    if selected_bank:
        context_args["bank"] = str(selected_bank["_id"])
    return render_template("library/list.html", banks=banks,
                           mine_groups=mine_groups, shared_groups=shared_groups,
                           selected_bank=selected_bank, question_types=QUESTION_TYPES,
                           owner_names=owner_names, query=query, mine_total=mine_total,
                           shared_total=shared_total, mine_page=mine_page, shared_page=shared_page,
                           page_size=LIBRARY_PAGE_SIZE, context_args=context_args,
                           bank_form_name=bank_form_name, bank_error=bank_error)


@bank_bp.route("/question-library/new", methods=["GET", "POST"])
@login_required
def create_question():
    if request.method == "POST" and not valid_csrf():
        abort(400)
    values = request.form if request.method == "POST" else request.args
    context = {key: values[key][:100] for key in ("q", "filter_bank") if values.get(key)}
    for key in ("mine_page", "shared_page"):
        if values.get(key):
            context[key] = page_number(values[key])
    if values.get("return_library") == "1" and not values.get("survey"):
        context["return_library"] = "1"
    origin = None
    if values.get("from_survey") and not values.get("survey") and not values.get("bank"):
        origin = optional_owned_survey(values["from_survey"])
        if origin:
            context["from_survey"] = str(origin["_id"])
            if values.get("from_question"):
                context["from_question"] = values["from_question"][:100]
    try:
        survey, bank = creation_context(values)
    except HTTPException as problem:
        if request.method != "POST" or problem.code not in (404, 409):
            raise
        return render_template("library/form.html", source=None, form=request.form,
                               error="原问卷或题库已无法添加题目。本次输入已保留，尚未保存；可以另存到题目库。",
                               question_types=QUESTION_TYPES,
                               form_action=url_for("bank.create_question")), 409
    form_context = {"context_args": context}
    if origin:
        form_context.update(return_url=url_for("surveys.survey_detail", survey_id=origin["_id"],
                            _anchor="question-" + context["from_question"] if context.get("from_question") else ""),
                            return_label="返回原问卷")
    if survey:
        # The filter bank is a return destination, not a second place to save the new question.
        selection_context = {key: value for key, value in context.items() if key != "filter_bank"}
        if context.get("filter_bank"):
            selection_context["bank"] = context["filter_bank"]
        form_context["return_url"] = url_for("bank.choose_question", survey_id=survey["_id"], **selection_context)
    elif context.get("return_library") == "1":
        catalog_context = {key: value for key, value in context.items()
                           if key in ("q", "mine_page", "shared_page")}
        if bank:
            catalog_context["bank"] = bank["_id"]
        form_context["return_url"] = url_for("bank.library", **catalog_context)
    error = None
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        try:
            item = make_version(None, request.form)
        except ValueError as problem:
            error = str(problem)
        else:
            if survey:
                try:
                    add_to_survey(survey, item)
                except HTTPException as problem:
                    if problem.code != 409:
                        raise
                    flash("题目已保存到题目库，但问卷已发生变化，本次没有加入。请核对问卷后再选择。", "error")
                    latest = get_db().surveys.find_one({"_id": survey["_id"], "owner_id": g.user["_id"]})
                    context = {"survey": str(survey["_id"])} if latest and latest["status"] == "draft" else {}
                    if latest and latest["status"] != "draft":
                        context = {"from_survey": str(survey["_id"])}
                    return redirect(url_for("bank.question_detail", question_id=item["_id"], **context))
                flash("题目已保存并加入这份问卷。", "success")
                return redirect(form_context["return_url"])
            if bank:
                result = get_db().question_banks.update_one({"_id": bank["_id"], "owner_id": g.user["_id"]},
                                                          {"$addToSet": {"question_ids": item["_id"]}})
                if not result.matched_count:
                    flash("题目已保存到题目库，但原题库已被删除，本次没有加入。可以在这里选择其他题库。", "error")
                    return redirect(url_for("bank.question_detail", question_id=item["_id"]))
                flash("题目已保存并加入题库。", "success")
                return redirect(form_context.get("return_url") or url_for("bank.bank_detail", bank_id=bank["_id"]))
            flash("题目已保存到题目库。", "success")
            if origin:
                return redirect(url_for("bank.question_detail", question_id=item["_id"],
                                        **{key: value for key, value in context.items()
                                           if key in ("from_survey", "from_question")}))
            return redirect(form_context.get("return_url") or url_for("bank.question_detail", question_id=item["_id"]))
    return render_template("library/form.html", source=None, form=request.form,
                           error=error, question_types=QUESTION_TYPES,
                           survey=survey, selected_bank=bank, **form_context)


@bank_bp.route("/question-library/<question_id>/version", methods=["GET", "POST"])
@login_required
def new_version(question_id):
    source = owned_question(question_id)
    context = navigation_context()
    preview_style = "numeric"
    preview_survey_id = context.get("survey") or context.get("from_survey")
    if preview_survey_id:
        preview_survey = optional_owned_survey(preview_survey_id)
        if preview_survey:
            preview_style = option_style(preview_survey)
        else:
            for key in ("survey", "from_survey", "from_question"):
                context.pop(key, None)
            flash("原问卷已不存在或无法访问，仍可在题目库中保存新版本。", "info")
    error = None
    status = 200
    if request.method == "POST":
        if not valid_csrf():
            abort(400)
        try:
            item = make_version(source, request.form)
        except ValueError as problem:
            error = str(problem)
        except DuplicateKeyError:
            error = "另一页面刚保存了新版本。本次输入已保留，请直接重试保存。"
            status = 409
        else:
            flash("新版本已保存，旧版本和已有问卷保持原样。", "success")
            return redirect(url_for("bank.question_detail", question_id=item["_id"], **context))
    return render_template("library/form.html", source=source,
                           form=request.form if request.method == "POST" else form_values(source),
                           error=error, question_types=QUESTION_TYPES, context_args=context,
                           preview_option_style=preview_style,
                           return_url=url_for("bank.question_detail", question_id=source["_id"], **context)), status


@bank_bp.post("/question-library/<question_id>/restore")
@login_required
def restore_version(question_id):
    if not valid_csrf():
        abort(400)
    source = owned_question(question_id)
    from werkzeug.datastructures import MultiDict
    try:
        item = make_version(source, MultiDict(form_values(source)), restoring=True)
    except DuplicateKeyError:
        abort(409)
    flash("已从旧版本生成新版本，原版本没有改变。", "success")
    return redirect(url_for("bank.question_detail", question_id=item["_id"], **navigation_context()))


@bank_bp.post("/question-library/<question_id>/share")
@login_required
def set_shared(question_id):
    if not valid_csrf():
        abort(400)
    item = owned_question(question_id)
    get_db().question_library.update_one({"_id": item["_id"]},
                                         {"$set": {"shared": request.form.get("shared") == "yes"}})
    return redirect(url_for("bank.question_detail", question_id=question_id, **navigation_context()))


@bank_bp.post("/surveys/<survey_id>/questions/<question_id>/save-to-library")
@login_required
def save_existing_question(survey_id, question_id):
    from .surveys import owned_survey
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    question = next((row for row in survey.get("questions", [])
                     if row["id"] == question_id and row.get("active", True)), None)
    if question is None:
        abort(404)
    try:
        item = save_survey_question(survey, question)
    except HTTPException as error:
        if error.code != 409:
            raise
        flash(error.description, "error")
        return redirect(url_for("surveys.survey_detail", survey_id=survey_id) + f"#question-{question_id}")
    if item is None:
        abort(404)
    flash("已保存到题目库，原问卷与答卷内容保持不变。", "success")
    return redirect(url_for("bank.question_detail", question_id=item["_id"],
                            from_survey=survey_id, from_question=question_id))


def save_survey_question(survey, question):
    from werkzeug.datastructures import MultiDict
    if question.get("library_question_id"):
        return get_db().question_library.find_one({"_id": question["library_question_id"]})
    item = make_version(None, MultiDict(form_values(question)))
    result = get_db().surveys.update_one(
        {"_id": survey["_id"], "owner_id": g.user["_id"],
         "status": survey["status"],
         "revision": survey["revision"] if "revision" in survey else {"$exists": False},
         "questions": {"$elemMatch": {"id": question["id"], "active": {"$ne": False},
                                      "library_question_id": {"$exists": False}}}},
        {"$set": {"questions.$.library_question_id": item["_id"],
                  "questions.$.library_root_id": item["root_id"],
                  "questions.$.library_version": item["version"],
                  "questions.$.library_source_question_version": question.get("version", 1)}})
    if not result.modified_count:
        get_db().question_library.delete_one({"_id": item["_id"]})
        abort(409, description="题目已在其他页面修改或保存，本次未入库。请核对当前题目后重试。")
    return item


def cross_statistics_many(versions):
    """Read the relevant surveys and responses once for all requested versions."""
    if not versions:
        return {}
    ids = {item["_id"] for item in versions}
    rows = list(get_db().surveys.find({"questions.library_question_id": {"$in": list(ids)}},
                                      {"title": 1, "owner_id": 1, "status": 1, "questions": 1}))
    data = {item["_id"]: {"rows": [], "responses": [], "own_responses": []} for item in versions}
    answer_sources = {}
    survey_owners = {}
    for survey in rows:
        included = set()
        for question in survey.get("questions", []):
            if survey["status"] == "draft" and not question.get("active", True):
                continue
            version_id = question.get("library_question_id")
            if version_id not in ids:
                continue
            answer_sources[(survey["_id"], question["id"])] = (
                version_id, question.get("library_source_question_version", 1))
            included.add(version_id)
        for version_id in included:
            data[version_id]["rows"].append(survey)
        if included:
            survey_owners[survey["_id"]] = survey["owner_id"]
    if survey_owners:
        for response in get_db().responses.find(
                {"survey_id": {"$in": list(survey_owners)}}).sort([("submitted_at", 1), ("_id", 1)]):
            survey_id = response["survey_id"]
            for answer in response.get("answers", []):
                source = answer_sources.get((survey_id, answer.get("question_id")))
                if source is None or answer.get("question_version", 1) != source[1]:
                    continue
                version_id = source[0]
                copied = deepcopy(answer)
                copied["question_id"] = str(version_id)
                copied["question_version"] = 1
                record = {"answers": [copied], "submitted_at": response.get("submitted_at"),
                          "respondent_id": response.get("respondent_id")}
                data[version_id]["responses"].append(record)
                if survey_owners[survey_id] == g.user["_id"]:
                    data[version_id]["own_responses"].append(record)
    for item in versions:
        row = data[item["_id"]]
        definition = {"id": str(item["_id"]), "version": 1, **question_fields(item)}
        summary = summarize_responses([definition], row.pop("responses"))
        own_summary = summarize_responses([definition], row.pop("own_responses"))
        row["summary"] = summary[0] if summary else None
        row["own_summary"] = own_summary[0] if own_summary else None
    return data


def numeric_distribution(summary):
    """Group equivalent numeric answers across surveys without exposing raw responses."""
    if not summary or summary["question"]["type"] != "number":
        return []
    return [{"value": value, "count": count,
             "percent": display_percent(count, summary["answered_count"])}
            for value, count in summary["number_frequencies"]]


def usage_references(rows):
    """Identify external usage consistently without exposing another owner's survey data."""
    key = current_app.secret_key
    key = key.encode("utf-8") if isinstance(key, str) else key
    result = []
    for row in rows:
        if row["owner_id"] == g.user["_id"]:
            result.append({"id": str(row["_id"]), "title": row["title"],
                           "status": row["status"], "own": True})
        else:
            reference = hmac.new(key, str(row["_id"]).encode("ascii"), hashlib.sha256).hexdigest()[:10].upper()
            result.append({"reference": reference, "own": False})
    return result


@bank_bp.get("/question-library/<question_id>")
@login_required
def question_detail(question_id):
    item = accessible_question(question_id, allow_used=True)
    db = get_db()
    versions = list(db.question_library.find({"root_id": item["root_id"]}).sort("version", 1))
    if item["owner_id"] != g.user["_id"]:
        versions = [version for version in versions if version.get("shared") or version["_id"] == item["_id"]]
    stats_by_version = cross_statistics_many(versions)
    rows = stats_by_version[item["_id"]]["rows"]
    version_by_id = {version["_id"]: version["version"] for version in versions}
    by_version_id = {version["_id"]: version for version in versions}
    version_changes = {version["_id"]: definition_changes(by_version_id[version["parent_id"]], version)
                       for version in versions if version.get("parent_id") in by_version_id}
    version_overview = []
    for version in versions:
        version_rows = stats_by_version[version["_id"]]["rows"]
        version_summary = stats_by_version[version["_id"]]["summary"]
        version_overview.append({"version": version, "survey_count": len(version_rows),
                                 "answered_count": version_summary["answered_count"] if version_summary else 0})
    visible_rows = [row for row in rows if row["owner_id"] == g.user["_id"]]
    external_references = [entry for entry in usage_references(rows) if not entry["own"]]
    banks = list(db.question_banks.find({"owner_id": g.user["_id"]}).sort("name", 1))
    survey_id = request.args.get("survey", "")
    survey = None
    readonly_selection_survey = None
    missing_survey_context = False
    if survey_id:
        survey = optional_owned_survey(survey_id)
        missing_survey_context = survey is None
        if survey and survey["status"] != "draft":
            readonly_selection_survey = survey
            survey = None
    return_survey_id = request.args.get("from_survey", "")
    return_survey = None
    return_question_id = ""
    return_question_matches = False
    if return_survey_id:
        return_survey = optional_owned_survey(return_survey_id)
    elif readonly_selection_survey:
        return_survey = readonly_selection_survey
        flash("原问卷已发布或关闭，不能再添加题目；仍可查看此题并返回原问卷。", "info")
    if missing_survey_context or (return_survey_id and not return_survey):
        flash("原问卷已不存在或无法访问，可继续查看这道题或返回题目库。", "info")
    if return_survey:
        requested_question = request.args.get("from_question", "")
        original_question = next((question for question in return_survey.get("questions", [])
                                  if question.get("id") == requested_question and question.get("active", True)), None)
        if original_question:
            return_question_id = requested_question
            return_question_matches = original_question.get("library_question_id") == item["_id"]
    bank_id = request.args.get("bank", "") if not survey else ""
    selected_bank = next((row for row in banks if str(row["_id"]) == bank_id), None)
    back_url = ((url_for("surveys.survey_detail", survey_id=return_survey["_id"])
                 + (f"#question-{return_question_id}" if return_question_id else ""))
                if return_survey else url_for("bank.choose_question", survey_id=survey["_id"],
                        q=request.args.get("q", "")[:100], bank=request.args.get("bank", ""),
                        mine_page=page_number(request.args.get("mine_page")),
                        shared_page=page_number(request.args.get("shared_page")))
                if survey else url_for("bank.bank_detail", bank_id=bank_id)
                if selected_bank and request.args.get("return_bank") == "1"
                else url_for("bank.library", bank=bank_id, q=request.args.get("q", "")[:100],
                             mine_page=page_number(request.args.get("mine_page")),
                             shared_page=page_number(request.args.get("shared_page")))
                if selected_bank else url_for("bank.library", q=request.args.get("q", "")[:100],
                                              mine_page=page_number(request.args.get("mine_page")),
                                              shared_page=page_number(request.args.get("shared_page"))))
    already_added = bool(survey and any(q.get("active", True) and q.get("library_question_id") == item["_id"]
                                        for q in survey.get("questions", [])))
    if not return_survey and not (selected_bank and request.args.get("return_bank") == "1"):
        back_url += f"#library-question-{item['root_id']}"
    context_args = {"q": request.args.get("q", "")[:100],
                    "mine_page": page_number(request.args.get("mine_page")),
                    "shared_page": page_number(request.args.get("shared_page"))}
    if survey:
        context_args["survey"] = str(survey["_id"])
        if request.args.get("bank"):
            context_args["bank"] = request.args["bank"]
    elif return_survey:
        context_args["from_survey"] = str(return_survey["_id"])
        if return_question_id:
            context_args["from_question"] = return_question_id
    elif selected_bank:
        context_args["bank"] = str(selected_bank["_id"])
        if request.args.get("return_bank") == "1":
            context_args["return_bank"] = "1"
    own_summary = stats_by_version[item["_id"]]["own_summary"]
    own_values = own_summary["values"] if own_summary else []
    own_value_pages = max(1, (len(own_values) + LIBRARY_VALUES_PAGE_SIZE - 1) // LIBRARY_VALUES_PAGE_SIZE)
    own_value_page = min(page_number(request.args.get("values_page")), own_value_pages)
    own_value_start = (own_value_page - 1) * LIBRARY_VALUES_PAGE_SIZE
    return render_template("library/detail.html", item=item, versions=versions,
                           version_by_id=version_by_id, version_changes=version_changes,
                           parent_version=by_version_id.get(item.get("parent_id")),
                           version_overview=version_overview,
                           usages=visible_rows, external_references=external_references,
                           usage_count=len(rows), banks=banks,
                           summary=stats_by_version[item["_id"]]["summary"],
                           own_summary=own_summary,
                           own_values=own_values[own_value_start:own_value_start + LIBRARY_VALUES_PAGE_SIZE],
                           own_value_count=len(own_values), own_value_page=own_value_page,
                           own_value_pages=own_value_pages, own_value_start=own_value_start,
                           number_distribution=numeric_distribution(stats_by_version[item["_id"]]["summary"]),
                           back_url=back_url, survey=survey, return_survey=return_survey,
                           preview_option_style=option_style(survey or return_survey) if survey or return_survey else "numeric",
                           can_edit_in_survey=bool(return_survey and return_survey["status"] == "draft"
                                                   and return_question_matches),
                           return_question_id=return_question_id,
                           selected_bank=selected_bank,
                           already_added=already_added, context_args=context_args,
                           reusable=item["owner_id"] == g.user["_id"] or item.get("shared", False),
                           question_types=QUESTION_TYPES)


@bank_bp.post("/question-banks")
@login_required
def create_bank():
    if not valid_csrf():
        abort(400)
    name = request.form.get("name", "").strip()
    error = None
    if not name or len(name) > 80:
        error = "题库名称应为 1 到 80 个字。"
    else:
        try:
            get_db().question_banks.insert_one({"owner_id": g.user["_id"], "name": name,
                                                 "question_ids": [], "created_at": datetime.now(timezone.utc)})
        except DuplicateKeyError:
            error = "你已经有同名题库。"
    if error:
        return library(bank_form_name=request.form.get("name", ""), bank_error=error), 422
    return redirect(url_for("bank.library"))


@bank_bp.get("/question-banks/<bank_id>")
@login_required
def bank_detail(bank_id, *, name_value=None, name_error=None):
    bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if bank is None:
        abort(404)
    all_items = list(get_db().question_library.find({"_id": {"$in": bank["question_ids"]}}))
    items = [item for item in all_items if item["owner_id"] == g.user["_id"] or item.get("shared")]
    by_id = {item["_id"]: item for item in items}
    updates = {}
    roots = list({item["root_id"] for item in all_items})
    latest_by_root = {}
    if roots:
        for candidate in get_db().question_library.find({"root_id": {"$in": roots}}).sort("version", -1):
            if (candidate["owner_id"] == g.user["_id"] or candidate.get("shared")):
                latest_by_root.setdefault(candidate["root_id"], candidate)
    for item in all_items:
        latest = latest_by_root.get(item["root_id"])
        if latest and latest["version"] > item["version"]:
            updates[item["_id"]] = {"target": latest,
                                     "changes": definition_changes(item, latest)}
    unavailable_ids = [key for key in bank["question_ids"] if key not in by_id]
    unavailable_by_id = {row["_id"]: row for row in get_db().question_library.find(
        {"_id": {"$in": unavailable_ids}}, {"title": 1, "version": 1})} if unavailable_ids else {}
    return render_template("library/bank.html", bank=bank,
                           items=[by_id[key] for key in bank["question_ids"] if key in by_id],
                           unavailable=[{"id": key,
                                         "title": unavailable_by_id.get(key, {}).get("title", "此前共享的题目"),
                                         "version": unavailable_by_id.get(key, {}).get("version")}
                                        for key in unavailable_ids],
                           updates=updates, question_types=QUESTION_TYPES,
                           name_value=bank["name"] if name_value is None else name_value,
                           name_error=name_error)


@bank_bp.post("/question-banks/<bank_id>/rename")
@login_required
def rename_bank(bank_id):
    if not valid_csrf():
        abort(400)
    bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if bank is None:
        abort(404)
    name = request.form.get("name", "").strip()
    if request.form.get("original_name", bank["name"]) != bank["name"]:
        return bank_detail(bank_id, name_value=request.form.get("name", ""),
                           name_error="题库已在其他页面改名。本次输入已保留，请核对后再保存。"), 409
    error = None
    if not name or len(name) > 80:
        error = "题库名称应为 1 到 80 个字。"
    else:
        try:
            result = get_db().question_banks.update_one(
                {"_id": bank["_id"], "owner_id": g.user["_id"], "name": bank["name"]}, {"$set": {"name": name}})
            if not result.matched_count:
                if get_db().question_banks.find_one({"_id": bank["_id"], "owner_id": g.user["_id"]}):
                    return bank_detail(bank_id, name_value=request.form.get("name", ""),
                                       name_error="题库已在其他页面改名。本次输入已保留，请核对后再保存。"), 409
                flash("题库已不存在，本次没有修改名称。", "error")
                return redirect(url_for("bank.library"))
        except DuplicateKeyError:
            error = "你已经有同名题库。"
    if error:
        return bank_detail(bank_id, name_value=request.form.get("name", ""), name_error=error), 422
    return redirect(url_for("bank.bank_detail", bank_id=bank_id))


@bank_bp.post("/question-banks/<bank_id>/delete")
@login_required
def delete_bank(bank_id):
    if not valid_csrf():
        abort(400)
    result = get_db().question_banks.delete_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if not result.deleted_count:
        abort(404)
    flash("题库已删除，题目及问卷不受影响。", "success")
    return redirect(url_for("bank.library"))


def update_bank_version(bank, question_id, target_id=None):
    """Pin the chosen version and preserve unrelated edits from another tab."""
    db = get_db()
    key = object_id(question_id)
    if key not in bank["question_ids"]:
        abort(409, description="这道题已从题库移除，请刷新后再操作。")
    source = db.question_library.find_one({"_id": key})
    if source is None:
        abort(404)
    if target_id is not None:
        target = accessible_question(target_id)
    else:
        target = db.question_library.find_one({"root_id": source["root_id"],
                     "$or": [{"owner_id": g.user["_id"]}, {"shared": True}]}, sort=[("version", -1)])
        if target is None:
            abort(409, description="这道题目前没有可用的共享版本。")
    if target["root_id"] != source["root_id"] or target["version"] < source["version"]:
        abort(422, description="请选择这道题的更新版本。")
    for _ in range(3):
        if key not in bank["question_ids"]:
            if target["_id"] in bank["question_ids"]:
                return bank["question_ids"]
            abort(409, description="题库中的版本已被其他页面修改，请刷新后再操作。")
        ids = list(dict.fromkeys(target["_id"] if old == key else old for old in bank["question_ids"]))
        result = db.question_banks.update_one(
            {"_id": bank["_id"], "owner_id": g.user["_id"], "question_ids": bank["question_ids"]},
            {"$set": {"question_ids": ids}})
        if result.matched_count:
            return ids
        bank = db.question_banks.find_one({"_id": bank["_id"], "owner_id": g.user["_id"]})
        if bank is None:
            abort(404)
    abort(409, description="题库正在被其他页面修改，请刷新后重试。")


@bank_bp.post("/question-banks/<bank_id>/questions/<question_id>")
@login_required
def bank_question(bank_id, question_id):
    if not valid_csrf():
        abort(400)
    action = request.form.get("action")
    return_to_detail = request.form.get("return_to_detail") == "yes"

    def detail_destination():
        context = navigation_context()
        try:
            accessible_question(question_id, allow_used=True)
        except HTTPException as problem:
            if problem.code != 404:
                raise
            return url_for("bank.library", **{key: value for key, value in context.items()
                                              if key in ("q", "bank", "mine_page", "shared_page")})
        return url_for("bank.question_detail", question_id=question_id,
                       _anchor="library-bank-actions", **context)

    def missing_bank_response():
        message = ("题库已不存在，无需再移除题目。" if action == "remove" else
                   "题库已不存在，本次没有更新版本。" if action == "update" else
                   "题库已不存在或已被删除，本次没有加入题库。")
        flash(message, "error")
        if return_to_detail:
            return redirect(detail_destination())
        return redirect(url_for("bank.library", q=request.form.get("q", "")[:100],
                                mine_page=page_number(request.form.get("mine_page")),
                                shared_page=page_number(request.form.get("shared_page"))))

    bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if bank is None:
        return missing_bank_response()

    def return_to_card(preferred_id):
        latest = get_db().question_banks.find_one(
            {"_id": bank["_id"], "owner_id": g.user["_id"]}, {"question_ids": 1})
        if latest is None:
            return missing_bank_response()
        anchor = (f"bank-question-{preferred_id}" if preferred_id in latest["question_ids"]
                  else "bank-questions")
        return redirect(url_for("bank.bank_detail", bank_id=bank_id, _anchor=anchor))

    if action == "remove":
        key = object_id(question_id)
        position = bank["question_ids"].index(key) if key in bank["question_ids"] else 0
        remaining = [item_id for item_id in bank["question_ids"] if item_id != key]
        preferred = remaining[min(position, len(remaining) - 1)] if remaining else None
        result = get_db().question_banks.update_one({"_id": bank["_id"]}, {"$pull": {"question_ids": key}})
        if not result.matched_count:
            return missing_bank_response()
        return return_to_card(preferred)
    if action == "update":
        if not request.form.get("target_id"):
            flash("请刷新题库页面，查看并确认要更新到的版本。", "error")
            return return_to_card(object_id(question_id))
        try:
            update_bank_version(bank, question_id, request.form["target_id"])
        except HTTPException as error:
            if error.code not in (404, 409, 422):
                raise
            if not get_db().question_banks.find_one({"_id": bank["_id"], "owner_id": g.user["_id"]}, {"_id": 1}):
                return missing_bank_response()
            flash("所选版本已无法选用，请查看当前可用版本后重试。" if error.code == 404 else error.description, "error")
        else:
            flash("题库版本已更新，已有问卷保持原样。", "success")
            return return_to_card(object_id(request.form["target_id"]))
        return return_to_card(object_id(question_id))
    return_to_library = request.form.get("continue") == "yes"
    destination = (detail_destination() if return_to_detail else
                   url_for("bank.library", bank=bank_id, q=request.form.get("q", "")[:100],
                            mine_page=page_number(request.form.get("mine_page")),
                            shared_page=page_number(request.form.get("shared_page")))
                   if return_to_library else url_for("bank.bank_detail", bank_id=bank_id))
    try:
        item = accessible_question(question_id)
    except HTTPException as error:
        if error.code != 404:
            raise
        flash("题目不存在或已停止共享，本次没有加入题库。", "error")
        return redirect(destination)
    update = {"$addToSet": {"question_ids": item["_id"]}}
    result = get_db().question_banks.update_one({"_id": bank["_id"]}, update)
    if not result.matched_count:
        return missing_bank_response()
    if return_to_detail:
        flash(f"已加入「{bank['name']}」。", "success")
        return redirect(destination)
    if return_to_library:
        flash("已加入题库。", "success")
        return redirect(destination + f"#library-question-{item['root_id']}")
    return return_to_card(item["_id"])


@bank_bp.get("/surveys/<survey_id>/question-library")
@login_required
def choose_question(survey_id):
    from .surveys import owned_survey
    survey = owned_survey(survey_id)
    if survey["status"] != "draft":
        flash("问卷已发布，不能再添加题目。" if survey["status"] == "published" else "问卷已关闭，不能再添加题目。", "error")
        return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    db = get_db()
    query = request.args.get("q", "").strip()[:100]
    mine_page = page_number(request.args.get("mine_page"))
    shared_page = page_number(request.args.get("shared_page"))
    banks = list(db.question_banks.find({"owner_id": g.user["_id"]}))
    chosen_bank = request.args.get("bank", "")
    if chosen_bank:
        bank = next((row for row in banks if str(row["_id"]) == chosen_bank), None)
        if bank is None:
            flash("该题库已不存在或无法访问，已显示全部可用题目。", "error")
            return redirect(url_for("bank.choose_question", survey_id=survey_id, q=query,
                                    mine_page=mine_page, shared_page=shared_page))
        allowed = bank["question_ids"]
    else:
        allowed = None
    mine_filter = {"owner_id": g.user["_id"]}
    shared_filter = {"shared": True, "owner_id": {"$ne": g.user["_id"]}}
    if allowed is not None:
        mine_filter["_id"] = {"$in": allowed}
        shared_filter["_id"] = {"$in": allowed}
    mine_groups, mine_total, mine_page = paged_version_groups(mine_filter, query, mine_page)
    shared_groups, shared_total, shared_page = paged_version_groups(shared_filter, query, shared_page)
    owner_names = {row["_id"]: row["username"] for row in db.users.find(
        {"_id": {"$in": list({group["latest"]["owner_id"] for group in shared_groups})}},
        {"username": 1})}
    added_ids = {row.get("library_question_id") for row in survey.get("questions", [])
                 if row.get("active", True) and row.get("library_question_id")}
    context_args = {"survey": str(survey["_id"]), "q": query, "bank": chosen_bank,
                    "mine_page": mine_page, "shared_page": shared_page}
    return render_template("library/choose.html", survey=survey,
                           mine_groups=mine_groups, shared_groups=shared_groups,
                           added_ids=added_ids, banks=banks, question_types=QUESTION_TYPES,
                           owner_names=owner_names, query=query, chosen_bank=chosen_bank,
                           mine_total=mine_total, shared_total=shared_total,
                           mine_page=mine_page, shared_page=shared_page,
                           page_size=LIBRARY_PAGE_SIZE, context_args=context_args)


@bank_bp.post("/surveys/<survey_id>/question-library/<question_id>")
@login_required
def attach_question(survey_id, question_id):
    from .surveys import owned_survey
    if not valid_csrf():
        abort(400)
    survey = owned_survey(survey_id)
    if survey["status"] != "draft":
        flash("问卷已发布，不能再添加题目。" if survey["status"] == "published" else "问卷已关闭，不能再添加题目。", "error")
        return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
    return_to_choose = request.form.get("continue") == "yes"
    destination = (url_for("bank.choose_question", survey_id=survey_id,
                           q=request.form.get("q", "")[:100], bank=request.form.get("bank", ""),
                           mine_page=page_number(request.form.get("mine_page")),
                           shared_page=page_number(request.form.get("shared_page")))
                   if return_to_choose else url_for("surveys.survey_detail", survey_id=survey_id))
    try:
        item = accessible_question(question_id)
    except HTTPException as error:
        if error.code != 404:
            raise
        flash("题目不存在或已停止共享，本次没有加入问卷。", "error")
        return redirect(destination)
    if any(row.get("active", True) and row.get("library_question_id") == item["_id"]
           for row in survey.get("questions", [])):
        flash("这道题的这个版本已经在问卷中。", "error")
        return redirect(destination + f"#library-question-{item['root_id']}" if return_to_choose else destination)
    try:
        add_to_survey(survey, item)
    except HTTPException as error:
        if error.code != 409:
            raise
        latest = get_db().surveys.find_one({"_id": survey["_id"], "owner_id": g.user["_id"]}, {"status": 1})
        flash("问卷已在其他页面发生变化，本次没有加入问卷。请核对后重试。", "error")
        if latest is None:
            return redirect(url_for("surveys.my_surveys"))
        if latest["status"] != "draft":
            return redirect(url_for("surveys.survey_detail", survey_id=survey_id))
        return redirect(destination)
    flash("已加入问卷。", "success")
    return redirect(destination + f"#library-question-{item['root_id']}" if return_to_choose else destination)


@bank_api_bp.before_request
def protect_api():
    if g.user is None:
        abort(401)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not valid_csrf():
            abort(400, description="CSRF 校验失败。")
        api_data()


def api_data():
    data = request.get_json(silent=True)
    if not request.is_json or not isinstance(data, dict):
        abort(415, description="请求体必须是 JSON 对象。")
    return data


def item_json(item):
    return {"id": str(item["_id"]), "root_id": str(item["root_id"]),
            "parent_id": str(item["parent_id"]) if item.get("parent_id") else None,
            "owner_id": str(item["owner_id"]), "version": item["version"],
            "shared": item.get("shared", False), "definition": question_fields(item)}


@bank_api_bp.route("/library/questions", methods=["GET", "POST"])
def api_questions():
    if request.method == "GET":
        query = {"$or": [{"owner_id": g.user["_id"]}, {"shared": True}]}
        return jsonify(questions=[item_json(row) for row in get_db().question_library.find(query).sort("created_at", -1)])
    from .api import form_for_question
    try:
        item = make_version(None, form_for_question(api_data()))
    except ValueError as problem:
        return jsonify(error=str(problem)), 422
    return jsonify(question=item_json(item)), 201


@bank_api_bp.get("/library/questions/<question_id>")
def api_question(question_id):
    item = accessible_question(question_id, allow_used=True)
    versions = get_db().question_library.find({"root_id": item["root_id"]}).sort("version", 1)
    return jsonify(question=item_json(item), versions=[item_json(row) for row in versions
                                                    if row["owner_id"] == g.user["_id"] or row.get("shared")
                                                    or row["_id"] == item["_id"]])


@bank_api_bp.post("/library/questions/<question_id>/versions")
def api_new_version(question_id):
    from .api import form_for_question
    source = owned_question(question_id)
    try:
        item = make_version(source, form_for_question(api_data()))
    except ValueError as problem:
        return jsonify(error=str(problem)), 422
    except DuplicateKeyError:
        abort(409, description="版本已变化，请重试。")
    return jsonify(question=item_json(item)), 201


@bank_api_bp.post("/library/questions/<question_id>/restore")
def api_restore(question_id):
    from werkzeug.datastructures import MultiDict
    source = owned_question(question_id)
    try:
        item = make_version(source, MultiDict(form_values(source)), restoring=True)
    except DuplicateKeyError:
        abort(409, description="版本已变化，请重试。")
    return jsonify(question=item_json(item)), 201


@bank_api_bp.patch("/library/questions/<question_id>/sharing")
def api_sharing(question_id):
    item = owned_question(question_id)
    shared = api_data().get("shared")
    if not isinstance(shared, bool):
        return jsonify(error="shared 必须是布尔值。"), 422
    get_db().question_library.update_one({"_id": item["_id"]}, {"$set": {"shared": shared}})
    item["shared"] = shared
    return jsonify(question=item_json(item))


@bank_api_bp.get("/library/questions/<question_id>/statistics")
def api_statistics(question_id):
    item = accessible_question(question_id, allow_used=True)
    if request.args.get("scope") == "lineage":
        versions = [version for version in get_db().question_library.find(
            {"root_id": item["root_id"]}).sort("version", 1)
            if version["owner_id"] == g.user["_id"] or version.get("shared") or version["_id"] == item["_id"]]
    else:
        versions = [item]
    stats_by_version = cross_statistics_many(versions)
    rows = stats_by_version[item["_id"]]["rows"]
    result = stats_by_version[item["_id"]]["summary"]
    own_result = stats_by_version[item["_id"]]["own_summary"]
    result_data = dict(question_id=question_id, survey_count=len(rows),
                   usages=usage_references(rows),
                   answered_count=result["answered_count"] if result else 0,
                   option_counts=result["option_counts"] if result else {},
                   values=own_result["values"] if own_result else [],
                   values_scope="own_surveys",
                   average=result["average"] if result else None)
    if item["type"] == "number":
        result_data["invalid_count"] = result["invalid_count"] if result else 0
        result_data["median"] = result["median"] if result else None
        result_data["minimum"] = result["minimum"] if result else None
        result_data["maximum"] = result["maximum"] if result else None
        result_data["number_distribution"] = numeric_distribution(result)
    if request.args.get("scope") == "lineage":
        breakdown = []
        for version in versions:
            version_rows = stats_by_version[version["_id"]]["rows"]
            version_result = stats_by_version[version["_id"]]["summary"]
            breakdown.append({"id": str(version["_id"]), "version": version["version"],
                              "survey_count": len(version_rows),
                              "answered_count": version_result["answered_count"] if version_result else 0})
        result_data["versions"] = breakdown
    return jsonify(result_data)


@bank_api_bp.route("/library/banks", methods=["GET", "POST"])
def api_banks():
    if request.method == "GET":
        rows = get_db().question_banks.find({"owner_id": g.user["_id"]}).sort("name", 1)
        return jsonify(banks=[{"id": str(row["_id"]), "name": row["name"],
                               "question_ids": [str(key) for key in row["question_ids"]]} for row in rows])
    name = api_data().get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        return jsonify(error="题库名称应为 1 到 80 个字。"), 422
    try:
        result = get_db().question_banks.insert_one({"owner_id": g.user["_id"], "name": name.strip(),
                                                     "question_ids": [], "created_at": datetime.now(timezone.utc)})
    except DuplicateKeyError:
        abort(409, description="题库名称已存在。")
    return jsonify(id=str(result.inserted_id), name=name.strip()), 201


@bank_api_bp.post("/library/banks/<bank_id>/questions")
def api_bank_question(bank_id):
    bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if bank is None:
        abort(404)
    question_id = api_data().get("question_id")
    item = accessible_question(question_id)
    result = get_db().question_banks.update_one(
        {"_id": bank["_id"], "owner_id": g.user["_id"]}, {"$addToSet": {"question_ids": item["_id"]}})
    if not result.matched_count:
        abort(404, description="题库已不存在，本次没有加入题库。")
    return jsonify(ok=True)


@bank_api_bp.route("/library/banks/<bank_id>", methods=["GET", "PATCH", "DELETE"])
def api_bank(bank_id):
    bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if bank is None:
        abort(404)
    if request.method == "DELETE":
        get_db().question_banks.delete_one({"_id": bank["_id"]})
        return jsonify(ok=True)
    if request.method == "PATCH":
        name = api_data().get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            return jsonify(error="题库名称应为 1 到 80 个字。"), 422
        try:
            result = get_db().question_banks.update_one(
                {"_id": bank["_id"], "owner_id": g.user["_id"], "name": bank["name"]}, {"$set": {"name": name.strip()}})
        except DuplicateKeyError:
            abort(409, description="题库名称已存在。")
        if not result.matched_count:
            if get_db().question_banks.find_one({"_id": bank["_id"], "owner_id": g.user["_id"]}):
                abort(409, description="题库已在其他页面改名，请获取最新名称后重试。")
            abort(404, description="题库已不存在，本次没有修改名称。")
        bank["name"] = name.strip()
    return jsonify(id=bank_id, name=bank["name"], question_ids=[str(key) for key in bank["question_ids"]])


@bank_api_bp.route("/library/banks/<bank_id>/questions/<question_id>", methods=["PATCH", "DELETE"])
def api_bank_item(bank_id, question_id):
    bank = get_db().question_banks.find_one({"_id": object_id(bank_id), "owner_id": g.user["_id"]})
    if bank is None:
        abort(404)
    key = object_id(question_id)
    if request.method == "DELETE":
        result = get_db().question_banks.update_one(
            {"_id": bank["_id"], "owner_id": g.user["_id"]}, {"$pull": {"question_ids": key}})
        if not result.matched_count:
            abort(404, description="题库已不存在，无需再移除题目。")
        return jsonify(ok=True)
    ids = update_bank_version(bank, question_id, api_data().get("target_id"))
    return jsonify(question_ids=[str(value) for value in ids])


@bank_api_bp.post("/surveys/<survey_id>/library-questions")
def api_attach_question(survey_id):
    from .surveys import owned_survey, save_questions
    from .api import revision_check
    survey = owned_survey(survey_id)
    if survey["status"] != "draft":
        abort(409, description="已发布问卷的题目不能修改。")
    data = api_data()
    revision_check(survey, data)
    item = accessible_question(data.get("question_id"))
    if any(row.get("active", True) and row.get("library_question_id") == item["_id"]
           for row in survey.get("questions", [])):
        abort(409, description="这道题的这个版本已经在问卷中。")
    question = {"id": secrets.token_hex(8), "version": 1, "active": True, "jumps": [],
                "library_question_id": item["_id"], "library_root_id": item["root_id"],
                "library_version": item["version"], "library_source_question_version": 1,
                **question_fields(item)}
    save_questions(survey, list(survey.get("questions", [])) + [question])
    return jsonify(question_id=question["id"], library_question_id=str(item["_id"]),
                   revision=survey.get("revision", 1) + 1), 201


@bank_api_bp.post("/surveys/<survey_id>/questions/<question_id>/save-to-library")
def api_save_existing_question(survey_id, question_id):
    from .surveys import owned_survey
    survey = owned_survey(survey_id)
    question = next((row for row in survey.get("questions", [])
                     if row["id"] == question_id and row.get("active", True)), None)
    if question is None:
        abort(404)
    item = save_survey_question(survey, question)
    if item is None:
        abort(404)
    return jsonify(question=item_json(item)), 201
