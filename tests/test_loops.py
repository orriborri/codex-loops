import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from loop import GitHub, Loops, State, check_gate, review_gate


def review(user='review-bot', state='APPROVED', sha='head', id=1):
    return {'id': id, 'user': {'login': user}, 'state': state, 'commit_id': sha}


def check(name='test', conclusion='success', status='completed'):
    return {'name': name, 'status': status, 'conclusion': conclusion, 'id': 1}


class Gates(unittest.TestCase):
    def test_requires_explicit_current_approval(self):
        self.assertFalse(review_gate([], 'head', 'review-bot'))
        self.assertFalse(review_gate([review(sha='old')], 'head', 'review-bot'))
        self.assertFalse(review_gate([review(user='someone')], 'head', 'review-bot'))
        self.assertTrue(review_gate([review()], 'head', 'review-bot'))

    def test_human_change_request_blocks_even_if_on_old_commit(self):
        self.assertFalse(review_gate([review(), review('human', 'CHANGES_REQUESTED', 'old', 2)], 'head', 'review-bot'))

    def test_comments_do_not_clear_decisions(self):
        self.assertFalse(review_gate([review(state='CHANGES_REQUESTED'), review(state='COMMENT', id=2)], 'head', 'review-bot'))

    def test_approval_supersedes_change_request(self):
        self.assertTrue(review_gate([review(state='CHANGES_REQUESTED', sha='old'), review(id=2)], 'head', 'review-bot'))

    def test_dismissal_removes_approval(self):
        self.assertFalse(review_gate([review(), review(state='DISMISSED', id=2)], 'head', 'review-bot'))

    def test_no_checks_or_missing_required_checks_block(self):
        self.assertFalse(check_gate([], [], ['test'])[0])
        self.assertFalse(check_gate([check()], [], ['test', 'lint'])[0])
        self.assertFalse(check_gate([check()], [], [])[0])

    def test_pending_skipped_and_failed_checks_block(self):
        for conclusion, status in [(None, 'in_progress'), ('skipped', 'completed'),
                                   ('neutral', 'completed'), ('failure', 'completed')]:
            self.assertFalse(check_gate([check(conclusion=conclusion, status=status)], [], ['test'])[0])

    def test_optional_failures_and_pending_also_block(self):
        self.assertFalse(check_gate([check(), check('optional', 'failure')], [], ['test'])[0])
        self.assertFalse(check_gate([check(), check('optional', None, 'queued')], [], ['test'])[0])

    def test_successful_status_rerun_supersedes_failure(self):
        statuses = [{'id': 2, 'context': 'external', 'state': 'success'},
                    {'id': 1, 'context': 'external', 'state': 'failure'}]
        self.assertEqual(check_gate([check()], statuses, ['test', 'external']), (True, []))

    def test_duplicate_name_does_not_hide_failure(self):
        self.assertFalse(check_gate([check(), check(conclusion='failure')], [], ['test'])[0])

    def test_pagination_is_complete(self):
        gh = GitHub('a/b', 'token')
        pages = [[{'id': x} for x in range(100)], [{'id': 100}]]
        with patch.object(gh, 'request', side_effect=pages) as request:
            self.assertEqual(len(gh.pages('pulls?state=all')), 101)
            self.assertIn('&per_page=100&page=2', request.call_args.args[0])

    def test_attempts_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory))
            self.assertTrue(state.claim('job', 1))
            state.db.close()
            state = State(Path(directory))
            self.assertFalse(state.claim('job', 1))
            state.db.close()

    def test_publish_uses_agent_files_with_trusted_git_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, control, source = root / 'remote', root / 'control', root / 'source'
            subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
            subprocess.run(['git', 'clone', str(remote), str(control)], check=True, capture_output=True)
            (control / 'app.txt').write_text('before')
            for args in [['add', '.'], ['-c', 'user.name=T', '-c', 'user.email=t@x', 'commit', '-m', 'base']]:
                subprocess.run(['git', *args], cwd=control, check=True, capture_output=True)
            source.mkdir()
            (source / 'app.txt').write_text('after')
            loops = object.__new__(Loops)
            # Local fake git remote for integration: production rejects file transport.
            original = loops.git
            loops.git = lambda repo, *args, auth=False: original(repo, '-c', 'protocol.file.allow=always', *args)
            loops.publish(control, source, 'agent/issue-1')
            value = subprocess.check_output(['git', '--git-dir', str(remote), 'show', 'agent/issue-1:app.txt'], text=True)
            self.assertEqual(value, 'after')
            with self.assertRaises(RuntimeError):
                loops.publish(control, source, 'agent/issue-1')

    def test_protected_workflow_cannot_be_published(self):
        loops = object.__new__(Loops)
        loops.git = lambda *args, **kwargs: '.github/workflows/ci.yml' if 'diff' in args else ''
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'Protected'):
                loops.publish(Path(directory), Path(directory), 'agent/issue-1')


class MergeController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.loops = object.__new__(Loops)
        self.loops.state = State(Path(self.tmp.name))
        self.loops.state.link(1, 2)
        self.loops.repo = 'owner/repo'
        self.loops.base = 'main'
        self.loops.required = ['test']
        self.loops.reviewer = 'review-bot'
        self.pr = {'number': 2, 'state': 'open', 'draft': False, 'labels': [],
                   'head': {'sha': 'head', 'ref': 'agent/issue-1', 'repo': {'full_name': 'owner/repo'}},
                   'base': {'sha': 'base', 'ref': 'main'}, 'mergeable': True, 'mergeable_state': 'clean'}
        from unittest.mock import Mock
        self.loops.gh = Mock()
        self.loops.gh.request.side_effect = [self.pr, self.pr, {'merged': True}]
        self.loops.snapshot = Mock(return_value=([review()], [check()], [], True, []))
        self.env = patch.dict(os.environ, {'AUTO_MERGE': 'true'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.loops.state.db.close()
        self.tmp.cleanup()

    def test_merge_is_bound_to_reviewed_sha(self):
        self.loops.merge()
        self.loops.gh.request.assert_called_with('pulls/2/merge', 'PUT', {'sha': 'head', 'merge_method': 'squash'})

    def test_new_head_during_checks_blocks_merge(self):
        changed = json.loads(json.dumps(self.pr))
        changed['head']['sha'] = 'new'
        self.loops.gh.request.side_effect = [self.pr, changed]
        self.loops.merge()
        self.assertEqual(self.loops.gh.request.call_count, 2)

    def test_base_moved_during_checks_blocks_merge(self):
        changed = json.loads(json.dumps(self.pr))
        changed['base']['sha'] = 'new-base'
        self.loops.gh.request.side_effect = [self.pr, changed]
        self.loops.merge()
        self.assertEqual(self.loops.gh.request.call_count, 2)

    def test_no_approval_or_failed_ci_never_calls_merge(self):
        for reviews, green in [([], True), ([review()], False)]:
            self.loops.gh.request.reset_mock(side_effect=True)
            self.loops.gh.request.side_effect = [self.pr]
            self.loops.snapshot.return_value = (reviews, [], [], green, [])
            self.loops.merge()
            self.assertEqual(self.loops.gh.request.call_count, 1)

    def test_paused_pr_blocks_merge(self):
        self.pr['labels'] = [{'name': 'agent:paused'}]
        self.loops.merge()
        self.assertEqual(self.loops.gh.request.call_count, 1)
        self.loops.snapshot.assert_not_called()

    def test_disabled_merge_has_no_side_effects(self):
        with patch.dict(os.environ, {'AUTO_MERGE': 'false'}):
            self.loops.merge()
        self.loops.gh.request.assert_not_called()

    def test_missing_required_checks_configuration_rejected(self):
        self.loops.required = []
        with self.assertRaisesRegex(RuntimeError, 'REQUIRED_CHECKS'):
            self.loops.merge()
        self.loops.gh.request.assert_not_called()


class ImprovementController(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.tmp = tempfile.TemporaryDirectory()
        self.loops = object.__new__(Loops)
        self.loops.state = State(Path(self.tmp.name))
        self.loops.base = 'main'
        self.loops.gh = Mock()
        self.loops.gh.pages.return_value = []
        self.loops.gh.request.side_effect = [
            {'html_url': 'https://github.com/o/r/pull/2', 'state': 'closed', 'merged': True},
            {'number': 9}]
        self.loops.snapshot = Mock(return_value=([], [], [], True, []))
        self.loops.workspace = Mock(return_value=(Path(self.tmp.name),) * 4)
        self.loops.agent = Mock(return_value={'needed': True, 'title': 'Add regression test',
                                             'body': 'Evidence: PR #2. Acceptance: reproduce and test.'})

    def tearDown(self):
        self.loops.state.db.close()
        self.tmp.cleanup()

    def test_no_history_does_not_invent_improvements(self):
        self.loops.improve()
        self.loops.agent.assert_not_called()

    def test_proposal_once_per_day_and_not_automatically_queued(self):
        self.loops.state.link(1, 2)
        with patch.dict(os.environ, {'AUTO_QUEUE_IMPROVEMENTS': 'false'}):
            self.loops.improve()
            self.loops.improve()
        self.loops.agent.assert_called_once()
        payload = self.loops.gh.request.call_args.args[2]
        self.assertEqual(payload['labels'], ['agent:improvement', 'agent:proposal'])
        self.assertIn('codex-improvement:', payload['body'])

    def test_auto_queue_requires_configuration(self):
        self.loops.state.link(1, 2)
        with patch.dict(os.environ, {'AUTO_QUEUE_IMPROVEMENTS': 'true'}):
            self.loops.improve()
        self.assertEqual(self.loops.gh.request.call_args.args[2]['labels'],
                         ['agent:improvement', 'agent:ready'])

    def test_model_can_decline_unjustified_proposal(self):
        self.loops.state.link(1, 2)
        self.loops.agent.return_value = {'needed': False, 'title': '', 'body': ''}
        self.loops.improve()
        self.assertEqual(self.loops.gh.request.call_count, 1)
        self.assertEqual(self.loops.state.db.execute('SELECT issue FROM improvements').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
