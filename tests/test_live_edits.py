import os
import re
import unittest
from uuid import uuid4

from app import create_app


class LiveQuestionEditTests(unittest.TestCase):
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

    def test_edit_mid_fill_preserves_unchanged_answers_and_separates_old_statistics(self):
        owner, visitor = self.sign_in(), self.sign_in()
        self.post(owner, "/surveys/new", {"title": "填写中修改"})
        survey = self.db.surveys.find_one({"title": "填写中修改"})
        detail, public = f"/surveys/{survey['_id']}", f"/survey/{survey['slug']}"
        for definition in (
            {"type": "text", "title": "保留题", "required": "on"},
            {"type": "single_choice", "title": "会修改的题", "options": "旧A\n旧B", "required": "on"},
            {"type": "number", "title": "末题", "required": "on"},
        ):
            self.post(owner, detail + "/questions/new", definition)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, detail + "/publish", {})
        keys = ["q_" + q["id"] for q in questions]

        first_page = visitor.get(public)
        first_snapshot = self.snapshot(first_page)
        self.assertEqual(visitor.post(public, data={
            "csrf_token": self.token(visitor), "question_snapshot": first_snapshot,
            keys[0]: "早期答案", keys[1]: "旧A", keys[2]: "7",
        }).status_code, 302)

        stale_page = visitor.get(public)
        stale_snapshot = self.snapshot(stale_page)
        edited = self.post(owner, f"{detail}/questions/{questions[1]['id']}/edit", {
            "type": "multiple_choice", "title": "改成多选", "options": "新A\n新B", "required": "on",
        })
        self.assertEqual(edited.status_code, 302)
        latest = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(latest["questions"][1]["version"], 2)
        self.assertEqual(len(latest["question_history"]), 1)

        stale_next = visitor.post(public + "/next", data={
            "csrf_token": self.token(visitor), "question_snapshot": stale_snapshot,
            "question_id": questions[0]["id"], keys[0]: "稍后提交的答案",
        })
        self.assertEqual(stale_next.status_code, 200)
        self.assertEqual(stale_next.json["next_question_id"], questions[1]["id"])
        old_choice_step = visitor.post(public + "/next", data={
            "csrf_token": self.token(visitor), "question_snapshot": stale_snapshot,
            "question_id": questions[1]["id"], keys[1]: "旧A",
        })
        self.assertEqual(old_choice_step.status_code, 200)
        self.assertEqual(old_choice_step.json["next_question_id"], questions[2]["id"])

        stale_submit = visitor.post(public, data={
            "csrf_token": self.token(visitor), "question_snapshot": stale_snapshot,
            keys[0]: "稍后提交的答案", keys[1]: "旧A", keys[2]: "9",
        })
        self.assertEqual(stale_submit.status_code, 409)
        html = stale_submit.get_data(as_text=True)
        self.assertIn("问卷在你填写期间发生改动", html)
        self.assertIn('>稍后提交的答案</textarea>', html)
        self.assertIn('value="9"', html)
        self.assertNotIn('value="旧A" checked', html)
        self.assertEqual(self.db.responses.count_documents({"survey_id": survey["_id"]}), 1)

        new_snapshot = self.snapshot(stale_submit)
        final = visitor.post(public, data={
            "csrf_token": self.token(visitor), "question_snapshot": new_snapshot,
            keys[0]: "稍后提交的答案", keys[1]: ["新A"], keys[2]: "9",
        })
        self.assertEqual(final.status_code, 302)
        rows = list(self.db.responses.find({"survey_id": survey["_id"]}).sort("submitted_at", 1))
        self.assertEqual(rows[0]["answers"][1]["question_version"], 1)
        self.assertEqual(rows[1]["answers"][1]["question_version"], 2)
        old_review = visitor.get(f"/responses/{rows[0]['_id']}").get_data(as_text=True)
        self.assertIn('aria-label="本题的全部选项"', old_review)
        self.assertIn('review-option-text">旧A</span><span class="review-selected-label">你的选择', old_review)
        self.assertIn('review-option-text">旧B</span>', old_review)
        self.assertNotIn('review-option-text">新A</span>', old_review)
        new_review = visitor.get(f"/responses/{rows[1]['_id']}").get_data(as_text=True)
        self.assertIn('review-option-text">新A</span><span class="review-selected-label">你的选择', new_review)
        self.assertIn('review-option-text">新B</span>', new_review)
        stats = owner.get(detail + "/statistics").get_data(as_text=True)
        self.assertIn("会修改的题<span class=\"question-type-label\">历史版本 1</span>", stats)
        self.assertIn('<b class="option-result-marker">1.</b> 旧A</span><strong>1 份 · 100%', stats)
        self.assertIn("改成多选", stats)
        self.assertIn('<b class="option-result-marker">1.</b> 新A</span><strong>1 次 · 100%', stats)

    def test_delete_target_removes_incoming_jump_and_keeps_history(self):
        owner, visitor = self.sign_in(), self.sign_in()
        self.post(owner, "/surveys/new", {"title": "删除跳转目标"})
        survey = self.db.surveys.find_one({"title": "删除跳转目标"})
        detail, public = f"/surveys/{survey['_id']}", f"/survey/{survey['slug']}"
        for definition in (
            {"type": "single_choice", "title": "入口", "options": "A\nB", "required": "on"},
            {"type": "text", "title": "将被删除", "required": "on"},
            {"type": "text", "title": "终点", "required": "on"},
        ):
            self.post(owner, detail + "/questions/new", definition)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "A", "target_id": questions[1]["id"],
        })
        self.post(owner, detail + "/publish", {})
        old_page = visitor.get(public)
        old_snapshot = self.snapshot(old_page)
        self.assertEqual(visitor.post(public, data={
            "csrf_token": self.token(visitor), "question_snapshot": old_snapshot,
            "q_" + questions[0]["id"]: "A", "q_" + questions[1]["id"]: "旧答案",
            "q_" + questions[2]["id"]: "完成",
        }).status_code, 302)
        self.assertEqual(self.post(owner, f"{detail}/questions/{questions[1]['id']}/delete", {}).status_code, 302)
        latest = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertFalse(latest["questions"][1]["active"])
        self.assertEqual(latest["questions"][0]["jumps"], [])
        self.assertEqual(latest["questions"][0]["version"], 3)
        self.assertNotIn("将被删除", visitor.get(public).get_data(as_text=True))
        old_step = visitor.post(public + "/next", data={
            "csrf_token": self.token(visitor), "question_snapshot": old_snapshot,
            "question_id": questions[0]["id"], "q_" + questions[0]["id"]: "A",
        })
        self.assertEqual(old_step.status_code, 200)
        self.assertEqual(old_step.json["next_question_id"], questions[1]["id"])
        stale = visitor.post(public, data={
            "csrf_token": self.token(visitor), "question_snapshot": old_snapshot,
            "q_" + questions[0]["id"]: "A", "q_" + questions[1]["id"]: "无效旧答案",
            "q_" + questions[2]["id"]: "保留答案",
        })
        self.assertEqual(stale.status_code, 409)
        self.assertIn("保留答案", stale.get_data(as_text=True))
        self.assertNotIn("无效旧答案", stale.get_data(as_text=True))
        stats = owner.get(detail + "/statistics").get_data(as_text=True)
        self.assertIn("将被删除<span class=\"question-type-label\">历史版本", stats)
        self.assertIn("旧答案", stats)
        self.assertEqual(owner.get(detail + "/statistics/" + questions[1]["id"]).status_code, 200)

    def test_published_add_full_edit_noop_and_last_question_protection(self):
        owner, visitor = self.sign_in(), self.sign_in()
        self.post(owner, "/surveys/new", {"title": "发布后维护"})
        survey = self.db.surveys.find_one({"title": "发布后维护"})
        detail, public = f"/surveys/{survey['_id']}", f"/survey/{survey['slug']}"
        self.post(owner, detail + "/questions/new", {
            "type": "single_choice", "title": "选择", "options": "A\nB", "required": "on",
        })
        self.post(owner, detail + "/questions/new", {"type": "text", "title": "备注"})
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "A", "target_id": questions[1]["id"],
        })
        self.post(owner, detail + "/publish", {})
        revision = self.db.surveys.find_one({"_id": survey["_id"]})["revision"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/edit", {
            "type": "single_choice", "title": "选择", "options": "A\nB", "required": "on",
        })
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["revision"], revision)
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/edit", {
            "type": "number", "title": "输入数字", "integer_only": "on", "min_value": "0",
        })
        changed = self.db.surveys.find_one({"_id": survey["_id"]})
        self.assertEqual(changed["questions"][0]["type"], "number")
        self.assertEqual(len(changed["questions"][0]["jumps"]), 1)
        self.assertIn("条件与当前题型不匹配", owner.get(detail).get_data(as_text=True))
        stale = visitor.get(public)
        old_snapshot = self.snapshot(stale)
        self.assertEqual(self.post(owner, detail + "/questions/new", {
            "type": "text", "title": "新增题", "required": "on",
        }).status_code, 302)
        stale_submit = visitor.post(public, data={
            "csrf_token": self.token(visitor), "question_snapshot": old_snapshot,
            "q_" + questions[0]["id"]: "5", "q_" + questions[1]["id"]: "保留备注",
        })
        self.assertEqual(stale_submit.status_code, 409)
        self.assertIn("保留备注", stale_submit.get_data(as_text=True))
        self.assertIn("新增题", stale_submit.get_data(as_text=True))
        current = self.db.surveys.find_one({"_id": survey["_id"]})
        new_id = current["questions"][2]["id"]
        self.assertEqual(self.post(owner, f"{detail}/questions/{new_id}/delete", {}).status_code, 302)
        self.assertEqual(self.post(owner, f"{detail}/questions/{questions[1]['id']}/delete", {}).status_code, 302)
        last = self.post(owner, f"{detail}/questions/{questions[0]['id']}/delete", {})
        self.assertEqual(last.status_code, 302)
        self.assertIn("至少需要保留一道题目", owner.get(detail).get_data(as_text=True))
        self.assertEqual(len([q for q in self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
                              if q.get("active", True)]), 1)


if __name__ == "__main__":
    unittest.main()
