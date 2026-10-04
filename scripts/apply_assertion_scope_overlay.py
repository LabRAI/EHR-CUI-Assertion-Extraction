#!/usr/bin/env python3
"""Apply the production anchor-local assertion-status second pass.

This layer is intentionally post-linker: it does not change entity spans, CUIs,
or filtering decisions. It only revisits assertion_status_preview using the
candidate's own span and local evidence sentence.

The rules bind negation, uncertainty, temporality, and conditional language to
the current target. An event-frame pass distinguishes the condition from the
action in constructions such as "if A, then B" before assigning a label.
"""

from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing as mp
import re
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any


TARGET_ACTIONS = {"keep_candidate", "review_candidate", "value_parse", "context_only", "reject_candidate"}
CLINICAL_CATEGORIES = {
    "disease_or_diagnosis",
    "finding",
    "procedure",
    "medication_or_substance",
    "lab_result_or_value",
    "family_history",
    "social_history",
}
PRESENT_DEFAULT_CATEGORIES = CLINICAL_CATEGORIES - {"social_history"}

NEG_BEFORE = re.compile(
    r"\b(?:no|none|denies?|denied|without|w/o|negative for|free of|no evidence of|"
    r"no signs? of|absence of|lack of)\b",
    re.I,
)
NEG_AFTER = re.compile(r"\b(?:absent|negative|not seen|not present|ruled out|excluded)\b", re.I)
DENIAL_AFTER = re.compile(r"\b(?:denies?|denied|declined)\b", re.I)
HOLD_BEFORE = re.compile(r"\b(?:discontinue|discontinued|stop|stopped)\b", re.I)
NEG_BLOCKER = re.compile(r"\b(?:but|however|although|except|other than)\b", re.I)

CONDITION_CUE = re.compile(
    r"\b(?:if|unless|when|whenever|provided that|in case of|pending|depending on)\b",
    re.I,
)
PENDING_CONDITION_CUE = re.compile(r"\bpending\b", re.I)
PRN_CUE = re.compile(r"\b(?:prn|as needed|if needed|if necessary|use as needed|take as needed)\b", re.I)
CONDITIONAL_ACTION = re.compile(
    r"\b(?:start|begin|give|administer|hold|stop|resume|restart|increase|decrease|"
    r"adjust|titrate|switch|escalate|de-escalate|call|return|consider|obtain|order|"
    r"schedule|arrange|repeat|transfuse|initiate|add|consult)\b",
    re.I,
)
PLANNED_ACTION_CUE = re.compile(
    r"\b(?:consider|(?:plans?|planned|planning)\s+(?:for|to)|plan(?=\s)(?!\s*[:/])|schedule(?:d)?|order(?:ed)?|"
    r"obtain|arrange|repeat|refer(?:ral)?(?:\s+for)?|to be discussed|outpatient)\b",
    re.I,
)
HYPOTHETICAL_CUE = re.compile(
    r"\b(?:(?:plans?|planned|planning)\s+(?:for|to)|plan\s+(?![:/])|schedule(?:d)?(?: for| to)?|"
    r"evaluate(?: for)?|evaluation for|workup for|screen(?:ing)? for|monitor(?:ing)? for|"
    r"watch for|risk of|risk for|prophylaxis for|prevent(?:ion)? of|to prevent|to avoid|"
    r"recommend(?:ed)?(?: for| to)?|would require|may require|could lead to|may cause)\b",
    re.I,
)
HYPOTHETICAL_AFTER_CUE = re.compile(
    r"\b(?:planned|scheduled|pending|recommended|deferred|not yet started|"
    r"would require|may require|could lead to|may cause)\b",
    re.I,
)
POSSIBLE_CUE = re.compile(
    r"\b(?:possible|possibly|possibility of|probable|probably|presumed|suspected|suspicious(?: for)?|"
    r"concern(?:ing)? for|primary concern(?: is| for)?|question of|questionable|under consideration|considered|"
    r"r/o|rule out|cannot rule out|cannot exclude|cannot be excluded|"
    r"may have|may represent|may reflect|might represent|could represent|could reflect|"
    r"suggestive of|compatible with|differential (?:also )?(?:includes|included))\b",
    re.I,
)
POSSIBLE_AFTER_CUE = re.compile(
    r"\b(?:possible|probable|presumed|suspected|questionable|under consideration|considered|"
    r"not excluded|cannot be excluded|cannot be ruled out|not ruled out|versus|vs\.?|"
    r"pending(?:\s+(?:confirmation|final|culture|cultures|cytology|imaging|cta|ct angiogram|"
    r"doppler|ultrasound|results?|workup|evaluation|testing))?)\b",
    re.I,
)
EVALUATION_POSSIBLE_CUE = re.compile(
    r"\b(?:evaluate(?: for)?|evaluation for|workup for|screen(?:ing)? for|"
    r"monitor(?:ing)? for|watch for|rule out|r/o|risk of|risk for|consider)\b",
    re.I,
)
IF_ANTECEDENT_HYPOTHETICAL = re.compile(
    r"^\s*(?:develops?|occurs?|arises?|recurs?|ensues?|appears?|is identified|identified|is noted|noted)\b",
    re.I,
)
IF_ANTECEDENT_PRESENT = re.compile(
    r"^\s*(?:persists?|remains?|continues?|worsens?|fails?\s+to\s+recover|does\s+not\s+recover|"
    r"does\s+not\s+improve|fail(?:s|ed)?\s+to\s+improve|deteriorates?|"
    r"remain(?:s|ed)?\s+(?:above|below)|drop(?:s|ped)?|fall(?:s|en)?|rises?)\b",
    re.I,
)
IF_ANTECEDENT_POSSIBLE = re.compile(
    r"^\s*(?:is\s+)?(?:confirmed|suspected|seen|found|positive|detected)\b",
    re.I,
)
UNCERTAIN_EXCLUSION = re.compile(
    r"\b(?:cannot\s+(?:be\s+)?(?:exclude|excluded|rule out)|not\s+(?:fully\s+)?excluded|"
    r"not\s+ruled\s+out|to\s+be\s+ruled\s+out|rule\s+out)\b",
    re.I,
)
DEFINITE_PRESENT_CUE = re.compile(
    r"\b(?:consistent with|confirmed|diagnostic of|demonstrated|demonstrates|revealed|reveals|"
    r"showed|shows|identified|noted|seen|evidence of|secondary to)\b",
    re.I,
)
CURRENT_TRANSITION = re.compile(
    r"\b(?:present(?:s|ed|ing)?(?:\s+(?:with|for))?|admitted(?:\s+(?:with|for))?|"
    r"found to have|noted to have|new|acute|secondary to|complicated by)\b",
    re.I,
)
HISTORICAL_CUE = re.compile(
    r"\b(?:remote|prior|previous|previously|former|resolved|status post|s/p|"
    r"underwent|treated|completed|post[- ]?op|postoperative|post(?=\s+[A-Za-z]))\b",
    re.I,
)
HISTORY_OF = re.compile(r"\b(?:history of|history includes|h/o|hx of|hx)\b", re.I)
CURRENT_CUE = re.compile(
    r"\b(?:present(?:s|ed|ing)? with|admitted with|found to have|noted to have|"
    r"shows?|showed|revealed|demonstrated|confirmed|include(?:s|d)?|has|had|with|"
    r"continue|continued|on|receiving|treated with|treated for|started on|maintained on|"
    r"reports?|reported|occasional|current|currently|active)\b",
    re.I,
)
FAMILY_HISTORY_CUE = re.compile(
    r"\b(?:family history|mother|father|sister|brother|daughter|son|maternal|paternal)\b",
    re.I,
)
HISTORICAL_AFTER_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:was\s+)?performed\s+(?:\w+\s+){0,3}ago|"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|\w+)\s+years?\s+(?:ago|prior)|"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|\w+)\s+months?\s+(?:ago|prior)|"
    r"quit(?:ting)?\s+(?:\w+\s+){0,4}ago|"
    r"in remission|sober\s+(?:\w+\s+){0,3}years?|"
    r"stopped\s+(?:\w+\s+){0,4}ago"
    r")\b",
    re.I,
)
SOCIAL_STATUS_CATEGORY = {"social_history"}
SOCIAL_SPAN = re.compile(
    r"\b(?:smok(?:e|er|ing)|tobacco|alcohol|homeless|homelessness|housing|substance use)\b",
    re.I,
)
SOCIAL_PRESENT_FRAME = re.compile(
    r"\b(?:reports?|reported|admits?|endorses?|uses?|currently|current|occasional|social(?:ly)?|"
    r"active|residing in shelter|lives? alone|living alone|homeless(?:ness)? currently|currently homeless)\b",
    re.I,
)
SOCIAL_ABSENT_SPAN = re.compile(r"\b(?:never smoker|non[- ]?smoker|nonsmoker)\b", re.I)
SOCIAL_ABSENT_FRAME = re.compile(
    r"\b(?:denies?|denied|no|not|without|never|non[- ]?smoker|nonsmoker)\b",
    re.I,
)
SOCIAL_PAST_FRAME = re.compile(
    r"\b(?:former|quit|cessation of|stopped|past|prior|remote|history of|sober|in remission)\b",
    re.I,
)
NO_HISTORY_CUE = re.compile(
    r"\b(?:no history of|without history of|denies history of|no hx of|no h/o|no prior history of)\b",
    re.I,
)
HISTORY_NEGATED_TARGET = re.compile(
    r"\b(?:cabg|avr|aortic valve replacement|nstemi|stemi|mi|myocardial infarction|stroke|cva|"
    r"dvt|pulmonary embolism|pe|pneumonia|sepsis|fall|fracture|cancer|carcinoma|lymphoma|"
    r"leukemia|neoplasm|malignancy|sarcoma|tumou?r|tumor|metastasis|metastatic)\b",
    re.I,
)
PROCEDURE_LIKE_SPAN = re.compile(
    r"\b(?:cabg|avr|valve replacement|biopsy|bronchoscopy|lavage|consult|consultation|"
    r"catheterization|echocardiogram|ultrasound|ct\b|mri\b|scan|testing|test|transfusion|"
    r"hemodialysis|dialysis|crrt|surgery|resection|hemicolectomy)\b",
    re.I,
)
DISEASE_UNCERTAIN_PENDING_AFTER = re.compile(
    r"^\s*(?:on\s+\w+\s+)?(?:pending|versus|vs\.?|cannot\s+be\s+excluded|not\s+ruled\s+out|"
    r"under consideration|considered)\b",
    re.I,
)
NEG_SCOPE_NEW_MENTION_AFTER_COMMA = re.compile(
    r"^\s*(?:regular|irregular|moderate|mild|severe|bilateral|unilateral|left|right|new|acute|"
    r"chronic|elevated|low|high|normal|abnormal|current|currently|active|former|reports?|reported|"
    r"admits?|endorses?|occasional|social(?:ly)?|living|lives?|homeless)\b",
    re.I,
)

CHRONIC_PRESENT_TERMS = {
    "cad",
    "coronary artery disease",
    "chf",
    "heart failure",
    "ckd",
    "chronic kidney disease",
    "diabetes",
    "type 2 diabetes",
    "type 2 diabetes mellitus",
    "hypertension",
    "hyperlipidemia",
    "copd",
    "asthma",
    "ipf",
    "idiopathic pulmonary fibrosis",
    "obstructive sleep apnea",
    "osa",
    "gout",
    "anemia",
    "iron deficiency anemia",
    "cancer",
    "breast cancer",
    "lung cancer",
    "metastatic breast cancer",
}
EVENT_HISTORY_TERMS = {
    "mi",
    "myocardial infarction",
    "nstemi",
    "stemi",
    "cabg",
    "avr",
    "aortic valve replacement",
    "stroke",
    "cva",
    "dvt",
    "pulmonary embolism",
    "pe",
    "pneumonia",
    "sepsis",
    "fall",
    "fracture",
}
NORMAL_PRESENT_FINDINGS = {
    "s1",
    "s1 heart sound",
    "s2",
    "s2 heart sound",
    "heart sound",
    "regular rhythm",
    "regular rate",
    "regular rate and rhythm",
    "bowel sounds",
    "breath sounds",
}


def open_input(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def open_output(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=3)
    return path.open("w", encoding="utf-8")


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def norm(text: Any) -> str:
    return clean(text).strip(" :;,.()[]{}").lower()


@lru_cache(maxsize=8192)
def flexible_target(span: str) -> str:
    span = clean(span)
    if not span:
        return ""
    pieces = [re.escape(part) for part in re.split(r"\s+", span)]
    return r"(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)"


@lru_cache(maxsize=64)
def _target_occurrences(text: str, span: str) -> tuple[re.Match[str], ...]:
    pattern = compiled_target(span)
    if pattern is None:
        return ()
    return tuple(pattern.finditer(text))


@lru_cache(maxsize=8192)
def compiled_target(span: str) -> re.Pattern[str] | None:
    pattern = flexible_target(span)
    return re.compile(pattern, re.I) if pattern else None


def target_occurrences(text: str, span: str) -> list[re.Match[str]]:
    return list(_target_occurrences(text, span))


def bounded_between(left: int, right: int, text: str) -> str:
    return text[left:right]


def reject_if_scope_crosses(middle: str) -> bool:
    return bool(NEG_BLOCKER.search(middle) or re.search(r"[.;\n]", middle))


def has_enumeration_break(middle: str) -> bool:
    return bool(re.search(r"[,;:]|\b(?:and|or|but|while|whereas)\b", middle, re.I))


def clean_between(text: str) -> str:
    return clean(text).strip("()[]{}-")


def is_social_target(span: str, category: str) -> bool:
    return category in SOCIAL_STATUS_CATEGORY or bool(SOCIAL_SPAN.search(span))


def is_procedure_like(span: str, category: str) -> bool:
    return category == "procedure" or bool(PROCEDURE_LIKE_SPAN.search(span))


def is_disease_or_finding(category: str) -> bool:
    return category in {"disease_or_diagnosis", "finding"}


def clause_has_family_subject(clause_before_target: str) -> bool:
    return bool(FAMILY_HISTORY_CUE.search(clause_before_target))


def cue_is_immediate_postmodifier(middle: str, max_chars: int = 14) -> bool:
    """True when a cue after the target plausibly modifies that target."""
    if has_enumeration_break(middle):
        return False
    compact = clean_between(middle)
    if not compact:
        return True
    if len(compact) > max_chars:
        return False
    return not has_enumeration_break(compact)


def cue_before_scopes_to_target(middle: str, *, target_is_event_or_procedure: bool = False) -> bool:
    """Reject broad cue leakage across another listed entity."""
    if "," in middle or ";" in middle or ":" in middle:
        return False
    compact = clean_between(middle)
    if reject_if_scope_crosses(middle):
        return False
    if "," in compact or ";" in compact or ":" in compact:
        return False
    if re.search(r"\b(?:but|while|whereas)\b", compact, re.I):
        return False
    if re.search(r"\b(?:and|or)\b", compact, re.I) and not target_is_event_or_procedure:
        return False
    return len(compact) <= 90


def condition_after_scopes_to_target(middle: str) -> bool:
    """For 'target ... if ...', reject if another action/object appears first."""
    if has_enumeration_break(middle):
        return False
    compact = clean_between(middle)
    if reject_if_scope_crosses(middle):
        return False
    if not compact:
        return True
    if CONDITIONAL_ACTION.search(compact):
        return False
    if has_enumeration_break(compact) and len(compact) > 8:
        return False
    return len(compact) <= 28


def clause_around(text: str, start: int, end: int) -> tuple[str, int, int]:
    left_candidates = [text.rfind(mark, 0, start) for mark in ".;\n"]
    left = max(left_candidates) + 1
    right_candidates = [idx for idx in (text.find(mark, end) for mark in ".;\n") if idx >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    clause = text[left:right]
    return clause, start - left, end - left


def action_before_target(segment: str) -> re.Match[str] | None:
    matches = list(CONDITIONAL_ACTION.finditer(segment))
    return matches[-1] if matches else None


def planned_before_target(segment: str) -> re.Match[str] | None:
    matches = list(PLANNED_ACTION_CUE.finditer(segment))
    return matches[-1] if matches else None


def target_in_condition_clause(clause: str, rel_start: int, rel_end: int) -> tuple[bool, str, str]:
    before = clause[:rel_start]
    after = clause[rel_end:]
    cond_matches = list(CONDITION_CUE.finditer(before))
    if not cond_matches:
        return False, "", ""
    cue = cond_matches[-1]
    middle = before[cue.end() :]
    if reject_if_scope_crosses(middle):
        return False, "", ""
    action = action_before_target(middle)
    if action:
        return False, "", ""
    predicate = re.split(r"[,;:]|\b(?:then|and then)\b", after, maxsplit=1, flags=re.I)[0]
    return True, clean(predicate), middle


def target_in_conditioned_action(clause: str, rel_start: int, rel_end: int) -> tuple[bool, str]:
    before = clause[:rel_start]
    after = clause[rel_end:]

    cond_before = list(CONDITION_CUE.finditer(before))
    if cond_before:
        cue = cond_before[-1]
        middle = before[cue.end() :]
        action = action_before_target(middle)
        if action and not reject_if_scope_crosses(middle[action.end() :]):
            return True, "if_before_action_target"

    cond_after = CONDITION_CUE.search(after)
    if cond_after and condition_after_scopes_to_target(after[: cond_after.start()]):
        if action_before_target(before[-90:]) or planned_before_target(before[-110:]):
            return True, "action_target_if_after"

    return False, ""


def has_event_frame_status(text: str, span: str, category: str) -> tuple[str, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        clause, rel_start, rel_end = clause_around(text, start, end)
        before = clause[:rel_start]
        after = clause[rel_end:]

        in_condition, predicate, _middle = target_in_condition_clause(clause, rel_start, rel_end)
        if in_condition:
            if IF_ANTECEDENT_PRESENT.search(predicate):
                return "present", "if_condition_existing_state"
            if IF_ANTECEDENT_POSSIBLE.search(predicate):
                return "possible", "if_condition_pending_confirmation"
            if IF_ANTECEDENT_HYPOTHETICAL.search(predicate):
                return "hypothetical", "if_condition_future_event"
            if category == "lab_result_or_value":
                return "conditional", "if_condition_lab_threshold"
            if is_procedure_like(span, category):
                return "hypothetical", "if_condition_procedure"
            return "conditional", "if_condition_generic"

        if is_disease_or_finding(category):
            if DISEASE_UNCERTAIN_PENDING_AFTER.search(after[:100]):
                return "possible", "disease_pending_uncertainty"
            if re.search(r"\bconsider(?:ed)?\b", before[-90:], re.I) and not action_before_target(before[-90:]):
                return "possible", "disease_considered_uncertainty"
            if POSSIBLE_CUE.search(before[-90:]) and cue_before_scopes_to_target(before[-90:]):
                return "possible", "disease_local_uncertainty"

        in_action, action_reason = target_in_conditioned_action(clause, rel_start, rel_end)
        if in_action:
            if is_disease_or_finding(category) and PENDING_CONDITION_CUE.search(after[:80]):
                return "possible", "disease_pending_uncertainty"
            return "conditional", action_reason

        planned = planned_before_target(before[-140:])
        if planned and (category in {"procedure", "medication_or_substance"} or is_procedure_like(span, category)):
            return "hypothetical", "planned_action_before_target"
        if (category in {"procedure", "medication_or_substance"} or is_procedure_like(span, category)) and re.search(
            r"\b(?:ordered|scheduled|planned|pending|to be discussed|as outpatient)\b",
            after[:90],
            re.I,
        ):
            return "hypothetical", "planned_action_after_target"
        if category in {"disease_or_diagnosis", "finding"}:
            eval_cue = EVALUATION_POSSIBLE_CUE.search(before[-120:])
            if eval_cue and cue_before_scopes_to_target(before[-120:][eval_cue.end() :]):
                return "possible", "disease_evaluation_frame"
    return "", "no_event_frame"


def neg_before_scopes_to_target(middle: str, span: str = "") -> bool:
    if reject_if_scope_crosses(middle):
        return False
    if CONDITIONAL_ACTION.search(middle) or PLANNED_ACTION_CUE.search(middle):
        return False
    if CONDITION_CUE.search(middle):
        return False
    if re.search(
        r"\b(?:and|but|while)\s+(?:reports?|reported|admits?|endorses?|has|had|uses?|using|"
        r"currently|current|occasional|active|social(?:ly)?|living|lives?)\b",
        middle,
        re.I,
    ):
        return False
    if re.match(r"^\s*,", middle):
        return False
    if "," in middle:
        tail = middle.rsplit(",", 1)[-1]
        if CURRENT_CUE.search(tail) or NEG_SCOPE_NEW_MENTION_AFTER_COMMA.search(tail):
            return False
        if re.match(
            r"^(?:moderate|mild|severe|bilateral|unilateral|left|right|new|acute|regular|irregular|"
            r"bp|hr|blood pressure|heart rate|systolic|diastolic)\b",
            norm(span),
            re.I,
        ):
            return False
    return len(clean_between(middle)) <= 120


def history_prefixed_present_status(text: str, span: str) -> tuple[str, str]:
    span_key = norm(span)
    match = re.match(r"^(?:history of|hx of|h/o)\s+(.+)$", span_key)
    if not match:
        return "", "not_history_prefixed_span"
    bare = match.group(1).strip()
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 80) : start]
        before_clause = re.split(r"[.;\n]", before)[-1]
        for cue in NEG_BEFORE.finditer(before_clause):
            if neg_before_scopes_to_target(before_clause[cue.end() :], span):
                return "absent", "negated_history_prefixed_span"
        after = text[end : min(len(text), end + 80)]
        if re.match(r"^\s*(?:is\s+)?(?:denied|negative|absent)\b", after, re.I):
            return "absent", "negated_history_prefixed_span_after"
    if bare in CHRONIC_PRESENT_TERMS:
        return "present", "history_prefixed_chronic_present"
    for occ in target_occurrences(text, span):
        _start, end = occ.span()
        after = text[end : min(len(text), end + 120)]
        if CURRENT_TRANSITION.search(after):
            return "present", "history_prefixed_current_transition"
    return "", "history_prefixed_no_override"


def social_history_status(text: str, span: str, category: str) -> tuple[str, str]:
    if not is_social_target(span, category):
        return "", "not_social_target"

    span_key = norm(span)
    if SOCIAL_ABSENT_SPAN.search(span_key):
        return "absent", "social_absent_span"
    if span_key.startswith(("former smoker", "former alcohol", "history of alcohol", "history of tobacco")):
        return "present", "social_status_phrase_present"

    for occ in target_occurrences(text, span):
        start, end = occ.span()
        clause, rel_start, rel_end = clause_around(text, start, end)
        before = clause[:rel_start]
        after = clause[rel_end:]
        local = clause[max(0, rel_start - 80) : min(len(clause), rel_end + 90)]

        if re.search(r"\bnot\s+(?:currently\s+)?homeless\b", local, re.I):
            return "absent", "social_not_homeless"
        if re.search(r"\blives?\s+with\b|\bhoused\b", local, re.I) and re.search(r"\bnot\s+homeless\b", local, re.I):
            return "absent", "social_not_homeless"

        if re.search(r"\b(?:denies?|denied|no)\s+current\s+(?:alcohol|tobacco|smok)", before[-80:] + after[:80], re.I):
            return "absent", "social_denies_current_use"

        if re.search(r"\b(?:reports?|reported|admits?|endorses?)\s+(?:to\s+)?(?:occasional|social|moderate|current)?\s*(?:alcohol|tobacco|smok)", local, re.I):
            return "present", "social_reported_current_use"
        if re.search(r"\b(?:currently|current)\s+(?:smok|uses?\s+(?:alcohol|tobacco)|homeless)\b", local, re.I):
            return "present", "social_current_use"
        if re.search(r"\b(?:alcohol use socially|social alcohol use|occasional alcohol use)\b", local, re.I):
            return "present", "social_current_use"

        if re.search(r"\b(?:quit|cessation of|stopped)\s+(?:\w+\s+){0,4}(?:ago|years?|months?)\b", local, re.I):
            return "historical", "social_past_behavior"
        if SOCIAL_PAST_FRAME.search(before[-50:]) and not SOCIAL_PRESENT_FRAME.search(before[-30:] + after[:40]):
            return "historical", "social_past_behavior"
        if SOCIAL_ABSENT_FRAME.search(before[-45:]) and cue_before_scopes_to_target(before[-45:]):
            return "absent", "social_scoped_absent"

    return "", "no_social_status"


def direct_social_status(text: str, span: str, category: str) -> tuple[str, str]:
    """Conservative target-specific social-history override."""
    if not is_social_target(span, category):
        return "", "not_social_target"

    span_key = norm(span)
    if span_key.startswith(("former smoker", "former alcohol use disorder")):
        return "present", "social_status_phrase_present"
    if SOCIAL_ABSENT_SPAN.search(span_key):
        return "absent", "social_absent_span"

    for occ in target_occurrences(text, span):
        start, end = occ.span()
        clause, rel_start, rel_end = clause_around(text, start, end)
        local = clause[max(0, rel_start - 80) : min(len(clause), rel_end + 90)]

        if re.search(r"\b(?:smok|tobacco)", span_key):
            if re.search(r"\b(?:quit|cessation of|stopped|former|past|prior)\s+(?:\w+\s+){0,3}(?:smoking|tobacco)\b", local, re.I):
                return "historical", "social_smoking_past_behavior"
            if re.search(r"\b(?:smoking|tobacco use)\s+(?:quit|stopped)\b", local, re.I):
                return "historical", "social_smoking_past_behavior"
            if re.search(r"\b(?:denies?|denied|no|without)\s+(?:current\s+)?(?:smoking|tobacco use|tobacco)\b", local, re.I):
                return "absent", "social_direct_smoking_absent"
            if re.search(r"\b(?:smoking|tobacco use)\s*,?\s*(?:denies?|denied)\s*(?:smoking|tobacco|current use|use)\b", local, re.I):
                return "absent", "social_direct_smoking_absent_after"

        if "alcohol" in span_key:
            if re.search(r"\b(?:denies?|denied|no|without)\s+(?:current\s+)?alcohol use\b", local, re.I):
                return "absent", "social_direct_alcohol_absent"
            if re.search(r"\balcohol use\s*,?\s*(?:denies?|denied)\s*(?:alcohol|use|current use|use)\b", local, re.I):
                return "absent", "social_direct_alcohol_absent_after"
            if re.search(r"\b(?:current|currently)\s+alcohol use\b|\b(?:current|currently)\s+alcohol\b", local, re.I):
                return "present", "social_direct_alcohol_present"
            if re.search(r"\b(?:reports?|reported|admits?|endorses?)\s+(?:to\s+)?(?:occasional|social|moderate)?\s*alcohol use\b", local, re.I):
                return "present", "social_direct_alcohol_present"
            if re.search(r"\b(?:occasional|social|moderate)\s+alcohol use\b|\balcohol use socially\b", local, re.I):
                return "present", "social_direct_alcohol_present"

        if re.search(r"\bhomeless|homelessness\b", span_key):
            if re.search(r"\b(?:not homeless|no homelessness|denies? homelessness|denied homelessness)\b", local, re.I):
                return "absent", "social_direct_homeless_absent"
            if re.search(r"\b(?:currently homeless|homelessness currently|residing in shelter|lives? in shelter)\b", local, re.I):
                return "present", "social_direct_homeless_present"

    return "", "no_direct_social_status"


def no_history_historical_status(text: str, span: str, category: str) -> tuple[str, str]:
    span_key = norm(span)
    if not is_procedure_like(span, category) and not HISTORY_NEGATED_TARGET.search(span_key):
        return "", "not_history_negated_target"
    for occ in target_occurrences(text, span):
        start, _end = occ.span()
        before = text[max(0, start - 140) : start]
        before_clause = re.split(r"[.;\n]", before)[-1]
        cue = NO_HISTORY_CUE.search(before_clause)
        if cue and cue_before_scopes_to_target(before_clause[cue.end() :], target_is_event_or_procedure=is_procedure_like(span, category)):
            return "absent", "no_history_negated_target"
    return "", "no_history_no_override"


def neg_after_scopes_to_target(middle: str) -> bool:
    if reject_if_scope_crosses(middle):
        return False
    if CONDITION_CUE.search(middle) or CONDITIONAL_ACTION.search(middle) or PLANNED_ACTION_CUE.search(middle):
        return False
    return cue_is_immediate_postmodifier(middle, max_chars=20)


def has_uncertain_exclusion(text: str, span: str) -> tuple[bool, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 120) : start]
        after = text[end : min(len(text), end + 120)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]
        cue = UNCERTAIN_EXCLUSION.search(before_clause)
        if cue and cue_before_scopes_to_target(before_clause[cue.end() :]):
            return True, "uncertain_exclusion_before_target"
        cue = UNCERTAIN_EXCLUSION.search(after_clause)
        if cue and not has_enumeration_break(after_clause[: cue.start()]) and len(clean_between(after_clause[: cue.start()])) <= 60:
            return True, "uncertain_exclusion_after_target"
    return False, "no_uncertain_exclusion"


def has_definite_present(text: str, span: str) -> tuple[bool, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 130) : start]
        after = text[end : min(len(text), end + 80)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]
        cue = DEFINITE_PRESENT_CUE.search(before_clause)
        if cue and cue_before_scopes_to_target(before_clause[cue.end() :]):
            middle = before_clause[cue.end() :]
            if POSSIBLE_CUE.search(middle) or POSSIBLE_AFTER_CUE.search(middle):
                continue
            return True, "definite_present_before_target"
        cue = DEFINITE_PRESENT_CUE.search(after_clause)
        if cue and cue_is_immediate_postmodifier(after_clause[: cue.start()], max_chars=28):
            if POSSIBLE_CUE.search(before_clause[-90:]) and cue_before_scopes_to_target(before_clause[-90:]):
                continue
            return True, "definite_present_after_target"
    return False, "no_definite_present"


def has_negation(text: str, span: str, category: str = "") -> tuple[bool, str]:
    span_key = norm(span)
    if span_key.startswith(("no ", "without ", "w/o ", "negative for ", "denies ", "denied ")):
        return True, "negated_span_text"
    if span_key in NORMAL_PRESENT_FINDINGS:
        return False, "normal_present_finding"
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 150) : start]
        after = text[end : min(len(text), end + 90)]
        hold = HOLD_BEFORE.search(before)
        if category in {"medication_or_substance", "procedure"} and hold and neg_before_scopes_to_target(before[hold.end() :], span):
            return True, "hold_or_discontinue_before"
        for cue in NEG_BEFORE.finditer(before):
            middle = before[cue.end() :]
            if neg_before_scopes_to_target(middle, span):
                return True, "scoped_negation_before"
        if NEG_AFTER.search(after):
            cue_start = NEG_AFTER.search(after)
            if cue_start and neg_after_scopes_to_target(after[: cue_start.start()]):
                return True, "scoped_negation_after"
        denial = DENIAL_AFTER.search(after)
        if denial and cue_is_immediate_postmodifier(after[: denial.start()], max_chars=16):
            return True, "denial_after_target"
    return False, "no_negation"


def has_conditional(text: str, span: str, category: str = "") -> tuple[bool, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 170) : start]
        after = text[end : min(len(text), end + 130)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]

        prn_after = PRN_CUE.search(after_clause)
        if prn_after and not reject_if_scope_crosses(after_clause[: prn_after.start()]):
            return True, "prn_after_target"

        prn_before = PRN_CUE.search(before_clause)
        if prn_before and not reject_if_scope_crosses(before_clause[prn_before.end() :]):
            return True, "prn_before_target"

        cond_after = CONDITION_CUE.search(after_clause)
        if cond_after:
            if is_disease_or_finding(category) and PENDING_CONDITION_CUE.fullmatch(cond_after.group(0) or ""):
                continue
            middle = after_clause[: cond_after.start()]
            if condition_after_scopes_to_target(middle) and not re.search(
                r"\b(?:plan(?:ned|s|ning)?|evaluate|evaluation|workup|screen|monitor|watch|risk)\b",
                middle,
                re.I,
            ):
                return True, "condition_after_target"

        cond_before = CONDITION_CUE.search(before_clause)
        if cond_before:
            middle = before_clause[cond_before.end() :]
            if not reject_if_scope_crosses(middle):
                action_near = CONDITIONAL_ACTION.search(middle)
                if action_near and action_near.start() < 110:
                    return True, "condition_before_action_target"
    return False, "no_conditional"


def has_hypothetical(text: str, span: str) -> tuple[bool, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 180) : start]
        after = text[end : min(len(text), end + 120)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]
        cue = HYPOTHETICAL_CUE.search(before_clause)
        if cue and cue_before_scopes_to_target(before_clause[cue.end() :]):
            return True, "hypothetical_before_target"
        cue = HYPOTHETICAL_AFTER_CUE.search(after_clause)
        if cue and cue_is_immediate_postmodifier(after_clause[: cue.start()]):
            return True, "hypothetical_after_target"
    return False, "no_hypothetical"


def has_possible(text: str, span: str, category: str = "") -> tuple[bool, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 160) : start]
        after = text[end : min(len(text), end + 120)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]
        if category in {"disease_or_diagnosis", "finding"}:
            eval_cue = EVALUATION_POSSIBLE_CUE.search(before_clause)
            if eval_cue and cue_before_scopes_to_target(before_clause[eval_cue.end() :]):
                return True, "evaluation_possible_before_target"
        cue = POSSIBLE_CUE.search(before_clause)
        if cue and cue_before_scopes_to_target(before_clause[cue.end() :]):
            return True, "possible_before_target"
        cue = POSSIBLE_AFTER_CUE.search(after_clause)
        if cue and cue_is_immediate_postmodifier(after_clause[: cue.start()], max_chars=10):
            return True, "possible_after_target"
    return False, "no_possible"


def has_if_antecedent_status(text: str, span: str, category: str) -> tuple[str, str]:
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 130) : start]
        after = text[end : min(len(text), end + 120)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[,.;\n]", after)[0]
        cue = CONDITION_CUE.search(before_clause)
        if not cue or reject_if_scope_crosses(before_clause[cue.end() :]):
            continue
        if category == "procedure":
            return "hypothetical", "if_antecedent_procedure"
        if IF_ANTECEDENT_HYPOTHETICAL.search(after_clause):
            return "hypothetical", "if_antecedent_future_event"
        if IF_ANTECEDENT_POSSIBLE.search(after_clause):
            return "possible", "if_antecedent_pending_confirmation"
    return "", "no_if_antecedent"


def has_historical(text: str, span: str, category: str) -> tuple[bool, str]:
    span_key = norm(span)
    if span_key in CHRONIC_PRESENT_TERMS:
        explicit_only = True
    else:
        explicit_only = False
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 170) : start]
        after = text[end : min(len(text), end + 80)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]
        target_is_event_or_procedure = is_procedure_like(span, category) or span_key in EVENT_HISTORY_TERMS
        for hist in reversed(list(HISTORICAL_CUE.finditer(before_clause))):
            hist_middle = before_clause[hist.end() :]
            if (
                cue_before_scopes_to_target(
                    hist_middle,
                    target_is_event_or_procedure=target_is_event_or_procedure,
                )
                and not CURRENT_TRANSITION.search(hist_middle)
                and not (span_key in CHRONIC_PRESENT_TERMS and clean_between(hist_middle))
            ):
                return True, "explicit_historical_before_closest"

        if HISTORICAL_AFTER_PATTERN.search(after_clause):
            return True, "explicit_historical_after_temporal"
        for hist in HISTORICAL_CUE.finditer(after_clause):
            if cue_is_immediate_postmodifier(after_clause[: hist.start()], max_chars=14):
                return True, "explicit_historical_after"
        hx = HISTORY_OF.search(before_clause)
        if hx and not reject_if_scope_crosses(before_clause[hx.end() :]):
            if CURRENT_TRANSITION.search(before_clause[hx.end() :]):
                continue
            if is_procedure_like(span, category) or span_key in EVENT_HISTORY_TERMS:
                return True, "history_of_event"
            if not explicit_only and re.search(r"\b(?:acute|infection|trauma|surgery|operation)\b", span_key):
                return True, "history_of_acute_event"
        if re.search(r"\b(?:PMH|past medical history)\b", before_clause, re.I):
            if target_is_event_or_procedure and not CURRENT_TRANSITION.search(before_clause):
                return True, "pmh_event_or_procedure_default_historical"
            remission = re.search(r"\bin remission\b", after_clause[:70], re.I)
            if (
                category == "disease_or_diagnosis"
                and remission
                and cue_is_immediate_postmodifier(after_clause[: remission.start()], max_chars=22)
            ):
                return True, "pmh_disease_in_remission"
    return False, "no_historical"


def has_current(text: str, span: str) -> tuple[bool, str]:
    span_key = norm(span)
    if span_key in NORMAL_PRESENT_FINDINGS:
        return True, "normal_present_finding"
    for occ in target_occurrences(text, span):
        start, end = occ.span()
        before = text[max(0, start - 150) : start]
        after = text[end : min(len(text), end + 80)]
        before_clause = re.split(r"[.;\n]", before)[-1]
        after_clause = re.split(r"[.;\n]", after)[0]
        cue = CURRENT_CUE.search(before_clause)
        if cue and not reject_if_scope_crosses(before_clause[cue.end() :]):
            return True, "current_before_target"
        cue = CURRENT_CUE.search(after_clause)
        if cue and cue_is_immediate_postmodifier(after_clause[: cue.start()], max_chars=20):
            return True, "current_after_target"
    return False, "no_current"


def choose_text(row: dict[str, Any], span: str) -> str:
    evidence = clean(row.get("evidence"))
    context = clean(row.get("evidence_context"))
    if target_occurrences(evidence, span):
        evidence_key = norm(evidence)
        span_key = norm(span)
        evidence_is_isolated = evidence_key == span_key or (
            len(evidence_key.split()) <= max(3, len(span_key.split()) + 1)
            and len(evidence_key) <= len(span_key) + 18
        )
        if evidence_is_isolated and target_occurrences(context, span) and len(context) > len(evidence) + 40:
            return context
        return evidence
    if target_occurrences(context, span):
        return context
    return evidence or context


def infer_assertion(row: dict[str, Any]) -> tuple[str, str]:
    old = row.get("assertion_status_preview") or "present"
    action = row.get("filter_action") or ""
    category = row.get("concept_category_preview") or row.get("concept_category") or ""
    span = row.get("span") or row.get("pretty_name") or ""

    text = choose_text(row, span)
    if not text:
        return old, "missing_evidence_keep_old"

    direct_social, direct_social_reason = direct_social_status(text, span, category)
    if direct_social:
        return direct_social, direct_social_reason

    no_history, no_history_reason = no_history_historical_status(text, span, category)
    if no_history:
        return no_history, no_history_reason

    hist_present, hist_present_reason = history_prefixed_present_status(text, span)
    if hist_present:
        return hist_present, hist_present_reason

    if action == "value_parse":
        event_status, event_reason = has_event_frame_status(text, span, category)
        if event_status and event_status != "present":
            return event_status, event_reason
        uncertain, uncertain_reason = has_uncertain_exclusion(text, span)
        if uncertain:
            return "possible", uncertain_reason
        neg, neg_reason = has_negation(text, span, category)
        if neg:
            return "absent", neg_reason
        return "not_applicable", "value_parse"

    if action not in TARGET_ACTIONS:
        return old, "unsupported_action_keep_old"

    if category and category not in CLINICAL_CATEGORIES:
        if norm(span) in NORMAL_PRESENT_FINDINGS:
            current, current_reason = has_current(text, span)
            if current:
                return "present", current_reason
        poss, poss_reason = has_possible(text, span, category)
        if poss:
            return "possible", poss_reason
        neg, neg_reason = has_negation(text, span, category)
        if neg:
            return "absent", neg_reason
        return old, "non_clinical_category_keep_old"

    event_status, event_reason = has_event_frame_status(text, span, category)
    if event_status:
        return event_status, event_reason

    uncertain, uncertain_reason = has_uncertain_exclusion(text, span)
    if uncertain:
        return "possible", uncertain_reason

    neg, neg_reason = has_negation(text, span, category)
    if neg:
        return "absent", neg_reason

    antecedent_status, antecedent_reason = has_if_antecedent_status(text, span, category)
    if antecedent_status:
        return antecedent_status, antecedent_reason

    if is_disease_or_finding(category):
        poss, poss_reason = has_possible(text, span, category)
        if poss:
            return "possible", poss_reason

    # Conditional is tested before hypothetical so "consider CRRT if unstable"
    # stays conditional instead of becoming a generic planned intervention.
    cond, cond_reason = has_conditional(text, span, category)
    if cond:
        return "conditional", cond_reason

    definite, definite_reason = has_definite_present(text, span)
    if definite:
        return "present", definite_reason

    if not is_disease_or_finding(category):
        poss, poss_reason = has_possible(text, span, category)
        if poss:
            return "possible", poss_reason

    hypo, hypo_reason = has_hypothetical(text, span)
    if hypo:
        return "hypothetical", hypo_reason

    hist, hist_reason = has_historical(text, span, category)
    if hist:
        return "historical", hist_reason

    current, current_reason = has_current(text, span)
    if current:
        return "present", current_reason

    if category in PRESENT_DEFAULT_CATEGORIES:
        return "present", "no_local_nonpresent_cue_default_present"
    return old, "no_high_confidence_rule_keep_old"


def overlay_row(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    old = row.get("assertion_status_preview") or "present"
    new, reason = infer_assertion(row)
    out = dict(row)
    out["assertion_status_preview"] = new
    out["assertion_scope_overlay_reason"] = reason
    out["assertion_scope_overlay_previous"] = old
    out["assertion_scope_overlay_changed"] = old != new
    return out, old != new


def process_lines(lines: list[str]) -> tuple[str, int, int, dict[str, Counter]]:
    output = []
    counts = {key: Counter() for key in ("old", "new", "reason", "change")}
    changed = 0
    for line in lines:
        row = json.loads(line)
        out, did_change = overlay_row(row)
        old = out.get("assertion_scope_overlay_previous") or "present"
        new = out.get("assertion_status_preview") or "present"
        changed += int(did_change)
        counts["old"][old] += 1
        counts["new"][new] += 1
        counts["reason"][out.get("assertion_scope_overlay_reason") or "unknown"] += 1
        if did_change:
            counts["change"][f"{old}->{new}"] += 1
        output.append(json.dumps(out, ensure_ascii=False) + "\n")
    return "".join(output), len(lines), changed, counts


def processed_batches(stream, workers: int):
    lines = (line for line in stream if line.strip())
    batches = iter(lambda: list(islice(lines, 512)), [])
    if workers == 1:
        for batch in batches:
            yield process_lines(batch)
        return
    # Bound queued clinical text and preserve input order and summary tie order.
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("fork")) as pool:
        pending = deque(pool.submit(process_lines, batch) for batch in islice(batches, 2 * workers))
        while pending:
            yield pending.popleft().result()
            batch = next(batches, None)
            if batch is not None:
                pending.append(pool.submit(process_lines, batch))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")

    total = 0
    changed = 0
    old_counts: Counter[str] = Counter()
    new_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    change_counts: Counter[str] = Counter()

    with open_input(Path(args.input)) as in_fh, open_output(Path(args.output)) as out_fh:
        for output, batch_total, batch_changed, counts in processed_batches(in_fh, args.workers):
            total += batch_total
            changed += batch_changed
            old_counts.update(counts["old"])
            new_counts.update(counts["new"])
            reason_counts.update(counts["reason"])
            change_counts.update(counts["change"])
            out_fh.write(output)

    summary = {
        "input": args.input,
        "output": args.output,
        "total_rows": total,
        "changed_rows": changed,
        "changed_fraction": changed / total if total else 0.0,
        "old_assertion_counts": dict(old_counts),
        "new_assertion_counts": dict(new_counts),
        "change_counts": dict(change_counts.most_common()),
        "reason_counts": dict(reason_counts.most_common()),
    }
    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
