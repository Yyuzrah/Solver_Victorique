"""The precheck flags visibly unfinished text, not ordinary mathematical wording."""
import re

import pytest

from victorique import adaptive_contracts as c
from victorique.precheck import MARKER, precheck

# A broad rule: any of these words anywhere in a manuscript fails it.
BROAD_MARKER = re.compile(r"\b(?:TODO|TBD|FIXME|omitted|placeholder)\b|留给读者|留給讀者|待证明|待證明", re.I)
# Sentences in four recorded full-policy drafts that say no case is left out.
NOTHING_OMITTED = {
    "r1-PB-Advanced-016-full": "This completes the requested implication whenever the named second intersections "
                               "exist; no further exceptional case is omitted within that domain.",
    "r2-PB-Advanced-003-full": "Thus the three asserted equations are established, with no exceptional scalene "
                               "cases omitted.",
    "r2-PB-Advanced-009-full": "Here a,c,z,t-p,U,V are strictly positive; b,p are strictly negative; g and r are "
                               "nonzero on the stipulated domain; and the displayed circle equations force "
                               "A_b,A_c≠0. Thus no admissible case is omitted.",
    "r2-PB-Advanced-022-full": "No division by D₁ or D₂ was made: if either were zero, the same identities would "
                               "make both angles 90 degrees, so no exceptional case is omitted.",
}


def candidate(content):
    return {"content": content, "artifact_id": "a", "revision": 1, "plan_id": "p",
            "plan_revision": 1, "route_id": "r", "obligation_id": "o"}


@pytest.mark.parametrize("content", [
    # A use from two first drafts that the broad rule stops.
    "Every monomial omits a variable.\r\nA factor for an omitted variable vanishes.\r\n",
    "We write f instead of f_i, with the subscript omitted.",
    "Here the dependence on n is omitted from the notation.",
    "The case where the index is omitted is handled in the same way.",
    "Let * be a placeholder symbol for the group operation.",
    # Four first drafts graded 7/7 that the broad rule stops, cut to the sentences around each match:
    # NEM-A2 and PB-Advanced-021 at the CLI default (highest) reasoning effort, and PB-Advanced-010
    # at low effort in two other runs.
    pytest.param("A factor corresponding to an omitted variable is ∑_{x_i=0}^{m-1} ζ^{x_i} = 0.",
                 id="r4-NEM-A2"),
    pytest.param("Matching the omitted and included coordinates expresses the deficit as a sum of positive "
                 "differences, each at least the gap.", id="r4-PB-Advanced-021"),
    pytest.param("The coincidence at G is verified below, so the qualification cannot simply be omitted "
                 "from a literal formulation.", id="low-r1-PB-Advanced-010"),
    pytest.param("For a fixed triangle with u≠1, the omitted parameter values are isolated: in particular, "
                 "BP is not fixed, since B lies on OG only if (u−1)(s−2u)=0, and s=2u would make AB=BC. "
                 "Every omitted position at which the original construction has a well-defined second "
                 "intersection is a limit of the calculated positions.", id="low-r17-PB-Advanced-010"),
    # External proofs from ProofBench (MIT) and IMO-GradingBench (CC BY 4.0) that the broad rule
    # matches where they drop absolute-value signs (expert scores 3/7 and 7/7).
    pytest.param(r"Since the polygons are convex and traversed counterclockwise, the sums "
                 r"\(\sum_{i<j} d_i \times d_j\) and \(\sum_{i<j} c_i \times c_j\) are positive, so the "
                 r"absolute values can be omitted for the difference:", id="cal-0165"),
    pytest.param(r"Because all angles \(\theta_{j}-\theta_{i}\;(0\le i < j\le n-1)\) lie in" "\n"
                 r"\((0,\pi)\), each determinant \(\det(v_i,v_j)=\sin(\theta_j-\theta_i)\)" "\n"
                 r"is positive, and the absolute value in (1) can be omitted.", id="cal-0167"),
])
def test_ordinary_mathematical_uses_are_not_markers(content):
    assert BROAD_MARKER.search(content)
    assert precheck(candidate(content), "Prove the claim.") == []


@pytest.mark.parametrize("content", [
    *(pytest.param(text, id=key) for key, text in NOTHING_OMITTED.items()),
    "This case is not omitted.",
    "None of the cases is omitted.",
])
def test_sentences_saying_nothing_is_omitted_are_not_markers(content):
    assert MARKER.search(content)  # the pattern matches; the negation excludes the match
    assert precheck(candidate(content), "Prove the claim.") == []


@pytest.mark.parametrize("content,quote", [
    ("This case is not omitted.\nTODO: check the last step.", "TODO"),
    ("None of the cases is omitted, but the verification is omitted.", "verification is omitted"),
    ("Thus no admissible case is omitted. The last case is omitted.", "case is omitted"),
    (NOTHING_OMITTED["r2-PB-Advanced-022-full"] + " Details omitted for brevity.", "Details omitted"),
    # A negation outside the noun phrase does not count.
    ("There is no gap and the details are omitted.", "details are omitted"),
])
def test_a_negation_does_not_hide_a_marker_in_the_same_text(content, quote):
    target = candidate(content)
    issues = precheck(target, "Prove the claim.")
    assert len(issues) == 1
    assert issues[0]["location"]["quote"] == quote and quote in issues[0]["claim"]
    # The marker is the last occurrence of its quote in each text.
    assert issues[0]["location"]["start_byte"] == len(content[:content.rindex(quote)].encode("utf-8"))
    c.validate_defect(issues[0], target)  # exact UTF-8 byte span


@pytest.mark.parametrize("content,quote", [
    ("TODO: finish the induction.", "TODO"),
    ("The proof is omitted.", "proof is omitted"),
    ("Details omitted for brevity.", "Details omitted"),
    ("The remaining steps are routinely omitted.", "steps are routinely omitted"),
    ("Lemma 2. [Omitted]", "[Omitted"),
    ("Case 3: (placeholder)", "(placeholder"),
    ("其余部分留给读者。", "留给读者"),
    # External proofs from the same two sources that say that part of the argument is left out
    # (expert scores 1/7, 2/7, 0/7 and 0/7).
    pytest.param("The detailed construction is omitted due to complexity, but it is possible by placing "
                 "the last few squares to connect back.", "construction is omitted", id="cal-0007"),
    pytest.param("(A proof can be found, for instance, in Lemma 4.35 of Evan Chen’s\n*Euclidean‐Geometry in "
                 "Mathematical Olympiads*; a short angle–chase is also\npossible but omitted here for brevity.)",
                 "omitted here", id="cal-0049"),
    pytest.param(r"For example, \(Q_1\) and \(Q_2\) have different areas (computed but omitted for brevity), "
                 r"and subsequent quadrilaterals deviate further.", "omitted for brevity", id="cal-0137"),
    pytest.param("We rigorously derived (details omitted for brevity) that for $L$ to be on $(HAL)$, the "
                 "center coordinate $C_x$ must satisfy:", "details omitted", id="cal-0286"),
])
def test_unfinished_markers_are_reported_with_their_exact_location(content, quote):
    target = candidate(content)
    issues = precheck(target, "Prove the claim.")
    assert len(issues) == 1
    assert issues[0]["location"]["quote"] == quote and quote in issues[0]["claim"]
    c.validate_defect(issues[0], target)  # exact UTF-8 byte span


def test_empty_and_missing_answer_rules_are_unchanged():
    assert [i["location"] for i in precheck(candidate(""), "Prove it")] == [None]
    issues = precheck(candidate("A discussion with no conclusion."), "Compute the integer value.")
    assert [i["claim"] for i in issues] == [
        "The problem asks for a definite answer but the manuscript gives no explicit answer."]
