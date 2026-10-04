from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import extract_medcat_candidates as extraction


class LinkerEfficiencyTests(unittest.TestCase):
    def test_sentence_boundaries_preserve_truncated_regex_semantics(self):
        texts = ['', 'Na 146.0; D5 then 1.23. Fever? No!\nPlan:',
                 '1.2\nA.9;\u0661.\u0662\r\nCaf\u00e9?\n', 'x' * 300 + ' end', 'a..b!?;c']
        boundary = re.compile(r'(?<!\d)[.!?](?!\d)|\n|;')
        for text in texts:
            positions = range(-1, len(text) + 2) if len(text) < 60 else [0, 1, 100, 220, 300, len(text)]
            for start in positions:
                for end in (start, start + 1, len(text), len(text) + 1):
                    left = -1
                    for match in boundary.finditer(text, 0, start):
                        left = match.start()
                    match = next(boundary.finditer(text, end), None)
                    right = match.start() if match else min(len(text), end + 220)
                    for limit in (-1, 0, 5, 420):
                        expected = extraction.clean(text[left + 1:right + 1])[:limit]
                        self.assertEqual(expected, extraction.sentence_for_span(text, start, end, limit))

    def test_context_cue_preserves_pattern_priority_over_text_order(self):
        families = [extraction.ANATOMY_CUE_PATTERNS, extraction.MODIFIER_CUE_PATTERNS,
                    extraction.TREND_CUE_PATTERNS,
                    [('priority', re.compile('late', re.I)), ('earlier_in_text', re.compile('early'))]]
        contexts = ['', 'Nothing relevant.', 'early then LATE',
                    'Mild left renal enlargement; severe cardiac dysfunction; stable but increasing.',
                    'Anterior lung opacity resolved; posterior hepatic lesion worsened.',
                    'Pancreatic cancer with improved pain.\nModerate bilateral edema persists.']
        for patterns in families:
            for context in contexts:
                matches = [(label, pattern.search(context)) for label, pattern in patterns]
                expected = next(({'label': label, 'text': extraction.clean(match.group(0).lower())}
                                 for label, match in matches if match), {'label': None, 'text': None})
                self.assertEqual(expected, extraction.infer_context_cue(context, patterns))
        class MustNotRun:
            def search(self, text):
                raise AssertionError('Scanned a pattern after finding the selected cue')
        self.assertEqual({'label': 'selected', 'text': 'mild'}, extraction.infer_context_cue(
            'MILD', [('selected', re.compile('mild', re.I)), ('unused', MustNotRun())]))


if __name__ == '__main__':
    unittest.main()
