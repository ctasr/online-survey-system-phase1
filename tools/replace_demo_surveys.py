"""Replace only the two specified experience surveys with verified practice surveys.

Run in each deployment with PYTHONPATH=. and a private backup path. Old answers are
backed up before removal. This script never touches surveys owned by other users.
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId, json_util

from app import create_app
from app.surveys import active_questions, assessed_answers
from tools.seed_experience_surveys import create_experience_survey, post
from werkzeug.datastructures import MultiDict


OLD = {
    ObjectId("6ad786c2e7b2bb4171cd065c"): "周末活动调查（体验）",
    ObjectId("6ad786c2e7b2bb4171cd065d"): "课程学习体验调查（体验）",
}
REVISED_LOCAL = {
    ObjectId("6ad8b776e93defbdcfa1c21e"): "基础数学题问卷",
    ObjectId("6ad8b776e93defbdcfa1c21f"): "足球规则与世界杯常识问卷",
}

# Football facts are from IFAB Laws 3, 7, 9, 11, 12, 15 and 17.
SPECS = [
    {
        "title": "基础数学题问卷",
        "settings": {
            "title": "基础数学题问卷",
            "description": "12 道基础运算与识数题，每题都有确定答案。答卷提交后不能修改。",
            "allow_anonymous": "on",
            "repeat_policy": "multiple",
            "option_label_style": "numeric",
        },
        "questions": [
            {"type": "single_choice", "title": "8＋7 等于多少？", "options": "14\n15\n16\n17", "required": "on"},
            {"type": "single_choice", "title": "20－6 等于多少？", "options": "12\n13\n14\n15", "required": "on"},
            {"type": "single_choice", "title": "6×3 等于多少？", "options": "16\n18\n20\n21", "required": "on"},
            {"type": "single_choice", "title": "28÷4 等于多少？", "options": "6\n7\n8\n9", "required": "on"},
            {"type": "single_choice", "title": "二分之一加四分之一等于多少？", "options": "四分之一\n二分之三\n四分之三\n一", "required": "on"},
            {"type": "multiple_choice", "title": "下面哪些数是偶数？", "options": "12\n15\n18\n21", "exact_select": "2", "required": "on"},
            {"type": "number", "title": "9 的平方是多少？", "integer_only": "on", "min_value": "0", "max_value": "100", "required": "on"},
            {"type": "number", "title": "长 5 厘米、宽 3 厘米的长方形，周长是多少厘米？", "integer_only": "on", "min_value": "0", "required": "on"},
            {"type": "single_choice", "title": "0.4＋0.3 等于多少？", "options": "0.5\n0.6\n0.7\n0.8", "required": "on"},
            {"type": "number", "title": "先算 36÷6，再加 2，结果是多少？", "integer_only": "on", "required": "on"},
            {"type": "single_choice", "title": "50 的 30% 是多少？", "options": "10\n15\n20\n30", "required": "on"},
            {"type": "single_choice", "title": "按规律填数：3、6、9、下一项是？", "options": "10\n11\n12\n13", "required": "on"},
        ],
        "jumps": [],
    },
    {
        "title": "足球兴趣与规则认知问卷",
        "settings": {
            "title": "足球兴趣与规则认知问卷",
            "description": "先聊聊你的足球兴趣，再按你自评的了解程度回答不同深度的规则题。规则题依据 IFAB 足球竞赛规则；本问卷不自动判分。",
            "repeat_policy": "once",
            "option_label_style": "letters",
        },
        "questions": [
            {"type": "single_choice", "title": "你对足球有多感兴趣？", "options": "很感兴趣\n有一点兴趣\n暂时没有兴趣", "required": "on"},
            {"type": "text", "title": "你有没有特别喜欢的足球球员？可以写下名字和原因。", "max_length": "120"},
            {"type": "single_choice", "title": "你觉得自己对足球规则了解多少？", "options": "刚接触，不太了解\n了解一些\n比较熟悉", "required": "on"},
            {"type": "single_choice", "title": "十一人制足球比赛，一队场上最多有多少名球员（包括守门员）？", "options": "9 名\n10 名\n11 名\n12 名", "required": "on"},
            {"type": "single_choice", "title": "标准比赛的每个半场通常为多少分钟？", "options": "30 分钟\n40 分钟\n45 分钟\n60 分钟", "required": "on"},
            {"type": "single_choice", "title": "裁判出示黄牌表示什么？", "options": "警告\n罚出场\n比赛结束\n判罚角球", "required": "on"},
            {"type": "single_choice", "title": "裁判出示红牌表示什么？", "options": "警告\n罚出场\n暂停比赛\n改判界外球", "required": "on"},
            {"type": "number", "title": "按足球竞赛规则，一队至少有多少名球员，比赛才能开始或继续？", "integer_only": "on", "min_value": "1", "max_value": "11", "required": "on"},
            {"type": "multiple_choice", "title": "球员直接接到以下哪几种发球，不会因这次接球被判越位？", "options": "球门球\n界外球\n角球\n任意球", "exact_select": "3", "required": "on"},
            {"type": "single_choice", "title": "界外球可以直接掷入球门得分吗？", "options": "可以\n不可以", "required": "on"},
            {"type": "single_choice", "title": "在没有进球的情况下，防守方最后触球后，球整体越过本方球门线，通常由进攻方以什么方式重新开始比赛？", "options": "角球\n球门球\n界外球\n坠球", "required": "on"},
            {"type": "single_choice", "title": "什么时候球算完全出了边线？", "options": "球碰到边线时\n球有一半越过边线时\n整个球越过边线时\n裁判举旗时", "required": "on"},
            {"type": "text", "title": "你最想进一步了解哪条足球规则？", "max_length": "120"},
        ],
        "jumps": [
            (2, 7, {"option": "了解一些"}),
            (2, 7, {"option": "比较熟悉"}),
            (6, 12, {"option": "警告"}),
            (6, 12, {"option": "罚出场"}),
            (6, 12, {"option": "暂停比赛"}),
            (6, 12, {"option": "改判界外球"}),
        ],
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    app = create_app()
    db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
    owner = db.users.find_one({"username": "yyx"})
    if not owner:
        raise RuntimeError("Expected owner account yyx")
    old = []
    preliminary = []
    for survey_id, title in {**OLD, **REVISED_LOCAL}.items():
        survey = db.surveys.find_one({"_id": survey_id, "owner_id": owner["_id"]})
        if survey and survey["title"] != title:
            raise RuntimeError(f"Unexpected title at {survey_id}")
        if survey:
            old.append(survey)
            if survey_id in REVISED_LOCAL:
                if db.responses.count_documents({"survey_id": survey_id}):
                    raise RuntimeError(f"Revised local survey has answers: {survey_id}")
                preliminary.append(survey)
    snapshot = {
        "saved_at": datetime.now(timezone.utc),
        "surveys": old,
        "responses": list(db.responses.find({"survey_id": {"$in": list(OLD)}})),
        "drafts": list(db.response_drafts.find({"survey_id": {"$in": list(OLD)}})),
    }
    args.backup.parent.mkdir(parents=True, exist_ok=True)
    if args.backup.exists():
        raise RuntimeError("Backup already exists; choose a fresh path")
    with args.backup.open("x", encoding="utf-8") as handle:
        handle.write(json_util.dumps(snapshot, ensure_ascii=False, indent=2))
    os.chmod(args.backup, 0o600)
    print("BACKUP", args.backup, "old surveys", len(old), "responses", len(snapshot["responses"]))

    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = str(owner["_id"])
    for survey in preliminary:
        client.get(f"/surveys/{survey['_id']}")
        post(client, f"/surveys/{survey['_id']}/delete", {})
        print("REPLACED EARLIER DRAFT", survey["_id"])
    created = []
    for spec in SPECS:
        survey = create_experience_survey(client, db, owner, spec)
        questions = active_questions(survey)
        if len(questions) != len(spec["questions"]):
            raise RuntimeError(f"Question count mismatch for {spec['title']}")
        if [q["title"] for q in questions] != [q["title"] for q in spec["questions"]]:
            raise RuntimeError(f"Question text mismatch for {spec['title']}")
        if client.get(f"/survey/{survey['slug']}").status_code != 200:
            raise RuntimeError(f"Public fill page failed for {spec['title']}")
        if client.get(f"/surveys/{survey['_id']}/statistics").status_code != 200:
            raise RuntimeError(f"Statistics page failed for {spec['title']}")
        if spec["title"] == "基础数学题问卷":
            values = [(0, "15"), (1, "14"), (2, "18"), (3, "7"), (4, "四分之三"),
                      (5, "12"), (5, "18"), (6, "81"), (7, "16"), (8, "0.7"),
                      (9, "8"), (10, "15"), (11, "12")]
            form = MultiDict([("q_" + questions[index]["id"], value) for index, value in values])
            result = assessed_answers(questions, form)
            if len(questions) != 12 or len(result["answered"]) != 12 or result["errors"]:
                raise RuntimeError("Math answer key did not cover all twelve required questions")
        else:
            def path(level, last_basic="罚出场"):
                fields = [("q_" + questions[0]["id"], "有一点兴趣"),
                          ("q_" + questions[2]["id"], level)]
                if level.startswith("刚接触"):
                    fields.append(("q_" + questions[6]["id"], last_basic))
                result = assessed_answers(questions, MultiDict(fields))
                return [questions.index(next(q for q in questions if q["id"] == qid))
                        for qid in result["reachable"]]
            if path("刚接触，不太了解") != [0, 1, 2, 3, 4, 5, 6, 12]:
                raise RuntimeError("Beginner branch is wrong")
            if path("比较熟悉") != [0, 1, 2, 7, 8, 9, 10, 11, 12]:
                raise RuntimeError("Advanced branch is wrong")
        created.append(survey)
        print("CREATED", survey["_id"], survey["slug"], spec["title"], len(questions))

    for survey in old:
        if survey["_id"] in REVISED_LOCAL:
            continue
        client.get(f"/surveys/{survey['_id']}")
        post(client, f"/surveys/{survey['_id']}/delete", {})
        if db.surveys.find_one({"_id": survey["_id"]}):
            raise RuntimeError(f"Old survey remained: {survey['_id']}")
        print("REMOVED", survey["_id"], survey["title"])

    print("DONE", "new surveys", len(created), "remaining old surveys", db.surveys.count_documents({"_id": {"$in": list(OLD)}}))
    app.extensions["mongo_client"].close()


if __name__ == "__main__":
    main()
