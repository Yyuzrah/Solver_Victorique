"""Provider-facing output schemas must satisfy strict structured-output rules.

OpenAI strict mode rejects, with HTTP 400 invalid_json_schema, any object that has a
property missing from "required" or that allows additional properties. The fake
transport does not enforce these rules, so check every schema statically. The
missing "assessments" entry in repair_obligation reached a real run this way.
"""
import json
from importlib.resources import files

import pytest

from victorique import adaptive_roles, bestofn, error_probe, grading


def violations(node, path=""):
    found = []
    if isinstance(node, dict):
        properties = node.get("properties")
        if node.get("type") == "object" or isinstance(properties, dict):
            properties = properties or {}
            missing = sorted(set(properties) - set(node.get("required", [])))
            if missing:
                found.append(f"{path or '/'}: not in required {missing}")
            if node.get("additionalProperties", True) is not False:
                found.append(f"{path or '/'}: additionalProperties must be false")
        for key, value in node.items():
            found += violations(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += violations(value, f"{path}[{index}]")
    return found


def adaptive_schemas():
    return json.loads(files("victorique").joinpath("adaptive-output-schemas.v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("role", sorted(adaptive_schemas()))
def test_adaptive_role_schema_is_strict(role):
    assert violations(adaptive_schemas()[role]) == []


def test_repair_schema_requires_assessments_with_safe_issue_ids():
    repair = adaptive_schemas()["repair_obligation"]
    assert "assessments" in repair["required"]
    item = repair["properties"]["assessments"]["items"]
    assert item["properties"]["issue_id"]["pattern"] == repair["properties"]["artifact_id"]["pattern"]
    assert "assessments" in adaptive_roles.output_contract("repair_obligation")
    assert "assessments" not in adaptive_roles.output_contract("solve_obligation")


@pytest.mark.parametrize("kind", ["solve_obligation", "repair_obligation", "integrate"])
def test_mutating_contract_names_exactly_the_required_schema_fields(kind):
    assert set(adaptive_roles.output_contract(kind)) == set(adaptive_schemas()[kind]["required"])


@pytest.mark.parametrize("name,schema", [("grade", grading.GRADE_SCHEMA),
                                         ("bestofn_selection", bestofn.selection_schema()),
                                         *sorted(error_probe.SCHEMAS.items())])
def test_other_output_schemas_are_strict(name, schema):
    assert violations(schema) == []
