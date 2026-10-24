from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from loop import Loops, State, review_marker


class WorkflowStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = State(self.root)
        self.flow = self.state.workflow

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def transition(self, state='waiting_for_ci', pr=2):
        return self.flow.transition(1, pr, state, 'Checks are pending.', 'ci', 'Wait for checks.')

    def test_repeated_observations_preserve_history_and_since_across_restart(self):
        self.assertTrue(self.transition())
        since = self.flow.history(1)[0]['at']
        self.assertFalse(self.transition())
        self.state.db.close()
        self.state = State(self.root)
        self.flow = self.state.workflow
        self.assertFalse(self.transition())
        self.assertEqual(len(self.flow.history(1)), 1)
        self.assertEqual(self.flow.history(1)[0]['at'], since)
        self.assertTrue(self.transition('waiting_for_review'))
        self.assertEqual(len(self.flow.history(1)), 2)

    def test_wall_time_accounts_for_waits_and_stops_at_merge(self):
        start = datetime(2026, 10, 24, tzinfo=timezone.utc)
        with patch('workflow.datetime', wraps=datetime) as clock:
            clock.now.return_value = start
            self.transition()
            clock.now.return_value = start + timedelta(seconds=30)
            self.transition('waiting_for_review')
            clock.now.return_value = start + timedelta(seconds=40)
            self.transition('merged')
            clock.now.return_value = start + timedelta(days=1)
            self.assertEqual(self.flow.durations(1), {'waiting_for_ci': 30, 'waiting_for_review': 10})

    def test_clarification_requires_goal_change_and_human_decision_requires_explicit_resume(self):
        goal = {'title': 'Feature', 'body': 'Unclear'}
        self.flow.hold(1, None, 'needs_clarification', 'Which format?', goal)
        self.assertTrue(self.flow.blocked(1, None, dict(goal, updated_at='changed-by-comment')))
        self.assertFalse(self.flow.blocked(1, None, dict(goal, body='Use JSON')))
        self.flow.hold(1, 2, 'needs_human_decision', 'Review finding disputed.', goal)
        self.assertTrue(self.flow.blocked(1, 2, dict(goal, body='Edited')))
        self.state.link(1, 2)
        self.state.claim('issue:1', 3)
        self.state.claim('fix:2', 5)
        self.state.claim('review:2:head:base', 3)
        self.state.claim('fix:20', 5)
        self.flow.resume(1, 2)
        self.assertFalse(self.flow.blocked(1, 2, goal))
        self.assertEqual(self.state.db.execute('SELECT key FROM attempts').fetchall(), [('fix:20',)])

    def test_progress_updates_one_comment_per_issue_and_pr_without_poll_spam(self):
        self.transition()
        gh = Mock()
        gh.pages.return_value = []
        gh.request.side_effect = [{'id': 11}, {'id': 12}]
        self.flow.sync(gh)
        self.assertEqual(gh.request.call_count, 2)
        self.assertIn('Next owner', gh.request.call_args.args[2]['body'])
        self.flow.sync(gh)
        self.assertEqual(gh.request.call_count, 2)
        gh.request.side_effect = None
        self.transition('waiting_for_review')
        self.flow.sync(gh)
        self.assertEqual(gh.request.call_count, 4)
        self.assertEqual(gh.request.call_args.args[:2], ('issues/comments/12', 'PATCH'))

    def test_lost_post_response_recovers_existing_comment_after_restart(self):
        self.transition(pr=None)
        gh = Mock()
        gh.pages.return_value = []
        gh.request.side_effect = RuntimeError('Response lost after GitHub created the comment')
        with self.assertRaises(RuntimeError):
            self.flow.sync(gh)
        posted_body = gh.request.call_args.args[2]['body']
        self.state.db.close()
        self.state = State(self.root)
        self.flow = self.state.workflow
        gh.request.reset_mock(side_effect=True)
        gh.pages.return_value = [{'id': 44, 'body': posted_body}]
        self.flow.sync(gh)
        gh.request.assert_called_once()
        self.assertEqual(gh.request.call_args.args[:2], ('issues/comments/44', 'PATCH'))

    def test_unrelated_comments_are_never_overwritten(self):
        self.transition(pr=None)
        gh = Mock()
        gh.pages.return_value = [{'id': 7, 'body': '<!-- codex-loop-status:someone-else -->\nHuman note'}]
        gh.request.return_value = {'id': 8}
        self.flow.sync(gh)
        self.assertEqual(gh.request.call_args.args[:2], ('issues/1/comments', 'POST'))

    def test_failed_issue_comment_does_not_starve_the_pr_comment(self):
        self.transition()
        gh = Mock()
        gh.pages.return_value = []
        gh.request.side_effect = [RuntimeError('Issue comment inaccessible'), {'id': 22}]
        with self.assertRaises(RuntimeError):
            self.flow.sync(gh)
        self.assertEqual(gh.request.call_count, 2)
        self.assertEqual(gh.request.call_args.args[:2], ('issues/2/comments', 'POST'))
        self.assertEqual(self.state.db.execute('SELECT comment_id FROM progress_comments WHERE target=2').fetchone(), (22,))


class WorkflowControllers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.loops = object.__new__(Loops)
        self.loops.state = State(self.root)
        self.loops.gh = Mock()
        self.loops.repo, self.loops.base, self.loops.reviewer = 'owner/repo', 'main', 'review-bot'
        self.loops.limit, self.loops.required = 2, ['test']
        self.goal = {'number': 1, 'title': 'Feature', 'body': 'Requirements', 'state': 'open',
                     'labels': [{'name': 'agent:ready'}]}
        self.pr = {'number': 2, 'state': 'open', 'draft': False, 'labels': [],
                   'head': {'sha': 'head', 'ref': 'agent/issue-1', 'repo': {'full_name': 'owner/repo'}},
                   'base': {'sha': 'base', 'ref': 'main'}, 'mergeable': True, 'mergeable_state': 'clean'}
        self.loops.workspace = Mock(return_value=(self.root,) * 4)
        self.loops.publish = Mock()
        self.loops.sync_progress = Mock()
        self.loops.agent = Mock(return_value={'outcome': 'completed', 'summary': 'Done and tested.'})
        self.loops.snapshot = Mock(return_value=([], [], [], False, []))
        def request(path, method='GET', data=None):
            if path == 'pulls/2':
                return self.pr
            if path == 'issues/1':
                return self.goal
            if path.startswith('git/ref/'):
                raise urllib.error.HTTPError('https://example.test', 404, 'Missing', {}, None)
            if path == 'pulls' and method == 'POST':
                return self.pr
            return {}
        self.loops.gh.request.side_effect = request
        self.loops.gh.pages.side_effect = lambda path: [self.goal] if path.startswith('issues?state=open') else []

    def tearDown(self):
        self.loops.state.db.close()
        self.tmp.cleanup()

    def current(self):
        return self.loops.state.db.execute('SELECT state FROM workflow WHERE issue=1').fetchone()[0]

    def test_initial_clarification_does_not_publish_partial_code_or_repeat_inference(self):
        self.loops.agent.return_value = {'outcome': 'needs_clarification', 'summary': 'Which file format?'}
        self.loops.implement()
        self.assertEqual(self.current(), 'needs_clarification')
        self.loops.publish.assert_not_called()
        self.loops.implement()
        self.loops.agent.assert_called_once()
        self.goal['body'] = 'Use JSON'
        self.loops.agent.return_value = {'outcome': 'completed', 'summary': 'Implemented JSON.'}
        self.loops.implement()
        self.loops.publish.assert_called_once()
        self.assertEqual(self.current(), 'waiting_for_ci')
        self.assertEqual(self.loops.state.db.execute('SELECT * FROM jobs').fetchall(), [(1, 2)])

    def test_repair_dispute_blocks_review_and_merge_until_explicit_resume(self):
        self.loops.state.link(1, 2)
        failure = {'id': 9, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}
        self.loops.snapshot.return_value = ([], [failure], [], False, [failure])
        self.loops.agent.return_value = {'outcome': 'needs_human_decision', 'summary': 'CI environment needs credentials.'}
        self.loops.implement()
        self.assertEqual(self.current(), 'needs_human_decision')
        self.loops.publish.assert_not_called()
        self.loops.gh.request.reset_mock()
        self.loops.review()
        with patch.dict(os.environ, {'AUTO_MERGE': 'true'}):
            self.loops.merge()
        self.loops.gh.request.assert_not_called()
        self.pr['labels'] = [{'name': 'agent:resume'}]
        self.loops.check_resume(1, 2, self.goal, self.pr)
        self.assertFalse(self.loops.held(1))
        self.loops.gh.request.assert_called_with('issues/2/labels/agent%3Aresume', 'DELETE')

    def test_exhausted_attempts_are_visible_and_do_not_repeat_model_jobs(self):
        self.loops.agent.side_effect = RuntimeError('Private diagnostics and secrets must stay off GitHub')
        for expected in ['retry_pending', 'retry_pending', 'retry_exhausted']:
            with self.assertRaises(RuntimeError):
                self.loops.implement()
            self.assertEqual(self.current(), expected)
        self.loops.implement()
        self.assertEqual(self.loops.agent.call_count, 3)
        self.assertEqual(self.current(), 'retry_exhausted')
        self.assertNotIn('secrets', json.dumps(self.loops.state.workflow.history(1)))

    def test_publishing_failure_is_tracked_as_a_bounded_retry(self):
        self.loops.publish.side_effect = RuntimeError('push failed')
        with self.assertRaises(RuntimeError):
            self.loops.implement()
        self.assertEqual(self.current(), 'retry_pending')

    def test_terminal_and_paused_prs_are_observed_without_model_calls(self):
        self.loops.state.link(1, 2)
        self.pr['labels'] = [{'name': 'agent:paused'}]
        self.loops.implement()
        self.assertEqual(self.current(), 'paused')
        self.pr.update(state='closed', merged=True)
        self.loops.implement()
        self.assertEqual(self.current(), 'merged')
        self.loops.agent.assert_not_called()

    def test_unqueued_initial_goal_is_visible_as_paused(self):
        self.loops.state.workflow.transition(1, None, 'retry_pending', 'Prior failure', 'implement', 'Retry')
        self.goal['labels'] = []
        self.loops.gh.pages.side_effect = lambda path: []
        self.loops.implement()
        self.assertEqual(self.current(), 'paused')
        self.loops.agent.assert_not_called()

    def test_queued_but_paused_goal_does_not_churn_history(self):
        self.goal['labels'].append({'name': 'agent:paused'})
        self.loops.implement()
        events = len(self.loops.state.workflow.history(1))
        self.loops.implement()
        self.assertEqual(self.current(), 'paused')
        self.assertEqual(len(self.loops.state.workflow.history(1)), events)
        self.loops.agent.assert_not_called()

    def test_ci_and_review_waits_and_manual_merge_have_distinct_owners(self):
        self.loops.state.link(1, 2)
        self.loops.implement()
        self.assertEqual(self.current(), 'waiting_for_ci')
        self.loops.snapshot.return_value = ([], [], [], True, [])
        self.loops.implement()
        self.assertEqual(self.current(), 'waiting_for_review')
        approval = {'id': 1, 'state': 'APPROVED', 'commit_id': 'head', 'user': {'login': 'review-bot'},
                    'body': review_marker('head', 'base')}
        self.loops.snapshot.return_value = ([approval], [], [], True, [])
        with patch.dict(os.environ, {'AUTO_MERGE': 'false'}):
            self.loops.implement()
        self.assertEqual(self.current(), 'ready_to_merge')
        self.assertEqual(self.loops.state.db.execute('SELECT owner FROM workflow').fetchone()[0], 'maintainer')

    def test_exhausted_review_remains_visible_across_controller_polls(self):
        self.loops.state.link(1, 2)
        for _ in range(3):
            self.loops.state.claim('review:2:head:base', 3)
        self.loops.implement()
        self.assertEqual(self.current(), 'retry_exhausted')
        events = len(self.loops.state.workflow.history(1))
        self.loops.implement()
        self.assertEqual(len(self.loops.state.workflow.history(1)), events)
        self.loops.agent.assert_not_called()

    def test_failed_status_publication_cannot_interrupt_work(self):
        self.loops.sync_progress = Loops.sync_progress.__get__(self.loops)
        with patch.object(self.loops.state.workflow, 'sync', side_effect=RuntimeError('private error')):
            self.loops.implement()
        self.loops.publish.assert_called_once()
        self.assertEqual(self.current(), 'waiting_for_ci')


class ImplementationOutput(unittest.TestCase):
    def test_structured_stop_outcomes_and_invalid_outputs(self):
        loops = object.__new__(Loops)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('loop.agent_auth', return_value=([], {})), \
                 patch('loop.subprocess.run', return_value=Mock(returncode=0)) as run:
                for outcome in ['completed', 'needs_clarification', 'needs_human_decision']:
                    (root / 'result.json').write_text(json.dumps({'outcome': outcome, 'summary': 'Details'}))
                    self.assertEqual(loops._agent(root, root, 'Goal')['outcome'], outcome)
                    command = run.call_args_list[-2].args[0]
                    self.assertIn('--output-schema', command)
                    self.assertIn('/out/result.json', command)
                for invalid in [{'outcome': 'completed', 'summary': ''},
                                {'outcome': 'ignore_policy', 'summary': 'Details'},
                                {'outcome': 'completed', 'summary': 'Details', 'extra': True}]:
                    (root / 'result.json').write_text(json.dumps(invalid))
                    with self.assertRaises(ValueError):
                        loops._agent(root, root, 'Goal')


if __name__ == '__main__':
    unittest.main()
