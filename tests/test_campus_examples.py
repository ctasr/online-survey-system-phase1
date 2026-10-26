"""Verify realistic labelled fixtures through APIs without touching daily data."""
import unittest
from datetime import datetime, timedelta, timezone
from tests import test_stage2_library as library_tests
from tests.campus_fixtures import seed, SUITE


class CampusExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library_tests.LibraryFlowTests.setUpClass()
        cls.flow=library_tests.LibraryFlowTests()

    @classmethod
    def tearDownClass(cls):
        library_tests.LibraryFlowTests.tearDownClass()

    def test_examples_are_diverse_valid_dated_and_repeatable_without_changing_existing_data(self):
        flow=self.flow
        client=flow.app.test_client()
        flow.login(client,'yyx')
        owner=flow.db.users.find_one({'username':'yyx'})
        flow.db.users.update_one({'_id':owner['_id']},{'$set':{'created_at':datetime.now(timezone.utc)-timedelta(days=2)}})
        old_survey=flow.survey(client,'yyx','已有问卷，不应被示例脚本改动')
        before_user=flow.db.users.find_one({'_id':owner['_id']})
        report=seed(flow.app)
        self.assertEqual(flow.db.users.find_one({'_id':owner['_id']}),before_user)
        self.assertEqual(flow.db.surveys.find_one({'_id':old_survey['_id']}),old_survey)
        self.assertEqual(report['new_responses'],39)
        self.assertEqual(report['distinct_submission_times'],39)
        surveys=list(flow.db.surveys.find({'demo_seed_suite':SUITE}))
        self.assertEqual({s['status'] for s in surveys},{'published','closed'})
        self.assertEqual({q['type'] for s in surveys for q in s['questions']},{'text','number','single_choice','multiple_choice'})
        self.assertEqual({j['kind'] for s in surveys for q in s['questions'] for j in q.get('jumps',[])},{'number','single_choice','multiple_choice'})
        self.assertEqual(len({s['created_at'] for s in surveys}),3)
        for survey in surveys:
            responses=list(flow.db.responses.find({'survey_id':survey['_id']}))
            self.assertEqual(len(responses),13)
            self.assertGreaterEqual(len({tuple(a['question_id'] for a in r['answers']) for r in responses}),2)
            for response in responses:
                self.assertLessEqual(survey['published_at'],response['submitted_at'])
                self.assertTrue(response['demo_timeline_simulated'])
            self.assertEqual(client.get(f"/surveys/{survey['_id']}/statistics").status_code,200)
        named=flow.db.responses.find_one({'demo_seed_suite':SUITE,'respondent_id':owner['_id']})
        self.assertEqual(client.get(f"/responses/{named['_id']}").status_code,200)
        shared=list(flow.db.question_library.find({'demo_seed_suite':SUITE,'shared':True}))
        self.assertEqual(len(shared),2)
        self.assertEqual({q['version'] for q in shared},{1,2})
        all_questions=list(flow.db.question_library.find({'demo_seed_suite':SUITE}))
        self.assertEqual(len({q['created_at'] for q in all_questions}),len(all_questions))
        for question in all_questions:
            creator=flow.db.users.find_one({'_id':question['owner_id']})
            self.assertLessEqual(creator['created_at'],question['created_at'])
        self.assertGreater(len({u['created_at'].date() for u in flow.db.users.find({'demo_seed_suite':SUITE})}),3)
        snapshot={name:{r['_id']:r for r in flow.db[name].find({})} for name in
                  ('users','surveys','responses','response_drafts','question_library','question_banks')}
        again=seed(flow.app)
        self.assertEqual(again['new_responses'],39)
        for name,rows in snapshot.items():
            self.assertEqual({r['_id']:r for r in flow.db[name].find({})},rows,name)
