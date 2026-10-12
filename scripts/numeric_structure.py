"""Bounded structural lab matches inspired by the Harvard numeric patch.

These are positive matches, not a replacement for the existing contextual
parser. Unsupported structures return None; no predictions or note text are
cached. Match coordinates are used internally against the original note.
"""
from __future__ import annotations

import re

GAP = r"[ \t\r\n\u00a0]*"
N = rf"[-+]?(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.{GAP}\d+)?(?!\d|\.{GAP}\d|,\d)"
UNIT = (rf"(?:mm{GAP}Hg|cm{GAP}H2O|g(?:m)?{GAP}/{GAP}dL|g{GAP}/{GAP}L|"
        rf"mg{GAP}/{GAP}(?:dL|L)|mmol{GAP}/{GAP}L|mEq{GAP}/{GAP}L|"
        rf"K{GAP}/{GAP}uL|cells{GAP}/{GAP}uL|U{GAP}/{GAP}(?:mL|L)|"
        rf"[np]g{GAP}/{GAP}mL|bpm|beats/min|breaths/min|%|seconds?|secs?|ratio|fL|pg|M/uL)")
START = r"^[ \t]*(?:[:=][ \t]*)?(?:(?:is|was|are|were|at|of)[ \t]+)?"
REF_START = re.compile(START + r"(?:(?:latest[ \t]+)?ref(?:erence)?\.?[ \t]*(?:range|interval)|normal[ \t]+range|latest[ \t]+range)\b", re.I)
REFERENCE = re.compile(
    START + rf"(?:(?:latest\s+)?ref(?:erence)?\.?\s*(?:range|interval)|normal\s+range|latest\s+range)\s*:?[ \t]*"
    rf"(?P<low>{N})(?:[ \t]*[-\u2013\u2014][ \t]*|[ \t]+)(?P<high>{N})[ \t]*"
    rf"(?P<ref_unit>{UNIT})?[ \t]*(?P<separator>[;,]\s*|\r?\n[ \t]*)?"
    rf"(?P<label>(?:actual|observed|result|value)(?:[ \t]+(?:value|is|was))?[ \t]*[:=]?[ \t]*)?"
    rf"(?P<comparator><=|>=|<|>)?[ \t]*(?P<value>{N})[ \t]*(?P<unit>{UNIT})?", re.I)
ONE_SIDED = re.compile(
    START + rf"(?:latest\s+)?(?:ref(?:erence)?\s+)?range\s*:\s*(?P<ref_cmp><=|>=|<|>)\s*"
    rf"(?P<ref>{N})\s*(?P<ref_unit>{UNIT})[ \t]*(?:[;,]\s*|\r?\n[ \t]*)?"
    rf"(?:(?:result|actual|observed|value)\s*[:=]?\s*)?"
    rf"(?P<comparator><=|>=|<|>)?[ \t]*(?P<value>{N})[ \t]*(?P<unit>{UNIT})?", re.I)
DECIMAL = re.compile(START + rf"(?P<comparator><=|>=|<|>)?[ \t]*(?P<value>[-+]?\d+\.[ \t]+\d+)[ \t]*(?P<unit>{UNIT})?", re.I)
BP = re.compile(START + rf"(?P<comparator><=|>=|<|>)?{GAP}(?P<value>\d{{2,3}}{GAP}/{GAP}\d{{2,3}}(?:{GAP}\({GAP}\d{{2,3}}{GAP}\))?){GAP}(?P<unit>mm{GAP}Hg)?", re.I)
PREFIX = re.compile(START + rf"(?:\((?P<flag>[HL])\)|(?P<bare>[HL])[ \t]+)[ \t]*(?P<comparator><=|>=|<|>)?[ \t]*(?P<value>{N})[ \t]*(?P<unit>{UNIT})?", re.I)
BP_NAME = re.compile(r"\b(?:bp|blood pressure)\b", re.I)
GOAL = re.compile(r"\b(?:goal|target|aim|keep|maintain)\b", re.I)
NON_RESULT = re.compile(r"\b(?:pending|unavailable|not measured|not reported|tablet|capsule|infusion|injection|dose)\b", re.I)
FLAG = re.compile(r"^[ \t\r\n\u00a0]*(?:\([ \t\r\n\u00a0]*(?P<flag>[HLAN])[ \t\r\n\u00a0]*\)|(?P<word>high|low|normal|abnormal|elevated|decreased|increased|reduced)\b)", re.I)
INCOMPLETE = re.compile(r"^[ \t]*(?:[/\uFF1A:]\s*\d|[-\u2013\u2014]\s*\d|\.\s*\d|,\d|\d)")
SPACE = re.compile(r"\s+")
SPLIT_BP = re.compile(r"\d[ \t]+/|/[ \t]+\d|\d[ \t]+\(")
UNKNOWN_TAIL = re.compile(r"^[ \t\r\n\u00a0]*(?:/|\d|[^\W\d_]|\u00b0|\([ \t\r\n\u00a0]*\d)")


def number(value):
    return SPACE.sub('', value).replace(',', '')


def unit_key(value):
    return SPACE.sub('', value or '').lower().replace('gm/', 'g/')


def match_value(text, start, end, span, pretty, known_lab):
    if not known_lab or start is None or end is None or not 0 <= start < end <= len(text):
        return None
    raw = text[start:end]
    if SPACE.sub(' ', raw).strip().lower() != SPACE.sub(' ', span).strip().lower():
        return None
    after = text[end:end + 260]
    method = None
    match = None
    if REF_START.match(after):
        match = REFERENCE.match(after)
        method = 'structural_reference_result'
        if match and not (match['ref_unit'] or match['separator'] or match['label']):
            return None
        if not match:
            match = ONE_SIDED.match(after)
            method = 'structural_one_sided_reference_result'
    elif BP_NAME.search(span + ' ' + pretty):
        match = BP.match(after)
        method = 'structural_blood_pressure_pair'
        if match and not SPLIT_BP.search(match['value']):
            return None
    else:
        match = DECIMAL.match(after)
        method = 'structural_spaced_decimal'
        if not match:
            match = PREFIX.match(after)
            method = 'structural_prefix_result_flag'
    if not match:
        return None
    # Artificial window edges never establish a complete number or unit.
    if match.end() == len(after) and end + len(after) < len(text):
        return None
    if INCOMPLETE.match(text[end + match.end('value'):end + match.end('value') + 80]):
        return None
    before = re.split(r'[;\n]', text[max(0, start - 65):start])[-1]
    # A structural rescue must not introduce a new observation from a goal/dose.
    if GOAL.search(before + ' ' + raw) or NON_RESULT.search(raw):
        return None
    # Do not consume a unit prefix (mg/L from mg/L/min), or a following scalar.
    tail = text[end + match.end():end + match.end() + 80]
    if UNKNOWN_TAIL.match(tail) and not FLAG.match(tail):
        return None
    groups = match.groupdict()
    unit, ref_unit = groups.get('unit'), groups.get('ref_unit')
    if unit and ref_unit and unit_key(unit) != unit_key(ref_unit):
        return None
    low, high = groups.get('low'), groups.get('high')
    if low is not None and float(number(low)) > float(number(high)):
        return None
    flag = groups.get('flag') or groups.get('bare')
    status = 'high' if flag == 'H' or flag == 'h' else 'low' if flag else None
    suffix_flag = FLAG.match(tail)
    if suffix_flag:
        token = (suffix_flag['flag'] or suffix_flag['word']).lower()
        suffix_status = {'h': 'high', 'l': 'low', 'a': 'abnormal', 'n': 'normal',
                         'elevated': 'high', 'increased': 'high', 'decreased': 'low', 'reduced': 'low'}.get(token, token)
        if status and status != suffix_status:
            return None
        status = suffix_status
        # A flag can sit between the numeric value and its unit. Consume the
        # unit only when it is complete; otherwise retain the baseline parser.
        trailing = tail[suffix_flag.end():]
        trailing_unit = re.match(rf"^{GAP}(?P<unit>{UNIT})", trailing, re.I)
        if trailing_unit:
            suffix_unit = trailing_unit.group('unit')
            if unit and unit_key(unit) != unit_key(suffix_unit):
                return None
            if ref_unit and unit_key(ref_unit) != unit_key(suffix_unit):
                return None
            unit = suffix_unit
            if UNKNOWN_TAIL.match(trailing[trailing_unit.end():]):
                return None
        elif trailing.strip('\t\r\n\u00a0'):
            return None
    if not status and low is not None and not groups.get('comparator'):
        value = float(number(match['value']))
        status = 'low' if value < float(number(low)) else 'high' if value > float(number(high)) else 'normal'
    return {'numeric_value': number(match['value']),
            'unit': unit or ref_unit or ('mmHg' if method == 'structural_blood_pressure_pair' else None),
            'comparator': groups.get('comparator'), 'value_status': status,
            'value_parse_method': method, 'value_text': raw + match.group(0),
            '_value_start': end + match.start('value'), '_value_end': end + match.end()}
