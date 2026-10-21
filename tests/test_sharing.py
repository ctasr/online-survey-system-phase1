import unittest
from flask import Flask

from app.sharing import survey_link


class SharingLinkTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["PUBLIC_BASE_URL"] = ""

    def test_localhost_has_no_public_link(self):
        with self.app.test_request_context("/", base_url="http://127.0.0.1:5001"):
            self.assertEqual(survey_link("/survey/example"), (None, "unconfigured"))

    def test_public_base_url_takes_priority(self):
        self.app.config["PUBLIC_BASE_URL"] = "https://surveys.example.com"
        with self.app.test_request_context("/", base_url="http://127.0.0.1:5001"):
            self.assertEqual(survey_link("/survey/example"),
                             ("https://surveys.example.com/survey/example", "public"))

    def test_request_host_cannot_forge_a_public_link(self):
        with self.app.test_request_context("/", base_url="https://unverified.example"):
            self.assertEqual(survey_link("/survey/example"), (None, "unconfigured"))

    def test_lan_host_has_no_public_link(self):
        with self.app.test_request_context("/", base_url="http://192.168.1.102:5001"):
            self.assertEqual(survey_link("/survey/example"), (None, "unconfigured"))
