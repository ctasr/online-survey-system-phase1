"""Call the JSON API over a real local HTTP socket with separate cookie jars."""

import json
import os
import threading
import unittest
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from werkzeug.serving import make_server

from app import create_app


class HttpClient:
    def __init__(self, base):
        self.base = base
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.token = None

    def call(self, method, path, data=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if self.token:
            headers["X-CSRF-Token"] = self.token
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def sign_in(self, username):
        status, result = self.call("GET", "/api/v1/csrf")
        assert status == 200
        self.token = result["csrf_token"]
        status, result = self.call("POST", "/api/v1/auth/register", {
            "username": username, "password": "123456", "confirmation": "123456",
        })
        assert status == 201, result
        status, result = self.call("POST", "/api/v1/auth/login", {
            "username": username, "password": "123456",
        })
        assert status == 200, result
        self.token = result["csrf_token"]


class JsonApiHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_name = "survey_phase1_http_test_" + uuid4().hex
        os.environ["MONGO_DB_NAME"] = cls.db_name
        cls.app = create_app()
        cls.server = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=10)
        client = cls.app.extensions["mongo_client"]
        client.drop_database(cls.db_name)
        client.close()
        os.environ.pop("MONGO_DB_NAME", None)

    def test_remote_style_http_flow(self):
        owner = HttpClient(self.base)
        writer = HttpClient(self.base)
        owner.sign_in("http_owner_" + uuid4().hex[:6])
        writer.sign_in("http_writer_" + uuid4().hex[:6])

        status, result = owner.call("POST", "/api/v1/surveys", {
            "title": "HTTP 问卷", "allow_anonymous": True,
        })
        self.assertEqual(status, 201, result)
        survey = result["survey"]
        detail = "/api/v1/surveys/" + survey["id"]
        status, result = owner.call("POST", detail + "/questions", {
            "type": "single_choice", "title": "是否满意",
            "options": ["满意", "不满意"], "required": True,
        })
        self.assertEqual(status, 201, result)
        question_id = result["question"]["id"]
        status, result = owner.call("POST", detail + "/publish", {
            "revision": result["revision"],
        })
        self.assertEqual(status, 200, result)
        public = "/api/v1/public/surveys/" + survey["slug"]
        status, result = writer.call("GET", public)
        self.assertEqual(status, 200, result)
        revision = result["survey"]["revision"]
        status, result = writer.call("POST", public + "/responses", {
            "revision": revision, "answers": {question_id: "满意"},
            "identity_mode": "anonymous",
        })
        self.assertEqual(status, 201, result)
        status, result = owner.call("GET", detail + "/statistics")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["response_count"], 1)
        self.assertEqual(result["results"][0]["option_counts"]["满意"], 1)
        status, result = writer.call("GET", detail)
        self.assertEqual(status, 404)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
