import os
import re
import unittest
from uuid import uuid4

from app import create_app


class JumpFlowTests(unittest.TestCase):
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
        page = client.get(path[:-5] if path.endswith("/next") else path)
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        fields = {"csrf_token": token, **data}
        if path.startswith("/survey/"):
            match = re.search(r'name="question_snapshot" value="([^"]+)"', page.get_data(as_text=True))
            if match:
                fields["question_snapshot"] = match.group(1)
        return client.post(path, data=fields)

    def sign_in(self, client, name):
        self.post(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456",
        })
        self.post(client, "/login", {"username": name, "password": "123456"})

    def test_configurable_forward_jumps_and_visited_answer_validation(self):
        owner = self.app.test_client()
        visitor = self.app.test_client()
        self.sign_in(owner, "owner_" + uuid4().hex)
        self.sign_in(visitor, "visitor_" + uuid4().hex)
        self.post(owner, "/surveys/new", {"title": "跳转测试"})
        survey = self.db.surveys.find_one({"title": "跳转测试"})
        detail = f"/surveys/{survey['_id']}"
        public = f"/survey/{survey['slug']}"
        definitions = [
            {"type": "single_choice", "title": "入口", "required": "on", "options": "A\nB"},
            {"type": "text", "title": "跳过的必答题", "required": "on"},
            {"type": "multiple_choice", "title": "多选", "required": "on", "options": "苹果\n香蕉\n西瓜"},
            {"type": "text", "title": "另一道跳过的必答题", "required": "on"},
            {"type": "number", "title": "年龄", "required": "on", "integer_only": "on"},
            {"type": "text", "title": "未成年补充", "required": "on"},
            {"type": "text", "title": "末题", "required": "on"},
        ]
        for definition in definitions:
            self.assertEqual(self.post(owner, detail + "/questions/new", definition).status_code, 302)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        editor = owner.get(f"{detail}/questions/{questions[0]['id']}/jumps/new")
        self.assertEqual(editor.status_code, 200)
        self.assertIn("什么回答会触发跳转", editor.get_data(as_text=True))
        self.assertIn("跳到哪一道题", editor.get_data(as_text=True))
        multi_editor = owner.get(f"{detail}/questions/{questions[2]['id']}/jumps/new")
        self.assertIn('name="match_options"', multi_editor.get_data(as_text=True))
        number_editor = owner.get(f"{detail}/questions/{questions[4]['id']}/jumps/new")
        self.assertIn('name="threshold"', number_editor.get_data(as_text=True))
        self.assertEqual(owner.get(f"{detail}/questions/{questions[-1]['id']}/jumps/new").status_code, 404)

        def add_rule(index, values):
            return self.post(owner, f"{detail}/questions/{questions[index]['id']}/jumps", values)

        self.assertEqual(add_rule(0, {"option": "A", "target_id": questions[4]["id"]}).status_code, 302)
        self.assertEqual(add_rule(0, {"option": "B", "target_id": questions[2]["id"]}).status_code, 302)
        self.assertEqual(add_rule(2, {
            "mode": "all", "match_options": ["苹果", "香蕉"], "target_id": questions[4]["id"],
        }).status_code, 302)
        self.assertEqual(add_rule(2, {
            "mode": "any", "match_options": ["西瓜"], "target_id": questions[5]["id"],
        }).status_code, 302)
        self.assertEqual(add_rule(2, {
            "mode": "exact", "match_options": ["苹果"], "target_id": questions[6]["id"],
        }).status_code, 302)
        self.assertEqual(add_rule(4, {
            "operator": "gte", "threshold": "18", "target_id": questions[6]["id"],
        }).status_code, 302)
        invalid = add_rule(4, {"operator": "lt", "threshold": "18", "target_id": questions[1]["id"]})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("只能跳转到后面的题目", invalid.get_data(as_text=True))
        self.assertIn('value="18"', invalid.get_data(as_text=True))
        invalid = add_rule(4, {"operator": "gte", "threshold": "18.5", "target_id": questions[6]["id"]})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("设定的数字也必须是整数", invalid.get_data(as_text=True))
        self.assertIn('value="18.5"', invalid.get_data(as_text=True))
        self.assertEqual(len(self.db.surveys.find_one({"_id": survey["_id"]})["questions"][4]["jumps"]), 1)

        self.post(owner, detail + "/publish", {})
        self.assertEqual(visitor.get(public).status_code, 200)
        self.assertEqual(add_rule(0, {"option": "A", "target_id": questions[3]["id"]}).status_code, 409)

        keys = ["q_" + question["id"] for question in questions]
        next_path = public + "/next"
        step = self.post(visitor, next_path, {"question_id": questions[0]["id"], keys[0]: "A"})
        self.assertEqual(step.json["next_question_id"], questions[4]["id"])
        step = self.post(visitor, next_path, {"question_id": questions[0]["id"], keys[0]: "B"})
        self.assertEqual(step.json["next_question_id"], questions[2]["id"])
        step = self.post(visitor, next_path, {
            "question_id": questions[2]["id"], keys[2]: ["苹果", "香蕉"],
        })
        self.assertEqual(step.json["next_question_id"], questions[4]["id"])
        step = self.post(visitor, next_path, {
            "question_id": questions[2]["id"], keys[2]: ["苹果"],
        })
        self.assertEqual(step.json["next_question_id"], questions[6]["id"])
        step = self.post(visitor, next_path, {
            "question_id": questions[2]["id"], keys[2]: ["西瓜"],
        })
        self.assertEqual(step.json["next_question_id"], questions[5]["id"])
        step = self.post(visitor, next_path, {
            "question_id": questions[2]["id"], keys[2]: ["苹果", "香蕉", "西瓜"],
        })
        self.assertEqual(step.json["next_question_id"], questions[4]["id"])
        step = self.post(visitor, next_path, {"question_id": questions[4]["id"], keys[4]: "18"})
        self.assertEqual(step.json["next_question_id"], questions[6]["id"])
        step = self.post(visitor, next_path, {"question_id": questions[4]["id"], keys[4]: "17"})
        self.assertEqual(step.json["next_question_id"], questions[5]["id"])
        step = self.post(visitor, next_path, {"question_id": questions[4]["id"], keys[4]: "17.5"})
        self.assertEqual(step.status_code, 422)
        self.assertIn("必须输入整数", step.json["error"])

        response = self.post(visitor, public, {keys[0]: "A", keys[4]: "20", keys[6]: "完成"})
        self.assertEqual(response.status_code, 302)
        saved = self.db.responses.find_one({"survey_id": survey["_id"]})
        self.assertEqual([a["question_id"] for a in saved["answers"]], [
            questions[0]["id"], questions[4]["id"], questions[6]["id"],
        ])
        self.assertEqual(self.db.responses.count_documents({}), 1)
        missing_reached = self.post(visitor, public, {keys[0]: "A", keys[4]: "17"})
        self.assertIn("未成年补充", missing_reached.get_data(as_text=True))
        self.assertEqual(self.db.responses.count_documents({}), 1)
        invalid_visited = self.post(visitor, public, {keys[0]: "B", keys[2]: ["香蕉"], keys[3]: ""})
        self.assertIn("另一道跳过的必答题", invalid_visited.get_data(as_text=True))
        self.assertEqual(self.db.responses.count_documents({}), 1)

    def test_rule_can_be_deleted_before_publication(self):
        owner = self.app.test_client()
        self.sign_in(owner, "owner_" + uuid4().hex)
        self.post(owner, "/surveys/new", {"title": "删除规则测试"})
        survey = self.db.surveys.find_one({"title": "删除规则测试"})
        detail = f"/surveys/{survey['_id']}"
        self.post(owner, detail + "/questions/new", {
            "type": "single_choice", "title": "入口", "options": "A\nB",
        })
        self.post(owner, detail + "/questions/new", {"type": "text", "title": "终点"})
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps", {
            "option": "A", "target_id": questions[1]["id"],
        })
        rule_id = self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]["jumps"][0]["id"]
        result = self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps/{rule_id}/delete", {})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["questions"][0]["jumps"], [])
        missing = self.post(owner, f"{detail}/questions/{questions[0]['id']}/jumps/{rule_id}/delete", {})
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
