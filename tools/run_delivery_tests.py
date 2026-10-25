"""Run the real test suite and preserve per-case evidence for the submission."""
import json
import platform
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class EvidenceResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.case_started = time.perf_counter()

    def stopTest(self, test):
        row = next((row for row in rows if row['id'] == test.id()), None)
        if row is not None:
            row['seconds'] = round(time.perf_counter() - self.case_started, 3)
        super().stopTest(test)

    def record(self, test, status, error=None, reason=None):
        parent = getattr(test, 'test_case', test)
        module = parent.__class__.__module__.rsplit('.', 1)[-1]
        response_only = {
            'test_stale_new_question_forms_preserve_input_without_saving_or_changing_survey',
        }
        browser = (module in ('test_draft_experience', 'test_library_context',
                              'test_editor_experience', 'test_fill_navigation',
                              'test_statistics_history_navigation', 'test_bank_navigation', 'test_error_page_experience') or
                   (module == 'test_lifecycle_experience' and getattr(parent, '_testMethodName', '') in (
                    'test_owner_reviews_latest_state_before_publish_close_and_delete',
                    'test_jump_and_bank_rename_keep_pending_inputs_on_conflicts')) or
                   (module == 'test_deadline_experience' and getattr(parent, '_testMethodName', '') ==
                    'test_inline_deadline_correction_conflict_retry_and_closed_form') or
                   (module == 'test_historical_review' and getattr(parent, '_testMethodName', '') ==
                    'test_removed_questions_do_not_duplicate_numbers_in_personal_or_owner_review') or
                   (module == 'test_creator_navigation' and getattr(parent, '_testMethodName', '') !=
                    'test_edit_final_write_conflicts_preserve_input_for_all_edit_branches') or
                   (module == 'test_library_creation_context' and getattr(parent, '_testMethodName', '') in (
                    'test_create_cancel_validation_and_save_keep_filtered_selection_page',
                    'test_catalog_creation_preserves_search_pages_and_bank_destination')) or
                   (module == 'test_library_entry_matrix' and getattr(parent, '_testMethodName', '') ==
                    'test_detail_bank_actions_keep_all_entry_contexts_and_report_stale_destination') or
                   (module == 'test_review_navigation' and getattr(parent, '_testMethodName', '') not in response_only))
        row = next((row for row in rows if row['id'] == parent.id()), None)
        if row is None:
            row = {'id': parent.id(), 'status': status,
                   'execution': 'automated_browser' if browser else 'automated_business_or_api',
                   'seconds': round(time.perf_counter() - self.case_started, 3)}
            rows.append(row)
        else:
            priority = {'passed': 0, 'skipped': 1, 'failed': 2, 'error': 3}
            if priority[status] > priority[row['status']]:
                row['status'] = status
        detail = self._exc_info_to_string(error, test) if error else reason
        if detail:
            row['detail'] = (row.get('detail', '') + '\n' + detail).strip()
        if parent is not test:
            row.setdefault('subtests', []).append({'id': test.id(), 'status': status,
                                                   **({'detail': detail} if detail else {})})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, 'passed')

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.record(test, 'failed', error)

    def addError(self, test, error):
        super().addError(test, error)
        self.record(test, 'error', error)

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            status = 'failed' if issubclass(error[0], test.failureException) else 'error'
            self.record(subtest, status, error)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, 'skipped', reason=reason)


if __name__ == '__main__':
    rows = []
    started = datetime.now(timezone.utc)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    output = ROOT / 'output' / 'verification'
    output.mkdir(parents=True, exist_ok=True)
    evidence = {'started_utc': started.isoformat(), 'finished_utc': datetime.now(timezone.utc).isoformat(),
                'python': platform.python_version(), 'tests_run': result.testsRun,
                'failures': len(result.failures), 'errors': len(result.errors),
                'skipped': len(result.skipped), 'successful': result.wasSuccessful(), 'cases': rows}
    evidence['execution_categories'] = {
        category: sum(row['execution'] == category for row in rows)
        for category in ('automated_browser', 'automated_business_or_api')}
    (output / 'final_test_results.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in evidence.items() if key != 'cases'}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
