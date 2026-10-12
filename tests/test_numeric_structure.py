from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import extract_medcat_candidates as base
import numeric_structure


def parse(text, span, pretty=None, occurrence=0, enabled=True):
    start = text.index(span, occurrence)
    end = start + len(span)
    context, cs, ce = base.context_with_marker(text, start, end)
    context, cs, ce = base.clean_context_with_span(context, cs, ce)
    kwargs = {'note_text': text, 'note_start': start, 'note_end': end} if enabled else {}
    return base.parse_lab_value(span, pretty or span, context, cs, ce, **kwargs)


class NumericStructureTests(unittest.TestCase):
    def test_explicit_structures(self):
        cases = [
            ('Creatinine 0. 79 mg/dL', 'Creatinine', '0.79', 'mg/dL', 'normal'),
            ('BP 120 / 80 mmHg', 'BP', '120/80', 'mmHg', 'normal'),
            ('BP 120 / 80 (93) mmHg', 'BP', '120/80(93)', 'mmHg', 'normal'),
            ('Hemoglobin Ref Range: 13.5\u201316.5 GM/DL; 16.0', 'Hemoglobin', '16.0', 'GM/DL', 'normal'),
            ('Hemoglobin Ref Range: 13.5-16.5 g/dL\n16.0', 'Hemoglobin', '16.0', 'g/dL', 'normal'),
            ('Potassium (H) 5.8 mmol/L', 'Potassium', '5.8', 'mmol/L', 'high'),
            ('Potassium L 2.9 mmol/L', 'Potassium', '2.9', 'mmol/L', 'low'),
            ('Creatinine <0. 79 mg/dL', 'Creatinine', '<0.79', 'mg/dL', 'normal'),
            ('Troponin Ref Range: <0.04 ng/mL; 0.20', 'Troponin', '0.20', 'ng/mL', 'high'),
            ('Hemoglobin Ref Range: 13.5-16.5 g/dL; 16.0\n(H)', 'Hemoglobin', '16.0', 'g/dL', 'high'),
            ('  BP 120 / 80 mmHg\n(H)', 'BP', '120/80', 'mmHg', 'high'),
            ('Creatinine (H) 20 (H) mg/L', 'Creatinine', '20', 'mg/L', 'high'),
        ]
        for text, span, value, unit, status in cases:
            with self.subTest(text=text):
                result = parse(text, span)
                self.assertEqual(value, result['numeric_value'])
                self.assertEqual(unit, result['unit'])
                self.assertEqual(status, result['value_status'])
                self.assertTrue(result['value_parse_method'].startswith('structural_'))
                self.assertEqual(set(result), {'numeric_value', 'unit', 'comparator', 'value_status',
                                               'value_text', 'value_parse_method', 'measurement_context'})
        self.assertEqual('<', parse('Creatinine <0. 79 mg/dL', 'Creatinine')['comparator'])

    def test_unsupported_inputs_retain_context_parser(self):
        cases = [
            ('CA 19-9: 1,041 (H)', 'CA 19-9'),
            ('Creatinine 1,04 mg/dL', 'Creatinine'),
            ('Creatinine 1.2 1.3 mg/dL', 'Creatinine'),
            ('Goal BP 120 / 80 mmHg', 'BP'),
            ('O2 goal 90-96%', 'O2 goal 90-96%'),
            ('Potassium pending as of 09:30', 'Potassium'),
            ('Potassium 03/14/2026', 'Potassium'),
            ('Potassium 09:30', 'Potassium'),
            ('BP 120 / 80 / 70', 'BP'),
            ('Creatinine 0. 79 mg/dL/min', 'Creatinine'),
            ('Hemoglobin Ref Range: 13.5-16.5 g/dL; 160 g/L', 'Hemoglobin'),
            ('Hemoglobin Ref Range: 13.5-16.5 g/dL; 160 (H) g/L', 'Hemoglobin'),
            ('Hemoglobin Ref Range: 13.5-16.5 g/dL. No patient result.', 'Hemoglobin'),
            ('Potassium (H) 5.8 mmol/L (L)', 'Potassium'),
            ('PT/PTT/INR 12/30/1.1', 'INR'),
            ('ALT/AST 32/19', 'AST'),
            ('Creatinine fell from 1.8 to 1.2 mg/dL', 'Creatinine'),
            ('BNP Reference interval: 0-1,234 pg/mL', 'BNP'),
            ('Potassium' + ' ' * 253 + '(H) 5.81 mmol/L', 'Potassium'),
            ('Temperature (H) 101.3 \u00b0F', 'Temperature'),
            ('Creatinine (H) 120 \u00b5mol/L', 'Creatinine'),
            ('Creatinine (H) 20 mg/dL (H) mg/L', 'Creatinine'),
            ('  BP 120/80 mmHg\n(H)', 'BP'),
            ('BP 120/80(93.5) mmHg', 'BP'),
            ('BP 120 / 80 (93.5) mmHg', 'BP'),
            ('Temperature (H) 101.3\n\u00b0F', 'Temperature'),
            ('Temperature (H) 101.3\u00a0\u00b0F', 'Temperature'),
            ('Hemoglobin Ref Range: 13.5-16.5 g/dL; 160\n g/L', 'Hemoglobin'),
        ]
        for text, span in cases:
            with self.subTest(text=text):
                self.assertEqual(parse(text, span, enabled=False), parse(text, span))

    def test_raw_offsets_and_repeated_mentions(self):
        text = '\u00e9 exam\r\nCreatinine 0. 79 mg/dL; Creatinine 1. 52 mg/dL'
        self.assertEqual('0.79', parse(text, 'Creatinine')['numeric_value'])
        self.assertEqual('1.52', parse(text, 'Creatinine', occurrence=text.index(';'))['numeric_value'])
        for start, end in ((-1, 2), (4, 4), (0, len(text) + 1)):
            self.assertIsNone(numeric_structure.match_value(text, start, end, 'Creatinine', 'Creatinine', True))
        self.assertIsNone(numeric_structure.match_value(text, 0, 10, 'Creatinine', 'Creatinine', True))


if __name__ == '__main__':
    unittest.main()
