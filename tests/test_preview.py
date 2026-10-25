import os
import re
import unittest
from uuid import uuid4

from app import create_app


class SurveyPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase1_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    @staticmethod
    def token(client):
        with client.session_transaction() as saved:
            return saved["csrf_token"]

    def post(self, client, path, data):
        client.get(path)
        return client.post(path, data={"csrf_token": self.token(client), **data})

    def sign_in(self):
        client = self.app.test_client()
        name = "user_" + uuid4().hex
        self.post(client, "/register", {"username": name, "password": "123456", "confirmation": "123456"})
        self.post(client, "/login", {"username": name, "password": "123456"})
        return client

    @staticmethod
    def snapshot(page):
        return re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True)).group(1)

    def test_draft_preview_uses_real_jumps_and_validation_without_saving(self):
        owner, visitor = self.sign_in(), self.sign_in()
        self.post(owner, "/surveys/new", {"title": "体验预览"})
        survey = self.db.surveys.find_one({"title": "体验预览"})
        detail = f"/surveys/{survey['_id']}"
        preview = detail + "/preview"
        self.assertEqual(visitor.get(preview).status_code, 404)
        empty = owner.get(preview)
        self.assertEqual(empty.status_code, 302)
        for definition in (
            {"type": "single_choice", "title": "入口", "options": "A\nB", "required": "on"},
            {"type": "text", "title": "应跳过", "required": "on"},
            {"type": "number", "title": "分数", "required": "on", "min_value": "0", "max_value": "10"},
        ):
            self.post(owner, detail + "/questions/new", definition)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "A", "target_id": questions[2]["id"],
        })
        page = owner.get(preview)
        self.assertEqual(page.status_code, 200)
        self.assertIn("预览模式", page.get_data(as_text=True))
        snapshot = self.snapshot(page)
        keys = ["q_" + q["id"] for q in questions]
        next_result = owner.post(preview + "/next", data={
            "csrf_token": self.token(owner), "question_snapshot": snapshot,
            "question_id": questions[0]["id"], keys[0]: "A",
        })
        self.assertEqual(next_result.json["next_question_id"], questions[2]["id"])
        bad = owner.post(preview, data={
            "csrf_token": self.token(owner), "question_snapshot": snapshot,
            keys[0]: "A", keys[2]: "11",
        })
        self.assertIn("不能大于 10", bad.get_data(as_text=True))
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)
        done = owner.post(preview, data={
            "csrf_token": self.token(owner), "question_snapshot": snapshot,
            keys[0]: "A", keys[2]: "8",
        })
        self.assertEqual(done.status_code, 200)
        self.assertIn("预览答案没有保存", done.get_data(as_text=True))
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)
        self.assertIn("已提交答卷</span><strong>0</strong>", owner.get(detail + "/statistics").get_data(as_text=True))

        self.post(owner, detail + "/publish", {})
        published_preview = owner.get(preview)
        self.assertEqual(published_preview.status_code, 200)
        self.assertEqual(visitor.get(preview).status_code, 404)
        stale_snapshot = self.snapshot(published_preview)
        self.assertEqual(self.post(owner, f"{detail}/questions/{questions[2]['id']}/edit", {
            "type": "number", "title": "分数（修正）", "required": "on",
            "min_value": "0", "max_value": "5",
        }).status_code, 409)
        stale = owner.post(preview, data={
            "csrf_token": self.token(owner), "question_snapshot": stale_snapshot,
            keys[0]: "A", keys[2]: "8",
        })
        self.assertEqual(stale.status_code, 200)
        self.assertIn("预览答案没有保存", stale.get_data(as_text=True))
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 0)


if __name__ == "__main__":
    unittest.main()
