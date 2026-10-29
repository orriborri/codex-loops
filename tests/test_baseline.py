from pathlib import Path
import os
import sqlite3
import subprocess
from unittest.mock import Mock, patch
import tempfile
import unittest
from baseline import BaselineChecks, parse_junit, compare_tests


class BaselineTests(unittest.TestCase):
    def parse(self, cases, code=1):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'tests.xml'
            path.write_text('<testsuite name="suite">' + cases + '</testsuite>')
            return parse_junit(Path(root), code)

    def test_junit_failure_identity_uses_suite_class_and_test(self):
        result = self.parse('<testcase classname="Export" name="retry"><failure>bad</failure></testcase>')
        self.assertEqual(result['failures'], ['suite::Export::retry'])
        self.assertEqual(result['status'], 'failed')

    def test_missing_invalid_or_unsafe_reports_are_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            self.assertEqual(parse_junit(folder, 1)['status'], 'unknown')
            (folder / 'tests.xml').write_text('invalid')
            self.assertEqual(parse_junit(folder, 1)['status'], 'unknown')
            (folder / 'tests.xml').unlink()
            (folder / 'tests.xml').symlink_to('/etc/passwd')
            self.assertEqual(parse_junit(folder, 1)['status'], 'unknown')

    def test_nonzero_exit_without_identified_failures_is_unknown(self):
        result = self.parse('<testcase name="ok"/>')
        self.assertEqual(result['status'], 'unknown')

    def test_duplicate_case_ids_and_empty_suites_are_unknown(self):
        self.assertEqual(self.parse('<testcase name="x"/><testcase name="x"><failure/></testcase>')['status'], 'unknown')
        self.assertEqual(self.parse('', 0)['status'], 'unknown')

    def test_suite_error_counts_cannot_hide_unidentified_failures(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / 'tests.xml').write_text('<testsuite errors="1"><testcase name="ok"/></testsuite>')
            self.assertEqual(parse_junit(folder, 1)['status'], 'unknown')

    def test_truncated_or_inconsistent_suite_and_aggregate_counts_are_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            for xml in [
                '<testsuite tests="3" failures="1"><testcase name="x"><failure/></testcase></testsuite>',
                '<testsuite tests="1" skipped="1"><testcase name="x"><failure/></testcase></testsuite>',
                '<testsuites tests="2"><testsuite tests="1"><testcase name="x"><failure/></testcase></testsuite></testsuites>',
            ]:
                (folder / 'tests.xml').write_text(xml)
                self.assertEqual(parse_junit(folder, 1)['status'], 'unknown', xml)

    def test_same_failures_are_baseline_only(self):
        base = self.parse('<testcase name="old"><failure/></testcase>')
        head = self.parse('<testcase name="old"><failure/></testcase><testcase name="new"/>')
        report = compare_tests(head, base)
        self.assertEqual(report['classification'], 'baseline_only')
        self.assertEqual(report['regressions'], [])

    def test_extra_failures_are_regressions_even_if_base_is_broken(self):
        base = self.parse('<testcase name="old"><failure/></testcase><testcase name="new"/>')
        head = self.parse('<testcase name="old"><failure/></testcase><testcase name="new"><failure/></testcase>')
        report = compare_tests(head, base)
        self.assertEqual(report['classification'], 'regression')
        self.assertEqual(report['regressions'], ['suite::::new'])
        self.assertEqual(report['baseline_failures'], ['suite::::old'])

    def test_removed_or_skipped_base_cases_cannot_prove_baseline_only(self):
        base = self.parse('<testcase name="old"><failure/></testcase><testcase name="required"/>')
        head = self.parse('<testcase name="old"><failure/></testcase>')
        self.assertEqual(compare_tests(head, base)['classification'], 'unknown')
        head = self.parse('<testcase name="old"><failure/></testcase><testcase name="required"><skipped/></testcase>')
        self.assertEqual(compare_tests(head, base)['classification'], 'unknown')

    def test_passing_head_does_not_explain_reported_ci_failure(self):
        passed = self.parse('<testcase name="ok"/>', 0)
        self.assertEqual(compare_tests(passed, passed)['classification'], 'not_reproduced')


class BaselineExecution(unittest.TestCase):
    def test_reproduction_job_has_no_credential_mounts_or_environment(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            source, output = folder / 'source', folder / 'output'
            source.mkdir(); output.mkdir()
            def run(args, **kwargs):
                if args[1] == 'run':
                    mounts = [args[i+1] for i, value in enumerate(args) if value == '--mount']
                    result_dir = Path(next(m for m in mounts if 'dst=/out' in m).split(',')[1].removeprefix('src='))
                    (result_dir / 'tests.xml').write_text('<testsuite tests="1"><testcase name="ok"/></testsuite>')
                    self.assertEqual([args[i+1] for i, value in enumerate(args) if value == '--env'], ['HOME=/tmp/home'])
                    self.assertEqual(len(mounts), 2)
                    self.assertNotIn('--env-file', args)
                    self.assertNotIn('env', kwargs)
                    self.assertEqual(kwargs['timeout'], 25)
                return subprocess.CompletedProcess(args, 0)
            with patch.dict(os.environ, {'BASELINE_TIMEOUT_SECONDS': '25', 'GITHUB_TOKEN': 'not-forwarded'}), patch('baseline.subprocess.run', side_effect=run), patch('baseline.os.chown'):
                self.assertEqual(BaselineChecks.run(source, output, 'project-tests', 'image:pinned')['status'], 'passed')

    def test_exact_pair_cache_bounds_failed_or_unknown_reproduction(self):
        db = sqlite3.connect(':memory:')
        try:
            checks = BaselineChecks(db)
            checks.run = Mock(return_value={'status': 'unknown', 'tests': [], 'failures': []})
            loops = Mock(); loops.workspace.return_value = (None, None, 'base-source', 'base-output')
            pr = {'head': {'sha': 'head'}, 'base': {'sha': 'base'}}
            with patch.dict(os.environ, {'BASELINE_TEST_COMMAND': 'trusted-tests', 'AGENT_IMAGE': 'image:pinned'}):
                first = checks.compare(loops, pr, 'head-source', 'head-output')
                self.assertEqual(checks.compare(loops, pr, 'head-source', 'head-output'), first)
            self.assertEqual(checks.run.call_count, 2)
            loops.workspace.assert_called_once_with('base')
            self.assertEqual(checks.run.call_args_list[0].args[:2], ('head-source', 'head-output'))
        finally: db.close()


if __name__ == '__main__': unittest.main()
