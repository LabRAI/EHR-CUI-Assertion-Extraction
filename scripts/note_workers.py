"""Bounded, ordered work over whole notes with fork-shared input state."""
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from itertools import islice
import multiprocessing as mp


def ordered_note_results(function, note_ids, *, workers, initializer, initargs):
    if workers < 1:
        raise ValueError('workers must be positive')
    note_ids = iter(note_ids)
    if workers == 1:
        initializer(*initargs)
        for note_id in note_ids:
            yield function(note_id)
        return
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context('fork'),
                             initializer=initializer, initargs=initargs) as pool:
        pending = deque(pool.submit(function, key) for key in islice(note_ids, workers * 2))
        while pending:
            yield pending.popleft().result()
            for key in islice(note_ids, 1):
                pending.append(pool.submit(function, key))
