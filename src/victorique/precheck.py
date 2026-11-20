"""Cheap, conservative checks for visibly unfinished proof text."""
import re

from .adaptive_contracts import candidate_ref

# Visible unfinished-text markers. "omitted" and "placeholder" also have ordinary
# mathematical uses ("a factor for an omitted variable"), so they count only in
# brackets or where the text says that proof content is omitted.
MARKER = re.compile(
    r"\b(?:TODO|TBD|FIXME)\b"
    r"|[\[(<]\s*(?:omitted|placeholder)\b"
    r"|\b(?P<noun>proofs?|details?|arguments?|verifications?|computations?|calculations?|derivations?|steps?|cases?"
    r"|constructions?)"
    r"\s+(?:(?:is|are|was|were|has\s+been|have\s+been)\s+)?(?:(?P<word>\w+)\s+)?omitted\b"
    r"|\bomitted\s+(?:for\s+brevity|here)\b"
    r"|留给读者|留給讀者|待证明|待證明", re.I)
# Complete proofs also say that nothing is omitted ("so no exceptional case is omitted",
# "This case is not omitted"). Such a noun match does not count: the word before "omitted"
# is "not" or "never", or a negation opens the noun phrase at most two words before the noun.
NEGATED_NOUN = re.compile(r"\b(?:no|none\s+of|nothing|without)\s+(?:[\w-]+\s+){0,2}$", re.I)


def _says_nothing_omitted(content, match):
    return bool(match.group("noun")) and (
        (match.group("word") or "").lower() in ("not", "never")
        or NEGATED_NOUN.search(content, 0, match.start()) is not None)


def _location(content, match):
    start = len(content[:match.start()].encode("utf-8"))
    return {"start_byte": start, "end_byte": start + len(match.group(0).encode("utf-8")),
            "quote": match.group(0)}


def precheck(candidate, problem):
    content = candidate["content"]
    reasons = []
    if not content.strip():
        reasons.append(("The manuscript is empty.", None))
    match = next((m for m in MARKER.finditer(content) if not _says_nothing_omitted(content, m)), None)
    if match:
        # The exact location lets a repair address the marker itself.
        reasons.append(('The manuscript contains an unfinished or placeholder marker: "' + match.group(0) + '".',
                        _location(content, match)))
    # Explicit answer tasks need an explicit answer statement; no answer is guessed.
    if re.search(r"\b(?:find|determine|compute|calculate)\b|求值|计算|計算", problem, re.I) and not re.search(
            r"\b(?:answer|therefore|thus|hence|equals?)\b|答案|故|所以|=|\\boxed", content, re.I):
        reasons.append(("The problem asks for a definite answer but the manuscript gives no explicit answer.", None))
    return [{"schema": "victorique.adaptive_defect.v1", "issue_id": "precheck-" + str(i),
             "target_ref": candidate_ref(candidate), "kind": "goal_gap", "severity": "material",
             "location": location, "claim": reason} for i, (reason, location) in enumerate(reasons)]
