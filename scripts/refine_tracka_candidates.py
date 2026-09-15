#!/usr/bin/env python3
"""Post-process final candidates for Track A entity/assertion evaluation.

This stage is deliberately downstream of MedCAT, measurement overlay,
ontology overlay, and assertion overlay. It keeps the enriched candidate rows
available, but can demote rows that are useful context yet outside a strict
entity/assertion scoring view.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import extract_medcat_candidates as base


TARGET_ACTIONS = {"keep_candidate", "review_candidate", "value_parse"}
STRICT_KEEP_ACTIONS = {"keep_candidate", "review_candidate", "value_parse"}

SPACE = re.compile(r"\s+")
TOKEN = r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*"
BOUNDARY = re.compile(r"[.;\n]")

STRICT_NOISE_EXACT = {
    "admission",
    "admitted",
    "adult",
    "air",
    "breathing",
    "discharge",
    "discharge home",
    "family history",
    "family history father",
    "follow up",
    "follow-up",
    "hospital admission",
    "hospitalized",
    "imaging",
    "oncology follow up",
    "oxygen",
    "oxygenation",
    "reports",
    "rises",
    "social history",
    "stent evaluation",
    "symptom directed care",
    "therapy",
}

STRICT_NOISE_PREFIXES = (
    "family history:",
    "social history",
    "discharge home",
    "follow-up",
    "follow up",
)

LOW_INFORMATION_SPANS = {
    "abnormal",
    "abnormality",
    "acute",
    "bilateral",
    "blood",
    "cardiac",
    "chest",
    "clinical",
    "current",
    "elevated",
    "evaluation",
    "high",
    "low",
    "moderate",
    "normal",
    "present",
    "regular",
    "right",
    "left",
    "severe",
}

STANDALONE_PROCEDURE_NOISE = {
    "admitted",
    "admission",
    "hospital admission",
    "hospitalized",
    "discharge",
    "discharge home",
    "follow-up",
    "follow up",
    "imaging",
    "therapy",
    "oxygen",
}

HEADER_ONLY_CATEGORIES = {"clinical_attribute", "other", "anatomy"}
HEADER_ONLY_SPAN = re.compile(
    r"^(?:social history|family history|history|assessment|plan|imaging|labs?|vitals?|"
    r"presentation|hospital course|discharge|follow[- ]?up)$",
    re.I,
)

FAMILY_CUE = re.compile(
    r"\b(?:family history|fhx|family hx|mother|father|sister|brother|daughter|son|"
    r"maternal|paternal|aunt|uncle|sibling)\b",
    re.I,
)
SOCIAL_CUE = re.compile(
    r"\b(?:social history|smok|tobacco|alcohol|asbestos exposure|pack[- ]?years?|homeless|lives? alone|housing)\b",
    re.I,
)

CURRENT_ADMISSION_SECTION = re.compile(
    r"\b(?:hospital course|brief hospital course|presentation|hpi|history of present illness|"
    r"assessment|plan|assessment/plan|treatment|therapeutic intervention)\b",
    re.I,
)
REMOTE_CUE = re.compile(r"\b(?:remote|prior|previous|previously|history of|h/o|hx of|s/p|status post|years? ago|months? ago)\b", re.I)
CURRENT_PROCEDURE_CUE = re.compile(
    r"\b(?:completed|underwent|received|treated with|started|started on|continued|continue|"
    r"maintained on|placed|exchanged|performed|done)\b",
    re.I,
)
POSTOP_AFTER_CUE = re.compile(r"\b(?:postoperative day|post-op day|post op day|pod)\b", re.I)

POSSIBLE_BEFORE = re.compile(
    r"\b(?:admission concern(?: was| is)?|primary concern(?: was| is| for)?|concern(?:ing)? for|"
    r"suspected|suspicious for|possible|question of|differential(?: diagnosis)? includes?|"
    r"under evaluation|workup for|evaluate for|less likely|cannot rule out|cannot exclude)\b",
    re.I,
)
POSSIBLE_AFTER = re.compile(
    r"\b(?:remains? (?:in the )?differential|is less likely|less likely|under evaluation|"
    r"pending (?:confirmation|workup|pathology|imaging|culture|cultures)|cannot be excluded|not ruled out)\b",
    re.I,
)
IF_CUE = re.compile(r"\b(?:if|unless|when|whenever)\b", re.I)
IF_ANTECEDENT_VERB = re.compile(
    r"^\s*(?:occurs?|develops?|recurs?|persists?|worsens?|returns?|rises?|falls?|drops?|"
    r"remains?|fails? to improve|is confirmed|is suspected|is seen|is found)\b",
    re.I,
)
ACTION_CUE = re.compile(
    r"\b(?:plan|plans|planned|planning|obtain|repeat|consider|schedule|arrange|pursue|"
    r"start|begin|initiate|give|administer|consult|order)\b",
    re.I,
)
PROCEDURE_ACTION_CUE = re.compile(
    r"\b(?:plan|plans|planned|planning|obtain|repeat|consider|schedule|arrange|pursue|"
    r"consult|order)\b",
    re.I,
)
RESERVED_CONDITIONAL = re.compile(r"\b(?:reserved for|as needed|prn|only if|if needed|if necessary)\b", re.I)
STRONG_ABSENT_BEFORE = re.compile(
    r"\b(?:no(?:\s+(?:evidence|signs?)\s+of)?|without|w/o|denies?|denied|negative for|absence of|lack of)"
    r"(?:\s+(?:current|prior|previous|known|personal))?"
    r"(?:\s+(?:history|hx|h/o)(?:\s+of)?)?\s*$",
    re.I,
)
STRONG_ABSENT_AFTER = re.compile(
    r"^\s*(?:is|are|was|were|remains?)?\s*(?:not present|absent|negative|"
    r"not\s+(?:in|part\s+of)\s+(?:the\s+)?history)\b",
    re.I,
)
NEGATED_DETECTION_BEFORE = re.compile(
    r"\b(?:did|does|do|was|were|is|are|has|have|had)\s+not\s+"
    r"(?:show|demonstrate|reveal|confirm|identify|find|detect|support|document|indicate)\b"
    r"(?:\s+(?:any|an?|the|new|acute|current|recurrent|definite|clear)){0,4}\s*$",
    re.I,
)
STRONG_PRESENT_AFTER = re.compile(
    r"^\s*(?:is|are|was|were|remains?)?\s*(?:(?:now|currently)\s+)?"
    r"(?:present|normal|active|ongoing|current|stable|controlled|uncontrolled)\b",
    re.I,
)
STRONG_PRESENT_BEFORE = re.compile(
    r"\b(?:shows?|showed|reveals?|revealed|demonstrates?|demonstrated|confirms?|confirmed|"
    r"diagnostic of|positive for|evidence of)\s*$",
    re.I,
)
CURRENT_TARGET_BEFORE = re.compile(
    r"\b(?:currently\s+(?:being\s+)?treated\s+for|being\s+treated\s+for|treated\s+for|"
    r"currently\s+(?:has|with)|now\s+(?:has|with))\s*$",
    re.I,
)
PLANNED_TARGET_BEFORE = re.compile(
    r"\b(?:planned|scheduled|considered|recommend(?:s|ed)?|plan(?:ned|s|ning)?\s+(?:for|to))\s*$",
    re.I,
)
EXPLICIT_REMOTE_BEFORE = re.compile(
    r"\b(?:remote|prior|previous)\s+(?:(?:personal\s+)?history\s+of\s+)?$|"
    r"\b(?:remote|prior|previous)\s+history\s+(?:is\s+)?(?:notable|significant)\s+for\s*$",
    re.I,
)
EXPLICIT_RESOLVED_AFTER = re.compile(
    r"^\s*(?:,\s*)?(?:(?:is|was|has)\s+)?(?:(?:now|currently)\s+)?(?:resolved|inactive|in remission)\b",
    re.I,
)
EXPLICIT_TIME_AFTER = re.compile(
    r"^\s*(?:,\s*)?(?:in|during)\s+(?:(?:19|20)\d{2}|childhood|adolescence)\b|"
    r"^\s*(?:,\s*)?(?:years?|months?|weeks?)\s+ago\b",
    re.I,
)
RESOLVED_LATER_IN_CLAUSE = re.compile(r"\b(?:now|currently)?\s*(?:resolved|inactive|in remission)\b", re.I)
CURRENT_RESIDUAL_BEFORE = re.compile(r"\b(?:with|has|having)\s+(?:a\s+)?residual\s*$", re.I)
CURRENT_TREATMENT_AFTER = re.compile(r"^\s*(?:is|was)?\s*(?:being\s+)?treated\s+with\b", re.I)
CURRENT_TREATMENT_BEFORE = re.compile(r"\b(?:is|was)?\s*(?:being\s+)?treated\s+with\s*$", re.I)
CURRENT_ENCOUNTER_EVENT_AFTER = re.compile(
    r"^\s*(?:was|were)?\s*(?:performed|completed)?\s*(?:today|yesterday|during\s+(?:this|the)\s+admission|this\s+admission)\b",
    re.I,
)
PLANNED_EVENT_AFTER = re.compile(
    r"^\s*[\)\]\}:,]*\s*(?:is|was|were|has been)?\s*(?:arranged|planned|scheduled|requested|recommended)\b",
    re.I,
)
PLANNED_LATER_AFTER = re.compile(
    r"^[^.;\n]{0,80}\b(?:is|was|were|has been)\s+"
    r"(?:arranged|planned|scheduled|requested|recommended)\b",
    re.I,
)
MONITOR_TARGET_BEFORE = re.compile(
    r"\b(?:monitor|monitoring|watch|watching|screen|screening|evaluate|evaluation|workup)\s+for\s*$",
    re.I,
)
QUALITATIVE_RESULT_AFTER = re.compile(
    r"^\s*(?:is|are|was|were|remains?|result(?:ed)?\s+as)?\s*"
    r"(?P<status>positive|negative|normal|abnormal|high|low|elevated|increased|decreased|reduced)\b",
    re.I,
)
QUALITATIVE_LAB_TARGET = re.compile(
    r"\b(?:cultures?|tests?|screens?|pcr|assays?|serolog(?:y|ies)|antigens?|antibod(?:y|ies))\b",
    re.I,
)
EXPLICIT_PAST_AFTER = re.compile(
    r"^\s*(?:is|are|was|were)?\s*(?:listed|documented|recorded)\s+in\s+(?:the\s+)?past\s+history\b|"
    r"^\s*(?:was|were)?\s*(?:stable|present|documented)?\s*(?:at|during)\s+(?:the\s+)?"
    r"(?:prior|previous)\s+admission\b",
    re.I,
)

TRAILING_FINDING = re.compile(
    r"\s+(?:infiltrate|infiltrates|opacity|opacities|consolidation|effusion|effusions|"
    r"dysfunction|thromboembolism|embolism|edema|oedema|bleeding|mass|lesion|lesions)\b",
    re.I,
)
PAREN_ABBREV = re.compile(r"\s*\(([A-Za-z][A-Za-z0-9/-]{0,12}s?)\)")
ETIOLOGY_SUFFIX = re.compile(
    r"^\s+due\s+to\s+(?P<etiology>(?:chronic\s+|viral\s+)?hepatitis\s+[A-E])\b",
    re.I,
)
REDUNDANT_LAB_PREFIXES = {
    "bp blood pressure": "blood pressure",
    "hr heart rate": "heart rate",
    "rr respiratory rate": "respiratory rate",
}
CONTRAST_COORDINATION = re.compile(
    r"\b(?:no|without)\s+(?P<left>[A-Za-z][A-Za-z0-9 /-]{1,40}?)\s*,\s*"
    r"(?:but\s+)?(?P<right>[A-Za-z][A-Za-z0-9 /-]{1,40}?)\s+(?:is\s+)?present\b",
    re.I,
)
LEADING_ASSERTION_ENTITY_CUE = re.compile(r"^(?:no|without|w/o|negative for|denies?|denied)\s+", re.I)
LEADING_CONTEXT_ENTITY_CUE = re.compile(
    r"^(?:(?:no|without|denies?|denied)\s+)?(?:personal\s+|family\s+)?(?:history|hx|h/o)\s+(?:of\s+)?",
    re.I,
)
LEADING_TEMPORAL_ENTITY_CUE = re.compile(r"^(?:remote|prior|previous|resolved|childhood)\s+", re.I)
TRAILING_ASSERTION_ENTITY_CUE = re.compile(
    r"\s*,?\s*(?:(?:now|currently)\s+)?(?:resolved|absent|present|inactive)\s*$",
    re.I,
)

FINDING_RESCUE_SPAN = re.compile(
    r"\b(?:murmur|crackles?|rales?|wheez(?:e|ing)|edema|oedema|infiltrate|infiltrates|"
    r"opacity|opacities|consolidation|effusion|effusions|dyspnea|hypoxia|hypoxemia|"
    r"distress|abnormality|tenderness|pain|bleeding)\b",
    re.I,
)

MEDICATION_RESCUE_SPANS = {
    "acetaminophen",
    "ampicillin-sulbactam",
    "calcium gluconate",
    "cefepime",
    "furosemide",
    "heparin",
    "heparin infusion",
    "hydroxyurea",
    "insulin",
    "insulin with dextrose",
    "intravenous antibiotics",
    "intravenous furosemide",
    "intravenous iron",
    "opioid analgesia",
    "packed red blood cells",
    "piperacillin-tazobactam",
    "sodium zirconium cyclosilicate",
}

LEFT_EXPANSIONS = (
    "acute decompensated",
    "acute on chronic",
    "metastatic",
    "acute",
    "moderate",
    "mild",
    "severe",
    "cardiac",
    "venous",
    "peripheral",
    "right lower lobe",
    "left lower lobe",
    "right middle lobe",
    "left upper lobe",
    "bilateral interstitial",
    "interstitial",
    "high-flow",
)

BOUNDARY_LEFT_TARGETS = {
    "asbestos exposure",
    "breast cancer",
    "cigarette smoking",
    "decompensated heart failure",
    "dysfunction",
    "heart failure",
    "heart sound",
    "mitral regurgitation",
    "pancreatic adenocarcinoma",
    "pulmonary angiography",
    "respiratory distress syndrome",
    "thromboembolism",
}

TARGETED_LEFT_EXPANSIONS = [
    {
        "span": "atelectasis",
        "pattern": re.compile(r"\bbibasilar\s+atelectasis\b", re.I),
        "reason": "tracka_boundary_bibasilar_atelectasis",
    },
    {
        "span": "colon cancer",
        "pattern": re.compile(r"\bmetastatic\s+colon\s+cancer\b", re.I),
        "reason": "tracka_boundary_metastatic_colon_cancer",
    },
    {
        "span": "melanoma",
        "pattern": re.compile(r"\bmetastatic\s+melanoma\b", re.I),
        "reason": "tracka_boundary_metastatic_melanoma",
    },
    {
        "span": "lymphoma",
        "pattern": re.compile(r"\brecurrent\s+lymphoma\b", re.I),
        "reason": "tracka_boundary_recurrent_lymphoma",
    },
    {
        "span": "weakness",
        "pattern": re.compile(r"\bfocal\s+weakness\b", re.I),
        "reason": "tracka_boundary_focal_weakness",
    },
    {
        "span": "chest pain",
        "pattern": re.compile(r"\bintermittent\s+chest\s+pain\b", re.I),
        "reason": "tracka_boundary_intermittent_chest_pain",
    },
    {
        "span": "mass",
        "pattern": re.compile(r"\b(?:new\s+palpable\s+)?left\s+breast\s+mass\b", re.I),
        "reason": "tracka_boundary_left_breast_mass",
    },
    {
        "span": "diabetes",
        "pattern": re.compile(r"\btype\s+2\s+diabetes\b", re.I),
        "reason": "tracka_boundary_type2_diabetes",
    },
    {
        "span": "kidney injury",
        "pattern": re.compile(r"\bacute(?:-|\s+)on(?:-|\s+)chronic\s+kidney\s+injury\b", re.I),
        "reason": "tracka_boundary_acute_on_chronic_kidney_injury",
    },
    {
        "span": "ischemic stroke",
        "pattern": re.compile(r"\bacute\s+ischemic\s+stroke\b", re.I),
        "reason": "tracka_boundary_acute_ischemic_stroke",
    },
    {
        "span": "creatinine",
        "pattern": re.compile(r"\bserum\s+creatinine\b", re.I),
        "reason": "tracka_boundary_serum_creatinine",
    },
    {
        "span": "vitamin",
        "pattern": re.compile(r"\bprenatal\s+vitamin\b", re.I),
        "reason": "tracka_boundary_prenatal_vitamin",
    },
    {
        "span": "s1",
        "pattern": re.compile(r"\bnormal\s+S1\b", re.I),
        "reason": "tracka_boundary_normal_s1",
    },
    {
        "span": "s2",
        "pattern": re.compile(r"\bnormal\s+S2\b", re.I),
        "reason": "tracka_boundary_normal_s2",
    },
    {
        "span": "heart sound",
        "pattern": re.compile(r"\b(?:S1|S2)\s+heart sound\b", re.I),
        "reason": "tracka_boundary_heart_sound_prefix",
    },
    {
        "span": "pulmonary angiography",
        "pattern": re.compile(r"\bCT\s+pulmonary angiography\b", re.I),
        "reason": "tracka_boundary_ct_pulmonary_angiography",
    },
    {
        "span": "tobacco use",
        "pattern": re.compile(r"\b(?:current|former|daily|active)\s+tobacco use\b", re.I),
        "reason": "tracka_boundary_tobacco_use_modifier",
    },
    {
        "span": "cigarette smoking",
        "pattern": re.compile(r"\b(?:current|former|daily|active)\s+cigarette smoking\b", re.I),
        "reason": "tracka_boundary_cigarette_smoking_modifier",
    },
    {
        "span": "smoking",
        "pattern": re.compile(r"\b(?:current|former|daily|active)\s+smoking\b", re.I),
        "reason": "tracka_boundary_smoking_modifier",
    },
    {
        "span": "alcohol use",
        "pattern": re.compile(r"\b(?:current|former|daily|occasional|heavy)\s+alcohol use\b", re.I),
        "reason": "tracka_boundary_alcohol_use_modifier",
    },
    {
        "span": "asbestos exposure",
        "pattern": re.compile(r"\b(?:prior|former|remote|occupational)\s+asbestos exposure\b", re.I),
        "reason": "tracka_boundary_asbestos_exposure_modifier",
    },
    {
        "span": "lymphoma",
        "pattern": re.compile(r"\b(?:treated|remote treated|previously treated)\s+(?:hodgkin\s+)?lymphoma\b", re.I),
        "reason": "tracka_boundary_treated_lymphoma",
    },
    {
        "span": "pulmonary embolism",
        "pattern": re.compile(r"\bpossible\s+pulmonary embolism\b", re.I),
        "reason": "tracka_boundary_possible_pulmonary_embolism",
    },
    {
        "span": "oxygen",
        "pattern": re.compile(r"\bsupplemental oxygen\b", re.I),
        "reason": "tracka_boundary_supplemental_oxygen",
    },
    {
        "span": "bleeding",
        "pattern": re.compile(r"\bactive bleeding\b", re.I),
        "reason": "tracka_boundary_active_bleeding",
    },
    {
        "span": "neurologic deficit",
        "pattern": re.compile(r"\bfocal neurologic deficit\b", re.I),
        "reason": "tracka_boundary_focal_neurologic_deficit",
    },
    {
        "span": "support",
        "pattern": re.compile(r"\bvasopressor support\b", re.I),
        "reason": "tracka_boundary_vasopressor_support",
    },
    {
        "span": "radiograph",
        "pattern": re.compile(r"\bchest radiograph\b", re.I),
        "reason": "tracka_boundary_chest_radiograph",
    },
    {
        "span": "mass",
        "pattern": re.compile(r"\bpancreatic head mass\b", re.I),
        "reason": "tracka_boundary_pancreatic_head_mass",
    },
    {
        "span": "obstruction",
        "pattern": re.compile(r"\bmalignant biliary obstruction\b", re.I),
        "reason": "tracka_boundary_malignant_biliary_obstruction",
    },
    {
        "span": "furosemide",
        "pattern": re.compile(r"\bintravenous furosemide\b", re.I),
        "reason": "tracka_boundary_intravenous_furosemide",
    },
    {
        "span": "iron",
        "pattern": re.compile(r"\bintravenous iron\b", re.I),
        "reason": "tracka_boundary_intravenous_iron",
    },
    {
        "span": "stent placement",
        "pattern": re.compile(r"\bbiliary stent placement\b", re.I),
        "reason": "tracka_boundary_biliary_stent_placement",
    },
    {
        "span": "signs",
        "pattern": re.compile(r"\bperitoneal signs\b", re.I),
        "reason": "tracka_boundary_peritoneal_signs",
    },
]

ANCHOR_SPECS: list[dict[str, Any]] = [
    {
        "name": "Non-Hodgkin lymphoma",
        "pattern": r"\bNon[- ]Hodgkin lymphoma\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:NON_HODGKIN_LYMPHOMA",
        "pretty_name": "Non-Hodgkin lymphoma",
        "assertion": "present",
    },
    {
        "name": "renal failure",
        "pattern": r"\brenal failure\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:RENAL_FAILURE",
        "pretty_name": "Renal failure",
        "assertion": "present",
    },
    {
        "name": "atrial fibrillation",
        "pattern": r"\batrial fibrillation\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:ATRIAL_FIBRILLATION",
        "pretty_name": "Atrial fibrillation",
        "assertion": "present",
    },
    {
        "name": "mild erythema",
        "pattern": r"\bmild erythema\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:MILD_ERYTHEMA",
        "pretty_name": "Mild erythema",
        "assertion": "present",
    },
    {
        "name": "acetaminophen",
        "pattern": r"\bacetaminophen\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:ACETAMINOPHEN",
        "pretty_name": "Acetaminophen",
        "assertion": "present",
    },
    {
        "name": "potassium chloride",
        "pattern": r"\bpotassium chloride\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:POTASSIUM_CHLORIDE",
        "pretty_name": "Potassium chloride",
        "assertion": "present",
    },
    {
        "name": "KCl",
        "pattern": r"(?<![A-Za-z0-9])KCl(?![A-Za-z0-9])",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:POTASSIUM_CHLORIDE",
        "pretty_name": "Potassium chloride",
        "assertion": "present",
    },
    {
        "name": "K-Dur",
        "pattern": r"(?i)(?<![A-Za-z0-9])K-?Dur(?![A-Za-z0-9])",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:POTASSIUM_CHLORIDE_EXTENDED_RELEASE",
        "pretty_name": "Potassium chloride extended release",
        "assertion": "present",
    },
    {
        "name": "physical therapy abbreviation",
        "pattern": r"(?<![A-Za-z0-9])PT(?![A-Za-z0-9])",
        "after_pattern": r"(?i)^\s+(?:daily|for\s+mobility|for\s+rehab|was\s+requested|is\s+requested)",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PHYSICAL_THERAPY",
        "pretty_name": "Physical therapy",
        "assertion": "present",
    },
    {
        "name": "nonstress test abbreviation",
        "pattern": r"(?<![A-Za-z0-9])NST(?![A-Za-z0-9])",
        "before_pattern": r"(?i)\b(?:recommend(?:s|ed)?|plan(?:ned)?|schedule(?:d)?)\b[^.;\n]{0,35}$",
        "after_pattern": r"(?i)^\s+(?:today|tomorrow|planned|scheduled)\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:NONSTRESS_TEST",
        "pretty_name": "Fetal nonstress test",
        "assertion": "hypothetical",
    },
    {
        "name": "echocardiogram abbreviation",
        "pattern": r"(?i)\becho\b",
        "before_pattern": r"(?i)\b(?:on|by|repeat|obtain|review(?:ed)?|shows?|confirms?|demonstrates?)\b[^.;\n]{0,35}$",
        "after_pattern": r"(?i)^\s+(?:shows?|confirms?|demonstrates?)\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:ECHOCARDIOGRAM",
        "pretty_name": "Echocardiogram",
        "assertion": "present",
    },
    {
        "name": "S1 and S2",
        "pattern": r"(?i)\bS1\s+and\s+S2\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:NORMAL_S1_S2",
        "pretty_name": "Normal S1 and S2 heart sounds",
        "assertion": "present",
    },
    {
        "name": "normal S1",
        "pattern": r"\bnormal\s+S1\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:NORMAL_S1",
        "pretty_name": "Normal S1 heart sound",
        "assertion": "present",
    },
    {
        "name": "normal S2",
        "pattern": r"\bnormal\s+S2\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:NORMAL_S2",
        "pretty_name": "Normal S2 heart sound",
        "assertion": "present",
    },
    {
        "name": "S1 heart sound",
        "pattern": r"\bS1\s+heart sound\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:S1_HEART_SOUND",
        "pretty_name": "S1 heart sound",
        "assertion": "present",
    },
    {
        "name": "S2 heart sound",
        "pattern": r"\bS2\s+heart sound\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:S2_HEART_SOUND",
        "pretty_name": "S2 heart sound",
        "assertion": "present",
    },
    {
        "name": "S1",
        "pattern": r"(?<![A-Za-z0-9])S1(?![A-Za-z0-9])",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:S1",
        "pretty_name": "S1 heart sound",
        "assertion": "present",
    },
    {
        "name": "S2",
        "pattern": r"(?<![A-Za-z0-9])S2(?![A-Za-z0-9])",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:S2",
        "pretty_name": "S2 heart sound",
        "assertion": "present",
    },
    {
        "name": "regular rate and rhythm",
        "pattern": r"\bregular rate and rhythm\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:REGULAR_RATE_RHYTHM",
        "pretty_name": "Regular rate and rhythm",
        "assertion": "present",
    },
    {
        "name": "repeat ERCP",
        "pattern": r"\brepeat\s+ERCP\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:REPEAT_ERCP",
        "pretty_name": "Repeat endoscopic retrograde cholangiopancreatography",
        "assertion": "hypothetical",
    },
    {
        "name": "ERCP",
        "pattern": r"(?<![A-Za-z0-9])ERCP(?![A-Za-z0-9])",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:ERCP",
        "pretty_name": "Endoscopic retrograde cholangiopancreatography",
        "assertion": "present",
    },
    {
        "name": "CRRT",
        "pattern": r"(?<![A-Za-z0-9])CRRT(?![A-Za-z0-9])",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:CRRT",
        "pretty_name": "Continuous renal replacement therapy",
        "assertion": "conditional",
    },
    {
        "name": "status post AVR",
        "pattern": r"\bstatus\s+post\s+AVR\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:STATUS_POST_AVR",
        "pretty_name": "Status post aortic valve replacement",
        "assertion": "historical",
    },
    {
        "name": "AVR",
        "pattern": r"(?<![A-Za-z0-9])AVR(?![A-Za-z0-9])",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:AVR",
        "pretty_name": "Aortic valve replacement",
        "assertion": "historical",
    },
    {
        "name": "CABG",
        "pattern": r"(?<![A-Za-z0-9])CABG(?![A-Za-z0-9])",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:CABG",
        "pretty_name": "Coronary artery bypass grafting",
        "assertion": "historical",
    },
    {
        "name": "high-flow nasal cannula",
        "pattern": r"\bhigh[- ]flow nasal cannula\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:HFNC",
        "pretty_name": "High-flow nasal cannula",
        "assertion": "present",
    },
    {
        "name": "broad-spectrum antibiotics",
        "pattern": r"\bbroad[- ]spectrum antibiotics\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:BROAD_SPECTRUM_ANTIBIOTICS",
        "pretty_name": "Broad-spectrum antibiotics",
        "assertion": "conditional",
    },
    {
        "name": "empiric antibiotics",
        "pattern": r"\bempiric antibiotics\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:EMPIRIC_ANTIBIOTICS",
        "pretty_name": "Empiric antibiotics",
        "assertion": "conditional",
    },
    {
        "name": "interventional radiology",
        "pattern": r"\binterventional radiology\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:INTERVENTIONAL_RADIOLOGY",
        "pretty_name": "Interventional radiology procedure",
        "assertion": "present",
    },
    {
        "name": "intra-aortic balloon pump",
        "pattern": r"\bintra[- ]aortic balloon pump\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:IABP",
        "pretty_name": "Intra-aortic balloon pump",
        "assertion": "hypothetical",
    },
    {
        "name": "temporary dialysis catheter placement",
        "pattern": r"\btemporary dialysis catheter placement\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:TEMP_DIALYSIS_CATHETER_PLACEMENT",
        "pretty_name": "Temporary dialysis catheter placement",
        "assertion": "hypothetical",
    },
    {
        "name": "indwelling pleural catheter placement",
        "pattern": r"\bindwelling pleural catheter placement\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:INDWELLING_PLEURAL_CATHETER_PLACEMENT",
        "pretty_name": "Indwelling pleural catheter placement",
        "assertion": "hypothetical",
    },
    {
        "name": "murmur",
        "pattern": r"\bmurmur\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:MURMUR",
        "pretty_name": "Heart murmur",
        "assertion": "present",
    },
    {
        "name": "peripheral edema",
        "pattern": r"\bperipheral edema\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:PERIPHERAL_EDEMA",
        "pretty_name": "Peripheral edema",
        "assertion": "present",
    },
    {
        "name": "right basilar crackles",
        "pattern": r"\bright basilar crackles\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:RIGHT_BASILAR_CRACKLES",
        "pretty_name": "Right basilar crackles",
        "assertion": "present",
    },
    {
        "name": "bibasilar crackles",
        "pattern": r"\bbibasilar crackles\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:BIBASILAR_CRACKLES",
        "pretty_name": "Bibasilar crackles",
        "assertion": "present",
    },
    {
        "name": "coarse crackles",
        "pattern": r"\bcoarse crackles\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:COARSE_CRACKLES",
        "pretty_name": "Coarse crackles",
        "assertion": "present",
    },
    {
        "name": "coarse breath sounds",
        "pattern": r"\bcoarse breath sounds\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:COARSE_BREATH_SOUNDS",
        "pretty_name": "Coarse breath sounds",
        "assertion": "present",
    },
    {
        "name": "poor oral intake",
        "pattern": r"\bpoor oral intake\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:POOR_ORAL_INTAKE",
        "pretty_name": "Poor oral intake",
        "assertion": "present",
    },
    {
        "name": "digital clubbing",
        "pattern": r"\bdigital clubbing\b",
        "category": "finding",
        "concept_id": "CLINICAL_FINDING:DIGITAL_CLUBBING",
        "pretty_name": "Digital clubbing",
        "assertion": "present",
    },
    {
        "name": "cholangiocarcinoma",
        "pattern": r"\bcholangiocarcinoma\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:CHOLANGIOCARCINOMA",
        "pretty_name": "Cholangiocarcinoma",
        "assertion": "present",
    },
    {
        "name": "treated Hodgkin lymphoma",
        "pattern": r"\btreated Hodgkin lymphoma\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:TREATED_HODGKIN_LYMPHOMA",
        "pretty_name": "Treated Hodgkin lymphoma",
        "assertion": "historical",
    },
    {
        "name": "Hodgkin lymphoma",
        "pattern": r"(?<!Non-)(?<!Non )\bHodgkin lymphoma\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:HODGKIN_LYMPHOMA",
        "pretty_name": "Hodgkin lymphoma",
        "assertion": "present",
    },
    {
        "name": "cystic fibrosis",
        "pattern": r"\bcystic fibrosis\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:CYSTIC_FIBROSIS",
        "pretty_name": "Cystic fibrosis",
        "assertion": "present",
    },
    {
        "name": "anastomotic leak",
        "pattern": r"\banastomotic leak\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:ANASTOMOTIC_LEAK",
        "pretty_name": "Anastomotic leak",
        "assertion": "present",
    },
    {
        "name": "COPD exacerbation",
        "pattern": r"\bCOPD exacerbation\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:COPD_EXACERBATION",
        "pretty_name": "COPD exacerbation",
        "assertion": "present",
    },
    {
        "name": "cholestatic liver injury",
        "pattern": r"\bcholestatic liver injury\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:CHOLESTATIC_LIVER_INJURY",
        "pretty_name": "Cholestatic liver injury",
        "assertion": "present",
    },
    {
        "name": "uremic asterixis",
        "pattern": r"\buremic asterixis\b",
        "category": "disease_or_diagnosis",
        "concept_id": "CLINICAL_DISEASE:UREMIC_ASTERIXIS",
        "pretty_name": "Uremic asterixis",
        "assertion": "present",
    },
    {
        "name": "piperacillin-tazobactam",
        "pattern": r"\bpiperacillin[- ]tazobactam\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:PIPERACILLIN_TAZOBACTAM",
        "pretty_name": "Piperacillin-tazobactam",
        "assertion": "present",
    },
    {
        "name": "ampicillin-sulbactam",
        "pattern": r"\bampicillin[- ]sulbactam\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:AMPICILLIN_SULBACTAM",
        "pretty_name": "Ampicillin-sulbactam",
        "assertion": "present",
    },
    {
        "name": "hydroxyurea",
        "pattern": r"\bhydroxyurea\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:HYDROXYUREA",
        "pretty_name": "Hydroxyurea",
        "assertion": "present",
    },
    {
        "name": "insulin with dextrose",
        "pattern": r"\binsulin with dextrose\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:INSULIN_WITH_DEXTROSE",
        "pretty_name": "Insulin with dextrose",
        "assertion": "present",
    },
    {
        "name": "sodium zirconium cyclosilicate",
        "pattern": r"\bsodium zirconium cyclosilicate\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:SODIUM_ZIRCONIUM_CYCLOSILICATE",
        "pretty_name": "Sodium zirconium cyclosilicate",
        "assertion": "present",
    },
    {
        "name": "ipratropium-albuterol",
        "pattern": r"\bipratropium[- ]albuterol\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:IPRATROPIUM_ALBUTEROL",
        "pretty_name": "Ipratropium-albuterol",
        "assertion": "present",
    },
    {
        "name": "levofloxacin",
        "pattern": r"\blevofloxacin\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:LEVOFLOXACIN",
        "pretty_name": "Levofloxacin",
        "assertion": "present",
    },
    {
        "name": "torsemide",
        "pattern": r"\btorsemide\b",
        "category": "medication_or_substance",
        "concept_id": "CLINICAL_MEDICATION:TORSEMIDE",
        "pretty_name": "Torsemide",
        "assertion": "present",
    },
    {
        "name": "urgent gastroenterology evaluation",
        "pattern": r"\burgent gastroenterology evaluation\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:URGENT_GI_EVALUATION",
        "pretty_name": "Urgent gastroenterology evaluation",
        "assertion": "hypothetical",
    },
    {
        "name": "percutaneous biliary drainage",
        "pattern": r"\bpercutaneous biliary drainage\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PERCUTANEOUS_BILIARY_DRAINAGE",
        "pretty_name": "Percutaneous biliary drainage",
        "assertion": "hypothetical",
    },
    {
        "name": "repeat chest radiograph",
        "pattern": r"\brepeat chest radiograph\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:REPEAT_CHEST_RADIOGRAPH",
        "pretty_name": "Repeat chest radiograph",
        "assertion": "hypothetical",
    },
    {
        "name": "repeat echocardiogram",
        "pattern": r"\brepeat echocardiogram\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:REPEAT_ECHOCARDIOGRAM",
        "pretty_name": "Repeat echocardiogram",
        "assertion": "hypothetical",
    },
    {
        "name": "repeat ECG",
        "pattern": r"\brepeat ECG\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:REPEAT_ECG",
        "pretty_name": "Repeat ECG",
        "assertion": "hypothetical",
    },
    {
        "name": "CTA chest",
        "pattern": r"\bCTA chest\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:CTA_CHEST",
        "pretty_name": "CTA chest",
        "assertion": "present",
    },
    {
        "name": "PET scan",
        "pattern": r"\bPET scan\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PET_SCAN",
        "pretty_name": "PET scan",
        "assertion": "present",
    },
    {
        "name": "port placement",
        "pattern": r"\bport placement\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PORT_PLACEMENT",
        "pretty_name": "Port placement",
        "assertion": "hypothetical",
    },
    {
        "name": "pleural catheter placement",
        "pattern": r"\bpleural catheter placement\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PLEURAL_CATHETER_PLACEMENT",
        "pretty_name": "Pleural catheter placement",
        "assertion": "hypothetical",
    },
    {
        "name": "platelet transfusion",
        "pattern": r"\bplatelet transfusion\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PLATELET_TRANSFUSION",
        "pretty_name": "Platelet transfusion",
        "assertion": "hypothetical",
    },
    {
        "name": "fluid bolus",
        "pattern": r"\bfluid bolus\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:FLUID_BOLUS",
        "pretty_name": "Fluid bolus",
        "assertion": "hypothetical",
    },
    {
        "name": "ICU transfer",
        "pattern": r"\bICU transfer\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:ICU_TRANSFER",
        "pretty_name": "ICU transfer",
        "assertion": "hypothetical",
    },
    {
        "name": "prone positioning",
        "pattern": r"\bprone positioning\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:PRONE_POSITIONING",
        "pretty_name": "Prone positioning",
        "assertion": "hypothetical",
    },
    {
        "name": "urgent dialysis",
        "pattern": r"\burgent dialysis\b",
        "category": "procedure",
        "concept_id": "CLINICAL_PROCEDURE:URGENT_DIALYSIS",
        "pretty_name": "Urgent dialysis",
        "assertion": "hypothetical",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes-jsonl-gz", action="append", required=True)
    parser.add_argument("--candidate-jsonl", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--name", default="tracka_refinement_v1")
    parser.add_argument("--strictness", choices=["conservative", "aggressive"], default="conservative")
    parser.add_argument("--disable-strict-gate", action="store_true")
    parser.add_argument("--disable-boundary-ranker", action="store_true")
    parser.add_argument("--disable-anchor-rescue", action="store_true")
    parser.add_argument("--disable-assertion-judge", action="store_true")
    return parser.parse_args()


def clean(text: Any) -> str:
    return SPACE.sub(" ", str(text or "")).strip()


def norm(text: Any) -> str:
    return clean(text).strip(" :;,.()[]{}").lower()


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def open_output(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", compresslevel=1)
    return path.open("w", encoding="utf-8")


def iter_prediction_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("**/medcat_candidates.jsonl")) + sorted(path.glob("**/medcat_candidates.jsonl.gz"))


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except FileNotFoundError:
        pass


def load_notes(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    notes: dict[str, dict[str, Any]] = {}
    for path in paths:
        with open_text(path) as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                text = str(row.get("text") or row.get("TEXT") or row.get("note_text") or "")
                keys = [
                    row.get("row_id"),
                    row.get("note_id"),
                    row.get("note_row_id"),
                    row.get("note_index"),
                    row.get("ROW_ID"),
                    row.get("synthetic_note_id"),
                ]
                meta = {
                    "text": text,
                    "subject_id": str(row.get("subject_id") or row.get("SUBJECT_ID") or ""),
                    "hadm_id": str(row.get("hadm_id") or row.get("HADM_ID") or ""),
                    "chartdate": str(row.get("chartdate") or row.get("CHARTDATE") or ""),
                    "category": str(row.get("category") or row.get("CATEGORY") or "unknown"),
                }
                for key in keys:
                    if key is not None and str(key):
                        notes[str(key)] = meta
    return notes


def row_start(row: dict[str, Any]) -> int:
    return int(row.get("span_start") or 0)


def row_end(row: dict[str, Any]) -> int:
    return int(row.get("span_end") or 0)


def overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def clause_for_span(text: str, start: int, end: int) -> tuple[str, int, int]:
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start), text.rfind("\n", 0, start))
    right_candidates = [idx for idx in (text.find(".", end), text.find(";", end), text.find("\n", end)) if idx >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    clause = text[left + 1 : right]
    return clause, start - left - 1, end - left - 1


def local_text(note_text: str, start: int, end: int, window: int = 180) -> str:
    return note_text[max(0, start - window) : min(len(note_text), end + window)]


def refresh_context(row: dict[str, Any], note_text: str) -> None:
    start = row_start(row)
    end = row_end(row)
    row["section"] = base.section_for_span(note_text, start)
    row["evidence"] = base.sentence_for_span(note_text, start, end, 420)
    context_raw, ctx_start, ctx_end = base.context_with_marker(note_text, start, end)
    context, _ctx_start, _ctx_end = base.clean_context_with_span(context_raw, ctx_start, ctx_end)
    row["evidence_context"] = base.clip(context, 650)
    if row.get("concept_category_preview") == "lab_result_or_value" or row.get("filter_action") == "value_parse":
        row["value_context"] = base.clip(context, 650)


def set_span(row: dict[str, Any], note_text: str, start: int, end: int, reason: str) -> bool:
    old_start = row_start(row)
    old_end = row_end(row)
    start = max(0, min(start, len(note_text)))
    end = max(start, min(end, len(note_text)))
    if start == old_start and end == old_end:
        return False
    old_span = row.get("span") or ""
    row["tracka_boundary_previous_span"] = old_span
    row["tracka_boundary_previous_start"] = old_start
    row["tracka_boundary_previous_end"] = old_end
    row["span_start"] = start
    row["span_end"] = end
    row["span"] = note_text[start:end]
    row["tracka_boundary_reason"] = reason
    row["tracka_boundary_changed"] = True
    row["reason_codes"] = sorted(set([*(row.get("reason_codes") or []), reason]))
    row["span_normalization_applied"] = True
    refresh_context(row, note_text)
    return True


def expand_left(row: dict[str, Any], note_text: str, reason: str) -> bool:
    start = row_start(row)
    end = row_end(row)
    before = note_text[max(0, start - 55) : start]
    best_start = start
    for phrase in LEFT_EXPANSIONS:
        m = re.search(rf"(?i)(?:^|[^A-Za-z0-9])({re.escape(phrase)})\s+$", before)
        if m:
            candidate_start = max(0, start - len(before) + m.start(1))
            if candidate_start < best_start:
                best_start = candidate_start
    if best_start < start:
        return set_span(row, note_text, best_start, end, reason)
    return False


def apply_targeted_left_expansion(row: dict[str, Any], note_text: str) -> bool:
    span_key = norm(row.get("span"))
    if not span_key:
        return False
    start = row_start(row)
    end = row_end(row)
    window_start = max(0, start - 50)
    window_end = min(len(note_text), end + 40)
    window = note_text[window_start:window_end]
    for spec in TARGETED_LEFT_EXPANSIONS:
        if spec["span"] != span_key:
            continue
        match = spec["pattern"].search(window)
        if not match:
            continue
        candidate_start = window_start + match.start()
        candidate_end = window_start + match.end()
        if candidate_start < start and candidate_end >= end:
            if set_span(row, note_text, candidate_start, candidate_end, spec["reason"]):
                return True
    return False


def refine_boundary(row: dict[str, Any], note_text: str) -> bool:
    if row.get("filter_action") not in TARGET_ACTIONS:
        return False
    span_key = norm(row.get("span"))
    category = row.get("concept_category_preview") or ""
    start = row_start(row)
    end = row_end(row)
    changed = False

    if not span_key or end <= start:
        return False

    replacement = REDUNDANT_LAB_PREFIXES.get(span_key)
    if replacement:
        raw_lower = str(row.get("span") or "").lower()
        rel_start = raw_lower.rfind(replacement)
        if rel_start >= 0:
            changed = set_span(
                row,
                note_text,
                start + rel_start,
                start + rel_start + len(replacement),
                "tracka_boundary_strip_redundant_lab_abbrev",
            ) or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    if category in {"disease_or_diagnosis", "finding", "procedure", "other", "clinical_attribute"}:
        cue = LEADING_ASSERTION_ENTITY_CUE.match(str(row.get("span") or ""))
        if cue and len(clean(str(row.get("span") or "")[cue.end() :])) >= 4:
            changed = set_span(row, note_text, start + cue.end(), end, "tracka_boundary_strip_assertion_prefix") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

        cue = LEADING_CONTEXT_ENTITY_CUE.match(str(row.get("span") or ""))
        if cue and len(clean(str(row.get("span") or "")[cue.end() :])) >= 3:
            changed = set_span(row, note_text, start + cue.end(), end, "tracka_boundary_strip_context_prefix") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    if category in {"disease_or_diagnosis", "finding"}:
        cue = LEADING_TEMPORAL_ENTITY_CUE.match(str(row.get("span") or ""))
        if cue and len(clean(str(row.get("span") or "")[cue.end() :])) >= 3:
            changed = set_span(row, note_text, start + cue.end(), end, "tracka_boundary_strip_temporal_prefix") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

        trailing = TRAILING_ASSERTION_ENTITY_CUE.search(str(row.get("span") or ""))
        if trailing and len(clean(str(row.get("span") or "")[: trailing.start()])) >= 3:
            changed = set_span(row, note_text, start, start + trailing.start(), "tracka_boundary_strip_assertion_suffix") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    raw_span = str(row.get("span") or "")
    if category in {
        "disease_or_diagnosis",
        "finding",
        "procedure",
        "medication_or_substance",
        "lab_result_or_value",
    } and raw_span.count("(") > raw_span.count(")"):
        if note_text[end : end + 1] == ")":
            changed = set_span(row, note_text, start, end + 1, "tracka_boundary_close_parenthesis") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    if category in {"disease_or_diagnosis", "finding"}:
        before = note_text[max(0, start - 8) : start]
        non_prefix = re.search(r"(?i)(?<![A-Za-z0-9])non[- ]$", before)
        if non_prefix:
            candidate_start = max(0, start - len(before) + non_prefix.start())
            changed = set_span(row, note_text, candidate_start, end, "tracka_boundary_preserve_non_prefix") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    if span_key in BOUNDARY_LEFT_TARGETS:
        changed = expand_left(row, note_text, "tracka_boundary_left_modifier") or changed
        span_key = norm(row.get("span"))
        start = row_start(row)
        end = row_end(row)

    if category == "finding" and span_key == "s1":
        match = re.match(r"(?i)\s+and\s+S2\b", note_text[end : min(len(note_text), end + 30)])
        if match:
            changed = set_span(row, note_text, start, end + match.end(), "tracka_boundary_s1_and_s2") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    if category in {"finding", "procedure", "clinical_attribute", "other"} and span_key == "incisional":
        match = re.match(r"(?i)\s+pain\b", note_text[end : min(len(note_text), end + 20)])
        if match:
            changed = set_span(row, note_text, start, end + match.end(), "tracka_boundary_incisional_pain") or changed
            span_key = norm(row.get("span"))
            start = row_start(row)
            end = row_end(row)

    changed = apply_targeted_left_expansion(row, note_text) or changed
    span_key = norm(row.get("span"))
    start = row_start(row)
    end = row_end(row)

    after = note_text[end : min(len(note_text), end + 80)]
    if category == "disease_or_diagnosis":
        match = ETIOLOGY_SUFFIX.match(after)
        if match:
            changed = set_span(row, note_text, start, end + match.end(), "tracka_boundary_etiology_suffix") or changed
            start = row_start(row)
            end = row_end(row)
            span_key = norm(row.get("span"))
            after = note_text[end : min(len(note_text), end + 80)]

    if category == "medication_or_substance" and span_key == "albuterol":
        match = re.match(r"(?i)\s+inhaler\b", after)
        if match:
            changed = set_span(row, note_text, start, end + match.end(), "tracka_boundary_medication_form") or changed
            start = row_start(row)
            end = row_end(row)
            span_key = norm(row.get("span"))
            after = note_text[end : min(len(note_text), end + 80)]

    if category in {"disease_or_diagnosis", "finding", "procedure", "medication_or_substance", "lab_result_or_value"} and len(span_key) >= 8:
        m = PAREN_ABBREV.match(after)
        if m:
            abbr = m.group(1)
            upper_count = sum(1 for ch in abbr if ch.isupper())
            if (
                upper_count >= 2
                or "-" in abbr
                or any(ch.isdigit() for ch in abbr)
                or (category == "lab_result_or_value" and len(abbr) == 1 and abbr.isupper())
            ):
                changed = set_span(row, note_text, start, end + m.end(), "tracka_boundary_include_parenthetical_abbrev") or changed
                start = row_start(row)
                end = row_end(row)
                span_key = norm(row.get("span"))

    if span_key in {"right lower lobe", "left lower lobe", "right middle lobe", "left upper lobe", "bilateral interstitial"}:
        m = TRAILING_FINDING.match(note_text[end : min(len(note_text), end + 60)])
        if m:
            changed = set_span(row, note_text, start, end + m.end(), "tracka_boundary_trailing_finding") or changed
            start = row_start(row)
            end = row_end(row)
            span_key = norm(row.get("span"))

    if span_key == "venous":
        m = re.match(r"(?i)\s+thromboembolism\b", note_text[end : min(len(note_text), end + 35)])
        if m:
            changed = set_span(row, note_text, start, end + m.end(), "tracka_boundary_trailing_disease_head") or changed

    return changed


def same_clause(row: dict[str, Any], note_text: str) -> tuple[str, int, int]:
    return clause_for_span(note_text, row_start(row), row_end(row))


def cue_before_scopes(before: str, cue_match: re.Match[str]) -> bool:
    middle = before[cue_match.end() :]
    if BOUNDARY.search(middle) or re.search(r"\b(?:but|however|although|except)\b", middle, re.I):
        return False
    if "," in middle and len(clean(middle)) > 50:
        return False
    return len(clean(middle).strip(" ,:()[]{}-")) <= 120


def refine_qualitative_lab_result(row: dict[str, Any], note_text: str) -> tuple[bool, str]:
    if row.get("filter_action") not in TARGET_ACTIONS:
        return False, ""
    category = row.get("concept_category_preview") or ""
    span_key = norm(row.get("span"))
    is_lab_target = category == "lab_result_or_value" or (
        category == "procedure" and QUALITATIVE_LAB_TARGET.search(span_key)
    )
    if not is_lab_target:
        return False, ""
    if row.get("numeric_value") not in (None, ""):
        return False, ""

    _clause, _rel_start, rel_end = same_clause(row, note_text)
    after = _clause[rel_end : rel_end + 80]
    match = QUALITATIVE_RESULT_AFTER.search(after)
    if not match:
        return False, ""

    status = match.group("status").lower()
    status = {
        "elevated": "high",
        "increased": "high",
        "decreased": "low",
        "reduced": "low",
    }.get(status, status)
    reason = "trackb_qualitative_result_binding"
    row["filter_action"] = "value_parse"
    row["concept_category_preview"] = "lab_result_or_value"
    row["assertion_status_preview"] = "not_applicable"
    row["assertion_evaluable_preview"] = False
    row["assertion_evaluable_reason"] = "qualitative_value_parse"
    row["value_status"] = status
    row["numeric_value"] = None
    row["unit"] = None
    row["comparator"] = None
    row["value_text"] = clean(match.group(0))
    row["value_parse_method"] = "qualitative_result_postmodifier"
    row["measurement_context"] = "observed_patient"
    row["trackb_qualitative_result_changed"] = True
    row["reason_codes"] = sorted(set([*(row.get("reason_codes") or []), reason]))
    return True, reason


def refine_assertion(row: dict[str, Any], note_text: str) -> tuple[bool, str]:
    if row.get("filter_action") not in TARGET_ACTIONS:
        return False, ""
    if row.get("filter_action") == "value_parse":
        return False, ""

    span_key = norm(row.get("span"))
    category = row.get("concept_category_preview") or ""
    if not span_key:
        return False, ""

    old = row.get("assertion_status_preview") or "present"
    clause, rel_start, rel_end = same_clause(row, note_text)
    before = clause[:rel_start]
    after = clause[rel_end:]
    before_tail = before[-180:]
    after_head = after[:160]

    new = ""
    reason = ""

    if "," in span_key and STRONG_PRESENT_AFTER.search(after_head):
        new, reason = "present", "tracka_assertion_mixed_span_explicit_present_after"

    if not new and STRONG_ABSENT_BEFORE.search(before_tail):
        new, reason = "absent", "tracka_assertion_explicit_absent_before"

    if not new and NEGATED_DETECTION_BEFORE.search(before_tail):
        new, reason = "absent", "tracka_assertion_negated_detection_before"

    if not new and STRONG_ABSENT_AFTER.search(after_head):
        new, reason = "absent", "tracka_assertion_explicit_absent_after"

    # Determine the target's role in an if/unless clause before considering
    # uncertainty elsewhere in the same sentence.
    if not new and IF_CUE.search(before_tail):
        cue = list(IF_CUE.finditer(before_tail))[-1]
        middle = before_tail[cue.end() :]
        if not re.search(r"[.;]", middle):
            predicate = re.split(r"[,;:]|\bthen\b|\band then\b", after_head, maxsplit=1, flags=re.I)[0]
            action_in_middle = ACTION_CUE.search(middle)
            if not action_in_middle and IF_ANTECEDENT_VERB.search(predicate):
                new, reason = "conditional", "tracka_assertion_if_antecedent_target"
            elif action_in_middle and category == "procedure":
                new, reason = "hypothetical", "tracka_assertion_conditioned_procedure_action"

    if not new and MONITOR_TARGET_BEFORE.search(before_tail):
        new, reason = "hypothetical", "tracka_assertion_monitor_future_target"

    if not new and category in {"disease_or_diagnosis", "finding"}:
        possible_before = list(POSSIBLE_BEFORE.finditer(before_tail))
        if possible_before and cue_before_scopes(before_tail, possible_before[-1]):
            new, reason = "possible", "tracka_assertion_local_possible_before"
        elif POSSIBLE_AFTER.search(after_head):
            new, reason = "possible", "tracka_assertion_local_possible_after"

    if not new and category in {"disease_or_diagnosis", "finding"} and CURRENT_RESIDUAL_BEFORE.search(before_tail):
        new, reason = "present", "tracka_assertion_residual_finding_present"

    if (
        not new
        and category in {"disease_or_diagnosis", "finding"}
        and CURRENT_TREATMENT_AFTER.search(after_head)
        and not REMOTE_CUE.search(before_tail[-120:])
    ):
        new, reason = "present", "tracka_assertion_current_treatment_after"

    if (
        not new
        and category == "medication_or_substance"
        and CURRENT_TREATMENT_BEFORE.search(before_tail)
        and not REMOTE_CUE.search(before_tail[-160:])
    ):
        new, reason = "present", "tracka_assertion_current_treatment_medication"

    if (
        not new
        and category == "procedure"
        and CURRENT_ENCOUNTER_EVENT_AFTER.search(after_head)
        and not PLANNED_TARGET_BEFORE.search(before_tail)
    ):
        new, reason = "present", "tracka_assertion_current_encounter_event"

    if not new and category in {"procedure", "medication_or_substance"} and PLANNED_EVENT_AFTER.search(after_head):
        new, reason = "hypothetical", "tracka_assertion_planned_event_after"

    if not new and category == "procedure" and PLANNED_LATER_AFTER.search(after_head):
        new, reason = "hypothetical", "tracka_assertion_planned_later_after"

    if not new and category == "medication_or_substance" and RESERVED_CONDITIONAL.search(after_head):
        new, reason = "conditional", "tracka_assertion_medication_as_needed"

    if not new and STRONG_PRESENT_AFTER.search(after_head):
        new, reason = "present", "tracka_assertion_explicit_present_after"

    if not new and STRONG_PRESENT_BEFORE.search(before_tail):
        new, reason = "present", "tracka_assertion_definite_present_before"

    if not new and CURRENT_TARGET_BEFORE.search(before_tail):
        new, reason = "present", "tracka_assertion_current_treatment_before"

    if not new and category in {"procedure", "medication_or_substance"} and PLANNED_TARGET_BEFORE.search(before_tail):
        new, reason = "hypothetical", "tracka_assertion_planned_target_before"

    if not new and EXPLICIT_REMOTE_BEFORE.search(before_tail):
        new, reason = "historical", "tracka_assertion_explicit_remote_before"

    if not new and EXPLICIT_RESOLVED_AFTER.search(after_head):
        new, reason = "historical", "tracka_assertion_explicit_resolved_after"

    if (
        not new
        and EXPLICIT_TIME_AFTER.search(after_head)
        and (
            RESOLVED_LATER_IN_CLAUSE.search(after_head)
            or re.search(r"\bhistory\b", str(row.get("section") or ""), re.I)
            or span_key in {"mi", "myocardial infarction"}
        )
    ):
        new, reason = "historical", "tracka_assertion_explicit_time_anchor"

    if not new and EXPLICIT_PAST_AFTER.search(after_head):
        new, reason = "historical", "tracka_assertion_explicit_past_after"

    if not new and category == "procedure":
        if IF_CUE.search(before_tail) and PROCEDURE_ACTION_CUE.search(before_tail):
            new, reason = "hypothetical", "tracka_assertion_conditioned_procedure_action"
        elif RESERVED_CONDITIONAL.search(after_head):
            if re.search(r"\bwill be pursued only if\b", after_head, re.I):
                new, reason = "hypothetical", "tracka_assertion_procedure_only_if_planned"
            else:
                new, reason = "conditional", "tracka_assertion_reserved_conditional"

    if not new and category in {"procedure", "medication_or_substance"}:
        if POSTOP_AFTER_CUE.search(before_tail) and re.search(r"\bafter\s+$", before_tail, re.I):
            new, reason = "historical", "tracka_assertion_postop_after_procedure"
        elif CURRENT_ADMISSION_SECTION.search(str(row.get("section") or "")) and CURRENT_PROCEDURE_CUE.search(before_tail):
            if not REMOTE_CUE.search(before_tail[-90:]):
                new, reason = "present", "tracka_assertion_current_admission_action"

    if not new and category == "family_history":
        if old == "absent":
            new, reason = "absent", "tracka_assertion_preserve_family_absent"

    if not new or new == old:
        return False, ""

    row["tracka_assertion_previous"] = old
    row["assertion_status_preview"] = new
    row["tracka_assertion_reason"] = reason
    row["tracka_assertion_changed"] = True
    row["reason_codes"] = sorted(set([*(row.get("reason_codes") or []), reason]))
    return True, reason


def refine_category(row: dict[str, Any], note_text: str) -> tuple[bool, str]:
    if row.get("filter_action") not in TARGET_ACTIONS:
        return False, ""
    old = row.get("concept_category_preview") or ""
    span_key = norm(row.get("span"))
    clause, rel_start, rel_end = same_clause(row, note_text)
    local = clause[max(0, rel_start - 120) : min(len(clause), rel_end + 120)]
    new = ""
    reason = ""

    if span_key == "incisional pain" and old in {"procedure", "clinical_attribute", "other", "anatomy"}:
        new, reason = "finding", "tracka_category_incisional_pain"
    elif old in {"clinical_attribute", "other", "anatomy"} and FINDING_RESCUE_SPAN.search(span_key):
        new, reason = "finding", "tracka_category_finding_rescue"
    elif old in {"other", "lab_result_or_value", "procedure", "finding"} and span_key in MEDICATION_RESCUE_SPANS:
        new, reason = "medication_or_substance", "tracka_category_medication_rescue"
    elif old in {"clinical_attribute", "other", "finding"} and SOCIAL_CUE.search(local) and re.search(
        r"\b(?:smok|tobacco|alcohol|asbestos exposure|pack[- ]?years?|homeless|lives? alone|housing)\b",
        span_key,
        re.I,
    ):
        new, reason = "social_history", "tracka_category_social_history"
    elif old in {"disease_or_diagnosis", "finding", "clinical_attribute"} and FAMILY_CUE.search(local):
        if not re.search(r"\b(?:patient|pt|he|she)\b[^.;]{0,35}$", clause[:rel_start], re.I):
            new, reason = "family_history", "tracka_category_family_experiencer"
    elif old in {"finding", "clinical_attribute", "other"} and span_key in {"s1", "s2", "s1 heart sound", "s2 heart sound", "regular rate and rhythm"}:
        new, reason = "finding", "tracka_category_cardiac_exam_finding"

    if not new or new == old:
        return False, ""

    row["tracka_category_previous"] = old
    row["concept_category_preview"] = new
    row["tracka_category_reason"] = reason
    row["tracka_category_changed"] = True
    row["reason_codes"] = sorted(set([*(row.get("reason_codes") or []), reason]))
    return True, reason


def demote(row: dict[str, Any], reason: str) -> bool:
    if row.get("filter_action") not in STRICT_KEEP_ACTIONS:
        return False
    old = row.get("filter_action")
    row["tracka_strict_previous_action"] = old
    row["filter_action"] = "context_only"
    row["tracka_strict_action"] = "demote_context_only"
    row["tracka_strict_reason"] = reason
    row["reason_codes"] = sorted(set([*(row.get("reason_codes") or []), reason]))
    return True


def should_demote_strict(row: dict[str, Any], strictness: str) -> str:
    if row.get("filter_action") not in STRICT_KEEP_ACTIONS:
        return ""
    if row.get("filter_action") == "value_parse":
        return ""

    span_key = norm(row.get("span"))
    category = row.get("concept_category_preview") or ""
    if not span_key:
        return "tracka_strict_empty_span"

    if span_key in STRICT_NOISE_EXACT or any(span_key.startswith(prefix) for prefix in STRICT_NOISE_PREFIXES):
        return "tracka_strict_exact_noise"
    if span_key in LOW_INFORMATION_SPANS:
        return "tracka_strict_low_information_span"
    if category == "procedure" and span_key in STANDALONE_PROCEDURE_NOISE:
        return "tracka_strict_procedure_noise"
    if category in HEADER_ONLY_CATEGORIES and HEADER_ONLY_SPAN.match(span_key):
        return "tracka_strict_header_or_context"
    if category == "lab_result_or_value" and not row.get("numeric_value") and row.get("value_status") in (None, "", "not_applicable"):
        return "tracka_strict_lab_without_value"
    if category == "other" and strictness == "aggressive":
        return "tracka_strict_other_aggressive"
    if category == "anatomy" and strictness == "aggressive":
        return "tracka_strict_anatomy_aggressive"
    if category == "clinical_attribute" and strictness == "aggressive" and span_key not in {"s1", "s2", "heart sound"}:
        return "tracka_strict_clinical_attribute_aggressive"
    return ""


def infer_anchor_assertion(spec: dict[str, Any], note_text: str, start: int, end: int) -> str:
    base_status = spec.get("assertion") or "present"
    clause, rel_start, rel_end = clause_for_span(note_text, start, end)
    before = clause[:rel_start]
    after = clause[rel_end:]
    if STRONG_ABSENT_BEFORE.search(before[-160:]) or re.search(
        r"\b(?:no|without|denies?|denied|negative for|does not support)\b",
        before[-120:],
        re.I,
    ):
        return "absent"
    if STRONG_ABSENT_AFTER.search(after[:100]):
        return "absent"
    if IF_CUE.search(before[-160:]):
        if spec.get("category") == "procedure" and PROCEDURE_ACTION_CUE.search(before[-160:]):
            return "hypothetical"
        return "conditional"
    if spec.get("category") == "procedure" and REMOTE_CUE.search(before[-90:]):
        return "historical"
    if spec.get("category") in {"procedure", "medication_or_substance"} and CURRENT_PROCEDURE_CUE.search(before[-140:]):
        return "present"
    if POSSIBLE_BEFORE.search(before[-120:]) or POSSIBLE_AFTER.search(after[:120]):
        return "possible"
    return str(base_status)


def build_anchor_row(
    note_id: str,
    note_meta: dict[str, Any],
    spec: dict[str, Any],
    match: re.Match[str],
) -> dict[str, Any]:
    note_text = str(note_meta.get("text") or "")
    start, end = match.start(), match.end()
    assertion = infer_anchor_assertion(spec, note_text, start, end)
    sentence = base.sentence_for_span(note_text, start, end, 420)
    context_raw, ctx_start, ctx_end = base.context_with_marker(note_text, start, end)
    context, _ctx_start, _ctx_end = base.clean_context_with_span(context_raw, ctx_start, ctx_end)
    category = str(spec["category"])
    return {
        "note_row_id": note_id,
        "subject_id": note_meta.get("subject_id") or "",
        "hadm_id": note_meta.get("hadm_id") or "",
        "chartdate": note_meta.get("chartdate") or "",
        "category": note_meta.get("category") or "unknown",
        "section": base.section_for_span(note_text, start),
        "span_start": start,
        "span_end": end,
        "span": note_text[start:end],
        "concept_id": spec["concept_id"],
        "concept_id_system": "TRACKA_LOCAL",
        "pretty_name": spec["pretty_name"],
        "type_ids": [],
        "raw_linker_concept_id": spec["concept_id"],
        "raw_linker_pretty_name": spec["pretty_name"],
        "raw_linker_span": note_text[start:end],
        "concept_override_applied": True,
        "span_normalization_applied": False,
        "linker_name": "tracka_anchor_rescue_v1",
        "linker_score": 1.0,
        "score_bin": "06_0.99_1.00",
        "filter_action": "keep_candidate",
        "reason_codes": ["tracka_anchor_rescue"],
        "filter_flags": ["tracka_anchor_rescue"],
        "concept_category_preview": category,
        "assertion_status_preview": assertion,
        "assertion_evaluable_preview": category != "lab_result_or_value",
        "assertion_evaluable_reason": "assertion_evaluable",
        "experiencer_preview": "family" if category == "family_history" else "patient",
        "temporality_preview": "current",
        "anatomical_location_cue": None,
        "anatomical_location_text": None,
        "modifier_cue": None,
        "modifier_text": None,
        "trend_cue": None,
        "trend_text": None,
        "value_status": "not_applicable",
        "numeric_value": None,
        "unit": None,
        "comparator": None,
        "value_text": None,
        "value_parse_method": "not_applicable",
        "measurement_context": "not_applicable",
        "needs_review": True,
        "evidence": sentence,
        "evidence_context": base.clip(context, 650),
        "value_context": "",
    }


def add_anchor_rescue_rows(note_id: str, note_meta: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    note_text = str(note_meta.get("text") or "")
    if not note_text:
        return rows, Counter()
    out = list(rows)
    counts: Counter[str] = Counter()
    existing_exact = {
        (row_start(row), row_end(row), norm(row.get("span")))
        for row in out
        if row.get("filter_action") in TARGET_ACTIONS
    }
    existing_spans = [
        (row_start(row), row_end(row), row.get("filter_action"), row.get("concept_category_preview"))
        for row in out
        if row.get("filter_action") in TARGET_ACTIONS
    ]
    for spec in ANCHOR_SPECS:
        pattern = re.compile(spec["pattern"])
        for match in pattern.finditer(note_text):
            start, end = match.start(), match.end()
            context_checks: list[bool] = []
            if spec.get("before_pattern"):
                before = note_text[max(0, start - 100) : start]
                context_checks.append(bool(re.search(str(spec["before_pattern"]), before)))
            if spec.get("after_pattern"):
                after = note_text[end : min(len(note_text), end + 100)]
                context_checks.append(bool(re.search(str(spec["after_pattern"]), after)))
            if spec.get("context_pattern"):
                local = note_text[max(0, start - 100) : min(len(note_text), end + 100)]
                context_checks.append(bool(re.search(str(spec["context_pattern"]), local)))
            if context_checks and not any(context_checks):
                continue
            key = (start, end, norm(note_text[start:end]))
            if key in existing_exact:
                continue
            # Do not add a short anchor when a longer strict target already
            # fully covers it.
            if any(overlap(start, end, s, e) == end - start for s, e, action, _cat in existing_spans if action in TARGET_ACTIONS):
                continue
            row = build_anchor_row(note_id, note_meta, spec, match)
            refine_boundary(row, note_text)
            refine_category(row, note_text)
            refine_assertion(row, note_text)
            out.append(row)
            existing_exact.add(key)
            existing_spans.append((start, end, row["filter_action"], row["concept_category_preview"]))
            counts[str(spec["name"])] += 1
    return out, counts


def add_parenthetical_abbrev_rows(note_id: str, note_meta: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    note_text = str(note_meta.get("text") or "")
    if not note_text:
        return rows, Counter()
    out = list(rows)
    counts: Counter[str] = Counter()
    existing_exact = {
        (row_start(row), row_end(row), norm(row.get("span")))
        for row in out
        if row.get("filter_action") in TARGET_ACTIONS
    }
    for row in list(out):
        if row.get("filter_action") not in TARGET_ACTIONS:
            continue
        start = row_start(row)
        end = row_end(row)
        after = note_text[end : min(len(note_text), end + 28)]
        match = PAREN_ABBREV.match(after)
        if not match:
            continue
        abbr_start = end + match.start(1)
        abbr_end = end + match.end(1)
        key = (abbr_start, abbr_end, norm(note_text[abbr_start:abbr_end]))
        if key in existing_exact:
            continue
        abbr = note_text[abbr_start:abbr_end]
        if len(norm(abbr)) < 2 or len(norm(abbr)) > 18:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9/-]{1,12}s?", abbr):
            continue
        upper_count = sum(1 for ch in abbr if ch.isupper())
        if upper_count < 2 and "-" not in abbr and not any(ch.isdigit() for ch in abbr):
            continue
        new_row = dict(row)
        new_row["tracka_parenthetical_abbrev"] = True
        new_row["tracka_parenthetical_abbrev_source_span"] = row.get("span")
        new_row["span_start"] = abbr_start
        new_row["span_end"] = abbr_end
        new_row["span"] = abbr
        new_row["raw_linker_span"] = abbr
        new_row["concept_override_applied"] = True
        new_row["span_normalization_applied"] = True
        new_row["linker_name"] = "tracka_parenthetical_abbrev_v1"
        new_row["tracka_boundary_reason"] = "tracka_boundary_parenthetical_abbrev_rescue"
        new_row["reason_codes"] = sorted(set([*(new_row.get("reason_codes") or []), "tracka_boundary_parenthetical_abbrev_rescue"]))
        refresh_context(new_row, note_text)
        out.append(new_row)
        existing_exact.add(key)
        counts["parenthetical_abbrev"] += 1
    return out, counts


def add_contrast_coordinate_rows(
    note_id: str,
    note_meta: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Split coordinated concepts only when their assertions explicitly differ."""

    note_text = str(note_meta.get("text") or "")
    if not note_text:
        return rows, Counter()

    out = list(rows)
    counts: Counter[str] = Counter()

    def source_for(start: int, end: int) -> dict[str, Any] | None:
        candidates = [
            row
            for row in out
            if row.get("concept_category_preview") in {"disease_or_diagnosis", "finding"}
            and overlap(start, end, row_start(row), row_end(row)) > 0
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda row: (
                row.get("filter_action") in TARGET_ACTIONS,
                overlap(start, end, row_start(row), row_end(row)),
                -(row_end(row) - row_start(row)),
            ),
            reverse=True,
        )
        return candidates[0]

    for match in CONTRAST_COORDINATION.finditer(note_text):
        component_specs = [
            (match.start("left"), match.end("left"), "absent"),
            (match.start("right"), match.end("right"), "present"),
        ]
        sources = [source_for(start, end) for start, end, _assertion in component_specs]
        if any(source is None for source in sources):
            continue

        for row in out:
            if row.get("filter_action") not in TARGET_ACTIONS:
                continue
            if row_start(row) <= component_specs[0][0] and row_end(row) >= component_specs[1][1] and "," in str(row.get("span") or ""):
                demote(row, "tracka_boundary_contrast_coordinate_split")
                counts["combined_demoted"] += 1

        for (start, end, assertion), source in zip(component_specs, sources):
            exact = next(
                (
                    row
                    for row in out
                    if row_start(row) == start
                    and row_end(row) == end
                    and row.get("concept_category_preview") in {"disease_or_diagnosis", "finding"}
                ),
                None,
            )
            target = exact if exact is not None else dict(source or {})
            if exact is None:
                set_span(target, note_text, start, end, "tracka_boundary_contrast_coordinate_component")
                target["concept_id"] = f"TRACKA_LOCAL:COORDINATE:{norm(note_text[start:end]).replace(' ', '_').upper()}"
                target["concept_id_system"] = "TRACKA_LOCAL"
                target["raw_linker_concept_id"] = target["concept_id"]
                target["raw_linker_pretty_name"] = note_text[start:end]
                target["raw_linker_span"] = note_text[start:end]
                target["pretty_name"] = note_text[start:end]
                target["linker_name"] = "tracka_coordinate_split_v1"
                target["concept_override_applied"] = True
                target["needs_review"] = True
                out.append(target)
                counts["components_added"] += 1
            elif target.get("filter_action") not in TARGET_ACTIONS:
                counts["components_promoted"] += 1

            target["filter_action"] = "keep_candidate"
            target["assertion_status_preview"] = assertion
            target["assertion_evaluable_preview"] = True
            target["assertion_evaluable_reason"] = "assertion_evaluable"
            reason = f"tracka_assertion_contrast_coordinate_{assertion}"
            target["tracka_assertion_reason"] = reason
            target["tracka_assertion_changed"] = True
            target["reason_codes"] = sorted(set([*(target.get("reason_codes") or []), reason]))

    return out, counts


def process_rows(
    notes: dict[str, dict[str, Any]],
    input_paths: list[Path],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_by_note: dict[str, list[dict[str, Any]]] = defaultdict(list)
    summary: dict[str, Any] = {
        "name": args.name,
        "strictness": args.strictness,
        "input_rows": 0,
        "output_rows": 0,
        "boundary_changes": Counter(),
        "category_changes": Counter(),
        "assertion_changes": Counter(),
        "value_changes": Counter(),
        "strict_demotions": Counter(),
        "anchor_rescues": Counter(),
        "parenthetical_abbrev_rescues": Counter(),
        "contrast_coordinate_splits": Counter(),
    }

    for input_path in input_paths:
        for path in iter_prediction_files(input_path):
            with open_text(path) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    summary["input_rows"] += 1
                    note_id = str(row.get("note_row_id") or row.get("row_id") or row.get("note_id") or "")
                    note_meta = notes.get(note_id)
                    out = dict(row)
                    if note_meta and str(note_meta.get("text") or ""):
                        note_text = str(note_meta.get("text") or "")
                        if not args.disable_boundary_ranker and refine_boundary(out, note_text):
                            summary["boundary_changes"][out.get("tracka_boundary_reason") or "unknown"] += 1
                        if refine_category(out, note_text)[0]:
                            summary["category_changes"][out.get("tracka_category_reason") or "unknown"] += 1
                        value_changed, value_reason = refine_qualitative_lab_result(out, note_text)
                        if value_changed:
                            summary["value_changes"][value_reason] += 1
                        if not args.disable_assertion_judge:
                            changed, reason = refine_assertion(out, note_text)
                            if changed:
                                summary["assertion_changes"][reason] += 1
                        if not args.disable_strict_gate:
                            demote_reason = should_demote_strict(out, args.strictness)
                            if demote_reason and demote(out, demote_reason):
                                summary["strict_demotions"][demote_reason] += 1
                    rows_by_note[note_id].append(out)

    final_rows: list[dict[str, Any]] = []
    for note_id, rows in sorted(rows_by_note.items(), key=lambda item: item[0]):
        if not args.disable_boundary_ranker and note_id in notes:
            rows, split_counts = add_contrast_coordinate_rows(note_id, notes[note_id], rows)
            summary["contrast_coordinate_splits"].update(split_counts)
        if not args.disable_anchor_rescue and note_id in notes:
            rows, rescue_counts = add_anchor_rescue_rows(note_id, notes[note_id], rows)
            summary["anchor_rescues"].update(rescue_counts)
            rows, abbrev_counts = add_parenthetical_abbrev_rows(note_id, notes[note_id], rows)
            summary["parenthetical_abbrev_rescues"].update(abbrev_counts)
        rows.sort(key=lambda r: (row_start(r), row_end(r), str(r.get("span") or ""), str(r.get("concept_id") or "")))
        final_rows.extend(rows)
    summary["output_rows"] = len(final_rows)
    return final_rows, summary


def serializable_summary(summary: dict[str, Any], output_path: Path, input_paths: list[Path]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in summary.items():
        if isinstance(value, Counter):
            out[key] = dict(value.most_common())
        else:
            out[key] = value
    out["input"] = [str(path) for path in input_paths]
    out["output"] = str(output_path)
    return out


def main() -> int:
    args = parse_args()
    if len(args.notes_jsonl_gz) != len(args.candidate_jsonl):
        # A single note file can still be used for multiple candidate files.
        if len(args.notes_jsonl_gz) != 1:
            raise SystemExit("--notes-jsonl-gz must be length 1 or match --candidate-jsonl")

    notes = load_notes(Path(path) for path in args.notes_jsonl_gz)
    input_paths = [Path(path) for path in args.candidate_jsonl]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.chmod(0o700)
    except PermissionError:
        pass
    output_path = output_dir / "medcat_candidates.jsonl.gz"

    final_rows, summary = process_rows(notes, input_paths, args)
    with open_output(output_path) as out_fh:
        for row in final_rows:
            out_fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    summary_payload = serializable_summary(summary, output_path, input_paths)
    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_private(output_path)
    chmod_private(summary_path)
    print(json.dumps(summary_payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
