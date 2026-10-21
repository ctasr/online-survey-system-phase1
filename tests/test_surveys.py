import os
import re
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import create_app
from app.surveys import CHINA_TIME


class SurveyManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase1_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        if cls.db_name.startswith("survey_phase1_test_"):
            cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    @staticmethod
    def post(client, path, data):
        client.get(path)
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        return client.post(path, data={"csrf_token": token, **data})

    def signed_in_client(self):
        client = self.app.test_client()
        name = "student_" + uuid4().hex
        self.post(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456"
        })
        response = self.post(client, "/login", {"username": name, "password": "123456"})
        self.assertEqual(response.status_code, 302)
        return client, self.db.users.find_one({"username": name})

    def test_owner_can_review_account_responses_without_identifying_anonymous_ones(self):
        owner, owner_user = self.signed_in_client()
        respondent, respondent_user = self.signed_in_client()
        outsider, _ = self.signed_in_client()
        self.post(owner, "/surveys/new", {"title": "账号答卷测试", "allow_anonymous": "on"})
        survey = self.db.surveys.find_one({"owner_id": owner_user["_id"], "title": "账号答卷测试"})
        question = {"id": "q1", "version": 1, "title": "你选哪项？", "type": "single_choice",
                    "options": ["甲", "乙"], "required": True, "active": True}
        self.db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"questions": [question]}})
        now = datetime.now(timezone.utc)
        named_id = self.db.responses.insert_one({
            "survey_id": survey["_id"], "respondent_id": respondent_user["_id"],
            "submitted_at": now, "answers": [{"question_id": "q1", "question_version": 1, "value": "甲"}],
        }).inserted_id
        anonymous_id = self.db.responses.insert_one({
            "survey_id": survey["_id"], "respondent_id": None,
            "submitted_at": now, "answers": [{"question_id": "q1", "question_version": 1, "value": "乙"}],
        }).inserted_id
        base = f"/surveys/{survey['_id']}/responses"
        listing = owner.get(base).get_data(as_text=True)
        self.assertIn(respondent_user["username"], listing)
        self.assertIn(str(named_id), listing)
        self.assertNotIn(str(anonymous_id), listing)
        self.assertIn("账号答卷", owner.get(f"/surveys/{survey['_id']}/statistics").get_data(as_text=True))
        detail = owner.get(f"{base}/{named_id}")
        self.assertIn("该账号选择", detail.get_data(as_text=True))
        self.assertIn("你选哪项？", detail.get_data(as_text=True))
        self.assertEqual(owner.get(f"{base}/{anonymous_id}").status_code, 404)
        self.assertEqual(outsider.get(base).status_code, 404)
        self.assertEqual(outsider.get(f"{base}/{named_id}").status_code, 404)
        self.assertEqual(respondent.get(f"{base}/{named_id}").status_code, 404)
        self.assertNotIn(respondent_user["username"], owner.get(base + "?q=not-a-user").get_data(as_text=True))
        by_question = f"/surveys/{survey['_id']}/statistics/q1/accounts?version=1"
        self.assertIn(respondent_user["username"], owner.get(by_question).get_data(as_text=True))
        self.assertIn(respondent_user["username"], owner.get(by_question + "&option=0").get_data(as_text=True))
        self.assertNotIn(respondent_user["username"], owner.get(by_question + "&option=1").get_data(as_text=True))
        self.assertEqual(owner.get(by_question + "&option=9").status_code, 404)
        self.assertEqual(owner.get(f"/surveys/{survey['_id']}/statistics/q1/accounts?version=9").status_code, 404)
        self.assertEqual(outsider.get(by_question).status_code, 404)
        self.assertIn("查看选择此项的账号", owner.get(f"/surveys/{survey['_id']}/statistics/q1").get_data(as_text=True))
        extra_questions = [
            {"id": "q2", "version": 1, "title": "写下建议", "type": "text", "required": False, "active": True},
            {"id": "q3", "version": 1, "title": "给出分数", "type": "number", "required": False, "active": True},
            {"id": "q4", "version": 1, "title": "选择喜欢的项目", "type": "multiple_choice",
             "options": ["电影", "桌游"], "required": False, "active": True},
        ]
        self.db.surveys.update_one({"_id": survey["_id"]}, {"$set": {"questions": [question] + extra_questions}})
        self.db.responses.update_one({"_id": named_id}, {"$push": {"answers": {"$each": [
            {"question_id": "q2", "question_version": 1, "value": "课程很好"},
            {"question_id": "q3", "question_version": 1, "value": "0"},
            {"question_id": "q4", "question_version": 1, "value": ["电影", "桌游"]},
        ]}}})
        for suffix in ("q2/accounts?version=1&q=课程很好&exact=1",
                       "q3/accounts?version=1&q=0&exact=1",
                       "q4/accounts?version=1&option=1"):
            content = owner.get(f"/surveys/{survey['_id']}/statistics/{suffix}").get_data(as_text=True)
            self.assertIn(respondent_user["username"], content)

    def test_delete_survey_requires_owner_and_removes_its_responses(self):
        owner, owner_user = self.signed_in_client()
        another, _ = self.signed_in_client()
        self.post(owner, "/surveys/new", {"title": "待删除问卷"})
        self.post(owner, "/surveys/new", {"title": "保留问卷"})
        doomed = self.db.surveys.find_one({"owner_id": owner_user["_id"], "title": "待删除问卷"})
        kept = self.db.surveys.find_one({"owner_id": owner_user["_id"], "title": "保留问卷"})
        self.db.responses.insert_one({"survey_id": doomed["_id"], "answers": []})
        self.db.responses.insert_one({"survey_id": kept["_id"], "answers": []})
        path = f"/surveys/{doomed['_id']}/delete"
        self.assertEqual(owner.post(path).status_code, 400)
        another.get("/dashboard")
        with another.session_transaction() as saved:
            another_token = saved["csrf_token"]
        self.assertEqual(another.post(path, data={"csrf_token": another_token}).status_code, 404)
        self.assertEqual(self.post(owner, path, {}).status_code, 302)
        self.assertIsNone(self.db.surveys.find_one({"_id": doomed["_id"]}))
        self.assertEqual(self.db.responses.count_documents({"survey_id": doomed["_id"]}), 0)
        self.assertIsNotNone(self.db.surveys.find_one({"_id": kept["_id"]}))
        self.assertEqual(self.db.responses.count_documents({"survey_id": kept["_id"]}), 1)
        self.assertEqual(owner.get(f"/surveys/{doomed['_id']}").status_code, 404)
        self.assertEqual(owner.get(f"/survey/{doomed['slug']}").status_code, 404)

    def test_edit_survey_can_set_and_clear_deadline_with_other_fields(self):
        owner, owner_user = self.signed_in_client()
        self.post(owner, "/surveys/new", {"title": "时间设置"})
        survey = self.db.surveys.find_one({"owner_id": owner_user["_id"], "title": "时间设置"})
        path = f"/surveys/{survey['_id']}/edit"
        future = (datetime.now(CHINA_TIME) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(self.post(owner, path, {
            "title": "时间设置", "description": "说明", "repeat_policy": "multiple", "deadline": future,
        }).status_code, 302)
        saved = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(saved["description"], "说明")
        self.assertIsNotNone(saved["deadline"])
        invalid = self.post(owner, path, {
            "title": "时间设置", "description": "说明", "repeat_policy": "multiple",
            "deadline": "2020-01-01T00:00",
        })
        self.assertIn("必须晚于当前时间", invalid.get_data(as_text=True))
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["deadline"], saved["deadline"])
        self.assertEqual(self.post(owner, path, {
            "title": "时间设置", "description": "说明", "repeat_policy": "multiple", "deadline": "",
        }).status_code, 302)
        self.assertIsNone(self.db.surveys.find_one({"_id": survey["_id"]})["deadline"])

    def test_creation_ownership_public_link_and_statistics(self):
        owner, owner_user = self.signed_in_client()
        another, _ = self.signed_in_client()
        deadline_text = (datetime.now(CHINA_TIME) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")

        response = self.post(owner, "/surveys/new", {
            "title": "课程反馈", "description": "第一行\n第二行",
            "allow_anonymous": "on", "deadline": deadline_text,
        })
        self.assertEqual(response.status_code, 302)
        survey = self.db.surveys.find_one({"owner_id": owner_user["_id"]})
        self.assertIsNotNone(survey)
        self.assertEqual(survey["status"], "draft")
        self.assertEqual(survey["description"], "第一行\n第二行")
        self.assertTrue(survey["allow_anonymous"])
        self.assertEqual(len(survey["slug"]), 8)
        self.assertIsNotNone(survey["deadline"])

        self.assertIn("创建于", owner.get("/dashboard").get_data(as_text=True))
        self.assertIn("创建于", owner.get("/surveys").get_data(as_text=True))

        survey_id = str(survey["_id"])
        detail = f"/surveys/{survey_id}"
        public = f"/survey/{survey['slug']}"
        self.assertIn("课程反馈", owner.get("/surveys").get_data(as_text=True))
        self.assertNotIn("课程反馈", another.get("/surveys").get_data(as_text=True))
        self.assertEqual(another.get(detail).status_code, 404)
        self.assertEqual(another.get(detail + "/statistics").status_code, 404)
        self.assertEqual(self.post(another, detail + "/publish", {}).status_code, 404)
        self.assertEqual(self.app.test_client().get(public).status_code, 404)

        added = self.post(owner, detail + "/questions/new", {
            "type": "single_choice", "title": "是否满意", "options": "是\n否", "required": "on",
        })
        self.assertEqual(added.status_code, 302)

        published = self.post(owner, detail + "/publish", {})
        self.assertEqual(published.status_code, 302)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["status"], "published")
        self.assertIsNotNone(self.db.surveys.find_one({"_id": survey["_id"]})["published_at"])
        self.assertIn("发布于", owner.get("/dashboard").get_data(as_text=True))
        self.assertIn("发布于", owner.get("/surveys").get_data(as_text=True))
        self.assertEqual(self.app.test_client().get(public).status_code, 302)
        self.assertEqual(another.get(public).status_code, 200)
        local_detail = owner.get(detail).get_data(as_text=True)
        self.assertIn("跨网络分享尚未启用", local_detail)
        self.assertNotIn("data-copy-url=", local_detail)
        self.assertIn(f'href="{public}"', local_detail)
        self.app.config["PUBLIC_BASE_URL"] = "https://surveys.example.com"
        try:
            remote_detail = owner.get(detail).get_data(as_text=True)
            self.assertIn(f"https://surveys.example.com{public}", remote_detail)
            self.assertIn("填写链接", remote_detail)
        finally:
            self.app.config["PUBLIC_BASE_URL"] = ""

        stats = owner.get(detail + "/statistics").get_data(as_text=True)
        self.assertIn("已提交答卷", stats)
        self.assertIn("0", stats)
        self.db.responses.insert_one({"survey_id": survey["_id"], "answers": []})
        self.assertIn("1", owner.get(detail + "/statistics").get_data(as_text=True))

        closed = self.post(owner, detail + "/close", {})
        self.assertEqual(closed.status_code, 302)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["status"], "closed")
        self.assertIn("无法填写", self.app.test_client().get(public).get_data(as_text=True))

    def test_deadline_validation_and_login_return(self):
        owner, owner_user = self.signed_in_client()
        invalid = self.post(owner, "/surveys/new", {
            "title": "无效日期", "deadline": "2020-01-01T00:00"
        })
        self.assertIn("必须晚于当前时间", invalid.get_data(as_text=True))

        created = self.post(owner, "/surveys/new", {"title": "登录检查"})
        self.assertEqual(created.status_code, 302)
        survey = self.db.surveys.find_one({"owner_id": owner_user["_id"], "title": "登录检查"})
        detail = f"/surveys/{survey['_id']}"

        new_deadline = (datetime.now(CHINA_TIME) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        updated = self.post(owner, detail + "/deadline", {"deadline": new_deadline})
        self.assertEqual(updated.status_code, 302)
        stored = self.db.surveys.find_one({"_id": survey["_id"]})["deadline"]
        expected = datetime.fromisoformat(new_deadline).replace(tzinfo=CHINA_TIME).astimezone(timezone.utc)
        self.assertEqual(stored, expected.replace(tzinfo=None))
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["revision"], survey["revision"] + 1)

        self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "意见", "required": "on",
        })
        self.post(owner, detail + "/publish", {})
        public = f"/survey/{survey['slug']}"
        visitor = self.app.test_client()
        self.assertEqual(visitor.get(public).location, "/login")
        visitor_name = "visitor_" + uuid4().hex
        self.post(visitor, "/register", {
            "username": visitor_name, "password": "123456", "confirmation": "123456"
        })
        signed_in = self.post(visitor, "/login", {"username": visitor_name, "password": "123456"})
        self.assertEqual(signed_in.location, public)
        page = visitor.get(public).get_data(as_text=True)
        snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
        question_id = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]["id"]
        with visitor.session_transaction() as saved:
            csrf = saved["csrf_token"]
        later_deadline = (datetime.now(CHINA_TIME) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        self.post(owner, detail + "/deadline", {"deadline": later_deadline})
        fields = {"csrf_token": csrf, "question_snapshot": snapshot, "q_" + question_id: "旧页面答案"}
        step = visitor.post(public + "/next", data={**fields, "question_id": question_id})
        self.assertEqual(step.status_code, 200)
        submitted = visitor.post(public, data=fields)
        self.assertEqual(submitted.status_code, 409)
        self.assertIn("问卷信息在你填写期间发生改动", submitted.get_data(as_text=True))
        self.db.surveys.update_one(
            {"_id": survey["_id"]},
            {"$set": {"deadline": datetime.now(timezone.utc) - timedelta(minutes=1)}},
        )
        self.assertIn("已截止", owner.get("/surveys").get_data(as_text=True))
        self.assertIn("无法填写", visitor.get(public).get_data(as_text=True))

    def test_title_and_description_changes_prompt_only_on_submission(self):
        owner, owner_user = self.signed_in_client()
        visitor, _ = self.signed_in_client()
        self.post(owner, "/surveys/new", {"title": "初始调查", "description": "初始说明"})
        survey = self.db.surveys.find_one({"owner_id": owner_user["_id"], "title": "初始调查"})
        detail = f"/surveys/{survey['_id']}"
        edit = detail + "/edit"
        public = f"/survey/{survey['slug']}"
        self.assertEqual(visitor.get(edit).status_code, 404)
        self.assertIn("初始说明", owner.get(edit).get_data(as_text=True))
        blank = self.post(owner, edit, {"title": "   ", "description": "新说明"})
        self.assertIn("标题不能为空", blank.get_data(as_text=True))
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["title"], "初始调查")
        self.assertEqual(self.post(owner, edit, {
            "title": " 修正后的调查 ", "description": "修正后的说明",
        }).status_code, 302)
        changed = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(changed["title"], "修正后的调查")
        self.assertEqual(changed["description"], "修正后的说明")
        self.assertIn("修正后的调查", owner.get("/surveys").get_data(as_text=True))

        self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "反馈内容", "required": "on",
        })
        self.post(owner, detail + "/publish", {})
        question_id = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]["id"]
        page = visitor.get(public).get_data(as_text=True)
        snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
        with visitor.session_transaction() as saved:
            csrf = saved["csrf_token"]
        revision = self.db.surveys.find_one({"_id": survey["_id"]})["revision"]
        self.post(owner, edit, {"title": "校园反馈调查", "description": "请如实填写"})
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["revision"], revision + 1)
        step = visitor.post(public + "/next", data={
            "csrf_token": csrf, "question_snapshot": snapshot,
            "question_id": question_id, "q_" + question_id: "填写时标题被修正",
        })
        self.assertEqual(step.status_code, 200)
        self.assertIsNone(step.json["next_question_id"])
        response = visitor.post(public, data={
            "csrf_token": csrf, "question_snapshot": snapshot,
            "q_" + question_id: "填写时标题被修正",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("原有答案已保留", response.get_data(as_text=True))
        self.assertIn("填写时标题被修正", response.get_data(as_text=True))
        self.assertIn("确认并提交已保留的答案", response.get_data(as_text=True))
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)
        fresh_snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', response.get_data(as_text=True)).group(1)
        accepted = visitor.post(public, data={
            "csrf_token": csrf, "question_snapshot": fresh_snapshot,
            "q_" + question_id: "填写时标题被修正",
        })
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 1)

        self.post(owner, detail + "/close", {})
        self.assertEqual(self.post(owner, edit, {
            "title": "归档后的标题", "description": "归档后的说明",
        }).status_code, 302)
        self.assertIn("归档后的标题", owner.get(detail).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
