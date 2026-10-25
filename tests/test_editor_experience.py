"""Keyboard option editing in both survey and independent library forms."""
import sys
import threading
import unittest
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class EditorExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library_tests.LibraryFlowTests.setUpClass()
        cls.flow = library_tests.LibraryFlowTests()
        cls.server = make_server('127.0.0.1', 0, cls.flow.app, threaded=True)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            channel='msedge' if sys.platform == 'win32' else None, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        library_tests.LibraryFlowTests.tearDownClass()

    def test_option_enter_respects_composition_and_moves_through_existing_rows(self):
        owner = self.flow.app.test_client()
        username = 'editor_keys_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        survey = self.flow.survey(owner, username, '学习空间使用偏好')
        context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                             'value': cookie, 'url': self.base}])
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        labels = ['独立自习', '小组讨论', '课程答疑']
        try:
            for path, save_button, scope in (
                (f"/surveys/{survey['_id']}/questions/new", '保存题目', '问卷'),
                ('/question-library/new', '保存到题目库', '题库'),
            ):
                with self.subTest(scope=scope):
                    page.goto(self.base + path)
                    title = f'你主要会在学习空间开展哪类活动？（{scope}）'
                    page.get_by_label('题目内容', exact=True).fill(title)
                    inputs = page.locator('[data-options-rows] input')
                    inputs.nth(0).fill(labels[0])
                    # Browser-dispatched composition states emulate the IME boundary,
                    # without manipulating the operating system input method.
                    for event in ({'key': 'Enter', 'isComposing': True},
                                  {'key': 'Enter', 'keyCode': 229}):
                        inputs.nth(0).dispatch_event('keydown', event)
                        self.assertEqual(inputs.count(), 2)
                        self.assertTrue(inputs.nth(0).evaluate('el => el === document.activeElement'))
                    inputs.nth(0).press('Enter')
                    self.assertEqual(inputs.count(), 2)
                    self.assertTrue(inputs.nth(1).evaluate('el => el === document.activeElement'))
                    # Repeated Enter on an empty final row must not create more blanks.
                    inputs.nth(1).press('Enter')
                    self.assertEqual(inputs.count(), 2)
                    inputs.nth(1).fill(labels[1])
                    inputs.nth(1).press('Enter')
                    self.assertEqual(inputs.count(), 3)
                    self.assertTrue(inputs.nth(2).evaluate('el => el === document.activeElement'))
                    inputs.nth(2).fill(labels[2])
                    inputs.nth(0).press('Enter')
                    self.assertEqual(inputs.count(), 3)
                    self.assertTrue(inputs.nth(1).evaluate('el => el === document.activeElement'))
                    self.assertEqual(page.locator('#options').input_value(), '\n'.join(labels))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                    page.get_by_role('button', name=save_button, exact=True).click()
                    if scope == '问卷':
                        page.wait_for_url(self.base + f"/surveys/{survey['_id']}#question-*")
                        saved = self.flow.db.surveys.find_one({'_id': survey['_id']})['questions'][0]
                        self.assertTrue(page.url.endswith('#question-' + saved['id']))
                    else:
                        page.wait_for_url('**/question-library/*')
                        saved = self.flow.db.question_library.find_one({'owner_id': user['_id'], 'title': title})
                    self.assertIsNotNone(saved)
                    self.assertEqual(saved['options'], labels)
        finally:
            context.close()
        self.assertFalse(errors, errors)
