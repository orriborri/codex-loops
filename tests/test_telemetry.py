from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import uuid

from loop import Loops, State
from telemetry import Telemetry, uuid7


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'OPIK_ENABLED': 'true',
            'OPIK_URL_OVERRIDE': 'http://opik:5173/api', 'OPIK_PROJECT_NAME': 'test-project'}, clear=True)
        self.env.start()
        self.state = State(Path(self.tmp.name))
        self.telemetry = self.state.telemetry
        self.telemetry._post = Mock()

    def tearDown(self):
        self.state.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def job(self, **overrides):
        values = dict(repo='owner/repo', role='implement', model='configured-model',
            prompt='secret goal and source', started=1770000000, duration=2.5,
            outcome='success', result={'outcome': 'completed', 'summary': 'secret result'},
            version='controller-hash')
        values.update(overrides)
        self.telemetry.job(**values)

    def pending(self):
        return self.state.db.execute('SELECT id,payload,scores FROM opik_outbox ORDER BY rowid').fetchall()

    def test_metadata_and_scores_exclude_prompts_findings_and_credentials(self):
        self.state.claim('fix:2', 5)
        self.state.workflow.transition(1, 2, 'repairing', 'private review text', 'implement',
                                       'private next action', 'a' * 40 + ':' + 'b' * 40)
        self.job(task=(1, 2, 'fix:2', 5))
        _, payload, scores = self.pending()[0]
        data = json.loads(payload)
        self.assertNotIn('secret', payload)
        self.assertNotIn('private', payload)
        self.assertEqual(data['thread_id'], 'owner/repo:issue-1')
        self.assertEqual(data['metadata']['attempt'], 1)
        self.assertEqual(data['metadata']['head_sha'], 'a' * 40)
        self.assertEqual(data['metadata']['controller_version'], 'controller-hash')
        self.assertEqual(json.loads(scores), {'execution_success': 1, 'task_completed': 1})
        self.assertEqual(data['output'], {'execution': 'success', 'outcome': 'completed'})

    def test_disabled_is_no_network_and_no_recording(self):
        with patch.dict(os.environ, {'OPIK_ENABLED': 'false'}):
            telemetry = Telemetry(self.state.db)
        telemetry._post = Mock()
        telemetry.job('r', 'review', 'model', 'secret', 0, 1, 'failed', None)
        telemetry.flush('r')
        self.assertEqual(self.pending(), [])
        telemetry._post.assert_not_called()

    def test_full_queue_drops_new_events_without_blocking(self):
        self.telemetry.capacity = 1
        self.job()
        with self.assertLogs('loops.telemetry', level='WARNING'):
            self.job()
        self.assertEqual(len(self.pending()), 1)

    def test_network_failure_survives_restart_and_reuses_trace_id(self):
        self.job()
        original = self.pending()[0][0]
        self.telemetry._post.side_effect = OSError('credential must not be logged')
        with self.assertLogs('loops.telemetry', level='WARNING') as logs:
            self.telemetry.flush('owner/repo')
        self.assertNotIn('credential', ''.join(logs.output))
        self.state.db.close()
        self.state = State(Path(self.tmp.name))
        self.telemetry = self.state.telemetry
        self.telemetry._post = Mock()
        self.telemetry.flush('owner/repo')
        trace = self.telemetry._post.call_args_list[0].args[2]['traces'][0]
        self.assertEqual(trace['id'], original)
        self.assertEqual(self.pending(), [])

    def test_failed_scoring_retains_the_same_full_payload_for_retry(self):
        self.job()
        original = self.pending()[0]
        self.telemetry._post.side_effect = [None, RuntimeError('score write failed')]
        with self.assertLogs('loops.telemetry', level='WARNING'):
            self.telemetry.flush('owner/repo')
        self.assertEqual(self.pending()[0], original)
        self.telemetry._post.side_effect = None
        self.telemetry.flush('owner/repo')
        self.assertEqual(self.pending(), [])

    def test_workflow_observations_are_exported_once_with_job_thread_correlation(self):
        self.state.workflow.transition(1, 2, 'ci_failed', 'private diagnostics', 'implement', 'fix')
        self.state.workflow.transition(1, 2, 'ready_to_merge', 'private approval', 'merge', 'merge')
        self.state.workflow.transition(1, 2, 'merged', 'private response', 'maintainer', 'deploy')
        self.telemetry.flush('owner/repo')
        traces = self.telemetry._post.call_args_list[0].args[2]['traces']
        self.assertEqual([t['name'] for t in traces], ['workflow.ci_failed', 'workflow.ready_to_merge', 'workflow.merged'])
        self.assertTrue(all(t['thread_id'] == 'owner/repo:issue-1' for t in traces))
        self.assertNotIn('private', json.dumps(traces))
        scores = self.telemetry._post.call_args_list[1].args[2]['scores']
        self.assertEqual([(s['name'], s['value']) for s in scores], [('ci_green', 0), ('ci_green', 1), ('merged', 1)])
        self.telemetry._post.reset_mock()
        self.telemetry.flush('owner/repo')
        self.telemetry._post.assert_not_called()

    def test_enabling_does_not_backfill_old_workflow_history(self):
        self.state.db.execute('DROP TABLE opik_cursor')
        self.state.workflow.transition(1, None, 'queued', 'historical private event', 'implement', 'work')
        telemetry = Telemetry(self.state.db)
        telemetry._post = Mock()
        telemetry.flush('owner/repo')
        telemetry._post.assert_not_called()

    def test_uuid7_has_current_timestamp_and_rfc_variant(self):
        value = uuid.UUID(uuid7())
        self.assertEqual(value.version, 7)
        self.assertEqual(value.variant, uuid.RFC_4122)
        ms = value.int >> 80
        self.assertLess(abs(ms / 1000 - datetime.now(timezone.utc).timestamp()), 5)

    def test_review_changes_request_is_recorded_without_findings(self):
        self.job(role='review', result={'decision': 'REQUEST_CHANGES', 'summary': 'secret finding'})
        _, payload, scores = self.pending()[0]
        self.assertEqual(json.loads(payload)['output']['decision'], 'REQUEST_CHANGES')
        self.assertEqual(json.loads(scores)['review_approved'], 0)
        self.assertNotIn('finding', payload)

    def test_invalid_destination_disables_only_telemetry(self):
        with patch.dict(os.environ, {'OPIK_URL_OVERRIDE': 'https://user:secret@host/api'}):
            with self.assertLogs('loops.telemetry', level='WARNING') as logs:
                telemetry = Telemetry(self.state.db)
        self.assertFalse(telemetry.enabled)
        self.assertNotIn('secret', ''.join(logs.output))

    def test_transport_uses_correct_headers_methods_timeout_and_rejects_redirects(self):
        with patch.dict(os.environ, {'OPIK_API_KEY': 'test-key', 'OPIK_WORKSPACE': 'test-workspace'}):
            telemetry = Telemetry(self.state.db)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 204
        telemetry.opener = Mock()
        telemetry.opener.open.return_value = response
        telemetry._post('/v1/private/traces/batch', 'POST', {'traces': []})
        request = telemetry.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://opik:5173/api/v1/private/traces/batch')
        self.assertEqual(request.get_header('Authorization'), 'test-key')
        self.assertEqual(request.get_header('Comet-workspace'), 'test-workspace')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(telemetry.opener.open.call_args.kwargs['timeout'], 2)
        from telemetry import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere'))

    def test_model_wrapper_preserves_result_when_opik_is_unreachable(self):
        loops = object.__new__(Loops)
        loops.state, loops.repo = self.state, 'owner/repo'
        loops._agent = Mock(return_value={'outcome': 'completed', 'summary': 'secret'})
        result = loops.agent(Path(self.tmp.name), Path(self.tmp.name), 'secret task')
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(len(self.pending()), 1)
        self.telemetry._post.side_effect = OSError('offline')
        with self.assertLogs('loops.telemetry', level='WARNING'):
            self.telemetry.flush(loops.repo)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.state.db.execute('SELECT outcome FROM model_jobs').fetchone()[0], 'success')

    def test_model_failure_is_recorded_and_original_exception_preserved(self):
        loops = object.__new__(Loops)
        loops.state, loops.repo = self.state, 'owner/repo'
        loops._agent = Mock(side_effect=RuntimeError('private error'))
        with self.assertRaisesRegex(RuntimeError, 'private error'):
            loops.agent(Path(self.tmp.name), Path(self.tmp.name), 'private task')
        _, payload, scores = self.pending()[0]
        self.assertEqual(json.loads(payload)['output']['execution'], 'failed')
        self.assertEqual(json.loads(scores)['execution_success'], 0)
        self.assertNotIn('private', payload)

    def test_real_http_export_sends_traces_and_scores_then_acknowledges_queue(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append((self.command, self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(204)
                self.end_headers()

            do_PUT = do_POST

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.dict(os.environ, {'OPIK_URL_OVERRIDE': f'http://127.0.0.1:{server.server_port}/api'}):
                telemetry = Telemetry(self.state.db)
            self.job()
            telemetry.flush('owner/repo')
            self.assertEqual([(m, p) for m, p, _ in captured], [
                ('POST', '/api/v1/private/traces/batch'), ('PUT', '/api/v1/private/traces/feedback-scores')])
            trace = captured[0][2]['traces'][0]
            scores = captured[1][2]['scores']
            self.assertTrue(all(s['id'] == trace['id'] and s['project_name'] == 'test-project' for s in scores))
            self.assertEqual(self.pending(), [])
            self.assertNotIn('secret', json.dumps(captured))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_local_queue_failure_cannot_change_successful_job_result(self):
        self.state.db.execute('DROP TABLE opik_outbox')
        loops = object.__new__(Loops)
        loops.state, loops.repo = self.state, 'owner/repo'
        loops._agent = Mock(return_value={'outcome': 'completed', 'summary': 'done'})
        with self.assertLogs('loops.telemetry', level='WARNING'):
            result = loops.agent(Path(self.tmp.name), Path(self.tmp.name), 'task')
        self.assertEqual(result['outcome'], 'completed')

    def test_actual_repair_path_keeps_checked_out_head_and_base_in_job_trace(self):
        loops = object.__new__(Loops)
        loops.state, loops.repo, loops.base, loops.reviewer = self.state, 'owner/repo', 'main', 'review-bot'
        loops.limit, loops.required = 5, ['test']
        root = Path(self.tmp.name)
        loops.workspace = Mock(return_value=(root,) * 4)
        loops.publish, loops.sync_progress = Mock(), Mock()
        loops._agent = Mock(return_value={'outcome': 'completed', 'summary': 'repaired'})
        self.state.link(1, 2)
        pr = {'number': 2, 'state': 'open', 'draft': False, 'labels': [],
              'head': {'sha': 'a' * 40, 'ref': 'agent/issue-1', 'repo': {'full_name': 'owner/repo'}},
              'base': {'sha': 'b' * 40, 'ref': 'main'}, 'mergeable': True}
        goal = {'number': 1, 'state': 'open', 'title': 'Private goal', 'body': '## Acceptance criteria\n- Private details', 'labels': []}
        loops.gh = Mock()
        loops.gh.request.side_effect = lambda path, *args: pr if path == 'pulls/2' else goal
        loops.gh.pages.return_value = []
        failure = {'id': 9, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}
        loops.snapshot = Mock(return_value=([], [failure], [], False, [failure]))
        loops.implement()
        metadata = json.loads(self.pending()[0][1])['metadata']
        self.assertEqual((metadata['issue'], metadata['pr']), (1, 2))
        self.assertEqual((metadata['head_sha'], metadata['base_sha']), ('a' * 40, 'b' * 40))
        self.assertEqual(metadata['attempt'], 1)
        loops.publish.assert_called_once()


if __name__ == '__main__':
    unittest.main()
