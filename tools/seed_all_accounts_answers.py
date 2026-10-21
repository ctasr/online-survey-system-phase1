"""Submit explicitly synthetic demo answers through the real survey form route.

Run once in each deployment with PYTHONPATH=. This is idempotent per user/survey.
The answers are test fixtures, not statements about the account holders.
"""

from bson import ObjectId
from werkzeug.datastructures import MultiDict

from app import create_app
from app.surveys import active_questions, assessed_answers, survey_snapshot


MATH_TITLE = "基础数学题问卷"
FOOTBALL_TITLE = "足球兴趣与规则认知问卷"
MATH_CORRECT = ["15", "14", "18", "7", "四分之三", ["12", "18"],
                "81", "16", "0.7", "8", "15", "12"]
BEGINNER_CORRECT = {3: "11 名", 4: "45 分钟", 5: "警告", 6: "罚出场"}
ADVANCED_CORRECT = {7: "7", 8: ["球门球", "界外球", "角球"],
                    9: "不可以", 10: "角球", 11: "整个球越过边线时"}


def profile_for(index):
    levels = ["刚接触，不太了解", "了解一些", "比较熟悉",
              "刚接触，不太了解", "比较熟悉", "了解一些",
              "刚接触，不太了解", "比较熟悉"]
    interests = ["有一点兴趣", "很感兴趣", "很感兴趣",
                 "暂时没有兴趣", "有一点兴趣", "很感兴趣",
                 "有一点兴趣", "很感兴趣"]
    return levels[index % len(levels)], interests[index % len(interests)]


def fields_for(survey, account_index, user):
    questions = active_questions(survey)
    fields = MultiDict()

    def put(index, value):
        values = value if isinstance(value, list) else [value]
        fields.setlist("q_" + questions[index]["id"], values)

    if survey["title"] == MATH_TITLE:
        values = list(MATH_CORRECT)
        # Deliberately vary valid answers so the distribution view is meaningful.
        if account_index in (1, 4):
            values[0] = "14"
        if account_index in (2, 5):
            values[4] = "二分之三"
        if account_index == 3:
            values[5] = ["12", "21"]
        if account_index == 6:
            values[6] = "72"
        for index, value in enumerate(values):
            put(index, value)
        expected = 12
    else:
        level, interest = profile_for(account_index)
        put(0, interest)
        put(2, level)
        # The two pre-existing personal accounts are given no invented favorite player.
        if user["username"].startswith("演示答题_"):
            favorites = ["梅西，喜欢他的传球", "C 罗，喜欢他的射门",
                         "孙兴慜，喜欢他的跑动", "姆巴佩，喜欢他的速度",
                         "梅西，喜欢他的配合", "没有特别喜欢的球员"]
            put(1, favorites[(account_index - 2) % len(favorites)])
        if level == "刚接触，不太了解":
            for index, value in BEGINNER_CORRECT.items():
                put(index, value)
            expected = 8
            if account_index % 2 == 0:
                put(12, "想进一步了解越位规则")
        else:
            for index, value in ADVANCED_CORRECT.items():
                put(index, value)
            expected = 9
            if account_index % 2 == 1:
                put(12, "想了解越位判罚的具体情况")

    assessment = assessed_answers(questions, fields)
    if assessment["errors"] or len(assessment["reachable"]) != expected:
        raise RuntimeError(f"Invalid fixture for {survey['title']}, {user['username']}: {assessment}")
    return fields, expected


def main():
    app = create_app()
    db = app.extensions["mongo_client"][app.config["MONGO_DB_NAME"]]
    owner = db.users.find_one({"username": "yyx"})
    if owner is None:
        raise RuntimeError("Owner yyx not found")
    surveys = []
    for title in (MATH_TITLE, FOOTBALL_TITLE):
        survey = db.surveys.find_one({"owner_id": owner["_id"], "title": title, "status": "published"})
        if survey is None:
            raise RuntimeError(f"Published survey not found: {title}")
        surveys.append(survey)
    users = list(db.users.find({}, {"username": 1}).sort("_id", 1))
    if len(users) != 8:
        raise RuntimeError(f"Expected 8 existing accounts, found {len(users)}; review before submitting")
    submitted = 0
    skipped = 0
    with app.app_context():
        for user_index, user in enumerate(users):
            for survey in surveys:
                existing = db.responses.find_one({"survey_id": survey["_id"], "respondent_id": user["_id"]})
                if existing:
                    skipped += 1
                    continue
                client = app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = str(user["_id"])
                url = "/survey/" + survey["slug"]
                page = client.get(url)
                if page.status_code != 200:
                    raise RuntimeError(f"GET {url} for {user['username']}: {page.status_code}")
                with client.session_transaction() as session:
                    csrf = session["csrf_token"]
                fields, expected = fields_for(survey, user_index, user)
                fields.add("identity_mode", "named")
                fields.add("csrf_token", csrf)
                fields.add("question_snapshot", survey_snapshot(survey))
                response = client.post(url, data=fields, follow_redirects=False)
                if response.status_code != 302:
                    raise RuntimeError(f"POST {url} for {user['username']}: {response.status_code}; "
                                       f"{response.get_data(as_text=True)[:200]}")
                saved = db.responses.find_one({"survey_id": survey["_id"], "respondent_id": user["_id"]})
                if saved is None or len(saved["answers"]) != expected:
                    raise RuntimeError(f"Response not saved correctly: {user['username']}, {survey['title']}")
                submitted += 1
                print("SUBMITTED", user["username"], survey["title"], "reached", expected)
    for survey in surveys:
        count = db.responses.count_documents({"survey_id": survey["_id"]})
        named = db.responses.count_documents({"survey_id": survey["_id"], "respondent_id": {"$ne": None}})
        if count < len(users) or named < len(users):
            raise RuntimeError(f"Insufficient responses: {survey['title']} {count}/{named}")
        owner_client = app.test_client()
        with owner_client.session_transaction() as session:
            session["user_id"] = str(owner["_id"])
        stats_url = f"/surveys/{survey['_id']}/statistics"
        if owner_client.get(stats_url).status_code != 200:
            raise RuntimeError(f"Statistics unavailable: {stats_url}")
        print("VERIFIED", survey["title"], "responses", count, "named", named, "stats", stats_url)
    print("DONE", "submitted", submitted, "skipped existing", skipped)
    app.extensions["mongo_client"].close()


if __name__ == "__main__":
    main()
