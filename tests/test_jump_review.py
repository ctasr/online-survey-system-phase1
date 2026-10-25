import os
import unittest
from uuid import uuid4

from app import create_app
from app.jumps import jump_warnings, next_question_index, rules_can_overlap


class JumpReviewLogicTests(unittest.TestCase):
    def test_number_overlap_respects_integer_and_value_limits(self):
        low = {"kind": "number", "operator": "gt", "threshold": "18"}
        high = {"kind": "number", "operator": "lt", "threshold": "19"}
        integer = {"type": "number", "integer_only": True, "min_value": None, "max_value": None}
        decimal = {**integer, "integer_only": False}
        self.assertFalse(rules_can_overlap(integer, [low, high]))
        self.assertTrue(rules_can_overlap(decimal, [low, high]))
        self.assertFalse(rules_can_overlap({**decimal, "max_value": "10"}, [low]))
        self.assertTrue(rules_can_overlap(integer, [
            {"kind": "number", "operator": "gte", "threshold": "18"},
            {"kind": "number", "operator": "lte", "threshold": "18"},
        ]))

    def test_multi_overlap_respects_exact_selection_count(self):
        question = {"type": "multiple_choice", "options": ["A", "B", "C"],
                    "exact_select": 1, "min_select": None, "max_select": None}
        a = {"kind": "multiple_choice", "mode": "any", "options": ["A"]}
        b = {"kind": "multiple_choice", "mode": "all", "options": ["B"]}
        exact = {"kind": "multiple_choice", "mode": "exact", "options": ["A"]}
        self.assertFalse(rules_can_overlap(question, [a, b]))
        self.assertTrue(rules_can_overlap({**question, "exact_select": 2}, [a, b]))
        self.assertTrue(rules_can_overlap(question, [a, exact]))
        self.assertFalse(rules_can_overlap(question, [b, exact]))

    def test_invalid_target_and_unreachable_condition_are_warnings(self):
        source = {"id": "one", "type": "number", "min_value": "0", "max_value": "10",
                  "integer_only": True, "jumps": [
                      {"id": "a", "kind": "number", "operator": "gt", "threshold": "10", "target_id": "two"},
                      {"id": "b", "kind": "number", "operator": "lt", "threshold": "5", "target_id": "gone"},
                  ]}
        target = {"id": "two", "type": "text"}
        messages = jump_warnings([source, target])["one"]
        self.assertTrue(any("不会有合法答案命中" in message for message in messages))
        self.assertTrue(any("跳转目标已失效" in message for message in messages))
        self.assertEqual(next_question_index([source, target], 0, "2"), 1)
        changed = {"id": "changed", "type": "number", "integer_only": True,
                   "min_value": None, "max_value": None,
                   "jumps": [{"id": "old", "kind": "single_choice", "option": "A", "target_id": "last"}]}
        middle, last = {"id": "middle", "type": "text"}, {"id": "last", "type": "text"}
        self.assertIn("条件与当前题型不匹配", jump_warnings([changed, middle, last])["changed"][0])
        self.assertEqual(next_question_index([changed, middle, last], 0, "5"), 1)


class JumpPriorityFlowTests(unittest.TestCase):
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
    def post(client, path, data):
        client.get(path)
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        return client.post(path, data={"csrf_token": token, **data})

    def test_overlapping_single_rules_can_be_reordered_after_publication(self):
        owner = self.app.test_client()
        name = "owner_" + uuid4().hex
        self.post(owner, "/register", {"username": name, "password": "123456", "confirmation": "123456"})
        self.post(owner, "/login", {"username": name, "password": "123456"})
        self.post(owner, "/surveys/new", {"title": "优先级测试"})
        survey = self.db.surveys.find_one({"title": "优先级测试"})
        detail = f"/surveys/{survey['_id']}"
        for definition in (
            {"type": "single_choice", "title": "入口", "options": "A\nB"},
            {"type": "text", "title": "目标一"},
            {"type": "text", "title": "目标二"},
        ):
            self.post(owner, detail + "/questions/new", definition)
        questions = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        source_id = questions[0]["id"]
        for target in questions[1:]:
            self.assertEqual(self.post(owner, f"{detail}/questions/{source_id}/jumps", {
                "option": "A", "target_id": target["id"],
            }).status_code, 302)
        html = owner.get(detail).get_data(as_text=True)
        self.assertIn("可能同时命中", html)
        self.assertIn("优先级 1", html)
        self.assertIn("优先级 2", html)
        current = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.assertEqual(next_question_index(current, 0, "A"), 1)
        rules = current[0]["jumps"]
        moved = self.post(owner, f"{detail}/questions/{source_id}/jumps/{rules[1]['id']}/move", {
            "direction": "up",
        })
        self.assertEqual(moved.status_code, 302)
        current = self.db.surveys.find_one({"_id": survey["_id"]})["questions"]
        self.assertEqual(current[0]["jumps"][0]["id"], rules[1]["id"])
        self.assertEqual(next_question_index(current, 0, "A"), 2)
        self.post(owner, detail + "/publish", {})
        self.assertEqual(self.post(owner, f"{detail}/questions/{source_id}/jumps/{rules[0]['id']}/move", {
            "direction": "up",
        }).status_code, 409)
        self.assertEqual(self.db.surveys.find_one({"_id": survey["_id"]})["status"], "published")


if __name__ == "__main__":
    unittest.main()
