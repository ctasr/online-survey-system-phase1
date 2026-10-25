"""Actual headless browser checks for keeping unfinished answers safe."""
import threading
import unittest
import sys
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from tests import test_stage2_library as library_tests


class DraftExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library_tests.LibraryFlowTests.setUpClass()
        cls.flow = library_tests.LibraryFlowTests()
        cls.server = make_server('127.0.0.1', 5067, cls.flow.app, threaded=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='msedge' if sys.platform == 'win32' else None, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        library_tests.LibraryFlowTests.tearDownClass()

    def setUp(self):
        self.owner, self.visitor = self.flow.app.test_client(), self.flow.app.test_client()
        self.owner_name, self.visitor_name = 'draft_owner_' + uuid4().hex, 'draft_visitor_' + uuid4().hex
        self.flow.login(self.owner, self.owner_name)
        self.flow.login(self.visitor, self.visitor_name)
        survey = self.flow.survey(self.owner, self.owner_name, '暂存与退出体验检查')
        self.survey_id = survey['_id']
        self.slug = survey['slug']
        self.question_ids = []
        for title, required in [('你对活动有什么期待？', True), ('还有哪些补充建议？', False)]:
            result = self.flow.api(self.owner, 'POST', f'/api/v1/surveys/{self.survey_id}/questions', {
                'type': 'text', 'title': title, 'required': required})
            self.assertEqual(result.status_code, 201)
            self.question_ids.append(result.get_json()['question']['id'])
        self.assertEqual(self.flow.post(self.owner, f'/surveys/{self.survey_id}/publish', {}).status_code, 302)
        self.user_id = self.flow.db.users.find_one({'username': self.visitor_name})['_id']
        self.context = self.browser.new_context(viewport={'width': 390, 'height': 844})
        self.page = self.context.new_page()
        self.page.set_default_timeout(6000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto('http://127.0.0.1:5067/login')
        self.page.locator('#username').fill(self.visitor_name)
        self.page.locator('#password').fill('123456')
        self.page.get_by_role('button', name='登录', exact=True).click()
        self.page.wait_for_url('**/dashboard')
        self.public_url = f'http://127.0.0.1:5067/survey/{self.slug}'
        self.draft_path = f'/survey/{self.slug}/draft'

    def tearDown(self):
        self.context.close()
        self.assertFalse(self.errors, self.errors)

    def draft(self):
        return self.flow.db.response_drafts.find_one({'survey_id': self.survey_id, 'user_id': self.user_id})

    def field(self, index):
        return self.page.locator(f'[name="q_{self.question_ids[index]}"]')

    def wait_saved(self):
        self.page.locator('[data-draft-state]').filter(has_text='答案已暂存，可稍后继续填写').wait_for()

    def capture(self, filename):
        output = Path(__file__).resolve().parents[1] / 'output' / '第二阶段_页面检查'
        self.page.screenshot(path=str(output / filename), full_page=True)

    def test_typing_during_slow_restore_preserves_new_input_and_restores_other_answers(self):
        saved = {'answers': {'q_' + self.question_ids[0]: '上次输入的期待',
                             'q_' + self.question_ids[1]: '上次暂存的补充建议'}, 'identity_mode': 'anonymous'}
        self.assertEqual(self.flow.api(self.visitor, 'POST', self.draft_path, saved).status_code, 200)
        pending = []

        def hold_restore(route):
            if route.request.method == 'GET':
                pending.append(route)
            else:
                route.continue_()

        self.page.route('**' + self.draft_path, hold_restore)
        self.page.goto(self.public_url)
        self.field(0).fill('这次刚输入的新期待')
        self.page.locator('#identity-mode').select_option('named')
        self.assertEqual(len(pending), 1)
        self.page.locator('[data-flow-submit]').click()
        self.page.wait_for_timeout(300)  # Submission must wait for the deliberately held restore.
        self.assertTrue(self.page.locator('[data-flow-submit]').is_disabled())
        self.assertFalse(self.page.locator('[data-confirm-dialog]').is_visible())
        pending[0].fulfill(json=saved)
        self.page.wait_for_function('(name) => document.querySelector(`[name="${name}"]`).value === "上次暂存的补充建议"',
                                    arg='q_' + self.question_ids[1])
        self.assertEqual(self.field(0).input_value(), '这次刚输入的新期待')
        self.assertEqual(self.page.locator('#identity-mode').input_value(), 'named')
        self.page.locator('[data-confirm-title]').filter(has_text='提交答卷？').wait_for()
        self.page.locator('[data-confirm-cancel]').click()
        self.wait_saved()
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], '这次刚输入的新期待')
        self.assertEqual(self.draft()['identity_mode'], 'named')
        self.capture('35_慢速恢复保留新输入及填写身份.png')

    def test_failed_exit_save_keeps_page_and_answers_until_retry_succeeds(self):
        self.page.goto(self.public_url)
        self.page.wait_for_timeout(100)  # Let the initial local draft GET settle before testing exit/submit.

        def reject_saving(route):
            if route.request.method == 'POST':
                route.fulfill(status=503, json={'error': '模拟临时连接失败'})
            else:
                route.continue_()

        self.page.route('**' + self.draft_path, reject_saving)
        self.field(0).fill('退出前需要保留的内容')
        self.page.locator('.flow-controls [data-exit-response]').click()
        self.page.locator('[data-confirm-proceed]').click()
        self.page.locator('[data-flow-error]').filter(has_text='暂存失败，尚未退出').wait_for()
        self.assertEqual(self.page.url, self.public_url)
        self.assertEqual(self.field(0).input_value(), '退出前需要保留的内容')
        self.capture('36_暂存失败后保留填写页.png')
        self.page.unroute('**' + self.draft_path, reject_saving)
        self.page.locator('.flow-controls [data-exit-response]').click()
        self.page.locator('[data-confirm-proceed]').click()
        self.page.wait_for_url('**/dashboard')
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], '退出前需要保留的内容')
        self.assertEqual(self.flow.db.responses.count_documents({'survey_id': self.survey_id}), 0)

    def test_closing_during_filling_keeps_draft_and_exit_available_until_submit(self):
        self.page.goto(self.public_url)
        self.page.wait_for_function("document.querySelector('[data-draft-state]').textContent === ''")
        self.field(0).fill('希望延长晚间活动时间。')
        self.wait_saved()
        self.assertEqual(self.flow.post(self.owner, f'/surveys/{self.survey_id}/close', {}).status_code, 302)
        with self.page.expect_response(lambda r: r.url.endswith('/assess') and r.request.method == 'POST') as checked:
            self.field(1).fill('周五晚上七点之后比较方便。')
        self.assertEqual(checked.value.status, 200)
        self.wait_saved()
        self.assertEqual(self.page.locator('[data-flow-error]').inner_text(), '')
        self.page.locator('[data-exit-response]').first.click()
        self.page.locator('[data-confirm-proceed]').click()
        self.page.wait_for_url('**/dashboard')
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[1]], '周五晚上七点之后比较方便。')
        self.assertEqual(self.flow.db.responses.count_documents({'survey_id': self.survey_id}), 0)
        self.page.goto(self.public_url)
        self.assertIn('无法填写', self.page.locator('main').inner_text())
        self.assertEqual(self.page.locator('[data-survey-flow]').count(), 0)

        # A second already-open form must still be rejected at final submission.
        self.flow.db.surveys.update_one({'_id': self.survey_id}, {'$set': {'status': 'published'}})
        self.page.goto(self.public_url)
        self.page.locator('[data-draft-state]').filter(has_text='已恢复上次暂存的答案').wait_for()
        self.assertEqual(self.flow.post(self.owner, f'/surveys/{self.survey_id}/close', {}).status_code, 302)
        self.page.locator('[data-flow-submit]').click()
        self.page.locator('[data-confirm-title]').filter(has_text='提交答卷？').wait_for()
        with self.page.expect_navigation() as response:
            self.page.locator('[data-confirm-proceed]').click()
        self.assertEqual(response.value.status, 409)
        self.assertIn('无法填写', self.page.locator('main').inner_text())
        self.assertEqual(self.flow.db.responses.count_documents({'survey_id': self.survey_id}), 0)
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], '希望延长晚间活动时间。')

    def test_cancel_submit_confirmation_resumes_pending_autosave(self):
        self.page.goto(self.public_url)
        self.page.wait_for_timeout(100)  # Let the initial local draft GET settle before testing exit/submit.
        self.field(0).fill('取消提交后仍应保留的内容')
        self.page.locator('[data-flow-submit]').click()
        self.page.locator('[data-confirm-title]').filter(has_text='提交答卷？').wait_for()
        self.page.locator('[data-confirm-cancel]').click()
        self.wait_saved()
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], '取消提交后仍应保留的内容')
        self.assertEqual(self.flow.db.responses.count_documents({'survey_id': self.survey_id}), 0)
        self.page.reload()
        self.page.wait_for_function('(name) => document.querySelector(`[name="${name}"]`).value === "取消提交后仍应保留的内容"',
                                    arg='q_' + self.question_ids[0])
        self.capture('37_取消提交后仍可恢复暂存内容.png')

    def test_submit_waits_for_latest_queued_autosave_and_does_not_recreate_draft(self):
        pending=[]
        def hold_save(route):
            if route.request.method=='POST':
                pending.append(route)
            else:
                route.continue_()
        self.page.route('**'+self.draft_path,hold_save)
        self.page.goto(self.public_url)
        self.page.locator('[data-draft-state]').filter(has_text='正在恢复').wait_for(state='hidden')
        self.field(0).fill('第一轮输入')
        self.page.wait_for_timeout(1100)  # Exercise the real 900ms autosave debounce.
        self.assertEqual(len(pending),1)
        self.field(0).fill('准备正式提交的新内容')
        self.page.wait_for_timeout(1100)  # Queue a second save behind the deliberately held first one.
        self.assertEqual(len(pending),1)
        with self.page.expect_request(lambda r:r.method=='POST' and r.url.endswith(self.draft_path)):
            pending[0].fulfill(json={'saved':True})
        for _ in range(60):
            if len(pending)==2:
                break
            self.page.wait_for_timeout(50)
        self.assertEqual(len(pending),2)
        self.page.locator('[data-flow-submit]').click()
        self.page.locator('[data-confirm-title]').filter(has_text='提交答卷？').wait_for()
        self.page.locator('[data-confirm-proceed]').click()
        self.page.wait_for_timeout(300)  # A held save must prevent formal submission, not merely delay a screenshot.
        self.assertEqual(self.flow.db.responses.count_documents({'survey_id':self.survey_id}),0)
        self.assertEqual(self.page.url,self.public_url)
        pending[1].continue_()
        self.page.wait_for_url('**/submitted')
        response=self.flow.db.responses.find_one({'survey_id':self.survey_id})
        self.assertEqual(response['answers'][0]['value'],'准备正式提交的新内容')
        self.assertIsNone(self.draft())

    def test_clearing_last_answer_then_exiting_does_not_restore_the_old_answer(self):
        self.assertEqual(self.flow.api(self.visitor, 'POST', self.draft_path, {
            'answers': {'q_' + self.question_ids[0]: '这段旧内容准备删除'}, 'identity_mode': 'anonymous'}).status_code, 200)
        self.page.goto(self.public_url)
        self.page.wait_for_function('(name) => document.querySelector(`[name="${name}"]`).value === "这段旧内容准备删除"',
                                    arg='q_' + self.question_ids[0])
        self.field(0).fill('')
        self.page.locator('.flow-controls [data-exit-response]').click()
        self.page.locator('[data-confirm-proceed]').click()
        self.page.wait_for_url('**/dashboard')
        self.assertEqual(self.draft()['answers'].get('q_' + self.question_ids[0], ''), '')
        self.page.goto(self.public_url)
        self.page.locator('[data-draft-state]').filter(has_text='已恢复上次暂存的答案').wait_for()
        self.assertEqual(self.field(0).input_value(), '')
        self.assertEqual(self.flow.db.responses.count_documents({'survey_id': self.survey_id}), 0)

    def test_optional_radio_can_be_cleared_and_branch_and_draft_follow_the_change(self):
        survey = self.flow.survey(self.owner, self.owner_name, '选答单选清除与跳转检查')
        sid = survey['_id']
        ids = []
        for definition in [
            {'type': 'single_choice', 'title': '有兴趣参加哪类活动？', 'required': False, 'options': ['室内', '户外']},
            {'type': 'text', 'title': '活动建议', 'required': True},
            {'type': 'text', 'title': '补充期待', 'required': True},
        ]:
            result = self.flow.api(self.owner, 'POST', f'/api/v1/surveys/{sid}/questions', definition)
            self.assertEqual(result.status_code, 201)
            ids.append(result.get_json()['question']['id'])
        self.assertEqual(self.flow.post(self.owner, f'/surveys/{sid}/questions/{ids[0]}/jumps',
                                        {'option': '室内', 'target_id': ids[2]}).status_code, 302)
        self.assertEqual(self.flow.post(self.owner, f'/surveys/{sid}/publish', {}).status_code, 302)
        self.page.goto(f'http://127.0.0.1:5067/survey/{survey["slug"]}')
        first = self.page.locator(f'[data-question-id="{ids[0]}"]')
        clear = first.get_by_role('button', name='清除选择', exact=True)
        self.assertFalse(clear.is_visible())
        first.get_by_role('radio', name='室内').check()
        second = self.page.locator(f'[data-question-id="{ids[1]}"]')
        second.wait_for(state='hidden')
        clear.click()
        second.wait_for(state='visible')
        self.assertFalse(clear.is_visible())
        self.assertEqual(first.locator('input:checked').count(), 0)
        self.wait_saved()
        draft = self.flow.db.response_drafts.find_one({'survey_id': sid, 'user_id': self.user_id})
        self.assertNotIn('q_' + ids[0], draft['answers'])
        self.page.reload()
        self.page.locator('[data-draft-state]').filter(has_text='已恢复上次暂存的答案').wait_for()
        self.assertEqual(first.locator('input:checked').count(), 0)
        self.assertFalse(clear.is_visible())
        self.page.locator('[data-flow-submit]').click()
        second.locator('[data-question-error]').filter(has_text='你还未作答本题').wait_for()
        second.locator('textarea').fill('活动建议已经填写')
        self.assertEqual(second.locator('[data-question-error]').inner_text(), '')
        self.assertEqual(second.get_attribute('aria-invalid'), None)

    def test_failed_restore_never_overwrites_saved_answers_and_retry_merges_new_input(self):
        original = {'answers': {'q_' + self.question_ids[0]: '之前保存的期待',
                                'q_' + self.question_ids[1]: '之前保存的补充'}, 'identity_mode': 'anonymous'}
        for failure in ('http', 'connection'):
            with self.subTest(failure=failure):
                self.assertEqual(self.flow.api(self.visitor, 'POST', self.draft_path, original).status_code, 200)
                state = {'fail': True, 'posts': 0}

                def fail_restore(route):
                    if route.request.method == 'GET' and state['fail']:
                        if failure == 'http':
                            route.fulfill(status=503, json={'error': '模拟恢复失败'})
                        else:
                            route.abort('failed')
                    else:
                        if route.request.method == 'POST':
                            state['posts'] += 1
                        route.continue_()

                self.page.route('**' + self.draft_path, fail_restore)
                self.page.goto(self.public_url)
                self.page.locator('[data-retry-draft]').wait_for(state='visible')
                self.field(0).fill('失败期间刚写的新期待')
                self.page.locator('#identity-mode').select_option('named')
                self.page.wait_for_timeout(1200)  # Exceed autosave debounce; a failed restore must block writes.
                self.assertEqual(state['posts'], 0)
                self.assertIn('等待恢复上次答案', self.page.locator('[data-flow-status]').inner_text())
                self.assertEqual(self.draft()['answers'], original['answers'])
                self.page.locator('[data-flow-submit]').click()
                self.page.locator('[data-flow-error]').filter(has_text='上次答案尚未恢复').wait_for()
                self.assertFalse(self.page.locator('[data-confirm-dialog]').is_visible())
                self.assertEqual(self.flow.db.responses.count_documents({'survey_id': self.survey_id}), 0)
                self.capture('48_恢复失败时保留输入并提供重试.png')
                self.page.locator('.flow-controls [data-exit-response]').click()
                self.page.locator('[data-confirm-proceed]').click()
                self.page.locator('[data-flow-error]').filter(has_text='尚未退出').wait_for()
                self.assertEqual(self.page.url, self.public_url)
                state['fail'] = False
                self.page.get_by_role('button', name='重试恢复', exact=True).click()
                self.wait_saved()
                self.assertEqual(self.field(0).input_value(), '失败期间刚写的新期待')
                self.assertEqual(self.field(1).input_value(), '之前保存的补充')
                self.assertEqual(self.draft()['answers']['q_' + self.question_ids[1]], '之前保存的补充')
                self.assertEqual(self.draft()['identity_mode'], 'named')
                self.assertFalse(self.page.locator('[data-retry-draft]').is_visible())
                self.capture('49_重试恢复后合并新旧答案.png')
                self.page.unroute('**' + self.draft_path, fail_restore)

    def test_long_answers_are_saved_in_full_and_capacity_failure_keeps_old_draft(self):
        self.page.goto(self.public_url)
        self.page.wait_for_function("document.querySelector('[data-draft-state]').textContent === ''")
        self.page.locator('[data-flow-status]').filter(has_text='待检查').wait_for()
        text = '长答案' * 3700
        self.field(0).fill(text)
        self.wait_saved()
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], text)
        result = self.flow.api(self.visitor, 'POST', self.draft_path, {
            'answers': {'q_' + self.question_ids[0]: 123}})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.flow.api(self.visitor, 'POST', self.draft_path, ['invalid']).status_code, 400)
        for invalid in ([], None, False, 0, '', {}, {'identity_mode':'named'}, {'answers':None}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.flow.api(self.visitor, 'POST', self.draft_path, invalid).status_code, 400)
                self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], text)
        with self.visitor.session_transaction() as saved:
            token = saved['csrf_token']
        malformed = self.visitor.post(self.draft_path, data='{broken',
                                      content_type='application/json', headers={'X-CSRF-Token': token})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], text)
        too_long = 'x' * 100001
        self.field(0).fill(too_long)
        self.page.locator('[data-draft-state]').filter(has_text='内容超过暂存容量').wait_for()
        self.assertEqual(self.draft()['answers']['q_' + self.question_ids[0]], text)
        self.assertEqual(self.field(0).input_value(), too_long)
        self.page.locator('.flow-controls [data-exit-response]').click()
        self.page.locator('[data-confirm-proceed]').click()
        self.page.locator('[data-flow-error]').filter(has_text='尚未退出').wait_for()
        self.assertEqual(self.page.url, self.public_url)


if __name__ == '__main__':
    unittest.main()
