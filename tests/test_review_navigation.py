"""Exercise list/detail round trips and stale creation forms without production writes."""
import sys
import threading
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class ReviewNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library_tests.LibraryFlowTests.setUpClass()
        cls.flow = library_tests.LibraryFlowTests()
        cls.server = make_server('127.0.0.1', 5069, cls.flow.app, threaded=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='msedge' if sys.platform == 'win32' else None, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        library_tests.LibraryFlowTests.tearDownClass()

    def setUp(self):
        self.client = self.flow.app.test_client()
        self.name = 'review_' + uuid4().hex
        self.flow.login(self.client, self.name)
        self.user = self.flow.db.users.find_one({'username': self.name})
        self.survey = self.flow.survey(self.client, self.name, '分页返回体验')
        self.sid = self.survey['_id']

    def test_mobile_bank_filter_and_statistics_details_do_not_require_horizontal_scrolling(self):
        from urllib.parse import parse_qs, urlsplit
        item = self.flow.api(self.client, 'POST', '/api/v1/library/questions',
                             {'type':'text', 'title':'移动筛选体验题'}).get_json()['question']
        for number in range(7):
            bank = self.flow.api(self.client, 'POST', '/api/v1/library/banks',
                                 {'name': f'常用题库{number}'}).get_json()
        self.flow.api(self.client, 'POST', f"/api/v1/library/banks/{bank['id']}/questions", {'question_id':item['id']})
        self.flow.api(self.client, 'POST', f'/api/v1/surveys/{self.sid}/library-questions', {'question_id':item['id']})
        context = self.browser.new_context(viewport={'width':390,'height':844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id':str(self.user['_id'])})
        context.add_cookies([{'name':self.flow.app.config['SESSION_COOKIE_NAME'], 'value':cookie, 'url':'http://127.0.0.1:5069'}])
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:5069/surveys/{self.sid}/question-library?q=移动')
            self.assertTrue(page.locator('#choose-bank').is_visible())
            page.locator('#choose-bank').select_option(bank['id'])
            page.locator('.library-mobile-filter button').click()
            args = parse_qs(urlsplit(page.url).query)
            self.assertEqual(args['bank'], [bank['id']])
            self.assertEqual(args['q'], ['移动'])
            self.assertIn('移动筛选体验题', page.locator('.library-choose-grid').first.inner_text())
            page.goto(f'http://127.0.0.1:5069/surveys/{self.sid}/statistics?view=table')
            title = page.locator('.statistics-summary-table tbody th')
            self.assertEqual(title.inner_text(), '移动筛选体验题')
            self.assertGreater(title.bounding_box()['width'], 240)
            link = page.locator('.statistics-summary-table tbody a').first
            box = link.bounding_box()
            self.assertGreaterEqual(box['x'], 0)
            self.assertLessEqual(box['x']+box['width'], 390)
            link.click()
            self.assertIn('/statistics/', page.url)
            self.assertEqual(page.locator('h1').count(), 1)
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            self.assertFalse(errors, errors)
        finally:
            context.close()

    def test_blocked_clipboard_shows_selected_complete_link_without_false_success(self):
        self.flow.db.surveys.update_one({'_id':self.sid},{'$set':{
            'status':'published','questions':[{'id':'copy','title':'分享体验',
                'type':'text','version':1,'required':True,'active':True}]}})
        context=self.browser.new_context(viewport={'width':390,'height':844})
        cookie=self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id':str(self.user['_id'])})
        context.add_cookies([{'name':self.flow.app.config['SESSION_COOKIE_NAME'],'value':cookie,
                             'url':'http://127.0.0.1:5069'}])
        context.add_init_script("""Object.defineProperty(navigator, 'clipboard', {value:{
            writeText:()=>Promise.reject(new Error('Clipboard permission denied'))}});""")
        page=context.new_page()
        original_base=self.flow.app.config['PUBLIC_BASE_URL']
        try:
            self.flow.app.config['PUBLIC_BASE_URL']=''
            page.goto('http://127.0.0.1:5069/surveys')
            self.assertEqual(page.locator('[data-copy-url]').count(),0)
            self.assertTrue(page.get_by_role('link',name='在本机填写',exact=True).is_visible())
            self.flow.app.config['PUBLIC_BASE_URL']='https://surveys.example.com'
            for path in ('/surveys',f'/surveys/{self.sid}'):
                page.goto('http://127.0.0.1:5069'+path)
                button=page.locator('[data-copy-url]').first
                expected=button.get_attribute('data-copy-url')
                self.assertTrue(expected.startswith('https://surveys.example.com/survey/'))
                button.click()
                fallback=page.get_by_role('textbox',name='填写链接，供手动复制')
                fallback.wait_for()
                self.assertEqual(fallback.input_value(),expected)
                self.assertTrue(fallback.evaluate('(e)=>e.readOnly && e.selectionStart===0 && e.selectionEnd===e.value.length && document.activeElement===e'))
                self.assertNotIn('链接已复制',page.locator('[data-copy-feedback]').first.inner_text())
                button.click()
                self.assertEqual(fallback.count(),1)
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                if path=='/surveys':
                    from pathlib import Path
                    page.screenshot(path=str(Path(__file__).resolve().parents[1]/'output/第二阶段_页面检查/77_手机手动复制链接.png'),full_page=True)
        finally:
            self.flow.app.config['PUBLIC_BASE_URL']=original_base
            context.close()

    def test_statistics_detail_returns_to_filtered_overview_page(self):
        questions=[{'id':str(i),'title':f'具体建议{i}','type':'text','version':1,'required':True,'active':True}
                   for i in range(1,27)]
        self.flow.db.surveys.update_one({'_id':self.sid},{'$set':{'questions':questions}})
        self.flow.db.responses.insert_one({'survey_id':self.sid,'respondent_id':self.user['_id'],
            'submitted_at':datetime.now(timezone.utc),
            'answers':[{'question_id':q['id'],'question_version':1,'value':'具体回答'} for q in questions]})
        context=self.browser.new_context(viewport={'width':390,'height':844})
        cookie=self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id':str(self.user['_id'])})
        context.add_cookies([{'name':self.flow.app.config['SESSION_COOKIE_NAME'],'value':cookie,'url':'http://127.0.0.1:5069'}])
        page=context.new_page()
        try:
            from urllib.parse import urlsplit,parse_qs
            page.goto(f'http://127.0.0.1:5069/surveys/{self.sid}/statistics?view=cards&q=具体&page=2')
            page.locator('.stat-detail-link').first.click()
            page.get_by_role('link',name='← 返回整卷统计',exact=True).click()
            args=parse_qs(urlsplit(page.url).query)
            self.assertEqual(args.get('page'),['2'])
            self.assertEqual(args.get('view'),['cards'])
            self.assertEqual(args.get('q'),['具体'])
            page.get_by_role('link',name='查看账号答卷 →',exact=True).click()
            page.get_by_role('link',name='查看答卷 →',exact=True).click()
            page.locator('.back-link').first.click()
            page.get_by_role('link',name='← 返回统计结果',exact=True).click()
            args=parse_qs(urlsplit(page.url).query)
            self.assertEqual(args.get('page'),['2'])
            self.assertEqual(args.get('q'),['具体'])
            self.assertEqual(args.get('view'),['cards'])
        finally:
            context.close()

    def test_long_text_statistics_filter_uses_short_link_and_survives_review_return(self):
        question={'id':'long_text','title':'详细建议','type':'text','version':1,'required':True,'active':True}
        text='希望增加实践活动并提供详细讲解。'*200
        self.flow.db.surveys.update_one({'_id':self.sid},{'$set':{'questions':[question]}})
        self.flow.db.responses.insert_many([{'survey_id':self.sid,'survey_title':self.survey['title'],
            'respondent_id':self.user['_id'] if i<21 else None,'submitted_at':datetime.now(timezone.utc),
            'answers':[{'question_id':'long_text','question_version':1,'value':text}]} for i in range(22)])
        context=self.browser.new_context(viewport={'width':390,'height':844})
        cookie=self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps({'user_id':str(self.user['_id'])})
        context.add_cookies([{'name':self.flow.app.config['SESSION_COOKIE_NAME'],'value':cookie,'url':'http://127.0.0.1:5069'}])
        page=context.new_page()
        try:
            from urllib.parse import urlsplit,parse_qs
            page.goto(f'http://127.0.0.1:5069/surveys/{self.sid}/statistics/long_text')
            link=page.locator('.stat-answer-preview a').first
            self.assertLess(len(link.get_attribute('href')),400)
            link.click()
            self.assertIn('21 份',page.locator('.statistics-range').inner_text())
            page.get_by_role('link',name='下一页 →',exact=True).click()
            page.get_by_role('link',name='查看答卷 →',exact=True).click()
            self.assertEqual(page.locator('.saved-answer').inner_text(),text)
            page.locator('.back-link').first.click()
            args=parse_qs(urlsplit(page.url).query)
            self.assertEqual(args['page'],['2'])
            self.assertEqual(len(args['answer_key'][0]),64)
            self.assertIn('21 份',page.locator('.statistics-range').inner_text())
            self.assertLess(len(page.url),500)
        finally:
            context.close()

    def browser_round_trip(self, owner, origin=False):
        questions = [{'id': str(i), 'title': f'第{i}项具体建议', 'type': 'text',
                      'version': 1, 'active': True, 'required': True} for i in range(1, 22)]
        for question in (questions[0], questions[-1]):
            question.update(type='single_choice', options=['室内', '户外'])
        self.flow.db.surveys.update_one({'_id': self.sid}, {'$set': {'questions': questions}})
        self.flow.db.responses.insert_many([
            {'survey_id': self.sid, 'survey_title': self.survey['title'],
             'respondent_id': self.user['_id'], 'submitted_at': datetime.now(timezone.utc),
             'answers': [{'question_id': q['id'], 'question_version': 1, 'value': '室内' if q['type'] == 'single_choice' else '具体回答'}
                         for q in questions]} for _ in range(21)])
        context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(self.user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                              'value': cookie, 'url': 'http://127.0.0.1:5069'}])
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        query = self.name if owner else self.survey['title']
        list_path = f'/surveys/{self.sid}/responses' if owner else '/responses'
        try:
            from urllib.parse import urlencode, urlsplit, parse_qs
            params = {'page': 2, 'q': query}
            if origin:
                list_path = f'/surveys/{self.sid}/statistics/1/accounts'
                params = {'page': 2, 'version': 1, 'option': 0}
            if not owner:
                from app.surveys import CHINA_TIME
                params['date'] = datetime.now(CHINA_TIME).date().isoformat()
            page.goto('http://127.0.0.1:5069' + list_path + '?' + urlencode(params))
            page.get_by_role('link', name='查看答卷 →', exact=True).click()
            self.assertIn('分页返回体验', page.title())
            page.get_by_role('button', name='显示全部选项', exact=True).click()
            page.get_by_role('link', name='下一页 →', exact=True).click()
            page.locator('.review-question-title').filter(has_text='第21项具体建议').wait_for()
            self.assertNotIn('is-compact', page.locator('[data-answer-review]').get_attribute('class'))
            self.assertTrue(page.locator('.review-option').filter(has_text='户外').is_visible())
            # The question-page jump form must preserve the independent list page too.
            page.get_by_role('spinbutton', name='答卷页码').fill('1')
            page.get_by_role('button', name='跳转', exact=True).click()
            page.locator('.review-question-title').filter(has_text='第1项具体建议').wait_for()
            self.assertNotIn('is-compact', page.locator('[data-answer-review]').get_attribute('class'))
            page.locator('[data-review-toggle]').click()
            page.get_by_role('link', name='下一页 →', exact=True).click()
            self.assertIn('is-compact', page.locator('[data-answer-review]').get_attribute('class'))
            self.assertFalse(page.locator('.review-option').filter(has_text='户外').is_visible())
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.get_by_role('link', name='← 返回本题账号回答' if origin else '← 返回账号答卷' if owner else '← 返回我的答卷', exact=True).click()
            page.wait_for_url('**' + list_path + '?**')
            actual = parse_qs(urlsplit(page.url).query)
            for key, value in params.items():
                self.assertEqual(actual[key], [str(value)])
            self.assertEqual(page.get_by_role('link', name='查看答卷 →', exact=True).count(), 1)
            if owner:
                row = page.locator('.account-responses-table tbody tr').first
                entry = row.get_by_role('link', name='查看答卷 →', exact=True)
                box = entry.bounding_box()
                self.assertGreaterEqual(box['x'], 0)
                self.assertLessEqual(box['x'] + box['width'], 390)
            self.assertFalse(errors)
        finally:
            context.close()

    def test_personal_filtered_second_page_survives_detail_and_question_paging(self):
        self.browser_round_trip(False)

    def test_owner_filtered_second_page_survives_detail_and_question_paging(self):
        self.browser_round_trip(True)

    def test_option_filtered_accounts_return_to_the_same_question_option_and_page(self):
        self.browser_round_trip(True, origin=True)

    def test_new_question_save_collision_keeps_input_and_the_other_pages_changes(self):
        from unittest.mock import patch
        from app import surveys
        original_save = surveys.save_questions
        data = {'type': 'text', 'title': '这一页尚未保存的新题目', 'required': 'on'}
        other_question = {'id': 'concurrent', 'title': '另一页保存的题目', 'type': 'text',
                          'active': True, 'version': 1, 'required': True}

        def concurrent_save(survey, questions, **kwargs):
            self.flow.db.surveys.update_one({'_id': self.sid}, {
                '$set': {'questions': [other_question]}, '$inc': {'revision': 1}})
            return original_save(survey, questions, **kwargs)

        with patch('app.surveys.save_questions', side_effect=concurrent_save):
            result = self.flow.post(self.client, f'/surveys/{self.sid}/questions/new', data)
        self.assertEqual(result.status_code, 409)
        html = result.get_data(as_text=True)
        self.assertIn(data['title'], html)
        self.assertIn('查看最新问卷', html)
        self.assertEqual(self.flow.db.surveys.find_one({'_id': self.sid})['questions'], [other_question])
        retried = self.flow.post(self.client, f'/surveys/{self.sid}/questions/new', data)
        self.assertEqual(retried.status_code, 302)
        saved = self.flow.db.surveys.find_one({'_id': self.sid})
        self.assertEqual([q['title'] for q in saved['questions']], [other_question['title'], data['title']])

        # Follow the settings conflict and retry using the actual visible form.
        from pymongo.collection import Collection
        original_update = Collection.update_one
        pending_title = '这次保存的标题需要保留'
        context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(self.user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                             'value': cookie, 'url': 'http://127.0.0.1:5069'}])
        page = context.new_page()
        page.set_default_timeout(6000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def change_during_save(collection, query, update, *args, **kwargs):
            if (collection.name == 'surveys' and query.get('_id') == self.sid
                    and update.get('$set', {}).get('title') == pending_title):
                original_update(collection, {'_id': self.sid}, {
                    '$set': {'description': '另一页面刚保存的说明'}, '$inc': {'revision': 1}})
            return original_update(collection, query, update, *args, **kwargs)

        try:
            page.goto(f'http://127.0.0.1:5069/surveys/{self.sid}/edit')
            page.locator('#title').fill(pending_title)
            page.locator('#description').fill('本页写好的说明不能丢')
            page.get_by_text('只能一次', exact=True).click()
            page.get_by_text('A、B、C、D', exact=True).click()
            with patch.object(Collection, 'update_one', change_during_save):
                with page.expect_response(lambda response: response.request.method == 'POST'
                                          and response.url.endswith(f'/surveys/{self.sid}/edit')) as rejected:
                    page.get_by_role('button', name='保存修改', exact=True).click()
                self.assertEqual(rejected.value.status, 409)
            self.assertEqual(page.locator('#title').input_value(), pending_title)
            self.assertEqual(page.locator('#description').input_value(), '本页写好的说明不能丢')
            self.assertTrue(page.locator('input[name="repeat_policy"][value="once"]').is_checked())
            self.assertTrue(page.locator('input[name="option_label_style"][value="letters"]').is_checked())
            self.assertIn('你的输入已保留', page.get_by_role('alert').inner_text())
            current = self.flow.db.surveys.find_one({'_id': self.sid})
            self.assertEqual(current['description'], '另一页面刚保存的说明')
            self.assertEqual(page.locator('input[name="revision"]').input_value(), str(current['revision']))
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
            page.get_by_role('button', name='保存修改', exact=True).click()
            page.wait_for_url(f'**/surveys/{self.sid}')
            retried = self.flow.db.surveys.find_one({'_id': self.sid})
            self.assertEqual(retried['title'], pending_title)
            self.assertEqual(retried['description'], '本页写好的说明不能丢')
            self.assertEqual(retried['questions'], saved['questions'])
            self.assertFalse(errors)
        finally:
            context.close()

    def test_stale_new_question_forms_preserve_input_without_saving_or_changing_survey(self):
        self.flow.db.surveys.update_one({'_id': self.sid}, {'$set': {'status': 'published'}})
        data = {'title': '刚写好的问题不能丢失', 'type': 'text', 'required': 'on', 'min_length': '3'}
        count = self.flow.db.question_library.count_documents({})
        for path, destination in [(f'/surveys/{self.sid}/questions/new', {}),
                                  ('/question-library/new', {'survey': str(self.sid)}),
                                  ('/question-library/new', {'bank': '000000000000000000000001'})]:
            with self.subTest(path=path, destination=destination):
                result = self.flow.post(self.client, path, {**data, **destination})
                self.assertEqual(result.status_code, 409)
                html = result.get_data(as_text=True)
                self.assertIn('刚写好的问题不能丢失', html)
                self.assertIn('尚未保存', html)
                self.assertIn('action="/question-library/new"', html)
                self.assertNotIn('name="survey"', html)
                self.assertNotIn('name="bank"', html)
                self.assertEqual(self.client.post(path, data={**data, **destination}).status_code, 400)
        self.assertEqual(self.flow.db.question_library.count_documents({}), count)
        self.assertEqual(self.flow.db.surveys.find_one({'_id': self.sid})['questions'], [])
        saved = self.flow.post(self.client, '/question-library/new', data)
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(self.flow.db.question_library.count_documents({}), count + 1)
