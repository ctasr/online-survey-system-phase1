"""User-oriented API checks with explicit inputs and independent expectations."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from bson import ObjectId
from pymongo.collection import Collection
from app import create_app


class ApiUserJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_database = os.environ.get('MONGO_DB_NAME')
        cls.db_name = 'survey_phase2_journey_test_' + uuid4().hex
        os.environ['MONGO_DB_NAME'] = cls.db_name
        cls.app = create_app()
        cls.db = cls.app.extensions['mongo_client'][cls.db_name]

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['mongo_client'].drop_database(cls.db_name)
        cls.app.extensions['mongo_client'].close()
        if cls.previous_database is None:
            os.environ.pop('MONGO_DB_NAME', None)
        else:
            os.environ['MONGO_DB_NAME'] = cls.previous_database

    def call(self, client, method, path, data=None):
        token = client.get('/api/v1/csrf').json['csrf_token']
        return client.open('/api/v1' + path, method=method, json=data,
                           headers={'X-CSRF-Token': token})

    def actor(self):
        client = self.app.test_client()
        name = 'journey_' + uuid4().hex[:12]
        registered = self.call(client, 'POST', '/auth/register', {
            'username': name, 'password': '123456', 'confirmation': '123456'})
        self.assertEqual(registered.status_code, 201, registered.json)
        login = self.call(client, 'POST', '/auth/login', {'username': name, 'password': '123456'})
        self.assertEqual(login.status_code, 200, login.json)
        return client, ObjectId(registered.json['id'])

    def survey(self, owner, definitions, **settings):
        created = self.call(owner, 'POST', '/surveys', {
            'title': '食堂午餐排队体验', 'allow_anonymous': True, **settings})
        self.assertEqual(created.status_code, 201, created.json)
        doc = created.json['survey']
        path = '/surveys/' + doc['id']
        for definition in definitions:
            added = self.call(owner, 'POST', path + '/questions', definition)
            self.assertEqual(added.status_code, 201, added.json)
        return self.call(owner, 'GET', path).json['survey']

    def publish(self, owner, doc):
        result = self.call(owner, 'POST', '/surveys/' + doc['id'] + '/publish', {})
        self.assertEqual(result.status_code, 200, result.json)
        return result.json['survey']

    def submit(self, client, doc, answers, **extra):
        return self.call(client, 'POST', '/public/surveys/' + doc['slug'] + '/responses', {
            'revision': doc['revision'], 'answers': answers, **extra})

    def test_api_rejects_wrong_answer_shapes_without_saving(self):
        owner, _ = self.actor()
        writer, _ = self.actor()
        doc = self.publish(owner, self.survey(owner, [
            dict(type='single_choice', title='最近去过食堂吗？', required=True, options=['去过', '没去过']),
            dict(type='multiple_choice', title='想改善哪些方面？', required=True,
                 options=['排队', '分量', '价格'], min_select=1),
            dict(type='text', title='补充建议', required=False),
            dict(type='number', title='排队分钟数', required=True, min_value=0, max_value=60),
        ]))
        keys = [q['id'] for q in doc['questions']]
        valid = dict(zip(keys, ['去过', ['排队'], '取餐口标识不清楚', 8]))
        cases = [(keys[0], ['去过']), (keys[1], '排队'),
                 (keys[2], ['取餐口标识不清楚']), (keys[3], [8]),
                 (keys[0], True), (keys[2], {'text': '建议'})]
        for key, value in cases:
            with self.subTest(question=key, value=value):
                malformed = {**valid, key: value}
                assess = self.call(writer, 'POST', '/public/surveys/' + doc['slug'] + '/assess',
                                   {'answers': malformed})
                self.assertEqual(assess.status_code, 422, assess.json)
                result = self.submit(writer, doc, malformed)
                self.assertEqual(result.status_code, 422, result.json)
                self.assertIn('error', result.json)
                self.assertEqual(self.db.responses.count_documents({'survey_id': ObjectId(doc['id'])}), 0)
        self.assertEqual(self.submit(writer, doc, valid).status_code, 201)

    def test_boundary_inputs_are_checked_before_any_response_is_saved(self):
        owner, _ = self.actor()
        writer, _ = self.actor()
        doc = self.publish(owner, self.survey(owner, [
            dict(type='multiple_choice', title='优先改善哪两项？', required=True,
                 options=['排队', '分量', '价格'], exact_select=2),
            dict(type='text', title='请具体描述', required=True, min_length=2, max_length=4),
            dict(type='number', title='等待分钟数', required=True, min_value=0, max_value=60, integer_only=True),
        ]))
        choice, text, number = [q['id'] for q in doc['questions']]
        valid = {choice: ['排队', '价格'], text: '取餐太慢', number: 0}
        cases = [(choice, ['排队']), (choice, ['排队', '分量', '价格']),
                 (choice, ['排队', '排队']), (choice, ['排队', '不存在']),
                 (text, '慢'), (text, '取餐速度太慢'), (text, '  '),
                 (number, -1), (number, 61), (number, '2.5'),
                 (number, 'NaN'), (number, 'Infinity')]
        for key, value in cases:
            with self.subTest(question=key, value=value):
                result = self.submit(writer, doc, {**valid, key: value})
                self.assertEqual(result.status_code, 422, result.json)
                self.assertEqual(result.json['question_id'], key)
                self.assertEqual(self.db.responses.count_documents({'survey_id': ObjectId(doc['id'])}), 0)
        for edge, words in [(0, '太慢'), (60, '取餐太慢')]:
            accepted = self.submit(writer, doc, {**valid, text: words, number: edge})
            self.assertEqual(accepted.status_code, 201, accepted.json)

    def test_multiuser_jump_results_match_hand_calculated_statistics(self):
        owner, _ = self.actor()
        doc = self.survey(owner, [
            dict(type='single_choice', title='这周在食堂吃过午饭吗？', required=True, options=['吃过', '没有']),
            dict(type='number', title='最近一次排队几分钟？', required=True, min_value=0, max_value=60, integer_only=True),
            dict(type='text', title='有什么具体建议？', required=False, max_length=80),
            dict(type='multiple_choice', title='优先改善哪两项？', required=True,
                 options=['排队', '价格', '座位'], exact_select=2),
        ])
        gate, wait, note, priority = [q['id'] for q in doc['questions']]
        rule = self.call(owner, 'POST', f"/surveys/{doc['id']}/questions/{gate}/jumps",
                         {'option': '没有', 'target_id': priority})
        self.assertEqual(rule.status_code, 201, rule.json)
        doc = self.publish(owner, doc)
        # Expected totals are specified by hand, not derived with production statistics code.
        rows = [
            ('吃过', 0, '取餐和点单的队伍容易混', ['排队', '价格'], 'named'),
            ('吃过', 10, '', ['排队', '座位'], 'anonymous'),
            ('吃过', 20, '希望有小份菜', ['价格', '座位'], 'named'),
            ('没有', '无效但应被跳过', '这条旧输入不应保存', ['排队', '价格'], 'anonymous'),
        ]
        writers = []
        for gate_value, minutes, feedback, choices, mode in rows:
            writer, _ = self.actor()
            writers.append(writer)
            result = self.submit(writer, doc, {gate: gate_value, wait: minutes, note: feedback,
                                              priority: choices}, identity_mode=mode)
            self.assertEqual(result.status_code, 201, result.json)
            response_id = result.json['response_id']
            self.assertEqual(writer.get('/api/v1/responses/' + response_id).status_code, 200)
            self.assertEqual(owner.get('/api/v1/responses/' + response_id).status_code, 404)
        self.assertEqual(writers[0].get('/api/v1/responses/' + response_id).status_code, 404)
        result = owner.get('/api/v1/surveys/' + doc['id'] + '/statistics').json
        self.assertEqual(result['response_count'], 4)
        self.assertEqual(result['overview']['anonymous'], 2)
        stats = {q['question']['id']: q for q in result['results']}
        self.assertEqual(stats[gate]['option_counts'], {'吃过': 3, '没有': 1})
        self.assertEqual(stats[wait]['answered_count'], 3)
        self.assertEqual(stats[wait]['not_included_count'], 1)
        self.assertEqual(stats[wait]['blank_count'], 0)
        self.assertEqual(stats[wait]['average'], '10')
        self.assertEqual(stats[note]['blank_count'], 1)
        self.assertEqual(stats[note]['not_included_count'], 1)
        self.assertEqual(stats[priority]['option_counts'], {'排队': 3, '价格': 3, '座位': 2})
        saved = self.db.responses.find_one({'_id': ObjectId(response_id)})
        self.assertEqual([a['question_id'] for a in saved['answers']], [gate, priority])

    def test_api_success_clears_only_its_own_draft_and_failure_keeps_input(self):
        owner, _ = self.actor()
        writer, writer_id = self.actor()
        other, other_id = self.actor()
        doc = self.publish(owner, self.survey(owner, [dict(type='text', title='午餐建议', required=True)]))
        key = doc['questions'][0]['id']
        survey_id = ObjectId(doc['id'])
        for actor, uid in [(writer, writer_id), (other, other_id)]:
            saved = self.call(actor, 'GET', '/csrf')
            result = actor.post('/survey/' + doc['slug'] + '/draft', json={
                'answers': {'q_' + key: '排队位置不明显'}, 'identity_mode': 'anonymous'},
                headers={'X-CSRF-Token': saved.json['csrf_token']})
            self.assertEqual(result.status_code, 200, result.json)
        before = self.db.response_drafts.find_one({'survey_id': survey_id, 'user_id': writer_id})
        self.assertEqual(self.submit(writer, doc, {key: ''}).status_code, 422)
        self.assertEqual(self.db.response_drafts.find_one({'_id': before['_id']}), before)
        self.assertEqual(self.submit(writer, doc, {key: '希望标清取餐队伍'}).status_code, 201)
        self.assertIsNone(self.db.response_drafts.find_one({'survey_id': survey_id, 'user_id': writer_id}))
        self.assertIsNotNone(self.db.response_drafts.find_one({'survey_id': survey_id, 'user_id': other_id}))

    def test_close_or_deadline_during_insert_rolls_back_and_keeps_draft(self):
        for change in [{'status': 'closed'}, {'deadline': datetime.now(timezone.utc) - timedelta(seconds=1)}]:
            with self.subTest(change=next(iter(change))):
                owner, _ = self.actor()
                writer, writer_id = self.actor()
                doc = self.publish(owner, self.survey(owner, [dict(type='text', title='午餐建议', required=True)]))
                survey_id = ObjectId(doc['id'])
                key = doc['questions'][0]['id']
                original_insert = Collection.insert_one
                draft_id = self.db.response_drafts.insert_one({'survey_id': survey_id,
                    'user_id': writer_id, 'answers': {'q_' + key: '保留这条建议'}}).inserted_id

                def insert_then_close(collection, record, *args, **kwargs):
                    inserted = original_insert(collection, record, *args, **kwargs)
                    if collection.name == 'responses' and record.get('survey_id') == survey_id:
                        self.db.surveys.update_one({'_id': survey_id}, {'$set': change})
                    return inserted

                with patch.object(Collection, 'insert_one', insert_then_close):
                    result = self.submit(writer, doc, {key: '保留这条建议'})
                self.assertEqual(result.status_code, 409, result.json)
                self.assertEqual(self.db.responses.count_documents({'survey_id': survey_id}), 0)
                self.assertIsNotNone(self.db.response_drafts.find_one({'_id': draft_id}))

    def test_delete_survey_cleans_its_drafts_without_deleting_reusable_sources_or_other_answers(self):
        owner, _ = self.actor()
        writer, writer_id = self.actor()
        source = self.call(owner, 'POST', '/library/questions', {
            'type': 'text', 'title': '午餐排队有什么不方便？', 'required': True})
        self.assertEqual(source.status_code, 201, source.json)
        source_id = ObjectId(source.json['question']['id'])
        docs = []
        for title in ['一食堂午餐回访', '二食堂午餐回访']:
            created = self.call(owner, 'POST', '/surveys', {'title': title})
            self.assertEqual(created.status_code, 201, created.json)
            doc = created.json['survey']
            attached = self.call(owner, 'POST', f"/surveys/{doc['id']}/library-questions",
                                 {'question_id': str(source_id)})
            self.assertEqual(attached.status_code, 201, attached.json)
            doc = self.publish(owner, doc)
            answer_id = doc['questions'][0]['id']
            submitted = self.submit(writer, doc, {answer_id: '取餐口需要标清窗口号'})
            self.assertEqual(submitted.status_code, 201, submitted.json)
            token = self.call(writer, 'GET', '/csrf').json['csrf_token']
            saved = writer.post('/survey/' + doc['slug'] + '/draft', json={
                'answers': {'q_' + answer_id: '下一次填写还没提交的建议'}},
                headers={'X-CSRF-Token': token})
            self.assertEqual(saved.status_code, 200, saved.json)
            docs.append(doc)
        first_id, second_id = [ObjectId(doc['id']) for doc in docs]
        second_answer = self.db.responses.find_one({'survey_id': second_id})
        second_draft = self.db.response_drafts.find_one({'survey_id': second_id, 'user_id': writer_id})
        source_before = self.db.question_library.find_one({'_id': source_id})
        token = self.call(owner, 'GET', '/csrf').json['csrf_token']
        owner_draft = owner.post('/survey/' + docs[0]['slug'] + '/draft', json={
            'answers': {'q_' + docs[0]['questions'][0]['id']: '创建者试填的未提交内容'}},
            headers={'X-CSRF-Token': token})
        self.assertEqual(owner_draft.status_code, 200, owner_draft.json)
        self.assertEqual(self.db.response_drafts.count_documents({'survey_id': first_id}), 2)
        deleted = self.call(owner, 'DELETE', '/surveys/' + str(first_id),
                            {'revision': docs[0]['revision']})
        self.assertEqual(deleted.status_code, 200, deleted.json)
        self.assertIsNone(self.db.surveys.find_one({'_id': first_id}))
        self.assertEqual(self.db.responses.count_documents({'survey_id': first_id}), 0)
        self.assertEqual(self.db.response_drafts.count_documents({'survey_id': first_id}), 0)
        self.assertEqual(self.db.responses.find_one({'_id': second_answer['_id']}), second_answer)
        self.assertEqual(self.db.response_drafts.find_one({'_id': second_draft['_id']}), second_draft)
        self.assertEqual(self.db.question_library.find_one({'_id': source_id}), source_before)
        self.assertEqual(writer.get('/api/v1/public/surveys/' + docs[0]['slug']).status_code, 404)
        stats = owner.get(f'/api/v1/library/questions/{source_id}/statistics')
        self.assertEqual(stats.status_code, 200, stats.json)
        self.assertEqual(stats.json['answered_count'], 1)

    def test_settings_save_rechecks_closed_status_at_final_write(self):
        for include_revision in (False, True):
            with self.subTest(revision=include_revision):
                owner, _ = self.actor()
                doc = self.publish(owner, self.survey(owner, [
                    dict(type='text', title='对午餐排队有什么建议？', required=True)]))
                sid = ObjectId(doc['id'])
                before = self.db.surveys.find_one({'_id': sid})
                deadline = (datetime.now(timezone.utc) + timedelta(days=3)).astimezone(
                    timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M')
                changes = {'deadline': deadline, 'description': '这段修改应在冲突时保留在客户端'}
                if include_revision:
                    changes['revision'] = doc['revision']
                real_update = Collection.update_one
                closed = []

                def close_before_update(collection, query, update, *args, **kwargs):
                    if collection.name == 'surveys' and query.get('_id') == sid and not closed:
                        closed.append(True)
                        real_update(collection, {'_id': sid}, {'$set': {'status': 'closed'}})
                    return real_update(collection, query, update, *args, **kwargs)

                with patch.object(Collection, 'update_one', close_before_update):
                    result = self.call(owner, 'PATCH', '/surveys/' + doc['id'], changes)
                self.assertTrue(closed)
                self.assertEqual(result.status_code, 409, result.json)
                after = self.db.surveys.find_one({'_id': sid})
                self.assertEqual(after, {**before, 'status': 'closed'})
                self.assertEqual(self.call(owner, 'PATCH', '/surveys/' + doc['id'], changes).status_code, 409)
                # Other closed-survey settings remain editable after reading the current state.
                retry = self.call(owner, 'PATCH', '/surveys/' + doc['id'],
                                  {'description': '关闭后核对并保存的说明', 'revision': doc['revision']})
                self.assertEqual(retry.status_code, 200, retry.json)
                self.assertEqual(retry.json['survey']['description'], '关闭后核对并保存的说明')
                self.assertEqual(retry.json['survey']['status'], 'closed')
                self.assertIsNone(retry.json['survey']['deadline'])

    def test_saving_unchanged_settings_does_not_interrupt_someone_filling(self):
        owner, _ = self.actor()
        writer, _ = self.actor()
        deadline = (datetime.now(timezone.utc) + timedelta(days=3)).astimezone(
            timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M')
        doc = self.publish(owner, self.survey(owner, [
            dict(type='text', title='取餐时有什么不方便？', required=True)], deadline=deadline))
        survey_id = ObjectId(doc['id'])
        before = self.db.surveys.find_one({'_id': survey_id})
        unchanged = {name: doc[name] for name in (
            'title', 'description', 'allow_anonymous', 'allow_multiple_submissions',
            'option_label_style', 'deadline', 'revision')}
        unchanged['deadline'] = deadline
        for payload in ({'title': doc['title']}, unchanged):
            with self.subTest(fields=list(payload)):
                result = self.call(owner, 'PATCH', '/surveys/' + doc['id'], payload)
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual(result.json['survey']['revision'], doc['revision'])
                self.assertEqual(self.db.surveys.find_one({'_id': survey_id}), before)
        key = doc['questions'][0]['id']
        self.assertEqual(self.submit(writer, doc, {key: '窗口标识不太清楚'}).status_code, 201)
        changed = self.call(owner, 'PATCH', '/surveys/' + doc['id'],
                            {'description': '这次说明确实修改了', 'revision': doc['revision']})
        self.assertEqual(changed.status_code, 200, changed.json)
        self.assertEqual(changed.json['survey']['revision'], doc['revision'] + 1)
        self.assertEqual(self.submit(writer, doc, {key: '旧页面再次提交'}).status_code, 409)
