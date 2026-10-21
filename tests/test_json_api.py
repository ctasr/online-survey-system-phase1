"""Exercise the JSON API through real HTTP semantics and an isolated MongoDB."""

import os
import unittest
from uuid import uuid4

from app import create_app


class JsonApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase1_api_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions["mongo_client"][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["mongo_client"].drop_database(cls.db_name)
        cls.app.extensions["mongo_client"].close()
        os.environ.pop("MONGO_DB_NAME", None)

    def client(self, username):
        client = self.app.test_client()
        token = client.get("/api/v1/csrf").json["csrf_token"]
        headers = {"X-CSRF-Token": token}
        registered = client.post("/api/v1/auth/register", json={
            "username": username, "password": "123456", "confirmation": "123456",
        }, headers=headers)
        self.assertEqual(registered.status_code, 201, registered.json)
        login = client.post("/api/v1/auth/login", json={
            "username": username, "password": "123456",
        }, headers=headers)
        self.assertEqual(login.status_code, 200, login.json)
        return client, {"X-CSRF-Token": login.json["csrf_token"]}

    def post(self, client, path, data, headers):
        return client.post(path, json=data, headers=headers)

    def test_full_json_workflow_and_permissions(self):
        owner, owner_headers = self.client("owner_" + uuid4().hex[:6])
        respondent, respondent_headers = self.client("writer_" + uuid4().hex[:6])
        outsider, outsider_headers = self.client("other_" + uuid4().hex[:6])

        created = self.post(owner, "/api/v1/surveys", {
            "title": "课程调查", "description": "了解学习体验",
            "allow_anonymous": True, "allow_multiple_submissions": False,
            "option_label_style": "letters",
        }, owner_headers)
        self.assertEqual(created.status_code, 201, created.json)
        survey = created.json["survey"]
        survey_id = survey["id"]
        path = f"/api/v1/surveys/{survey_id}"
        self.assertEqual(survey["revision"], 1)
        self.assertEqual(survey["status"], "draft")
        self.assertEqual(outsider.get(path).status_code, 404)
        self.assertEqual(respondent.get("/api/v1/public/surveys/" + survey["slug"]).status_code, 404)

        definitions = [
            {"type": "single_choice", "title": "是否上课", "options": ["是", "否"], "required": True},
            {"type": "text", "title": "原因", "required": True, "min_length": 2},
            {"type": "multiple_choice", "title": "方式", "options": ["读书", "视频", "讨论"],
             "exact_select": 2},
            {"type": "number", "title": "时间", "required": True, "integer_only": True,
             "min_value": 0, "max_value": 40, "number_statistics": "measure"},
        ]
        ids = []
        revision = 1
        for definition in definitions:
            response = self.post(owner, path + "/questions",
                                 {**definition, "revision": revision}, owner_headers)
            self.assertEqual(response.status_code, 201, response.json)
            ids.append(response.json["question"]["id"])
            revision = response.json["revision"]
        self.assertEqual(len(owner.get(path + "/questions").json["questions"]), 4)
        self.assertEqual(owner.get(path + "/questions/" + ids[0]).json["question"]["title"],
                         "是否上课")

        jump_path = path + "/questions/" + ids[0] + "/jumps"
        added = self.post(owner, jump_path, {
            "option": "是", "target_id": ids[2], "revision": revision,
        }, owner_headers)
        self.assertEqual(added.status_code, 201, added.json)
        revision = added.json["revision"]
        rule_id = added.json["rule"]["id"]
        self.assertEqual(self.post(owner, path + "/publish", {"revision": revision},
                                   owner_headers).status_code, 200)
        public = "/api/v1/public/surveys/" + survey["slug"]
        public_data = respondent.get(public)
        self.assertEqual(public_data.status_code, 200, public_data.json)
        self.assertEqual(public_data.json["survey"]["option_label_style"], "letters")
        self.assertEqual(len(public_data.json["survey"]["questions"]), 4)

        answers = {ids[0]: "是", ids[2]: ["读书", "视频"], ids[3]: "12"}
        assess = self.post(respondent, public + "/assess", {
            "answers": answers,
        }, respondent_headers)
        self.assertEqual(assess.status_code, 200, assess.json)
        self.assertEqual(assess.json["reachable"], [ids[0], ids[2], ids[3]])
        self.assertEqual(assess.json["errors"], {})
        invalid = self.post(respondent, public + "/responses", {
            "revision": revision, "answers": {**answers, ids[2]: ["读书"]},
        }, respondent_headers)
        self.assertEqual(invalid.status_code, 422, invalid.json)
        self.assertEqual(invalid.json["question_id"], ids[2])

        stale = self.post(respondent, public + "/responses", {
            "revision": revision - 1, "answers": answers,
        }, respondent_headers)
        self.assertEqual(stale.status_code, 409)
        submitted = self.post(respondent, public + "/responses", {
            "revision": revision, "answers": answers, "identity_mode": "anonymous",
        }, respondent_headers)
        self.assertEqual(submitted.status_code, 201, submitted.json)
        response_id = submitted.json["response_id"]
        saved = self.db.responses.find_one({"survey_id": self.db.surveys.find_one(
            {"slug": survey["slug"]})["_id"]})
        self.assertIsNone(saved["respondent_id"])
        self.assertEqual([answer["question_id"] for answer in saved["answers"]],
                         [ids[0], ids[2], ids[3]])
        self.assertEqual(respondent.get("/api/v1/responses/" + response_id).status_code, 200)
        self.assertEqual(outsider.get("/api/v1/responses/" + response_id).status_code, 404)
        self.assertEqual(len(respondent.get("/api/v1/responses").json["responses"]), 1)
        self.assertEqual(self.post(respondent, public + "/responses", {
            "revision": revision, "answers": answers,
        }, respondent_headers).status_code, 409)

        stats = owner.get(path + "/statistics")
        self.assertEqual(stats.status_code, 200, stats.json)
        self.assertEqual(stats.json["response_count"], 1)
        self.assertEqual(stats.json["overview"]["anonymous"], 1)
        single = owner.get(path + "/statistics/" + ids[0])
        self.assertEqual(single.json["results"][0]["option_counts"]["是"], 1)
        numeric = owner.get(path + "/statistics/" + ids[3])
        self.assertEqual(numeric.json["results"][0]["average"], "12")
        self.assertEqual(outsider.get(path + "/statistics").status_code, 404)

        revised = owner.patch(path, json={
            "title": "课程体验调查", "revision": revision,
        }, headers=owner_headers)
        self.assertEqual(revised.status_code, 200, revised.json)
        revision = revised.json["survey"]["revision"]
        self.assertEqual(self.post(owner, path + "/close", {
            "revision": revision,
        }, owner_headers).status_code, 200)
        self.assertEqual(respondent.get(public).status_code, 409)
        self.assertEqual(owner.delete(path, json={"revision": revision},
                                      headers=owner_headers).status_code, 200)
        self.assertEqual(self.db.responses.count_documents({}), 0)

    def test_validation_auth_csrf_and_question_versions(self):
        owner, headers = self.client("owner_" + uuid4().hex[:6])
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/v1/surveys").status_code, 401)
        self.assertEqual(owner.post("/api/v1/surveys", json={"title": "无 CSRF"}).status_code, 400)
        self.assertEqual(owner.post("/api/v1/surveys", data="bad",
                                    headers=headers).status_code, 415)
        self.assertEqual(self.post(owner, "/api/v1/surveys", {
            "title": "",
        }, headers).status_code, 422)
        created = self.post(owner, "/api/v1/surveys", {"title": "校验调查"}, headers)
        path = "/api/v1/surveys/" + created.json["survey"]["id"]
        self.assertEqual(self.post(owner, path + "/publish", {}, headers).status_code, 422)
        bad = self.post(owner, path + "/questions", {
            "type": "multiple_choice", "title": "水果", "options": ["苹果", "苹果"],
        }, headers)
        self.assertEqual(bad.status_code, 422)
        good = self.post(owner, path + "/questions", {
            "type": "multiple_choice", "title": "水果", "options": ["苹果", "香蕉", "西瓜"],
            "min_select": 2, "max_select": 3, "required": True,
        }, headers)
        self.assertEqual(good.status_code, 201, good.json)
        question_id = good.json["question"]["id"]
        revision = good.json["revision"]
        changed = owner.patch(path + "/questions/" + question_id, json={
            "title": "最喜欢的水果", "revision": revision,
        }, headers=headers)
        self.assertEqual(changed.status_code, 200, changed.json)
        self.assertEqual(changed.json["question"]["version"], 2)
        self.assertEqual(len(self.db.surveys.find_one({"title": "校验调查"})["question_history"]), 1)
        self.assertEqual(owner.delete(path + "/questions/" + question_id, json={
            "revision": changed.json["revision"],
        }, headers=headers).status_code, 200)
        self.assertEqual(owner.get(path + "/questions").json["questions"], [])

    def test_multiple_and_numeric_jumps_and_live_edit_conflict(self):
        owner, owner_headers = self.client("owner_" + uuid4().hex[:6])
        writer, writer_headers = self.client("writer_" + uuid4().hex[:6])
        created = self.post(owner, "/api/v1/surveys", {
            "title": "跳转调查", "allow_multiple_submissions": True,
        }, owner_headers)
        survey = created.json["survey"]
        path = "/api/v1/surveys/" + survey["id"]
        definitions = [
            {"type": "multiple_choice", "title": "喜欢什么", "options": ["苹果", "香蕉", "西瓜"],
             "required": True, "min_select": 2},
            {"type": "text", "title": "水果理由", "required": True},
            {"type": "number", "title": "每周次数", "required": True, "integer_only": True,
             "min_value": 0, "max_value": 10},
            {"type": "text", "title": "次数说明", "required": True},
            {"type": "text", "title": "总结", "required": True},
        ]
        ids = []
        revision = survey["revision"]
        for definition in definitions:
            response = self.post(owner, path + "/questions",
                                 {**definition, "revision": revision}, owner_headers)
            self.assertEqual(response.status_code, 201, response.json)
            ids.append(response.json["question"]["id"])
            revision = response.json["revision"]
        multi = self.post(owner, path + "/questions/" + ids[0] + "/jumps", {
            "mode": "all", "match_options": ["苹果", "香蕉"],
            "target_id": ids[2], "revision": revision,
        }, owner_headers)
        self.assertEqual(multi.status_code, 201, multi.json)
        revision = multi.json["revision"]
        numeric = self.post(owner, path + "/questions/" + ids[2] + "/jumps", {
            "operator": "gte", "threshold": 5,
            "target_id": ids[4], "revision": revision,
        }, owner_headers)
        self.assertEqual(numeric.status_code, 201, numeric.json)
        revision = numeric.json["revision"]
        self.assertEqual(self.post(owner, path + "/publish", {
            "revision": revision,
        }, owner_headers).status_code, 200)
        public = "/api/v1/public/surveys/" + survey["slug"]
        opened = writer.get(public).json["survey"]
        self.assertEqual(opened["revision"], revision)
        answers = {ids[0]: ["苹果", "香蕉"], ids[2]: "5", ids[4]: "满意"}
        assessed = self.post(writer, public + "/assess", {
            "answers": answers,
        }, writer_headers)
        self.assertEqual(assessed.json["reachable"], [ids[0], ids[2], ids[4]])
        invalid = self.post(writer, public + "/responses", {
            "revision": revision, "answers": {**answers, ids[2]: "5.5"},
        }, writer_headers)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json["question_id"], ids[2])

        edited = owner.patch(path + "/questions/" + ids[4], json={
            "title": "其他建议", "revision": revision,
        }, headers=owner_headers)
        self.assertEqual(edited.status_code, 200, edited.json)
        self.assertEqual(self.post(writer, public + "/responses", {
            "revision": revision, "answers": answers,
        }, writer_headers).status_code, 409)
        latest = writer.get(public).json["survey"]
        submitted = self.post(writer, public + "/responses", {
            "revision": latest["revision"], "answers": answers,
        }, writer_headers)
        self.assertEqual(submitted.status_code, 201, submitted.json)
        repeated = self.post(writer, public + "/responses", {
            "revision": latest["revision"], "answers": answers,
        }, writer_headers)
        self.assertEqual(repeated.status_code, 201, repeated.json)
        self.assertNotEqual(submitted.json["response_id"], repeated.json["response_id"])
        self.assertEqual(owner.get(path + "/statistics").json["response_count"], 2)

    def test_json_error_shape_and_duplicate_username(self):
        client, headers = self.client("person_" + uuid4().hex[:6])
        self.assertEqual(client.get("/api/v1/not-found").status_code, 404)
        self.assertIn("error", client.get("/api/v1/not-found").json)
        second = self.post(client, "/api/v1/auth/register", {
            "username": client.get("/api/v1/auth/me").json["username"],
            "password": "123456", "confirmation": "123456",
        }, headers)
        self.assertEqual(second.status_code, 409)
        self.assertIn("error", second.json)


if __name__ == "__main__":
    unittest.main()
