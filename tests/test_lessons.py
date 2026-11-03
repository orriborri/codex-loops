import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from lessons import advisory, recall
from support import goal


def lesson(**changes):
    row = {'id': 'retry', 'repository': 'owner/repo', 'approved': True, 'keywords': ['retry'],
           'lesson': 'Keep retry identifiers stable.', 'evidence': {'type': 'pull', 'number': 12},
           'reviewed': '2026-01-01', 'expires': '2027-01-01'}
    row.update(changes)
    return row


class LessonRecall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.today = date(2026, 11, 1)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows):
        (self.root / 'lessons.json').write_text(json.dumps({'version': 1, 'lessons': rows}))

    def read(self):
        return recall(self.root, 'owner/repo', goal(), True, self.today)

    def test_disabled_does_not_access_file(self):
        with patch('lessons._read_file', side_effect=AssertionError('read while disabled')):
            self.assertEqual(recall(self.root, 'owner/repo', goal()), [])

    def test_only_approved_current_same_repository_matching_notes_are_selected(self):
        self.write([lesson(), lesson(id='unapproved', approved=False),
                    lesson(id='wrongrepo', repository='another/repo'),
                    lesson(id='expired', expires='2026-10-31'),
                    lesson(id='future', reviewed='2026-12-01'),
                    lesson(id='unrelated', keywords=['payment'])])
        result = self.read()
        self.assertEqual([r['id'] for r in result], ['retry'])
        self.assertEqual(result[0]['source'], 'https://github.com/owner/repo/pull/12')
        self.assertEqual(result[0]['reviewed'], '2026-01-01')

    def test_matching_is_whole_keyword_and_limits_are_deterministic(self):
        self.write([lesson(id=f'item{i}', lesson='x' * 400) for i in range(5)])
        result = self.read()
        self.assertLessEqual(len(result), 3)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True)), 1500)
        self.assertEqual([r['id'] for r in result], ['item0', 'item1'])
        self.write([lesson(keywords=['retr'])])
        self.assertEqual(self.read(), [])

    def test_rejects_invalid_schema_unapproved_fields_and_duplicate_ids(self):
        self.write([lesson(evidence={'type': 'url', 'number': 1}),
                    lesson(id='first'), lesson(id='first', lesson='duplicate'),
                    lesson(id='unknown', extra='secret')])
        self.assertEqual([r['id'] for r in self.read()], ['first'])

    def test_bad_missing_symlink_and_oversized_files_have_no_effect(self):
        path = self.root / 'lessons.json'
        self.assertEqual(self.read(), [])
        path.write_text('{invalid'); self.assertEqual(self.read(), [])
        path.write_text('x' * (64 * 1024 + 1)); self.assertEqual(self.read(), [])
        path.unlink(); path.symlink_to('/etc/passwd')
        self.assertEqual(self.read(), [])

    def test_prompt_frames_notes_as_untrusted(self):
        self.write([lesson(reviewed='2020-01-01', expires='2099-01-01')])
        with patch.dict(os.environ, {'LESSONS_ENABLED': 'true'}):
            rendered = advisory(self.root, 'owner/repo', goal())
        self.assertIn('UNTRUSTED', rendered)
        self.assertIn('review or merge gates', rendered)
        self.assertIn('https://github.com/owner/repo/pull/12', rendered)


if __name__ == '__main__':
    unittest.main()
