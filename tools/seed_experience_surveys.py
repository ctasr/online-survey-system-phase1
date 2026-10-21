"""Create two published questionnaires for the existing local owner's UX trial."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.datastructures import MultiDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app
from app.jumps import jump_warnings
from app.surveys import CHINA_TIME, active_questions, assessed_answers, validated_answers


def post(client, path, fields):
    client.get(path)
    with client.session_transaction() as saved:
        csrf = saved["csrf_token"]
    response = client.post(path, data={"csrf_token": csrf, **fields})
    if response.status_code != 302:
        raise RuntimeError(f"{path}: expected redirect, got {response.status_code}")
    return response


def add_questions(client, db, survey_id, definitions):
    detail = f"/surveys/{survey_id}"
    for fields in definitions:
        post(client, detail + "/questions/new", fields)
    questions = active_questions(db.surveys.find_one({"_id": survey_id}))
    if len(questions) != len(definitions):
        raise RuntimeError("Question count did not match the planned questionnaire")
    return detail, questions


def add_jump(client, detail, source, target, **condition):
    post(client, f"{detail}/questions/{source['id']}/jumps",
         {"target_id": target["id"], **condition})


def create_experience_survey(client, db, owner, spec):
    existing = db.surveys.find_one({"owner_id": owner["_id"], "title": spec["title"]})
    if existing:
        if existing["status"] != "published":
            raise RuntimeError(f"Existing survey is not published: {spec['title']}")
        return existing

    response = post(client, "/surveys/new", spec["settings"])
    title = spec["title"]
    survey = db.surveys.find_one({"owner_id": owner["_id"], "title": title})
    if survey is None or str(survey["_id"]) not in response.location:
        raise RuntimeError(f"Could not create {title}")
    detail, questions = add_questions(client, db, survey["_id"], spec["questions"])
    for source, target, condition in spec["jumps"]:
        add_jump(client, detail, questions[source], questions[target], **condition)

    survey = db.surveys.find_one({"_id": survey["_id"]})
    warnings = jump_warnings(active_questions(survey))
    if warnings:
        raise RuntimeError(f"Jump rules need review for {title}: {warnings}")
    post(client, detail + "/publish", {})
    survey = db.surveys.find_one({"_id": survey["_id"]})
    if survey["status"] != "published":
        raise RuntimeError(f"Could not publish {title}")
    return survey


def trial_specs():
    deadline = (datetime.now(CHINA_TIME) + timedelta(days=14)).strftime("%Y-%m-%dT23:59")
    return [
        {
            "title": "周末活动调查（体验）",
            "settings": {
                "title": "周末活动调查（体验）",
                "description": "选择感兴趣的活动；不同回答会看到不同的后续问题。",
                "allow_anonymous": "on",
                "repeat_policy": "multiple",
                "option_label_style": "numeric",
                "deadline": deadline,
            },
            "questions": [
                {"type": "single_choice", "title": "你更想参加哪类周末活动？", "options": "室内活动\n户外活动", "required": "on"},
                {"type": "single_choice", "title": "室内活动中你更喜欢哪一种？", "options": "电影\n桌游", "required": "on"},
                {"type": "single_choice", "title": "户外活动中你更喜欢哪一种？", "options": "徒步\n骑行", "required": "on"},
                {"type": "multiple_choice", "title": "你有兴趣参加哪些活动？", "options": "电影\n桌游\n徒步\n骑行\n暂时不参加", "min_select": "1", "max_select": "3", "required": "on"},
                {"type": "number", "title": "一个月大约能参加几次？", "integer_only": "on", "min_value": "0", "max_value": "12", "required": "on"},
                {"type": "text", "title": "你偏好的活动时间或地点是？", "max_length": "80"},
                {"type": "text", "title": "还有什么建议？", "max_length": "120"},
            ],
            "jumps": [
                (0, 2, {"option": "户外活动"}),
                (1, 3, {"option": "电影"}),
                (1, 3, {"option": "桌游"}),
                (3, 6, {"mode": "exact", "match_options": ["暂时不参加"]}),
                (4, 6, {"operator": "eq", "threshold": "0"}),
            ],
        },
        {
            "title": "课程学习体验调查（体验）",
            "settings": {
                "title": "课程学习体验调查（体验）",
                "description": "想了解你的学习习惯和课程体验，也欢迎分享改进建议。",
                "repeat_policy": "once",
                "option_label_style": "letters",
            },
            "questions": [
                {"type": "single_choice", "title": "你目前是否正在学习这门课程？", "options": "是\n还没有", "required": "on"},
                {"type": "multiple_choice", "title": "你主要通过哪些方式学习？", "options": "课堂听讲\n录播视频\n阅读教材\n小组讨论", "min_select": "1", "max_select": "3", "required": "on"},
                {"type": "number", "title": "你每周用于这门课程的课外学习时间（小时）？", "integer_only": "on", "min_value": "0", "max_value": "40", "required": "on"},
                {"type": "text", "title": "哪种学习方式对你最有帮助？", "max_length": "80"},
                {"type": "single_choice", "title": "你是否参加过本课程的小测？", "options": "是\n还没有", "required": "on"},
                {"type": "number", "title": "最近一次小测得分是多少？", "integer_only": "on", "min_value": "0", "max_value": "100"},
                {"type": "text", "title": "希望课程在哪方面改进？", "min_length": "5", "max_length": "120", "required": "on"},
                {"type": "multiple_choice", "title": "你最希望增加哪两种学习支持？", "options": "例题讲解\n课堂答疑\n练习反馈\n复习资料", "exact_select": "2", "required": "on"},
            ],
            "jumps": [
                (0, 4, {"option": "还没有"}),
                (2, 4, {"operator": "lte", "threshold": "1"}),
                (4, 6, {"option": "还没有"}),
            ],
        },
    ]


def verify_sample_paths(survey):
    questions = active_questions(survey)
    def check(values, expected):
        form = MultiDict([("q_" + questions[index]["id"], value)
                          for index, value in values])
        answers = validated_answers(questions, form)
        actual = [answer["question_id"] for answer in answers]
        wanted = [questions[index]["id"] for index in expected]
        if actual != wanted:
            raise RuntimeError(f"Unexpected jump path in {survey['title']}: {actual}")

    if survey["title"].startswith("周末活动"):
        check([(0, "户外活动"), (2, "徒步"), (3, "暂时不参加")], [0, 2, 3, 6])
        check([(0, "室内活动"), (1, "电影"), (3, "电影"), (4, "0")], [0, 1, 3, 4, 6])
        check([(0, "室内活动"), (1, "桌游"), (3, "电影"), (3, "桌游"), (4, "2")], [0, 1, 3, 4, 5, 6])
    else:
        check([(0, "还没有"), (4, "还没有"), (6, "希望增加练习"),
               (7, "例题讲解"), (7, "练习反馈")], [0, 4, 6, 7])
        check([(0, "是"), (1, "课堂听讲"), (2, "1"), (4, "还没有"),
               (6, "希望增加答疑"), (7, "课堂答疑"), (7, "复习资料")], [0, 1, 2, 4, 6, 7])
        check([(0, "是"), (1, "录播视频"), (2, "4"), (4, "是"), (5, "85"),
               (6, "希望增加反馈"), (7, "例题讲解"), (7, "课堂答疑")], list(range(8)))
        invalid = MultiDict([("q_" + questions[0]["id"], "还没有"),
                             ("q_" + questions[4]["id"], "还没有"),
                             ("q_" + questions[6]["id"], "建议"),
                             ("q_" + questions[7]["id"], "例题讲解")])
        result = assessed_answers(questions, invalid)
        if set(result["errors"]) != {questions[6]["id"], questions[7]["id"]}:
            raise RuntimeError("Text length and exact choice count were not checked")


def main():
    app = create_app()
    db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
    owners = list(db.users.find({}, {"username": 1}))
    if len(owners) != 1 or owners[0]["username"] != "yyx":
        raise RuntimeError("Expected the existing single yyx account; no data was changed")
    owner = owners[0]
    client = app.test_client()
    with client.session_transaction() as saved:
        saved["user_id"] = str(owner["_id"])

    try:
        for spec in trial_specs():
            survey = create_experience_survey(client, db, owner, spec)
            questions = active_questions(survey)
            if len(questions) != len(spec["questions"]):
                raise RuntimeError(f"Incomplete survey: {survey['title']}")
            if db.responses.count_documents({"survey_id": survey["_id"]}):
                raise RuntimeError(f"Unexpected prefilled response: {survey['title']}")
            verify_sample_paths(survey)
            public = f"/survey/{survey['slug']}"
            if client.get(public).status_code != 200:
                raise RuntimeError(f"Public page did not open: {survey['title']}")
            print(f"{survey['title']} | {len(questions)} questions | {len(sum((q.get('jumps', []) for q in questions), []))} jumps | {public} | /surveys/{survey['_id']}")
    finally:
        app.extensions["mongo_client"].close()


if __name__ == "__main__":
    main()
