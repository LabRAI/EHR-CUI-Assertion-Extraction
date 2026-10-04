from concurrent.futures.process import BrokenProcessPool
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from note_workers import ordered_note_results

STATE = None


def initialize(data):
    global STATE
    STATE = data


def lookup(key):
    if key == 'die':
        os._exit(17)
    return key, STATE[key]


class NoteWorkerTests(unittest.TestCase):
    def test_order_duplicates_and_empty_input(self):
        data = {str(i): 'clinical_text_' + str(i) for i in range(37)}
        keys = list(reversed(data)) + list(data)[:3]
        for workers in (1, 4):
            self.assertEqual([(key, data[key]) for key in keys],
                             list(ordered_note_results(lookup, keys, workers=workers,
                                                       initializer=initialize, initargs=(data,))))
            self.assertEqual([], list(ordered_note_results(lookup, [], workers=workers,
                                                           initializer=initialize, initargs=(data,))))

    def test_invalid_worker_count_and_exception_propagation(self):
        with self.assertRaises(ValueError):
            list(ordered_note_results(lookup, [], workers=0, initializer=initialize, initargs=({},)))
        for workers in (1, 4):
            with self.assertRaises(KeyError):
                list(ordered_note_results(lookup, ['missing'], workers=workers,
                                          initializer=initialize, initargs=({},)))

    def test_worker_exit_propagates(self):
        with self.assertRaises(BrokenProcessPool):
            list(ordered_note_results(lookup, ['die'], workers=2,
                                      initializer=initialize, initargs=({},)))


if __name__ == '__main__':
    unittest.main()
