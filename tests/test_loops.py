import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from loop import (GitHub, Loops, State, agent_auth, agent_model, agent_schema,
                  check_gate, parse_agent_output, review_gate, review_marker)


def review(user='review-bot', state='APPROVED', sha='head', id=1, base='base'):
    return {'id': id, 'user': {'login': user}, 'state': state, 'commit_id': sha,
            'body': 'Review findings\n' + review_marker(sha, base)}


def check(name='test', conclusion='success', status='completed'):
    return {'name': name, 'status': status, 'conclusion': conclusion, 'id': 1}


class Gates(unittest.TestCase):
    def test_approval_must_cover_current_base_when_merging(self):
        self.assertTrue(review_gate([review()], 'head', 'review-bot', 'base'))
        self.assertFalse(review_gate([review()], 'head', 'review-bot', 'new-base'))
        legacy = review()
        legacy['body'] = 'Approved before base tracking existed'
        self.assertFalse(review_gate([legacy], 'head', 'review-bot', 'base'))

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


class AgentOutputs(unittest.TestCase):
    def test_all_job_roles_use_closed_typed_output_contracts(self):
        cases = [({}, {'outcome': 'completed', 'summary': 'Tested'}),
                 ({'review': True}, {'decision': 'APPROVE', 'summary': 'Reviewed'}),
                 ({'review': True, 'improve': True}, {'needed': False, 'title': '', 'body': ''})]
        for options, data in cases:
            with self.subTest(options=options):
                schema = agent_schema(**options)
                self.assertEqual(parse_agent_output(json.dumps(data), schema), data)
                for invalid in [dict(data, extra=True), {}, [], None]:
                    with self.assertRaises(ValueError):
                        parse_agent_output(json.dumps(invalid), schema)

    def test_invalid_decisions_types_and_empty_summaries_are_rejected(self):
        cases = [({}, {'outcome': 'bypass', 'summary': 'Tested'}),
                 ({'review': True}, {'decision': 'APPROVE', 'summary': '  '}),
                 ({'review': True}, {'decision': 'APPROVE', 'summary': 1}),
                 ({'improve': True}, {'needed': 1, 'title': '', 'body': ''})]
        for options, data in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                parse_agent_output(json.dumps(data), agent_schema(**options))


class Authentication(unittest.TestCase):
    def test_codex_mode_uses_persistent_volume_without_api_key(self):
        with patch.dict(os.environ, {'AUTH_MODE': 'codex', 'CODEX_AUTH_VOLUME': 'runner-auth',
                                      'OPENAI_API_KEY': 'fake-secret', 'CODEX_API_KEY': 'stale-secret'}):
            args, env = agent_auth()
        self.assertIn('type=volume,src=runner-auth,dst=/home/node/.codex', args)
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('fake-secret', ' '.join(args))

    def test_api_mode_only_passes_key_in_environment(self):
        with patch.dict(os.environ, {'AUTH_MODE': 'api', 'OPENAI_API_KEY': 'fake-secret'}):
            args, env = agent_auth()
        self.assertEqual(env['CODEX_API_KEY'], 'fake-secret')
        self.assertNotIn('fake-secret', ' '.join(args))
        self.assertFalse(any('type=volume' in x for x in args))

    def test_api_mode_never_falls_back_to_saved_login(self):
        with patch.dict(os.environ, {'AUTH_MODE': 'api', 'OPENAI_API_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'requires OPENAI_API_KEY'):
                agent_auth()

    def test_invalid_auth_mode_rejected(self):
        with patch.dict(os.environ, {'AUTH_MODE': 'typo'}):
            with self.assertRaisesRegex(ValueError, 'AUTH_MODE must be'):
                agent_auth()

    def test_mount_injection_rejected(self):
        with patch.dict(os.environ, {'AUTH_MODE': 'codex', 'CODEX_AUTH_VOLUME': '/host,dst=/escape'}):
            with self.assertRaisesRegex(ValueError, 'named volume'):
                agent_auth()

    def test_default_auth_uses_codex_without_api_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            args, env = agent_auth()
        self.assertIn('type=volume,src=codex-loops-auth,dst=/home/node/.codex', args)
        self.assertNotIn('CODEX_API_KEY', env)


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

    def test_mergeability_changed_during_checks_blocks_merge(self):
        changed = dict(self.pr, mergeable_state='blocked')
        self.loops.gh.request.side_effect = [self.pr, changed]
        self.loops.merge()
        self.assertEqual(self.loops.gh.request.call_count, 2)

    def test_old_base_approval_blocks_merge(self):
        self.loops.snapshot.return_value = ([review(base='old-base')], [check()], [], True, [])
        self.loops.merge()
        self.assertEqual(self.loops.gh.request.call_count, 1)

    def test_missing_required_checks_configuration_rejected(self):
        self.loops.required = []
        with self.assertRaisesRegex(RuntimeError, 'REQUIRED_CHECKS'):
            self.loops.merge()
        self.loops.gh.request.assert_not_called()


class ReviewController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.loops = object.__new__(Loops)
        self.loops.state = State(self.root)
        self.loops.state.link(1, 2)
        self.loops.repo, self.loops.base, self.loops.reviewer = 'owner/repo', 'main', 'review-bot'
        self.pr = {'number': 2, 'state': 'open', 'draft': False, 'labels': [],
                   'user': {'login': 'author'},
                   'head': {'sha': 'head', 'ref': 'agent/issue-1', 'repo': {'full_name': 'owner/repo'}},
                   'base': {'sha': 'base', 'ref': 'main'}}
        self.goal = {'title': 'Fix the bug', 'body': 'Expected behavior', 'unneeded_metadata': 'omit-me'}
        self.loops.gh = Mock()
        self.loops.gh.request.side_effect = [self.pr, self.goal, self.pr, {}]
        self.loops.gh.pages.side_effect = [[], [{'filename': 'new.py', 'previous_filename': 'old.py',
             'status': 'renamed', 'patch': 'large-patch-' * 10000, 'additions': 1, 'deletions': 1}]]
        self.loops.workspace = Mock(return_value=(self.root,) * 4)
        self.loops.git = Mock()
        self.loops.agent = Mock(return_value={'decision': 'APPROVE', 'summary': 'Looks correct'})
        self.archive = patch('loop.tarfile.open')
        self.archive.start()

    def tearDown(self):
        self.archive.stop()
        self.loops.state.db.close()
        self.tmp.cleanup()

    def test_review_reads_source_instead_of_duplicating_patches(self):
        self.loops.review()
        prompt = self.loops.agent.call_args.args[2]
        self.assertIn('new.py', prompt)
        self.assertIn('old.py', prompt)
        self.assertIn('Expected behavior', prompt)
        self.assertNotIn('large-patch', prompt)
        self.assertNotIn('omit-me', prompt)
        self.assertTrue(self.loops.agent.call_args.kwargs['review'])
        payload = self.loops.gh.request.call_args.args[2]
        self.assertEqual(payload['commit_id'], 'head')
        self.assertIn(review_marker('head', 'base'), payload['body'])

    def test_current_review_skips_inference(self):
        self.loops.gh.pages.side_effect = [[review()]]
        self.loops.review()
        self.loops.agent.assert_not_called()

    def test_base_change_triggers_new_review(self):
        self.loops.gh.pages.side_effect = [[review(base='old-base')], []]
        self.loops.review()
        self.loops.agent.assert_called_once()

    def test_dismissed_review_triggers_new_review(self):
        self.loops.gh.pages.side_effect = [[review(), review(state='DISMISSED', id=2)], []]
        self.loops.review()
        self.loops.agent.assert_called_once()

    def test_base_change_during_inference_discards_verdict(self):
        changed = json.loads(json.dumps(self.pr))
        changed['base']['sha'] = 'new-base'
        self.loops.gh.request.side_effect = [self.pr, self.goal, changed]
        self.loops.review()
        self.assertEqual(self.loops.gh.request.call_count, 3)
        self.assertTrue(all(call.args[0] != 'pulls/2/reviews'
                            for call in self.loops.gh.request.call_args_list))


class JobMetrics(unittest.TestCase):
    def test_job_success_failure_and_models_survive_restart_without_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loops = object.__new__(Loops)
            loops.state = State(root)
            loops._agent = Mock(side_effect=['done', RuntimeError('job failed')])
            with patch.dict(os.environ, {'IMPLEMENT_MODEL': 'coder', 'REVIEW_MODEL': 'critic'}):
                self.assertEqual(loops.agent(root, root, 'private implementation prompt'), 'done')
                with self.assertRaisesRegex(RuntimeError, 'job failed'):
                    loops.agent(root, root, 'private review prompt', review=True)
            loops.state.db.close()
            state = State(root)
            metrics = {row['role']: row for row in state.metrics()}
            self.assertEqual(metrics['implement']['model'], 'coder')
            self.assertEqual(metrics['implement']['failures'], 0)
            self.assertEqual(metrics['review']['model'], 'critic')
            self.assertEqual(metrics['review']['failures'], 1)
            self.assertEqual(metrics['review']['avg_prompt_bytes'], len('private review prompt'))
            self.assertNotIn('private', json.dumps(metrics))
            state.db.close()

    def test_improvement_can_use_its_own_model_and_falls_back_to_reviewer(self):
        with patch.dict(os.environ, {'IMPLEMENT_MODEL': 'coder', 'REVIEW_MODEL': 'critic',
                                     'IMPROVE_MODEL': 'cheap'}):
            self.assertEqual(agent_model(), 'coder')
            self.assertEqual(agent_model(review=True), 'critic')
            self.assertEqual(agent_model(review=True, improve=True), 'cheap')
            with patch.dict(os.environ, {'IMPROVE_MODEL': ''}):
                self.assertEqual(agent_model(review=True, improve=True), 'critic')


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
        self.loops.snapshot = Mock(return_value=([review(state='CHANGES_REQUESTED')], [], [], True, []))
        self.loops.workspace = Mock(return_value=(Path(self.tmp.name),) * 4)
        self.loops.agent = Mock(return_value={'needed': True, 'title': 'Add regression test',
                                             'body': 'Evidence: PR #2. Acceptance: reproduce and test.'})

    def tearDown(self):
        self.loops.state.db.close()
        self.tmp.cleanup()

    def test_no_history_does_not_invent_improvements(self):
        self.loops.improve()
        self.loops.agent.assert_not_called()

    def test_clean_history_does_not_spend_a_model_call(self):
        self.loops.state.link(1, 2)
        self.loops.snapshot.return_value = ([review()], [check()], [], True, [])
        self.loops.improve()
        self.loops.agent.assert_not_called()

    def test_improvement_receives_recent_job_metrics(self):
        self.loops.state.link(1, 2)
        self.loops.improve()
        self.assertIn('Recent job metrics', self.loops.agent.call_args.args[2])

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
