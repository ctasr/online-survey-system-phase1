"""Creator operations retain the relevant item in long question and survey lists."""
import sys
import threading
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class CreatorNavigationTests(unittest.TestCase):
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

    def scenario(self, width):
        owner = self.flow.app.test_client()
        username = 'creator_' + uuid4().hex
        self.flow.login(owner, username)
        user = self.flow.db.users.find_one({'username': username})
        survey = self.flow.survey(owner, username, '图书馆空间使用调查')
        questions = [{'id': f'q{i}', 'title': f'空间使用意见 {i}', 'type': 'text',
                      'version': 1, 'required': True, 'active': True} for i in range(1, 25)]
        questions[21].update(type='single_choice', options=['安静阅览', '小组讨论'], jumps=[
            {'id': 'r1', 'kind': 'single_choice', 'option': '安静阅览', 'target_id': 'q23'},
            {'id': 'r2', 'kind': 'single_choice', 'option': '小组讨论', 'target_id': 'q24'}])
        self.flow.db.surveys.update_one({'_id': survey['_id']}, {'$set': {'questions': questions}})
        context = self.browser.new_context(viewport={'width': width, 'height': 900})
        cookie = self.flow.app.session_interface.get_signing_serializer(self.flow.app).dumps(
            {'user_id': str(user['_id'])})
        context.add_cookies([{'name': self.flow.app.config['SESSION_COOKIE_NAME'],
                             'value': cookie, 'url': self.base}])
        page = context.new_page()
        page.set_default_timeout(6000)
        return owner, user, survey, context, page

    def assert_question(self, page, question_id, notice=None):
        page.wait_for_url(f'**#question-{question_id}')
        card = page.locator(f'#question-{question_id}')
        self.assertTrue(card.is_visible())
        self.assertGreaterEqual(card.get_by_role('button', name='删除题目', exact=True).bounding_box()['height'],
                                44 if page.viewport_size['width'] < 650 else 40)
        self.assertIn('第 2 /', page.locator('[data-question-page-status]').inner_text())
        self.assertEqual(page.evaluate('document.activeElement.id'), card.get_attribute('id'))
        if notice:
            self.assertIn(notice, card.locator('.notice').inner_text())
            self.assertTrue(card.locator('.notice').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight}'))
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        return card

    def test_long_question_edit_add_delete_and_jump_actions_return_to_relevant_card(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                owner, user, survey, context, page = self.scenario(width)
                detail = f"/surveys/{survey['_id']}"
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                try:
                    page.goto(self.base + detail + '/questions/q21/edit')
                    page.get_by_role('link', name='取消', exact=True).click()
                    self.assert_question(page, 'q21')
                    for changed in (True, False):
                        page.goto(self.base + detail + '/questions/q21/edit')
                        if changed:
                            page.locator('[name=title]').fill('你希望增加哪些学习空间？')
                        page.get_by_role('button', name='保存修改', exact=True).click()
                        self.assert_question(page, 'q21', '题目已更新' if changed else '题目内容没有变化')
                    page.goto(self.base + detail + '/questions/new')
                    page.locator('[data-question-type=text]').click()
                    page.locator('[name=title]').fill('还有哪些空间使用建议？')
                    page.get_by_role('button', name='保存题目', exact=True).click()
                    current = self.flow.db.surveys.find_one({'_id': survey['_id']})
                    new_id = current['questions'][-1]['id']
                    card = self.assert_question(page, new_id, '题目已添加')
                    card.get_by_role('button', name='删除题目', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    self.assert_question(page, 'q24', '题目已从后续填写中移除')
                    page.goto(self.base + detail + '#question-q21')
                    page.locator('#question-q21').get_by_role('button', name='删除题目', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    card = self.assert_question(page, 'q22', '题目已从后续填写中移除')
                    card.locator('.question-settings > summary').click()
                    card.get_by_role('button', name='下移', exact=True).click()
                    card = self.assert_question(page, 'q22', '跳转优先级已调整')
                    self.assertIsNotNone(card.locator('.question-settings').get_attribute('open'))
                    self.assertIn('小组讨论', card.locator('.jump-list > li').first.inner_text())
                    card.locator('.jump-list > li').first.get_by_role('button', name='删除', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    card = self.assert_question(page, 'q22', '跳转规则已删除')
                    self.assertEqual(card.locator('.jump-list > li').count(), 1)
                    # A publish at the final write must keep the actual browser input,
                    # then allow an explicit library save without changing the survey.
                    from app import surveys
                    original_save = surveys.save_questions
                    pending_title = '你最希望使用哪类学习空间？'
                    page.goto(self.base + detail + '/questions/q22/edit')
                    page.locator('[name=title]').fill(pending_title)
                    def publish_during_save(document, questions, history=None, **kwargs):
                        self.flow.db.surveys.update_one({'_id': survey['_id']},
                            {'$set': {'status': 'published'}, '$inc': {'revision': 1}})
                        return original_save(document, questions, history, **kwargs)
                    with patch('app.surveys.save_questions', side_effect=publish_during_save):
                        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/questions/q22/edit')) as rejected:
                            page.get_by_role('button', name='保存修改', exact=True).click()
                        self.assertEqual(rejected.value.status, 409)
                    self.assertEqual(page.locator('[name=title]').input_value(), pending_title)
                    self.assertEqual(page.locator('[data-options-rows] input').nth(1).input_value(), '小组讨论')
                    self.assertIn('输入已保留', page.get_by_role('alert').inner_text())
                    self.assertEqual(self.flow.db.question_library.count_documents({'owner_id': user['_id']}), 0)
                    page.get_by_role('button', name='保存到题目库', exact=True).click()
                    page.wait_for_url('**/question-library/*')
                    self.assertEqual(page.locator('h1').inner_text(), pending_title)
                    self.assertEqual(self.flow.db.question_library.count_documents({'owner_id': user['_id']}), 1)
                    page.get_by_role('link', name='← 返回原问卷', exact=True).click()
                    page.wait_for_url('**#question-q22')
                    card = page.locator('#question-q22')
                    self.assertTrue(card.is_visible())
                    self.assertIn('第 2 /', page.locator('[data-question-page-status]').inner_text())
                    self.assertEqual(card.get_by_role('link', name='编辑题目', exact=True).count(), 0)
                    self.assertEqual(card.locator('.question-list-heading > strong').inner_text(), '空间使用意见 22')
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_deleting_from_filtered_survey_page_keeps_filters_and_clamps_empty_last_page(self):
        for width in (390, 1280):
            with self.subTest(width=width):
                owner, user, survey, context, page = self.scenario(width)
                now = datetime.now(timezone.utc)
                self.flow.db.surveys.insert_many([
                    {**{key: value for key, value in survey.items() if key != '_id'},
                     'title': f'图书馆空间预约 {i:02d}', 'slug': uuid4().hex, 'created_at': now}
                    for i in range(20)])
                self.flow.db.responses.insert_one({'survey_id': survey['_id'], 'respondent_id': user['_id'],
                    'submitted_at': now, 'answers': [{'question_id': 'q1', 'question_version': 1, 'value': '增加安静阅览区'}]})
                try:
                    page.goto(self.base + '/surveys?q=图书馆空间&status=草稿&page=2')
                    self.assertEqual(page.locator('[data-survey-item]').count(), 1)
                    removed_title = page.locator('.survey-list-main > a').inner_text()
                    page.get_by_role('link', name='管理问卷', exact=True).click()
                    # Different tabs carry their own explicit return context.
                    other_tab = context.new_page()
                    other_tab.goto(self.base + f"/surveys/{survey['_id']}?survey_list_q=空间&survey_list_status=草稿&survey_list_page=1")
                    other_back = parse_qs(urlsplit(other_tab.get_by_role('link', name='← 返回我的问卷', exact=True).get_attribute('href')).query)
                    self.assertEqual(other_back['q'], ['空间'])
                    other_tab.close()
                    page.get_by_role('link', name='编辑问卷信息', exact=True).click()
                    page.get_by_role('link', name='取消', exact=True).click()
                    page.get_by_role('link', name='查看统计', exact=True).click()
                    page.locator('#statistics-query').fill('空间使用')
                    page.get_by_role('button', name='查找', exact=True).click()
                    page.locator('.statistics-table a').first.click()
                    page.get_by_role('link', name='查看本题的账号回答 →', exact=True).click()
                    page.get_by_role('link', name='查看答卷 →', exact=True).click()
                    page.get_by_role('link', name='← 本题账号回答', exact=True).click()
                    page.get_by_role('link', name='← 返回单题统计', exact=True).click()
                    page.get_by_role('link', name='← 返回整卷统计', exact=True).click()
                    page.get_by_role('link', name='← 返回问卷', exact=True).click()
                    page.get_by_role('link', name='← 返回我的问卷', exact=True).click()
                    returned = parse_qs(urlsplit(page.url).query)
                    self.assertEqual(returned['q'], ['图书馆空间'])
                    self.assertEqual(returned['status'], ['草稿'])
                    self.assertEqual(returned['page'], ['2'])
                    page.get_by_role('button', name='删除', exact=True).click()
                    page.locator('[data-confirm-cancel]').click()
                    self.assertEqual(page.locator('[data-survey-item]').count(), 1)
                    page.get_by_role('button', name='删除', exact=True).click()
                    page.locator('[data-confirm-proceed]').click()
                    page.wait_for_load_state('load')
                    query = parse_qs(urlsplit(page.url).query)
                    self.assertEqual(query['q'], ['图书馆空间'])
                    self.assertEqual(query['status'], ['草稿'])
                    self.assertEqual(page.locator('.survey-filters [name=q]').input_value(), '图书馆空间')
                    self.assertEqual(page.locator('.survey-filters [name=status]').input_value(), '草稿')
                    self.assertEqual(page.locator('[data-survey-item]').count(), 20)
                    self.assertNotIn(removed_title, page.locator('.survey-list').inner_text())
                    page.get_by_role('link', name='清除筛选', exact=True).click()
                    self.assertEqual(urlsplit(page.url).query, '')
                    self.assertEqual(page.locator('.survey-filters [name=q]').input_value(), '')
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                finally:
                    context.close()

    def test_edit_final_write_conflicts_preserve_input_for_all_edit_branches(self):
        from app import surveys
        original_save = surveys.save_questions
        owner = self.flow.app.test_client()
        username = 'edit_collision_' + uuid4().hex
        self.flow.login(owner, username)
        item = self.flow.survey(owner, username, '自习室使用调查')
        initial = {'id': 'hours', 'title': '你每周自习多少小时？', 'type': 'number',
                   'version': 1, 'active': True, 'required': True, 'min_value': None,
                   'max_value': None, 'integer_only': False, 'number_statistics': 'distribution', 'jumps': []}
        for branch in ('content', 'option_style', 'number_statistics'):
            for state in ('draft', 'published', 'closed', 'deleted', 'removed'):
                with self.subTest(branch=branch, state=state):
                    self.flow.db.surveys.replace_one({'_id': item['_id']}, {
                        **item, 'questions': [initial], 'question_history': [], 'revision': 1}, upsert=True)
                    data = {'type': 'number', 'title': initial['title'], 'required': 'on',
                            'number_statistics': 'distribution', 'revision': '1'}
                    if branch == 'content': data['title'] = '你希望每周安排多少自习时间？'
                    elif branch == 'option_style': data['option_label_style'] = 'letters'
                    else: data['number_statistics'] = 'measure'

                    def concurrent_save(survey, questions, history=None, **kwargs):
                        if state == 'deleted': self.flow.db.surveys.delete_one({'_id': item['_id']})
                        else:
                            changes = {'description': '另一页已经保存的说明'}
                            if state == 'removed': changes['questions.0.active'] = False
                            else: changes['status'] = state
                            self.flow.db.surveys.update_one({'_id': item['_id']},
                                {'$set': changes, '$inc': {'revision': 1}})
                        return original_save(survey, questions, history, **kwargs)

                    with patch('app.surveys.save_questions', side_effect=concurrent_save):
                        result = self.flow.post(owner, f"/surveys/{item['_id']}/questions/hours/edit", data)
                    self.assertEqual(result.status_code, 409)
                    html = result.get_data(as_text=True)
                    self.assertIn(data['title'], html)
                    self.assertIn('输入已保留', html)
                    self.assertIn('data-question-form', html)
                    saved = self.flow.db.surveys.find_one({'_id': item['_id']})
                    if saved:
                        self.assertEqual(saved['questions'][0]['title'], initial['title'])
                        self.assertEqual(saved['description'], '另一页已经保存的说明')
                        self.assertEqual(saved['questions'][0]['number_statistics'], 'distribution')
                    if state == 'draft':
                        self.assertIn('查看最新问卷', html)
                        self.assertIn('name="revision" value="2"', html)
                        self.assertEqual(self.flow.post(owner, f"/surveys/{item['_id']}/questions/hours/edit",
                            {**data, 'revision': '2'}).status_code, 302)
                    else:
                        self.assertIn('另存到题目库', html)
                    self.assertEqual(self.flow.db.question_library.count_documents({}), 0)
