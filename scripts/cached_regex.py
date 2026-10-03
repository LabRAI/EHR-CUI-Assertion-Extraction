"""Bounded compiled-pattern reuse for extraction's many target-specific rules.

Python's small shared regex cache churns when each entity creates several
patterns. Keep a separate bounded cache without changing patterns or matches.
"""
from functools import lru_cache
import re

I = IGNORECASE = re.I
M = MULTILINE = re.M
S = DOTALL = re.S
X = VERBOSE = re.X
Pattern = re.Pattern
Match = re.Match
escape = re.escape

compile = lru_cache(maxsize=16384)(re.compile)


def search(pattern, string, flags=0):
    return compile(pattern, flags).search(string)


def match(pattern, string, flags=0):
    return compile(pattern, flags).match(string)


def fullmatch(pattern, string, flags=0):
    return compile(pattern, flags).fullmatch(string)


def finditer(pattern, string, flags=0):
    return compile(pattern, flags).finditer(string)


def findall(pattern, string, flags=0):
    return compile(pattern, flags).findall(string)


def sub(pattern, repl, string, count=0, flags=0):
    return compile(pattern, flags).sub(repl, string, count)


def split(pattern, string, maxsplit=0, flags=0):
    return compile(pattern, flags).split(string, maxsplit)
