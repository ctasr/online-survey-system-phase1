import os
import re
import unittest
from uuid import uuid4

from app import create_app


class PublishedQuestionImmutabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase2_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    def post(self, client, path, data):
        client.get("/api/v1/csrf")
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        return client.post(path, data={"csrf_token": token, **data})

    def account(self):
        client = self.app.test_client()
        name = "user_" + uuid4().hex
        self.post(client, "/register", {"username": name, "password": "123456", "confirmation": "123456"})
        self.post(client, "/login", {"username": name, "password": "123456"})
        return client

    def create_survey(self, owner):
        title = "发布锁定_" + uuid4().hex
        self.post(owner, "/surveys/new", {"title": title})
        survey = self.db.surveys.find_one({"title": title})
        detail = f"/surveys/{survey['_id']}"
        self.post(owner, detail + "/questions/new", {
            "type": "single_choice", "title": "原题", "required": "on", "options": "甲\n乙"})
        self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "后续题", "required": "on"})
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "甲", "target_id": questions[1]["id"]})
        self.post(owner, detail + "/publish", {})
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        return survey, detail, questions

    def test_published_question_and_jump_cannot_be_changed(self):
        owner = self.account()
        survey, detail, questions = self.create_survey(owner)
        before = self.db.surveys.find_one({"_id": survey["_id"]})
        first, second = questions
        detail_html = owner.get(detail).get_data(as_text=True)
        self.assertNotIn(f"/jumps/{first['jumps'][0]['id']}/delete", detail_html)
        self.assertNotIn(f"/jumps/{first['jumps'][0]['id']}/move", detail_html)
        self.assertEqual(self.post(owner, f"{detail}/questions/{first['id']}/edit", {
            "type": "single_choice", "title": "改后题", "required": "on", "options": "甲\n乙"}).status_code, 409)
        self.assertEqual(self.post(owner, f"{detail}/questions/{second['id']}/delete", {}).status_code, 409)
        self.assertEqual(self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "新增题"}).status_code, 409)
        self.assertEqual(self.post(owner, f"{detail}/questions/{first['id']}/jumps", {
            "option": "乙", "target_id": second["id"]}).status_code, 409)
        self.assertEqual(self.post(owner, f"{detail}/questions/{first['id']}/jumps/{first['jumps'][0]['id']}/delete", {}).status_code, 409)
        after = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["questions"], before["questions"])

    def test_filling_snapshot_stays_valid_after_rejected_edit(self):
        owner, visitor = self.account(), self.account()
        survey, detail, questions = self.create_survey(owner)
        public = f"/survey/{survey['slug']}"
        page = visitor.get(public).get_data(as_text=True)
        snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', page).group(1)
        self.assertEqual(self.post(owner, f"{detail}/questions/{questions[0]['id']}/edit", {
            "type": "single_choice", "title": "错误修改", "options": "丙\n丁"}).status_code, 409)
        submitted = self.post(visitor, public, {
            "question_snapshot": snapshot,
            "q_" + questions[0]["id"]: "甲",
            "q_" + questions[1]["id"]: "旧题答案"})
        self.assertEqual(submitted.status_code, 302)
        answer = self.db.responses.find_one({"survey_id": survey["_id"]})
        self.assertEqual(answer["answers"][0]["value"], "甲")
        self.assertEqual(answer["answers"][0]["question_version"], questions[0]["version"])


if __name__ == "__main__":
    unittest.main()
