"""Long survey navigation and non-persistent preview exits."""
import sys
import threading
import unittest
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class FillNavigationTests(unittest.TestCase):
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

    def new_scenario(self, width=390):
        owner = self.flow.app.test_client()
        username = 'navigation_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        survey = self.flow.survey(owner, username, '校园服务体验')
        questions = [{'id': str(i), 'title': f'校园服务意见 {i}', 'type': 'text',
                      'version': 1, 'required': True, 'active': True} for i in range(1, 17)]
        questions[0].update(type='single_choice', options=['跳过前两项', '逐项填写'],
                            jumps=[{'id': 'route', 'kind': 'single_choice',
                                         'option': '跳过前两项', 'target_id': '4'}])
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'questions': questions}})
        context = self.browser.new_context(viewport={'width': width, 'height': 844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                             'value': cookie, 'url': self.base}])
        page = context.new_page()
        page.set_default_timeout(6000)
        return context, page, survey

    def test_branching_group_labels_and_keyboard_focus_match_visible_questions(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                context, page, survey = self.new_scenario(width)
                try:
                    page.goto(self.base + f"/surveys/{survey['_id']}/preview")
                    page.locator('[name="q_1"]').first.check()
                    page.wait_for_function("document.querySelector('[data-overview-question=\"2\"]').hidden")
                    page.locator('[data-response-next]').click()
                    page.locator('[data-question-id="11"]').wait_for(state='visible')
                    self.assertEqual(page.locator('[data-response-page-status]').inner_text(),
                                     '第 2 组 / 共 2 组 · 本组 6 题')
                    self.assertEqual(page.evaluate('document.activeElement.dataset.questionId'), '11')
                    page.keyboard.press('Tab')
                    self.assertEqual(page.evaluate('document.activeElement.name'), 'q_11')
                    toggle = page.locator('[data-overview-toggle]')
                    if toggle.is_visible():
                        toggle.click()
                    page.locator('[data-overview-question="4"]').click()
                    self.assertEqual(page.evaluate('document.activeElement.dataset.questionId'), '4')
                    self.assertFalse(page.locator('[data-question-id="4"]').is_hidden())
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                finally:
                    context.close()

    def test_preview_top_return_does_not_prompt_or_save_answers(self):
        context, page, survey = self.new_scenario()
        dialogs = []
        def dismiss(dialog):
            dialogs.append(dialog.type)
            dialog.accept()
        page.on('dialog', dismiss)
        try:
            page.goto(self.base + f"/surveys/{survey['_id']}/preview")
            page.locator('[name="q_4"]').fill('预览试填内容')
            page.get_by_role('link', name='← 返回问卷', exact=True).click()
            page.wait_for_url(self.base + f"/surveys/{survey['_id']}")
            self.assertEqual(dialogs, [])
            self.assertEqual(self.flow.db.responses.count_documents({'survey_id': survey['_id']}), 0)
            self.assertEqual(self.flow.db.response_drafts.count_documents({'survey_id': survey['_id']}), 0)
        finally:
            context.close()
