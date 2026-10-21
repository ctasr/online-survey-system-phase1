import os
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from app import create_app


class SubmissionPolicyTests(unittest.TestCase):
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
        page = client.get(path)
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        fields = {"csrf_token": token, **data}
        if path.startswith("/survey/"):
            match = re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True))
            if match:
                fields["question_snapshot"] = match.group(1)
        return client.post(path, data=fields)

    def create_account(self, name):
        client = self.app.test_client()
        self.post(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456",
        })
        self.post(client, "/login", {"username": name, "password": "123456"})
        return client

    def login_existing(self, name):
        client = self.app.test_client()
        self.post(client, "/login", {"username": name, "password": "123456"})
        return client

    def create_published_survey(self, owner, title, policy):
        self.post(owner, "/surveys/new", {
            "title": title, "repeat_policy": policy, "allow_anonymous": "on",
        })
        survey = self.db.surveys.find_one({"title": title})
        detail = f"/surveys/{survey['_id']}"
        self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "回答", "required": "on",
        })
        self.post(owner, detail + "/publish", {})
        question_id = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]["id"]
        return survey, f"/survey/{survey['slug']}", "q_" + question_id

    def test_personal_history_is_private_and_preserves_original_questions(self):
        owner = self.create_account("owner_" + uuid4().hex)
        survey, public, key = self.create_published_survey(owner, "答卷历史_" + uuid4().hex, "multiple")
        visitor = self.create_account("visitor_" + uuid4().hex)
        stranger = self.create_account("stranger_" + uuid4().hex)
        self.assertEqual(self.post(visitor, public, {key: "我的匿名回答"}).status_code, 302)
        saved = self.db.responses.find_one({"survey_id": survey["_id"]})
        self.assertIsNone(saved["respondent_id"])
        self.assertNotIn("respondent_key", saved)
        path = f"/responses/{saved['_id']}"
        self.assertIn(survey["title"], visitor.get("/responses").get_data(as_text=True))
        self.assertIn("我的匿名回答", visitor.get(path).get_data(as_text=True))
        self.assertEqual(owner.get(path).status_code, 404)
        self.assertEqual(stranger.get(path).status_code, 404)
        self.assertEqual(self.app.test_client().get(path).status_code, 302)
        edited = self.post(owner, f"/surveys/{survey['_id']}/questions/{key[2:]}/edit", {
            "type": "text", "title": "新的题目内容", "required": "on",
        })
        self.assertEqual(edited.status_code, 302)
        detail = visitor.get(path).get_data(as_text=True)
        self.assertIn('<span class="stat-question-number">1</span>回答</h2>', detail)
        self.assertNotIn("新的题目内容", detail)
        self.assertEqual(visitor.get("/responses?page=not-a-number").status_code, 200)
        self.assertEqual(visitor.get("/responses/not-an-id").status_code, 404)

        self.assertEqual(self.post(visitor, public, {key: "旧实名答卷", "identity_mode": "named"}).status_code, 302)
        named = self.db.responses.find_one({"survey_id": survey["_id"], "respondent_id": {"$ne": None}})
        self.db.responses.update_one({"_id": named["_id"]}, {"$unset": {"viewer_key": "", "survey_title": ""}})
        legacy_path = f"/responses/{named['_id']}"
        self.assertIn("旧实名答卷", visitor.get(legacy_path).get_data(as_text=True))
        self.assertEqual(stranger.get(legacy_path).status_code, 404)

    def test_anonymous_one_submission_per_account_even_when_simultaneous(self):
        owner = self.create_account("owner_" + uuid4().hex)
        survey, public, key = self.create_published_survey(owner, "限一次_" + uuid4().hex, "once")
        self.assertFalse(self.db.surveys.find_one({"_id": survey["_id"]})["allow_multiple_submissions"])
        name = "visitor_" + uuid4().hex
        first = self.create_account(name)
        second = self.login_existing(name)
        clients = [first, second]
        tokens = []
        snapshots = []
        for client in clients:
            page = client.get(public)
            with client.session_transaction() as saved:
                tokens.append(saved["csrf_token"])
            snapshots.append(re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True)).group(1))

        barrier = Barrier(2)

        def submit(index):
            barrier.wait()
            return clients[index].post(public, data={
                "csrf_token": tokens[index], "question_snapshot": snapshots[index], key: f"第{index + 1}份",
            }).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(sorted(results), [302, 409])
        saved = list(self.db.responses.find({"survey_id": survey["_id"]}))
        self.assertEqual(len(saved), 1)
        self.assertIsNone(saved[0]["respondent_id"])
        self.assertTrue(saved[0]["repeat_limited"])
        self.assertEqual(len(saved[0]["respondent_key"]), 64)
        self.assertIn("不能重复填写", first.get(public).get_data(as_text=True))

    def test_multiple_people_and_repeated_submissions_are_saved_separately(self):
        owner = self.create_account("owner_" + uuid4().hex)
        survey, public, key = self.create_published_survey(owner, "可重复_" + uuid4().hex, "multiple")
        first = self.create_account("visitor_" + uuid4().hex)
        second = self.create_account("visitor_" + uuid4().hex)
        clients = [first, second]
        tokens = []
        snapshots = []
        for client in clients:
            page = client.get(public)
            with client.session_transaction() as saved:
                tokens.append(saved["csrf_token"])
            snapshots.append(re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True)).group(1))
        barrier = Barrier(2)

        def submit(index):
            barrier.wait()
            return clients[index].post(public, data={
                "csrf_token": tokens[index], "question_snapshot": snapshots[index], key: f"第{index + 1}人",
            }).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(submit, range(2))), [302, 302])
        self.assertEqual(self.post(first, public, {key: "再次填写", "identity_mode": "named"}).status_code, 302)
        saved = list(self.db.responses.find({"survey_id": survey["_id"]}))
        self.assertEqual(len(saved), 3)
        self.assertEqual({row["answers"][0]["value"] for row in saved}, {
            "第1人", "第2人", "再次填写",
        })
        self.assertEqual(sum(row["respondent_id"] is None for row in saved), 2)
        self.assertIsNotNone(next(row for row in saved if row["answers"][0]["value"] == "再次填写")["respondent_id"])
        self.assertTrue(all("respondent_key" not in row for row in saved))

    def test_editing_fill_settings_preserves_old_answers_and_starts_new_once_period(self):
        owner = self.create_account("owner_" + uuid4().hex)
        survey, public, key = self.create_published_survey(owner, "设置变更_" + uuid4().hex, "multiple")
        visitor_name = "visitor_" + uuid4().hex
        visitor = self.create_account(visitor_name)
        visitor_id = self.db.users.find_one({"username": visitor_name})["_id"]
        detail = f"/surveys/{survey['_id']}"
        edit = detail + "/edit"

        self.assertEqual(self.post(visitor, public, {key: "旧匿名答卷"}).status_code, 302)
        old = self.db.responses.find_one({"survey_id": survey["_id"]})
        self.assertIsNone(old["respondent_id"])
        self.assertNotIn("respondent_key", old)
        opened = visitor.get(public).get_data(as_text=True)
        old_snapshot = re.search(r'name="question_snapshot" value="([^"]+)"', opened).group(1)
        with visitor.session_transaction() as saved:
            csrf = saved["csrf_token"]

        before = self.db.surveys.find_one({"_id": survey["_id"]})["revision"]
        self.assertEqual(self.post(owner, edit, {
            "title": survey["title"], "description": "", "allow_anonymous": "on",
            "repeat_policy": "once",
        }).status_code, 302)
        changed = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(changed["revision"], before + 1)
        self.assertEqual(changed["repeat_policy_epoch"], 1)
        interrupted = visitor.post(public, data={
            "csrf_token": csrf, "question_snapshot": old_snapshot, key: "保留这段输入",
        })
        self.assertEqual(interrupted.status_code, 409)
        self.assertIn("保留这段输入", interrupted.get_data(as_text=True))
        self.assertEqual(self.post(visitor, public, {key: "新限制下首次提交"}).status_code, 302)
        self.assertIn("不能重复填写", visitor.get(public).get_data(as_text=True))

        self.post(owner, edit, {"title": survey["title"], "description": "", "allow_anonymous": "on",
                                "repeat_policy": "multiple"})
        self.assertEqual(self.post(visitor, public, {key: "再次开放后提交"}).status_code, 302)
        self.post(owner, edit, {"title": survey["title"], "description": "", "repeat_policy": "once"})
        final_survey = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(final_survey["repeat_policy_epoch"], 2)
        self.assertFalse(final_survey["allow_anonymous"])
        self.assertEqual(self.post(visitor, public, {key: "实名提交"}).status_code, 302)
        self.assertIn("不能重复填写", visitor.get(public).get_data(as_text=True))
        rows = list(self.db.responses.find({"survey_id": survey["_id"]}))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["respondent_id"], visitor_id)


if __name__ == "__main__":
    unittest.main()
