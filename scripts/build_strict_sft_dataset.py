#!/usr/bin/env python3
"""Build strict NuExtract SFT examples from MedCAT/UMLS candidate records.

The linker output is intentionally broad. This script is the narrow gate that
keeps only candidates suitable as supervised targets.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any


SPACE = re.compile(r"\s+")

ALLOWED_VALUE_CONTEXTS = {"observed_patient", "ventilator_set", "ventilator_spontaneous"}
ALLOWED_KEEP_CATEGORIES = {
    "disease_or_diagnosis",
    "finding",
    "medication_or_substance",
    "procedure",
    "social_history",
}

BLOCKLIST_SPANS = {
    "activity",
    "addictions",
    "abstained",
    "admit",
    "admitted",
    "advice",
    "age",
    "ambulation",
    "abnormality",
    "abnormalities",
    "assessment of nutritional status",
    "appearance",
    "arousal",
    "attention",
    "beers",
    "blood products",
    "breath",
    "breathing",
    "cardiac",
    "children",
    "chips",
    "communicated",
    "consult",
    "consulted",
    "consulting",
    "color doppler",
    "coronary",
    "cracker",
    "cross",
    "diet",
    "discharge",
    "discharged",
    "discussion",
    "disease",
    "dysfunction",
    "dropping",
    "enhancement",
    "expressed",
    "eyes open",
    "family",
    "finger stick",
    "fluid",
    "follow up",
    "follow-up",
    "food",
    "food allergies",
    "gave",
    "heme",
    "hopes",
    "ice",
    "illness",
    "imaging",
    "injury",
    "intake",
    "interested",
    "issue",
    "lap",
    "leave",
    "light",
    "lipase",
    "amylase",
    "lying in bed",
    "mass",
    "nursery",
    "near",
    "neck",
    "notify",
    "other injuries",
    "output",
    "overlying",
    "page",
    "peds",
    "plastics",
    "pull",
    "pulling",
    "reach",
    "protein",
    "prognosis",
    "radiology",
    "referrals to",
    "rehab",
    "rehabilitation",
    "removal",
    "reports",
    "rest",
    "right lower extremity",
    "related",
    "role",
    "saline",
    "sequences",
    "signs",
    "solution",
    "sit",
    "sit to stand",
    "standing",
    "suction",
    "swallowing",
    "pulled",
    "term",
    "therapeutic",
    "turning",
    "trial",
    "vital signs",
    "waffle",
    "weight",
    "weight bear",
    "whiskey",
    "willing",
    "works in",
    "work assessment",
    "alcohol, delirium",
}

BLOCKLIST_PRETTY = {
    "appearance",
    "assessment of nutritional status",
    "beer",
    "current chronological age",
    "current non-drinker of alcohol",
    "clinical trial",
    "child of",
    "cigarette smoke",
    "consultation",
    "cross syndrome",
    "determination of prognosis",
    "dietary finding",
    "disease",
    "discussion",
    "does climb",
    "does pull",
    "does stand from sitting",
    "droppings",
    "ethanol measurement",
    "food",
    "finger-prick sampling",
    "follow-up status",
    "gene expression",
    "guarded prognosis",
    "heme",
    "hopeful",
    "ice",
    "heart structure",
    "hospital admission",
    "illness",
    "imaging",
    "lead",
    "light therapy",
    "liquid substance",
    "mass of body structure",
    "measurement of fluid intake",
    "clinical finding",
    "orthostatic body position",
    "overlying behavior",
    "patient discharge",
    "polyacrylamide gel electrophoresis",
    "potato chips",
    "ready for discharge",
    "removal",
    "recommendation to",
    "rehabilitation therapy",
    "refractive surgery enhancement",
    "role",
    "sign",
    "sitting position",
    "surname",
    "taking patient vital signs",
    "term birth of newborn",
    "therapy",
    "triacylglycerol lipase",
    "toxic liver disease",
    "willing",
    "weighing patient",
    "weight-bearing",
    "whiskey",
    "work assessment",
    "1/3 meter",
    "periodontal ehlers-danlos syndrome",
}

LAB_ANALYTE_SPANS = {
    "albumin",
    "alkphos",
    "alkaline phosphatase",
    "alt",
    "amylase",
    "ast",
    "bilirubin",
    "bun",
    "calcium",
    "ck-mb",
    "creatinine",
    "glucose",
    "hct",
    "hematocrit",
    "hgb",
    "inr",
    "lactate",
    "lipase",
    "magnesium",
    "phosphorus",
    "platelets",
    "plt",
    "pt",
    "ptt",
    "troponin",
    "wbc",
}

NON_MEDICATION_SUBSTANCE_SPANS = {
    "amylase",
    "antigen",
    "antibody",
    "acid",
    "bili",
    "bilirubin",
    "bile",
    "body secretion",
    "breath",
    "bicarb",
    "bicarbonate",
    "calcium",
    "cracker",
    "d-dimer",
    "dropping",
    "drug",
    "drugs",
    "effusion",
    "electrolyte",
    "element",
    "food",
    "hco3",
    "heme",
    "ice",
    "ionized calcium",
    "lactic",
    "legionella antigen",
    "liver enzymes",
    "lymph",
    "lymphs",
    "metal",
    "metabolite",
    "metabolites",
    "mixed drinks",
    "mono",
    "monos",
    "phos",
    "phosphate",
    "pleural fluid",
    "peritoneal fluid",
    "plastics",
    "protein",
    "rise",
    "secretions",
    "soap",
    "soaps",
    "sputum",
    "stool",
    "supplement",
    "solution",
    "tear",
    "tears",
    "titrate",
    "transaminases",
    "troponin",
    "troponin i",
    "troponin t",
    "troponin-t",
    "waffle",
    "urine",
    "vanco",
    "viral antigen",
    "vodka",
    "yeast",
    "gram stain",
    "gram stains",
}

NON_MEDICATION_PRETTY_TERMS = {
    "amylase",
    "acid",
    "antigen",
    "antigen of legionella",
    "antigen of virus",
    "antibody",
    "bile",
    "body secretion",
    "calcium electrolyte",
    "chemical element",
    "cocktail",
    "cracker",
    "creatine kinase",
    "drug or medicament",
    "droppings",
    "feces",
    "effusion",
    "electrolyte",
    "ethanol",
    "food",
    "gram stain",
    "heme",
    "ice",
    "liver enzyme",
    "lymph",
    "metal",
    "metabolite",
    "plastic",
    "phosphate",
    "pleural fluid",
    "protein",
    "soap",
    "sputum",
    "substance with aminotransferase",
    "troponin",
    "urine specimen",
    "vodka",
    "yeast",
    "hypotensive agent",
    "mica",
    "potato chips",
    "milk",
    "semi-skimmed milk",
    "solution",
    "soup",
    "tinned soup",
}

GENERIC_DISEASE_SPANS = {
    "accessory muscle",
    "bili",
    "disease",
    "dysfunction",
    "eyes open",
    "esophageal",
    "hepatic toxicit",
    "injury",
    "mono",
    "other injuries",
    "trauma",
}

GENERIC_FINDING_SPANS = {
    "comfortable",
    "aggressive",
    "behavior",
    "children",
    "climb",
    "communicated",
    "disability",
    "finding",
    "findings",
    "follow up",
    "follow-up",
    "gaze",
    "good prognosis",
    "guarded",
    "hit head",
    "hopes",
    "illness",
    "issue",
    "expressed",
    "liquid",
    "liquids",
    "lying in bed",
    "mass",
    "mental status",
    "near",
    "nonsense",
    "overlying",
    "oriented",
    "oriented times",
    "pain",
    "pulled",
    "pulling",
    "reflect",
    "reach",
    "relief",
    "related",
    "retracted",
    "signs",
    "sit",
    "sit to stand",
    "sitting upright",
    "skipping",
    "standing",
    "suspicion",
    "term",
    "turn",
    "worried",
    "weight bear",
}

WRONG_CONCEPT_PAIRS = {
    ("abnormalities", "congenital malformation"),
    ("abnormality", "congenital malformation"),
    ("albuterol", "levosalbutamol"),
    ("bilis", "bilis"),
    ("breath", "breath"),
    ("cavity", "dental caries"),
    ("climb", "does climb"),
    ("cross", "cross syndrome"),
    ("ddavp", "argipressin"),
    ("discussion", "discussion"),
    ("element", "chemical element"),
    ("enhancement", "refractive surgery enhancement"),
    ("expressed", "gene expression"),
    ("faint", "syncope"),
    ("heme", "heme"),
    ("hypotensive", "hypotensive agent"),
    ("liver enzymes", "liver enzyme"),
    ("lungs", "disorder of lung"),
    ("mica", "mica"),
    ("nonsense", "nonsense mutation"),
    ("peds", "periodontal ehlers-danlos syndrome"),
    ("pred", "prednisone"),
    ("chips", "potato chips"),
    ("pulled", "does pull"),
    ("reflect", "reflecting"),
    ("rise", "risedronate"),
    ("sinus", "fistula"),
    ("term", "term birth of newborn"),
    ("titrate", "citrate"),
    ("waffle", "waffle"),
}

PAST_SECTIONS = {
    "past medical history",
    "past medical / surgical history",
    "past medical/surgical history",
    "past surgical history",
    "pmh",
    "pmhx",
    "other past medical history",
    "medications prior to admission",
    "prior medications",
}

PROCEDURE_ALLOW_TERMS = {
    "angioplasty",
    "biopsy",
    "bypass",
    "cannulation",
    "catheter",
    "catheterization",
    "chemotherapy",
    "colonoscopy",
    "computed tomography",
    "craniotomy",
    "ct scan",
    "dialysis",
    "eeg",
    "endoscopy",
    "ercp",
    "extubation",
    "fasciotomy",
    "grafting",
    "hemodialysis",
    "intubation",
    "mri",
    "oxygen therapy",
    "plain x-ray",
    "radiation",
    "stent",
    "stenting",
    "surgery",
    "surgical",
    "tomography",
    "tracheostomy",
    "transfusion",
    "transplant",
    "ultrasound",
    "ventilation",
    "x-ray",
}

ADMIN_OR_CONTEXT_CATEGORIES = {"case management", "social work"}
ADMIN_SECTIONS = {
    "title",
    "recommendation",
    "final recommendation",
    "discharge plan",
    "patient information",
    "additional information",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--prompt-template", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--seed", type=int, default=20260514)
    parser.add_argument("--max-examples", type=int, default=0, help="0 means no cap")
    parser.add_argument("--min-keep-score", type=float, default=0.99)
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--dev-frac", type=float, default=0.1)
    parser.add_argument("--test-frac", type=float, default=0.1)
    return parser.parse_args()


def clean(text: str | None) -> str:
    return SPACE.sub(" ", text or "").strip()


def norm(text: str | None) -> str:
    return clean(text).strip(" :;,.()[]{}").lower()


def contains_normalized(haystack: str | None, needle: str | None) -> bool:
    h = clean(haystack).lower()
    n = clean(needle).lower()
    if not n:
        return False
    return n in h or SPACE.sub("", n) in SPACE.sub("", h)


def contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def contains_discontinuation_context(text: str, span_key: str) -> bool:
    if not span_key:
        return False
    local_patterns = [
        rf"\b{re.escape(span_key)}\b[^.;:]{{0,60}}\b(d/?c|dc|discontinued|held|stopped|tapered off|tapered)\b",
        rf"\b(d/?c|dc|discontinued|held|stopped|tapered off|tapered)\b[^.;:]{{0,60}}\b{re.escape(span_key)}\b",
        rf"\b{re.escape(span_key)}\b\s*/\s*[^.;:]{{0,40}}\bd/?c\b",
    ]
    return any(re.search(pattern, text) for pattern in local_patterns)


def contains_negated_coordination(text: str, span_key: str) -> bool:
    if not span_key:
        return False
    negation_patterns = [
        rf"\bno\s+(?:episodes?\s+of\s+)?[^.;:]{{0,200}}\b{re.escape(span_key)}\b",
        rf"\bwithout\s+[^.;:]{{0,200}}\b{re.escape(span_key)}\b",
        rf"\bdenies?\s+[^.;:]{{0,200}}\b{re.escape(span_key)}\b",
        rf"\bnegative\s+for\s+[^.;:]{{0,200}}\b{re.escape(span_key)}\b",
        rf"\babsence\s+of\s+[^.;:]{{0,200}}\b{re.escape(span_key)}\b",
    ]
    return any(re.search(pattern, text) for pattern in negation_patterns)


def contains_uncertainty_context(text: str, span_key: str) -> bool:
    if not span_key:
        return False
    uncertainty_patterns = [
        rf"\b(concerning for|concern for|may represent|may reflect|could represent|could reflect|possibly|possible|probable|suspected|question of|rule out|r/o)\b[^.;:]{{0,120}}\b{re.escape(span_key)}\b",
        rf"\b{re.escape(span_key)}\b[^.;:]{{0,120}}\b(vs|versus|possible|possibly|probable|suspected|cannot be excluded|cannot exclude)\b",
    ]
    return any(re.search(pattern, text) for pattern in uncertainty_patterns)


def is_susceptibility_table_row(text: str) -> bool:
    return bool(re.search(r"\b[A-Z][A-Z/-]{3,}\s*-{2,}\s*(<=|>=|[sir]\b|\d)", text, re.I))


def is_noncontrast_ct_mismatch(span_key: str, pretty_key: str, text: str) -> bool:
    if "contrast" not in span_key and "contrast" not in pretty_key:
        return False
    return bool(re.search(r"\b(non-?contrast|without contrast)\b[^.;:]{0,40}\b(head\s+)?ct\b", text))


def is_prn_indication_context(text: str, span_key: str) -> bool:
    if not span_key:
        return False
    return bool(re.search(r"\bprn\b[^.;:]{0,80}\b" + re.escape(span_key) + r"\b", text))


def is_prophylaxis_heading_context(span_key: str, pretty_key: str, text: str) -> bool:
    target = f"{span_key} {pretty_key}"
    if "stress ulcer" not in target:
        return False
    return bool(re.search(r"\bstress ulcer\s*:\s*(h2 blocker|ppi|prophylaxis)?\b", text))


def contains_procedure_allow_term(text: str) -> bool:
    if any(term in text for term in PROCEDURE_ALLOW_TERMS):
        return True
    return bool(re.search(r"\b(ct|mri|eeg)\b", text))


def assertion_is_locally_supported(record: dict[str, Any], span_key: str, section: str) -> tuple[bool, str]:
    assertion = record.get("assertion_status_preview") or "present"
    evidence = norm(record.get("evidence"))
    context = norm(record.get("evidence_context"))
    local_text = f"{evidence} {context}"
    if assertion != "absent" and contains_negated_coordination(local_text, span_key):
        return False, "candidate_in_negated_scope"
    if assertion == "present":
        if contains_uncertainty_context(local_text, span_key):
            return False, "present_candidate_in_uncertain_scope"
        if contains_discontinuation_context(local_text, span_key):
            return False, "present_candidate_in_discontinued_scope"
        return True, "present_default"
    if assertion == "absent":
        negation_patterns = [
            rf"\bno\s+[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
            rf"\bwithout\s+[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
            rf"\bdenies?\s+[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
            rf"\bnegative\s+for\s+[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
            rf"\babsence\s+of\s+[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
        ]
        if span_key and any(re.search(pattern, evidence) for pattern in negation_patterns):
            return True, "explicit_local_negation"
        if contains_negated_coordination(context, span_key):
            return True, "explicit_context_negation"
        return False, "unsupported_absent_scope"
    if assertion == "possible":
        evidence_for_uncertainty = re.sub(r"\bas soon as possible\b", "", evidence)
        uncertainty_patterns = [
            rf"\b(possible|possibly|probable|probably|suspected|question|concern for|rule out|r/o)\b[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
            rf"\b{re.escape(span_key)}\b[^.;:,]{{0,80}}\b(vs|versus|possible|possibly|probable|probably|suspected)\b",
        ]
        if span_key and any(re.search(pattern, evidence_for_uncertainty) for pattern in uncertainty_patterns):
            return True, "explicit_local_uncertainty"
        return False, "unsupported_possible_scope"
    if assertion == "hypothetical":
        hypothetical_patterns = [
            rf"\b(if|should|would|could|call if|return if|plan to|consider|please consider)\b[^.;:,]{{0,80}}\b{re.escape(span_key)}\b",
            rf"\b{re.escape(span_key)}\b[^.;:,]{{0,80}}\b(if needed|if necessary|would|could|should|consider)\b",
        ]
        if span_key and any(re.search(pattern, evidence) for pattern in hypothetical_patterns):
            return True, "explicit_local_hypothetical"
        return False, "unsupported_hypothetical_scope"
    if assertion == "historical":
        if section in PAST_SECTIONS:
            return True, "past_section"
        if re.search(r"\b(history of|h/o|hx of|prior|previous|previously|recently|s/p|status post|was|were|had|underwent)\b", evidence):
            return True, "explicit_local_history"
        return False, "unsupported_historical_scope"
    return True, "assertion_not_checked"


def confidence_for(record: dict[str, Any]) -> str:
    score = float(record.get("linker_score") or 0.0)
    if record.get("filter_action") == "value_parse":
        return "high"
    if score >= 0.99 and not record.get("needs_review"):
        return "high"
    if score >= 0.90:
        return "medium"
    return "low"


def strict_decision(record: dict[str, Any], min_keep_score: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    action = record.get("filter_action")
    span_key = norm(record.get("span"))
    pretty_key = norm(record.get("pretty_name"))
    category = record.get("concept_category_preview")
    note_category = norm(record.get("category"))
    section = norm(record.get("section"))
    flags = set(record.get("filter_flags") or [])

    if not record.get("umls_cui"):
        return False, ["missing_umls_cui"]
    if record.get("umls_mapping_needs_review"):
        return False, ["umls_mapping_needs_review"]
    if record.get("umls_mapping_status") not in {None, "snomed_single_cui"}:
        return False, [f"non_single_umls_mapping:{record.get('umls_mapping_status')}"]
    if span_key in BLOCKLIST_SPANS or pretty_key in BLOCKLIST_PRETTY:
        return False, ["strict_blocklist_span_or_pretty"]
    if (span_key, pretty_key) in WRONG_CONCEPT_PAIRS:
        return False, ["known_wrong_span_concept_pair"]
    if "generic_or_artifact_target" in flags:
        return False, ["generic_or_artifact_target"]

    assertion_ok, assertion_reason = assertion_is_locally_supported(record, span_key, section)
    if not assertion_ok:
        return False, [assertion_reason]

    if action == "value_parse":
        if category != "lab_result_or_value":
            return False, [f"value_parse_bad_category:{category}"]
        if record.get("numeric_value") in (None, ""):
            return False, ["value_parse_missing_numeric_value"]
        if record.get("value_parse_method") in {None, "", "not_parsed", "not_applicable"}:
            return False, ["value_parse_bad_method"]
        if record.get("measurement_context") not in ALLOWED_VALUE_CONTEXTS:
            return False, [f"value_parse_bad_measurement_context:{record.get('measurement_context')}"]
        value_text = record.get("value_text")
        if value_text and not contains_normalized(record.get("evidence_context"), value_text):
            return False, ["value_text_not_in_bounded_context"]
        reasons.append("strict_value_parse")
        return True, reasons

    if action != "keep_candidate":
        return False, [f"not_auto_keep_or_value:{action}"]

    score = float(record.get("linker_score") or 0.0)
    if score < min_keep_score:
        return False, [f"keep_score_below_threshold:{score:.3f}"]
    if record.get("needs_review"):
        return False, ["keep_candidate_needs_review"]
    if category not in ALLOWED_KEEP_CATEGORIES:
        return False, [f"keep_category_not_allowed:{category}"]

    evidence_key = norm(record.get("evidence"))
    context_key = norm(record.get("evidence_context"))
    local_key = f"{evidence_key} {context_key}"

    if note_category in ADMIN_OR_CONTEXT_CATEGORIES and "social_target_context" not in flags:
        return False, ["admin_social_category_without_social_target"]
    if section in ADMIN_SECTIONS and category not in {"social_history"}:
        return False, ["administrative_section"]
    if "header_like_span" in flags and category not in {"disease_or_diagnosis", "medication_or_substance"}:
        return False, ["header_like_non_disease_medication"]
    if is_noncontrast_ct_mismatch(span_key, pretty_key, local_key):
        return False, ["contrast_ct_candidate_but_noncontrast_context"]
    if is_prophylaxis_heading_context(span_key, pretty_key, local_key):
        return False, ["prophylaxis_heading_not_diagnosis"]

    if category == "disease_or_diagnosis":
        if span_key in GENERIC_DISEASE_SPANS:
            return False, ["generic_disease_or_diagnosis_span"]
        if "other than" in evidence_key or "noted to be significant for any other" in evidence_key:
            return False, ["contrastive_or_exception_disease_scope"]
        if any(term in evidence_key for term in ("contact epilepsy", "epilepsy clinic", "epilepsy service")):
            return False, ["service_name_not_patient_diagnosis"]
        if is_prn_indication_context(local_key, span_key):
            return False, ["prn_indication_not_patient_diagnosis"]
        if "indication:" in evidence_key and not re.search(r"\b(showed|shows|revealed|demonstrated|seen|present|evidence of)\b", context_key):
            return False, ["test_indication_not_confirmed_diagnosis"]

    if category == "finding":
        if span_key in GENERIC_FINDING_SPANS or pretty_key in GENERIC_FINDING_SPANS:
            return False, ["generic_finding_or_functional_span"]
        if any(term in local_key for term in ("communicated with", "education / communication")):
            return False, ["workflow_communication_not_patient_finding"]

    if category == "medication_or_substance":
        if span_key in LAB_ANALYTE_SPANS or span_key in NON_MEDICATION_SUBSTANCE_SPANS:
            return False, ["lab_analyte_kept_as_medication"]
        if contains_any(pretty_key, NON_MEDICATION_PRETTY_TERMS):
            return False, ["non_medication_substance_concept"]
        if any(term in span_key for term in ("beer", "whiskey", "alcohol", "protein")):
            return False, ["substance_use_or_nutrition_context"]
        if is_susceptibility_table_row(evidence_key):
            return False, ["susceptibility_table_not_medication_order"]
        if contains_discontinuation_context(local_key, span_key):
            return False, ["medication_discontinued_or_held"]
        if " vs placebo" in local_key or " versus placebo" in local_key:
            return False, ["trial_placebo_medication_context"]
        if record.get("assertion_status_preview") == "present" and re.search(r"\b(may require|consider|plan(?:s)? (?:were )?initiated|plan to|would need)\b", local_key):
            return False, ["planned_or_hypothetical_medication_context"]

    if category == "procedure":
        procedure_text = f"{span_key} {pretty_key} {evidence_key}"
        if not contains_procedure_allow_term(procedure_text):
            return False, ["procedure_not_in_strict_allowlist"]
        if any(term in procedure_text for term in ("admit weight", "current diet order", "nutrition support")):
            return False, ["administrative_or_nutrition_procedure"]
        if "oxygen therapy" in pretty_key and "desaturation" in local_key:
            return False, ["oxygen_desaturation_not_oxygen_therapy"]
        if "assessment" in evidence_key and not re.search(r"\b(on|using|started|continued|maintained|requires?|received|placed on)\b[^.;:]{0,60}\b" + re.escape(span_key), local_key):
            return False, ["procedure_assessment_heading_not_assertion"]
        if any(term in local_key for term in ("consult", "service list", "renal/plastics/vascular")) and "wound care" in local_key:
            return False, ["service_list_not_procedure_assertion"]
        if record.get("assertion_status_preview") == "possible":
            present_procedure_patterns = [
                rf"\b(underwent|had|completed|performed)\b[^.;:,]{{0,45}}\b{re.escape(span_key)}\b",
                rf"\b(seen|found|noted)\s+on\s+[^.;:,]{{0,20}}\b{re.escape(span_key)}\b",
                rf"\b{re.escape(span_key)}\b[^.;:,]{{0,45}}\b(showed|shows|demonstrated|revealed|was consistent)\b",
                rf"\bwork-?up\s+of\s+possible\b[^.;:,]{{0,80}}\bwith\s+{re.escape(span_key)}\b",
            ]
            if span_key and any(re.search(pattern, evidence_key) for pattern in present_procedure_patterns):
                return False, ["procedure_possible_scope_describes_result_not_procedure"]
            possible_procedure_patterns = [
                rf"\b(possible|probable|planned|pending|consider|considering|would obtain|would need|plan to|for possible|for probable)\b[^.;:,]{{0,50}}\b{re.escape(span_key)}\b",
                rf"\b{re.escape(span_key)}\b[^.;:,]{{0,50}}\b(possible|probable|pending|planned|this admission|tomorrow|vs|versus|under discussion|were discussed)\b",
            ]
            if span_key and not any(re.search(pattern, evidence_key) for pattern in possible_procedure_patterns):
                return False, ["procedure_possible_scope_not_about_procedure"]

    if category == "social_history" and "social_target_context" not in flags:
        return False, ["social_history_without_social_target_context"]
    if category == "social_history" and not any(
        term in context_key
        for term in ("alcohol", "etoh", "smok", "tobacco", "lives with", "living", "homeless", "occupation", "married")
    ):
        return False, ["social_history_without_specific_social_evidence"]

    reasons.append("strict_keep_candidate")
    return True, reasons


def make_pair(record: dict[str, Any]) -> dict[str, Any]:
    is_value = record.get("filter_action") == "value_parse"
    assertion = record.get("assertion_status_preview")
    if is_value and assertion == "not_applicable":
        assertion = "present"
    return {
        "cui": record.get("umls_cui"),
        "canonical_name": record.get("pretty_name") or "",
        "span": clean(record.get("span")),
        "semantic_types": list(record.get("type_ids") or []),
        "semantic_groups": [record.get("concept_category_preview") or "unknown"],
        "concept_category": record.get("concept_category_preview") or "unknown",
        "assertion_status": assertion or "present",
        "experiencer": record.get("experiencer_preview") or "patient",
        "temporality": record.get("temporality_preview") or "current",
        "value_status": record.get("value_status") if is_value else "not_applicable",
        "numeric_value": record.get("numeric_value") if is_value else None,
        "unit": record.get("unit") if is_value else None,
        "measurement_context": record.get("measurement_context") if is_value else "not_applicable",
        "section": record.get("section") or "unknown",
        "evidence": clean(record.get("evidence")),
        "linker_name": "medcat_umls_snomed",
        "linker_score": record.get("linker_score"),
        "confidence": confidence_for(record),
        "needs_review": False,
    }


def make_candidate(record: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "span",
        "pretty_name",
        "umls_cui",
        "concept_id",
        "concept_id_system",
        "concept_category_preview",
        "assertion_status_preview",
        "experiencer_preview",
        "temporality_preview",
        "value_status",
        "numeric_value",
        "unit",
        "measurement_context",
        "filter_action",
        "reason_codes",
        "filter_flags",
        "linker_score",
        "section",
    ]
    return {key: record.get(key) for key in keys}


def render_prompt(template: str, schema_json: str, note_text: str, candidates: list[dict[str, Any]]) -> str:
    return (
        template.replace("{{SCHEMA_JSON}}", schema_json)
        .replace("{{NOTE_TEXT}}", note_text)
        .replace("{{LINKER_CANDIDATES_JSON}}", json.dumps(candidates, ensure_ascii=False, indent=2))
    )


def split_examples(examples: list[dict[str, Any]], train_frac: float, dev_frac: float) -> dict[str, list[dict[str, Any]]]:
    n = len(examples)
    n_train = int(n * train_frac)
    n_dev = int(n * dev_frac)
    return {
        "train": examples[:n_train],
        "dev": examples[n_train : n_train + n_dev],
        "test": examples[n_train + n_dev :],
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def open_input_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8")


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_jsonl)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)

    schema_json = json.dumps(json.loads(Path(args.schema).read_text(encoding="utf-8")), indent=2)
    template = Path(args.prompt_template).read_text(encoding="utf-8")
    rng = random.Random(args.seed)

    examples: list[dict[str, Any]] = []
    rejected = Counter()
    accepted = Counter()
    accepted_categories = Counter()
    accepted_actions = Counter()
    input_records = 0

    with open_input_text(input_path) as fh:
        for line_no, line in enumerate(fh, 1):
            input_records = line_no
            record = json.loads(line)
            ok, reasons = strict_decision(record, args.min_keep_score)
            if not ok:
                rejected.update(reasons)
                continue
            pair = make_pair(record)
            target = {"pairs": [pair]}
            note_text = clean(record.get("evidence_context"))
            candidates = [make_candidate(record)]
            completion = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
            example = {
                "example_id": f"strict_v2_4_12_{len(examples) + 1:06d}",
                "source_line_no": line_no,
                "source_candidate_file": str(input_path),
                "note_row_id": record.get("note_row_id"),
                "subject_id": record.get("subject_id"),
                "hadm_id": record.get("hadm_id"),
                "chartdate": record.get("chartdate"),
                "category": record.get("category"),
                "section": record.get("section"),
                "note_text": note_text,
                "linker_candidates": candidates,
                "target": target,
                "prompt": render_prompt(template, schema_json, note_text, candidates),
                "completion": completion,
                "messages": [
                    {"role": "user", "content": render_prompt(template, schema_json, note_text, candidates)},
                    {"role": "assistant", "content": completion},
                ],
                "strict_reasons": reasons,
            }
            examples.append(example)
            accepted.update(reasons)
            accepted_categories[pair["concept_category"]] += 1
            accepted_actions[record.get("filter_action")] += 1

    eligible_examples_before_cap = len(examples)
    rng.shuffle(examples)
    if args.max_examples > 0:
        examples = examples[: args.max_examples]

    written_accepted = Counter()
    written_categories = Counter()
    written_actions = Counter()
    for example in examples:
        written_accepted.update(example.get("strict_reasons", []))
        pair = example["target"]["pairs"][0]
        written_categories[pair["concept_category"]] += 1
        written_actions[example["linker_candidates"][0].get("filter_action")] += 1

    splits = split_examples(examples, args.train_frac, args.dev_frac)
    for split, rows in splits.items():
        write_jsonl(out_dir / f"{split}.jsonl", rows)
    write_jsonl(out_dir / "all_examples.jsonl", examples)

    summary = {
        "input_jsonl": str(input_path),
        "input_records": input_records,
        "output_dir": str(out_dir),
        "seed": args.seed,
        "total_examples": len(examples),
        "eligible_examples_before_cap": eligible_examples_before_cap,
        "split_counts": {key: len(value) for key, value in splits.items()},
        "accepted_reason_counts": dict(written_accepted),
        "accepted_action_counts": dict(written_actions),
        "accepted_concept_category_counts": dict(written_categories),
        "eligible_reason_counts_before_cap": dict(accepted),
        "eligible_action_counts_before_cap": dict(accepted_actions),
        "eligible_concept_category_counts_before_cap": dict(accepted_categories),
        "rejected_reason_counts": dict(rejected),
        "min_keep_score": args.min_keep_score,
        "schema": str(Path(args.schema)),
        "prompt_template": str(Path(args.prompt_template)),
    }
    summary_path = out_dir / "dataset_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary_path.chmod(0o600)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
