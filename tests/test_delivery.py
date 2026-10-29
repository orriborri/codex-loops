import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from loop import Loops, State, review_marker
from reviewing import ChangeTooLarge, goal_contract, risk_command
from support import goal, verdict, finding, response


class DeliveryFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.loops = object.__new__(Loops)
        self.loops.repo, self.loops.base, self.loops.reviewer = 'owner/repo', 'main', 'review-bot'
        self.loops.limit, self.loops.required = 5, ['test']
        self.loops.state = State(self.root)
        self.loops.state.link(1, 2)
        self.goal = goal()
        self.pr = {'number': 2, 'state': 'open', 'draft': False, 'labels': [], 'user': {'login': 'author'},
            'head': {'sha': 'a' * 40, 'ref': 'agent/issue-1', 'repo': {'full_name': 'owner/repo'}},
            'base': {'sha': 'b' * 40, 'ref': 'main'}, 'mergeable': True, 'mergeable_state': 'clean'}
        self.reviews, self.comments, self.issues, self.merges = [], [], [], []
        self.files = [{'filename': 'src/export.py', 'status': 'modified', 'additions': 5, 'deletions': 2}]
        self.ci_good = True
        self.drop_review_response = False
        self.loops.gh = Mock()
        self.loops.gh.request.side_effect = self.request
        self.loops.gh.pages.side_effect = self.pages
        self.loops.sync_progress = Mock()
        self.loops.publish = Mock(return_value=True)
        self.loops._agent = Mock(return_value=verdict())
        self.loops.workspace = Mock(side_effect=self.workspace)
        self.loops.git = Mock(return_value='')
        self.tar = patch('loop.tarfile.open'); self.tar.start()
        self.env = patch.dict(os.environ, {'AUTO_MERGE': 'true', 'BASELINE_TEST_COMMAND': '',
                                          'RISK_APPROVERS': 'owner'}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tar.stop()
        self.loops.state.db.close(); self.tmp.cleanup()

    def workspace(self, ref):
        folder = self.root / ('job-' + str(self.loops.workspace.call_count))
        folder.mkdir(exist_ok=True)
        source, output, control = folder / 'source', folder / 'output', folder / 'control'
        for p in [source, output, control]: p.mkdir(exist_ok=True)
        return folder, control, source, output

    def request(self, path, method='GET', data=None):
        if path == 'pulls/2': return copy.deepcopy(self.pr)
        if path == 'issues/1': return copy.deepcopy(self.goal)
        if path == 'pulls/2/reviews':
            review = {'id': len(self.reviews) + 10, 'state': {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED',
                      'COMMENT': 'COMMENTED'}[data['event']], 'body': data['body'], 'commit_id': data['commit_id'],
                      'user': {'login': 'review-bot', 'type': 'User'}}
            self.reviews.append(review)
            if self.drop_review_response:
                self.drop_review_response = False
                raise OSError('response lost after successful POST')
            return review
        if path == 'collaborators/owner/permission': return {'permission': 'write'}
        if path == 'pulls/2/merge': self.merges.append(data); return {'merged': True}
        if path == 'issues' and method == 'POST':
            issue = dict(data, number=100 + len(self.issues), labels=[{'name': name} for name in data['labels']]); self.issues.append(issue); return issue
        if path.endswith('/labels/agent%3Aresume'):
            for item in [self.goal, self.pr]: item['labels'] = [x for x in item['labels'] if x['name'] != 'agent:resume']
            return None
        raise AssertionError((path, method))

    def pages(self, path, key=None):
        if path == 'pulls/2/reviews': return copy.deepcopy(self.reviews)
        if path == 'pulls/2/files': return copy.deepcopy(self.files)
        if path == 'pulls/2/comments': return []
        if path == 'issues/2/comments': return copy.deepcopy(self.comments)
        if 'check-runs' in path:
            return [{'id': 7, 'name': 'test', 'status': 'completed', 'conclusion': 'success' if self.ci_good else 'failure'}]
        if path.endswith('/statuses'): return []
        if path.startswith('issues?state=all&labels=agent%3Aproposal'): return self.issues
        if path.startswith('issues?state=open'): return []
        raise AssertionError(path)

    def review(self, data=None):
        self.loops._agent.return_value = data or verdict()
        self.loops.review()

    def test_resume_requests_fresh_review_before_repair(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        self.review(data)
        self.goal['labels'].append({'name': 'agent:resume'})
        self.loops._agent.reset_mock()
        self.loops.implement()
        self.loops._agent.assert_not_called()
        self.assertEqual(self.loops.state.db.execute('SELECT state FROM workflow WHERE issue=1').fetchone()[0], 'waiting_for_review')
        self.review(data)
        self.assertEqual(len(self.reviews), 2)

    def test_review_pack_and_actual_approval_enable_low_risk_merge(self):
        self.review()
        self.assertIn('AC1', self.reviews[0]['body'])
        self.assertIn('Restart test', self.reviews[0]['body'])
        self.loops.merge()
        self.assertEqual(self.merges, [{'sha': 'a' * 40, 'merge_method': 'squash'}])

    def test_later_low_risk_review_cannot_clear_a_raised_risk(self):
        data = verdict(); data['risk'] = 'high'
        self.review(data)
        self.loops.merge()
        self.assertEqual(self.merges, [])
        self.loops.implement()
        self.assertEqual(self.loops.state.db.execute('SELECT state FROM workflow WHERE issue=1').fetchone()[0], 'needs_human_approval')
        self.reviews[-1]['state'] = 'DISMISSED'
        self.review(verdict())
        self.assertTrue(self.loops.state.reviews.latest(2)['risks'])
        self.loops.merge()
        self.assertEqual(self.merges, [])
        command = risk_command(self.pr['head']['sha'], self.pr['base']['sha'], goal_contract(self.goal)['fingerprint'])
        self.comments.append({'body': command, 'user': {'login': 'owner', 'type': 'User'}})
        self.loops.merge()
        self.assertEqual(len(self.merges), 1)

    def test_a_plain_github_approval_without_evidence_record_cannot_merge(self):
        self.reviews.append({'id': 10, 'state': 'APPROVED', 'commit_id': 'a' * 40,
            'body': review_marker('a' * 40, 'b' * 40), 'user': {'login': 'review-bot'}})
        self.loops.merge()
        self.assertEqual(self.merges, [])

    def test_changing_the_goal_invalidates_review_even_without_code_changes(self):
        self.review()
        self.goal['body'] += '\nClarify a requirement.'
        self.loops.merge()
        self.assertEqual(self.merges, [])
        self.review()
        self.assertEqual(len(self.reviews), 2)
        self.loops.merge()
        self.assertEqual(len(self.merges), 1)

    def test_high_risk_paths_require_exact_allowlisted_human_approval(self):
        self.files[0]['filename'] = 'src/auth/session.py'
        self.review()
        self.loops.merge()
        self.assertEqual(self.merges, [])
        command = risk_command('a' * 40, 'b' * 40, goal_contract(self.goal)['fingerprint'])
        self.assertIn(command, self.reviews[0]['body'])
        self.comments = [{'body': command, 'user': {'login': 'unknown', 'type': 'User'}}]
        self.loops.merge(); self.assertEqual(self.merges, [])
        self.comments[0]['user']['login'] = 'owner'
        self.loops.merge(); self.assertEqual(len(self.merges), 1)

    def test_high_risk_label_and_reviewer_flag_cannot_be_lowered_by_low_risk_label(self):
        for source in ['label', 'reviewer']:
            with self.subTest(source=source):
                self.reviews.clear(); self.merges.clear()
                data = verdict()
                self.pr['labels'] = [{'name': 'agent:low-risk'}]
                if source == 'label': self.pr['labels'].append({'name': 'agent:high-risk'})
                else: data['risk'] = 'high'
                # New goal fingerprint forces a fresh independent review for the scenario.
                self.goal['body'] += '\nScenario ' + source
                self.review(data); self.loops.merge()
                self.assertEqual(self.merges, [])

    def test_lost_review_post_response_recovers_pack_without_repeating_inference(self):
        self.drop_review_response = True
        with self.assertRaises(OSError): self.review()
        self.assertIsNone(self.loops.state.reviews.latest(2)['review_id'])
        self.loops.review()
        self.loops._agent.assert_called_once()
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(self.loops.state.reviews.latest(2)['review_id'], 10)

    def test_two_disputes_of_same_blocker_escalate_and_do_not_publish_second_repair(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        self.review(data)
        self.loops._agent.return_value = {'outcome': 'completed', 'summary': 'Disputed with evidence.',
                                         'finding_responses': [response()]}
        self.loops.implement()
        self.loops.publish.assert_called_once()
        self.review(data)
        self.loops._agent.return_value = {'outcome': 'completed', 'summary': 'Still disputed.',
                                         'finding_responses': [response()]}
        self.loops.implement()
        self.loops.publish.assert_called_once()
        hold = self.loops.state.db.execute('SELECT state,reason FROM workflow_holds WHERE issue=1').fetchone()
        self.assertEqual(hold[0], 'needs_human_decision')
        self.assertIn('Should retries', hold[1]); self.assertIn('Recommendation', hold[1])
        count = self.loops._agent.call_count
        self.loops.review(); self.loops.merge()
        self.assertEqual(self.loops._agent.call_count, count)
        self.assertEqual(self.merges, [])

    def test_fixes_need_reviewer_to_resolve_blockers_before_merging(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        self.review(data)
        self.loops._agent.return_value = {'outcome': 'completed', 'summary': 'Fixed.',
                                         'finding_responses': [response('fixed')]}
        def publish(*args, **kwargs): self.pr['head']['sha'] = 'c' * 40
        self.loops.publish.side_effect = publish
        self.loops.implement(); self.loops.merge()
        self.assertEqual(self.merges, [])
        good = verdict(); good['findings'] = [finding(status='resolved')]
        self.review(good); self.loops.merge()
        self.assertEqual(self.merges[0]['sha'], 'c' * 40)

    def test_reviewer_cannot_silently_drop_previous_blocker(self):
        data = verdict('REQUEST_CHANGES'); data['findings'] = [finding()]
        self.review(data)
        self.pr['head']['sha'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'F1'): self.review(verdict())
        self.assertEqual(len(self.reviews), 1)
        self.loops.merge(); self.assertEqual(self.merges, [])

    def test_oversized_diff_holds_for_split_without_publishing(self):
        self.ci_good = False
        self.loops._agent.return_value = {'outcome': 'completed', 'summary': 'Done.', 'finding_responses': []}
        self.loops.publish.side_effect = ChangeTooLarge('Split the oversized change into smaller issues.')
        self.loops.implement()
        row = self.loops.state.db.execute('SELECT state,reason FROM workflow_holds').fetchone()
        self.assertEqual(row[0], 'needs_human_decision'); self.assertIn('Split', row[1])

    def test_missing_acceptance_criteria_stops_before_inference(self):
        self.goal['body'] = 'Please do several things.'
        self.loops.implement()
        self.loops._agent.assert_not_called(); self.loops.publish.assert_not_called()
        self.assertEqual(self.loops.state.db.execute('SELECT state FROM workflow_holds').fetchone()[0], 'needs_clarification')

    def test_baseline_only_failures_create_separate_issue_and_hold_current_work(self):
        self.ci_good = False
        baseline = {'classification': 'baseline_only', 'baseline_failures': ['suite::existing'],
                    'regressions': [], 'base': 'b' * 40, 'head': 'a' * 40, 'command_hash': 'd' * 64}
        self.loops.state.baselines.compare = Mock(return_value=baseline)
        self.loops.implement()
        self.loops._agent.assert_not_called(); self.loops.publish.assert_not_called()
        self.assertEqual(len(self.issues), 1)
        self.assertEqual(self.issues[0]['labels'], [{'name': 'agent:proposal'}])
        goal_contract(self.issues[0])
        self.loops.implement(); self.assertEqual(len(self.issues), 1)
        self.assertEqual(self.loops.state.db.execute('SELECT state FROM workflow_holds').fetchone()[0], 'needs_human_decision')

    def test_regressions_stay_in_current_task_while_baseline_failure_gets_own_issue(self):
        self.ci_good = False
        baseline = {'classification': 'regression', 'baseline_failures': ['suite::existing'],
                    'regressions': ['suite::new'], 'base': 'b' * 40, 'head': 'a' * 40, 'command_hash': 'd' * 64}
        self.loops.state.baselines.compare = Mock(return_value=baseline)
        self.loops._agent.return_value = {'outcome': 'completed', 'summary': 'Fixed regression.', 'finding_responses': []}
        self.loops.implement()
        self.assertEqual(len(self.issues), 1)
        self.loops.publish.assert_called_once()
        self.assertIn('suite::new', self.loops._agent.call_args.args[2])


if __name__ == '__main__': unittest.main()
