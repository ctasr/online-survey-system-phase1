"""Keep the selection task in place while creating a missing reusable question."""
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class LibraryCreationContextTests(unittest.TestCase):
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

    def test_create_cancel_validation_and_save_keep_filtered_selection_page(self):
        owner = self.flow.app.test_client()
        name = 'selection_context_' + uuid4().hex
        self.flow.login(owner, name)
        user = self.flow.db.users.find_one({'username': name})
        survey = self.flow.survey(owner, name, '图书馆服务反馈')
        bank_id = self.flow.api(owner, 'POST', '/api/v1/library/banks',
                                {'name': '图书馆服务题库'}).get_json()['id']
        for number in range(15):
            item = self.flow.api(owner, 'POST', '/api/v1/library/questions', {
                'type': 'text', 'title': f'图书馆服务意见 {number + 1}',
                'required': False}).get_json()['question']
            self.flow.api(owner, 'POST', f'/api/v1/library/banks/{bank_id}/questions',
                          {'question_id': item['id']})
        expected_query = {'q': ['图书馆'], 'bank': [bank_id],
                          'mine_page': ['2'], 'shared_page': ['1']}
        original = f"/surveys/{survey['_id']}/question-library?" + urlencode(
            {key: value[0] for key, value in expected_query.items()})
        context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                             'value': cookie, 'url': self.base}])
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def assert_selection():
            self.assertEqual(parse_qs(urlsplit(page.url).query), expected_query)
            self.assertEqual(page.locator('#choose-query').input_value(), '图书馆')
            self.assertEqual(page.locator('.library-choose-card').count(), 3)
            self.assertEqual(page.locator('#choose-bank').input_value(), bank_id)

        try:
            page.goto(self.base + original)
            assert_selection()
            # Both explicit return paths keep the search, bank filter and pagination.
            for selector in ('[data-discard-changes]', '.form-page-heading > a'):
                page.get_by_role('link', name='＋ 新建并加入问卷', exact=True).click()
                page.locator(selector).click()
                assert_selection()

            page.get_by_role('link', name='＋ 新建并加入问卷', exact=True).click()
            page.locator('[data-question-type="text"]').click()
            title = '你希望图书馆增加哪些服务？'
            page.get_by_label('题目内容', exact=True).fill(title)
            page.get_by_label('最少字数', exact=True).fill('10')
            page.get_by_label('最多字数', exact=True).fill('2')
            page.get_by_role('button', name='保存并加入问卷', exact=True).click()
            self.assertTrue(page.locator('[role="alert"]').is_visible())
            self.assertEqual(page.get_by_label('题目内容', exact=True).input_value(), title)
            self.assertEqual(page.locator('input[name="filter_bank"]').input_value(), bank_id)
            self.assertEqual(page.locator('input[name="mine_page"]').input_value(), '2')

            page.get_by_label('最多字数', exact=True).fill('100')
            page.get_by_role('button', name='保存并加入问卷', exact=True).click()
            page.wait_for_url(lambda value: urlsplit(str(value)).path == urlsplit(original).path)
            assert_selection()
            saved_survey = self.flow.db.surveys.find_one({'_id': survey['_id']})
            self.assertEqual(len(saved_survey['questions']), 1)
            self.assertEqual(saved_survey['questions'][0]['title'], title)
            # A return filter must not silently add the new question to another destination.
            selected_bank = self.flow.api(owner, 'GET', f'/api/v1/library/banks/{bank_id}', {}).get_json()
            self.assertEqual(len(selected_bank['question_ids']), 15)
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.assertFalse(errors, errors)
        finally:
            context.close()

    def test_catalog_creation_preserves_search_pages_and_bank_destination(self):
        owner = self.flow.app.test_client()
        name = 'catalog_create_' + uuid4().hex
        self.flow.login(owner, name)
        user = self.flow.db.users.find_one({'username': name})
        bank_id = self.flow.api(owner, 'POST', '/api/v1/library/banks', {'name': '校园出行题库'}).get_json()['id']
        for number in range(15):
            self.flow.api(owner, 'POST', '/api/v1/library/questions', {
                'type': 'text', 'title': f'校园出行建议 {number + 1}', 'required': False})
        for bank in ('', bank_id):
            with self.subTest(bank=bool(bank)):
                expected = {'q': ['校园出行'], 'mine_page': ['2'], 'shared_page': ['1']}
                if bank: expected['bank'] = [bank]
                path = '/question-library?' + urlencode({key: value[0] for key, value in expected.items()})
                context = self.browser.new_context(viewport={'width': 390, 'height': 844})
                cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id': str(user['_id'])})
                context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'], 'value': cookie, 'url': self.base}])
                page = context.new_page(); page.set_default_timeout(6000)
                try:
                    page.goto(self.base + path)
                    new_label = '＋ 新建并加入题库' if bank else '＋ 新建题目'
                    page.get_by_role('link', name=new_label, exact=True).click()
                    if bank:
                        self.assertTrue(page.get_by_role('link', name='← 返回题目列表', exact=True).is_visible())
                    page.locator('[data-discard-changes]').click()
                    self.assertEqual(parse_qs(urlsplit(page.url).query), expected)
                    self.assertEqual(page.locator('#library-query').input_value(), '校园出行')
                    page.get_by_role('link', name=new_label, exact=True).click()
                    page.locator('[data-question-type="text"]').click()
                    title = '校园出行还有哪些需要改善的地方？' + ('（题库）' if bank else '')
                    page.get_by_label('题目内容', exact=True).fill(title)
                    page.get_by_label('最少字数', exact=True).fill('8')
                    page.get_by_label('最多字数', exact=True).fill('2')
                    page.get_by_role('button', name='保存并加入题库' if bank else '保存到题目库', exact=True).click()
                    self.assertTrue(page.locator('[role="alert"]').is_visible())
                    self.assertEqual(page.get_by_label('题目内容', exact=True).input_value(), title)
                    page.get_by_label('最多字数', exact=True).fill('100')
                    page.get_by_role('button', name='保存并加入题库' if bank else '保存到题目库', exact=True).click()
                    page.wait_for_url(lambda u: urlsplit(str(u)).path == '/question-library')
                    self.assertEqual(parse_qs(urlsplit(page.url).query), expected)
                    saved = self.flow.db.question_library.find_one({'owner_id': user['_id'], 'title': title})
                    self.assertIsNotNone(saved)
                    from bson import ObjectId
                    actual = self.flow.db.question_banks.find_one({'_id': ObjectId(bank_id)})['question_ids']
                    self.assertEqual(saved['_id'] in actual, bool(bank))
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                finally: context.close()

    def test_selection_return_context_cannot_change_destination_permissions(self):
        owner = self.flow.app.test_client()
        other = self.flow.app.test_client()
        name = 'creation_owner_' + uuid4().hex
        self.flow.login(owner, name)
        self.flow.login(other, 'creation_other_' + uuid4().hex)
        survey = self.flow.survey(owner, name, '私人问卷')
        bank_id = self.flow.api(owner, 'POST', '/api/v1/library/banks',
                                {'name': '私人题库'}).get_json()['id']
        context = {'survey': str(survey['_id']), 'filter_bank': bank_id,
                   'q': '私人', 'mine_page': '2', 'shared_page': '1'}
        before = self.flow.db.question_library.count_documents({})
        self.assertEqual(other.get('/question-library/new?' + urlencode(context)).status_code, 404)
        denied = self.flow.post(other, '/question-library/new', {
            **context, 'type': 'text', 'title': '不得写入他人问卷'})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(self.flow.db.question_library.count_documents({}), before)
        self.assertEqual(owner.get('/question-library/new?' + urlencode({
            **context, 'bank': bank_id})).status_code, 400)
        self.assertEqual(self.flow.db.surveys.find_one({'_id': survey['_id']})['questions'], [])


if __name__ == '__main__':
    unittest.main()
