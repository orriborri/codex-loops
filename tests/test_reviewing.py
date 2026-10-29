import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from reviewing import (ReviewLog, goal_contract, review_schema, implementation_schema,
                       validate_shape, validate_verdict, risk_reasons, risk_command, risk_approved,
                       render_pack, ChangeTooLarge, changed_lines)


from support import goal, verdict, finding, response


class ReviewContracts(unittest.TestCase):
    def test_readiness_extracts_stable_criteria(self):
        contract = goal_contract(goal())
        self.assertEqual([c['id'] for c in contract['criteria']], ['AC1', 'AC2'])
        self.assertEqual(contract['outcome'], 'Exports survive restarts.')
        self.assertEqual(contract['fingerprint'], goal_contract(goal())['fingerprint'])
        self.assertNotEqual(contract['fingerprint'], goal_contract(goal(goal()['body'] + '\nMore detail'))['fingerprint'])

    def test_unready_goals_request_clarification_before_model_work(self):
        for value in [goal('No acceptance criteria'), goal('## Acceptance criteria\n' + '\n'.join('- Task ' + str(n) for n in range(6)))]:
            with self.assertRaisesRegex(ValueError, '[Aa]cceptance criteria'):
                goal_contract(value)
        large = goal(); large['labels'] = [{'name': 'agent:large'}]
        with self.assertRaisesRegex(ValueError, 'Split'):
            goal_contract(large)

    def test_strict_nested_contract_and_complete_approval(self):
        data = verdict()
        self.assertEqual(validate_shape(data, review_schema()), data)
        validate_verdict(data, goal_contract(goal()), [])
        for mutate in ['missing', 'duplicate', 'unclear', 'blocker']:
            invalid = verdict()
            if mutate == 'missing': invalid['criteria'].pop()
            if mutate == 'duplicate': invalid['criteria'][1]['id'] = 'AC1'
            if mutate == 'unclear': invalid['criteria'][0]['result'] = 'unclear'
            if mutate == 'blocker': invalid['findings'] = [finding()]
            with self.assertRaises(ValueError): validate_verdict(invalid, goal_contract(goal()), [])
        data['criteria'][0]['unexpected'] = True
        with self.assertRaises(ValueError): validate_shape(data, review_schema())

    def test_blocking_findings_cannot_silently_disappear_or_be_downgraded(self):
        with self.assertRaisesRegex(ValueError, 'F1'):
            validate_verdict(verdict(), goal_contract(goal()), [finding()])
        data = verdict(); data['findings'] = [finding(severity='minor')]
        with self.assertRaises(ValueError): validate_verdict(data, goal_contract(goal()), [finding()])
        data['findings'] = [finding(status='resolved')]
        validate_verdict(data, goal_contract(goal()), [finding()])

    def test_minor_findings_do_not_prevent_approval(self):
        data = verdict(); data['findings'] = [finding(severity='minor')]
        validate_verdict(data, goal_contract(goal()), [])

    def test_diff_budget_excludes_tests_and_lockfiles_but_counts_application_code(self):
        self.assertEqual(changed_lines('200\t50\tsrc/app.py\n100\t20\ttests/test_app.py\n1000\t0\tpnpm-lock.yaml\n-\t-\tlogo.png\n'), 250)

    def test_risk_can_be_raised_by_paths_labels_or_reviewer_but_not_lowered(self):
        for path in ['src/auth/session.ts', 'authentication/session.py', 'authorization/roles.py', 'auth.py', 'src/authentication.ts']:
            self.assertTrue(risk_reasons([path], [], 'low'), path)
        self.assertTrue(risk_reasons(['src/db/migrations/001.sql'], [], 'low'))
        self.assertTrue(risk_reasons(['infra/main.tf'], [], 'low'))
        self.assertTrue(risk_reasons(['src/a.py'], ['agent:high-risk', 'agent:low-risk'], 'low'))
        self.assertTrue(risk_reasons(['src/a.py'], [], 'high'))
        self.assertEqual(risk_reasons(['src/a.py', 'tests/test_a.py'], [], 'low'), [])

    def test_risk_approval_binds_to_revision_and_goal_and_trusted_identity(self):
        command = risk_command('head', 'base', 'goalhash')
        comments = [{'body': command, 'user': {'login': 'owner', 'type': 'User'}}]
        gh = Mock(); gh.request.return_value = {'permission': 'write'}
        self.assertTrue(risk_approved(gh, comments, ['owner'], 'head', 'base', 'goalhash', 'review-bot'))
        self.assertFalse(risk_approved(gh, comments, ['owner'], 'new', 'base', 'goalhash', 'review-bot'))
        self.assertFalse(risk_approved(gh, comments, ['owner'], 'head', 'base', 'newgoal', 'review-bot'))
        self.assertFalse(risk_approved(gh, comments, [], 'head', 'base', 'goalhash', 'review-bot'))
        comments[0]['user']['type'] = 'Bot'
        self.assertFalse(risk_approved(gh, comments, ['owner'], 'head', 'base', 'goalhash', 'review-bot'))


class ReviewHistory(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.log = ReviewLog(self.db)
        self.contract = goal_contract(goal())

    def tearDown(self): self.db.close()

    def record(self, data=None, head='head'):
        return self.log.record(1, 2, head, 'base', self.contract, data or verdict(), [])

    def test_published_report_binds_to_github_review_and_goal(self):
        row = self.record()
        self.assertFalse(self.log.approved(2, 'head', 'base', self.contract['fingerprint'], 17))
        self.log.published(row['id'], 17)
        self.assertTrue(self.log.approved(2, 'head', 'base', self.contract['fingerprint'], 17))
        self.assertFalse(self.log.approved(2, 'head', 'base', 'changed-goal', 17))
        self.assertFalse(self.log.approved(2, 'head', 'base', self.contract['fingerprint'], 18))

    def test_disputes_require_responses_to_every_blocker_and_stop_after_two_rounds(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        row = self.record(data)
        with self.assertRaises(ValueError): self.log.responses(2, row['id'], [])
        self.assertIsNone(self.log.responses(2, row['id'], [response()]))
        self.assertTrue(self.log.unreviewed_responses(2))
        self.assertFalse(self.log.unreviewed_responses(2, row['id']))
        row2 = self.record(data)
        escalation = self.log.responses(2, row2['id'], [response()])
        self.assertIn('Should retries', escalation)
        self.assertIn('Recommendation', escalation)

    def test_fixed_response_breaks_consecutive_dispute_streak(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        row = self.record(data); self.log.responses(2, row['id'], [response()])
        row = self.record(data); self.log.responses(2, row['id'], [response('fixed')])
        row = self.record(data)
        self.assertIsNone(self.log.responses(2, row['id'], [response()]))

    def test_retry_cannot_rewrite_a_persisted_response(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        row = self.record(data)
        self.log.responses(2, row['id'], [response()])
        self.assertIsNone(self.log.responses(2, row['id'], [response()]))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.log.responses(2, row['id'], [response('fixed')])

    def test_needs_decision_escalates_without_waiting_for_repeat(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        row = self.record(data)
        self.assertIn('Recommendation', self.log.responses(2, row['id'], [response('needs_decision')]))

    def test_review_pack_contains_criterion_evidence_risk_and_revision(self):
        row = self.record()
        rendered = render_pack(row, self.log.history(2), [])
        for text in ['AC1', 'Restart test', 'test_retry', 'head', 'base', 'Review pack']:
            self.assertIn(text, rendered)
        self.assertIn(row['marker'], rendered)


if __name__ == '__main__': unittest.main()
