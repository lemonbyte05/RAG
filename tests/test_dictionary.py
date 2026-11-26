import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app import dictionary, openehr


class DataDictionaryTests(unittest.TestCase):
    def test_dictionary_valid_and_covers_all_numeric_template_fields(self):
        data = dictionary.load()
        self.assertEqual(data['version'], '0.3.0-draft')
        dictionary.validate_against(openehr.TEMPLATES)
        for code, metric in data['metrics'].items():
            self.assertEqual(metric['code'], code)
            self.assertIn(metric['standard_unit'], metric['allowed_units'])

    def test_unit_conversion_and_formatting(self):
        self.assertEqual(dictionary.convert_to_standard('128', 'BP.SYS', 'mmHg'), Decimal('128'))
        self.assertEqual(dictionary.format_standard(dictionary.convert_to_standard('16', 'BP.SYS', 'kPa'), 'BP.SYS'), '120')
        self.assertEqual(dictionary.format_standard(dictionary.convert_to_standard('98.6', 'TEMP', '°F'), 'TEMP'), '37.0')
        self.assertEqual(dictionary.format_standard(dictionary.convert_to_standard('90', 'GLU', 'mg/dL'), 'GLU'), '5.0')
        self.assertEqual(dictionary.format_standard(dictionary.convert_to_standard('500', 'WEIGHT', 'g'), 'WEIGHT'), '0.5')
        self.assertEqual(dictionary.convert_to_standard('1', 'GLU', 'MMOL/L'), Decimal('1.0'))

    def test_plausibility_marks_but_keeps_values(self):
        self.assertEqual(dictionary.plausible_status(128, 'BP.SYS'), 'ok')
        self.assertEqual(dictionary.plausible_status(20, 'BP.SYS'), 'below_range')
        self.assertEqual(dictionary.plausible_status(400, 'BP.SYS'), 'above_range')

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            dictionary.convert_to_standard('128', 'UNKNOWN', 'mmHg')
        with self.assertRaises(ValueError):
            dictionary.convert_to_standard('128', 'BP.SYS', 'cm')
        with self.assertRaises(ValueError):
            dictionary.convert_to_standard('abc', 'BP.SYS', 'mmHg')
        with self.assertRaises(ValueError):
            dictionary.convert_to_standard('nan', 'BP.SYS', 'mmHg')

    def test_template_coverage_validation_detects_gaps(self):
        data = dictionary.load()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dict.json'
            broken = copy.deepcopy(data)
            del broken['metrics']['SPO2']
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(ValueError):
                dictionary.validate_against(openehr.TEMPLATES, path=path)
            broken = copy.deepcopy(data)
            broken['metrics']['BP.SYS']['template']['field_key'] = 'nope'
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(ValueError):
                dictionary.validate_against(openehr.TEMPLATES, path=path)


if __name__ == '__main__':
    unittest.main()
