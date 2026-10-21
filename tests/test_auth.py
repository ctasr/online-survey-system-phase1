import os
import unittest
from uuid import uuid4

from werkzeug.security import check_password_hash

from app import create_app


class AccountFlowTests(unittest.TestCase):
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
    def submit(client, path, data):
        client.get(path)
        with client.session_transaction() as saved:
            token = saved["csrf_token"]
        return client.post(path, data={"csrf_token": token, **data})

    def test_registration_validation_and_unique_username(self):
        client = self.app.test_client()
        name = "中文 用户 " + uuid4().hex

        mismatch = self.submit(client, "/register", {
            "username": name, "password": "123456", "confirmation": "654321"
        })
        self.assertIn("两次输入的密码不一致", mismatch.get_data(as_text=True))

        too_short = self.submit(client, "/register", {
            "username": name, "password": "12345", "confirmation": "12345"
        })
        self.assertIn("6～12", too_short.get_data(as_text=True))

        too_long = self.submit(client, "/register", {
            "username": name, "password": "1234567890123", "confirmation": "1234567890123"
        })
        self.assertIn("6～12", too_long.get_data(as_text=True))

        created = self.submit(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456"
        })
        self.assertEqual(created.status_code, 302)
        user = self.db.users.find_one({"username": name})
        self.assertIsNotNone(user)
        self.assertNotIn("password", user)
        self.assertNotEqual(user["password_hash"], "123456")
        self.assertTrue(check_password_hash(user["password_hash"], "123456"))
        self.assertIn("created_at", user)

        duplicate = self.submit(client, "/register", {
            "username": name, "password": "123456789012", "confirmation": "123456789012"
        })
        self.assertIn("用户名已被使用", duplicate.get_data(as_text=True))
        self.assertEqual(self.db.users.count_documents({"username": name}), 1)

    def test_login_persists_across_app_restart_and_logout(self):
        client = self.app.test_client()
        name = "persist_" + uuid4().hex
        self.assertEqual(client.get("/dashboard").status_code, 302)
        self.submit(client, "/register", {
            "username": name, "password": "123456789012", "confirmation": "123456789012"
        })

        wrong = self.submit(client, "/login", {"username": name, "password": "wrong"})
        self.assertIn("用户名或密码错误", wrong.get_data(as_text=True))

        signed_in = self.submit(client, "/login", {"username": name, "password": "123456789012"})
        self.assertEqual(signed_in.status_code, 302)
        self.assertEqual(signed_in.location, "/dashboard")
        self.assertIn("expires=", signed_in.headers["Set-Cookie"].lower())
        self.assertIn(name, client.get("/dashboard").get_data(as_text=True))

        cookie = client.get_cookie("session")
        restarted_app = create_app()
        same_browser = restarted_app.test_client()
        same_browser.set_cookie("session", cookie.value)
        self.assertIn(name, same_browser.get("/dashboard").get_data(as_text=True))
        self.assertEqual(restarted_app.test_client().get("/dashboard").status_code, 302)

        same_browser.get("/dashboard")
        with same_browser.session_transaction() as saved:
            token = saved["csrf_token"]
        self.assertEqual(same_browser.post("/logout", data={"csrf_token": token}).status_code, 302)
        self.assertEqual(same_browser.get("/dashboard").status_code, 302)
        restarted_app.extensions["mongo_client"].close()

    def test_home_is_guest_entry_and_signed_in_users_go_to_dashboard(self):
        client = self.app.test_client()
        guest_home = client.get("/")
        self.assertEqual(guest_home.status_code, 200)
        self.assertIn("注册账号", guest_home.get_data(as_text=True))

        name = "home_" + uuid4().hex
        self.submit(client, "/register", {
            "username": name, "password": "123456", "confirmation": "123456"
        })
        self.submit(client, "/login", {"username": name, "password": "123456"})
        signed_in_home = client.get("/")
        self.assertEqual(signed_in_home.status_code, 302)
        self.assertEqual(signed_in_home.location, "/dashboard")
        dashboard = client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn('>首页</a>', dashboard)
        self.assertIn('>个人首页</a>', dashboard)
        self.assertIn('>我的答卷</a>', dashboard)
        self.assertIn('>我的问卷</a>', dashboard)


if __name__ == "__main__":
    unittest.main()
