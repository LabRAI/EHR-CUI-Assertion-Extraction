from __future__ import annotations

from collections import Counter
import io
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_assertion_scope_overlay as assertion


class AssertionEfficiencyTests(unittest.TestCase):
    def test_occurrences_preserve_regex_and_do_not_share_lists(self):
        cases = [
            ('No S1 murmur; s1 heart sound is normal.', 'S1'),
            ('Continue intermittent\n hemodialysis.', ' intermittent hemodialysis '),
            ('Na+ followed by Na+ then Na++', 'Na+'),
            ('No\u00a0caf\u00e9; CAF\u00c9 present', 'caf\u00e9'),
            ('pneumonia before pneumonia', ''),
        ]
        for turn in range(2):
            if turn:
                for i in range(8200):
                    assertion.target_occurrences('pneumonia', f'evict_{i}')
            for text, span in cases:
                pattern = assertion.flexible_target(span)
                expected = [(m.span(), m.group()) for m in re.finditer(pattern, text, re.I)] if pattern else []
                actual = assertion.target_occurrences(text, span)
                self.assertEqual(expected, [(m.span(), m.group()) for m in actual])
                actual.clear()
                self.assertEqual(expected, [(m.span(), m.group()) for m in assertion.target_occurrences(text, span)])
        self.assertLessEqual(assertion._target_occurrences.cache_info().currsize, 64)
        self.assertLessEqual(assertion.compiled_target.cache_info().currsize, 8192)

    def test_batches_preserve_every_row_duplicate_order_and_summary(self):
        examples = [
            ('pneumonia', 'No pneumonia, but persistent fever.', 'disease_or_diagnosis', 'present'),
            ('hemodialysis', 'Continue hemodialysis; consider CRRT if unstable.', 'procedure', 'conditional'),
            ('CRRT', 'Continue hemodialysis; consider CRRT if unstable.', 'procedure', 'present'),
            ('diabetes', 'Past medical history: diabetes on insulin.', 'disease_or_diagnosis', 'historical'),
            ('cough', 'Possible cough; no fever.', 'finding', 'absent'),
        ]
        rows = []
        for i in range(1031):
            span, evidence, category, old = examples[i % len(examples)]
            rows.append({'span': span, 'evidence': evidence, 'concept_category_preview': category,
                         'assertion_status_preview': old, 'filter_action': 'keep_candidate', 'note_id': str(i // 7)})
        rows.extend(rows[:3])
        expected_rows = [assertion.overlay_row(row)[0] for row in rows]
        self.assertGreater(sum(row['assertion_scope_overlay_changed'] for row in expected_rows), 0)
        self.assertGreaterEqual(len({row['assertion_status_preview'] for row in expected_rows}), 3)
        stream = '\n\n'.join(json.dumps(row) for row in rows) + '\n\n'
        summaries = []
        for workers in (1, 4):
            actual = []
            total = changed = 0
            counts = {key: Counter() for key in ('old', 'new', 'reason', 'change')}
            for block, n, c, batch_counts in assertion.processed_batches(io.StringIO(stream), workers):
                actual.extend(json.loads(line) for line in block.splitlines())
                total += n
                changed += c
                for key in counts:
                    counts[key].update(batch_counts[key])
            self.assertEqual(expected_rows, actual)
            self.assertEqual(total, len(rows))
            self.assertEqual(changed, sum(row['assertion_scope_overlay_changed'] for row in expected_rows))
            summaries.append({key: list(value.items()) for key, value in counts.items()})
        self.assertEqual(summaries[0], summaries[1])

    def test_empty_batches_and_invalid_json(self):
        for workers in (1, 2):
            self.assertEqual([], list(assertion.processed_batches(io.StringIO('\n \n'), workers)))
            with self.assertRaises(json.JSONDecodeError):
                list(assertion.processed_batches(io.StringIO('{bad json}\n'), workers))


if __name__ == '__main__':
    unittest.main()
