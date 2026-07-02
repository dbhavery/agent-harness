"""Tool contract + schema validation tests."""

from __future__ import annotations

import pytest

from agent_harness.classification import (
    MalformedOutput,
    MissingContext,
    SchemaViolation,
)
from agent_harness.tools.calculator import CalculatorTool
from agent_harness.tools.doc_search import DocSearchTool
from agent_harness.tools.flaky_api import FlakyApiTool


def test_doc_search_returns_typed_hits():
    out = DocSearchTool().invoke({"query": "retry backoff", "top_k": 2})
    assert len(out.results) <= 2
    assert out.results[0].doc_id  # typed hit
    assert 0.0 <= out.results[0].score <= 1.0


def test_doc_search_blank_query_is_schema_violation():
    # Blank/whitespace query fails the pydantic contract at the input boundary.
    with pytest.raises(SchemaViolation):
        DocSearchTool().invoke({"query": "   "})


def test_doc_search_unknown_field_rejected_by_extra_forbid():
    with pytest.raises(SchemaViolation):
        DocSearchTool().invoke({"query": "retry", "bogus": 1})


def test_doc_search_empty_corpus_raises_missing_context():
    with pytest.raises(MissingContext):
        DocSearchTool(corpus=[]).invoke({"query": "retry"})


def test_calculator_arithmetic_and_convert():
    calc = CalculatorTool()
    assert calc.invoke({"op": "add", "operands": [2, 3, 4]}).value == 9
    conv = calc.invoke(
        {"op": "convert", "value": 1.0, "from_unit": "mi", "to_unit": "km"}
    )
    assert round(conv.value, 3) == 1.609
    assert conv.unit == "km"


def test_calculator_missing_operands_is_missing_context():
    with pytest.raises(MissingContext):
        CalculatorTool().invoke({"op": "add", "operands": [1]})


def test_calculator_bad_conversion_is_schema_violation():
    with pytest.raises(SchemaViolation):
        CalculatorTool().invoke(
            {"op": "convert", "value": 1.0, "from_unit": "mi", "to_unit": "parsec"}
        )


def test_calculator_div_by_zero_is_schema_violation():
    with pytest.raises(SchemaViolation):
        CalculatorTool().invoke({"op": "div", "operands": [1, 0]})


def test_flaky_api_malformed_output_classified():
    # Scripted to return an off-contract dict -> caught at output validation.
    tool = FlakyApiTool(script=["malformed"])
    with pytest.raises(MalformedOutput):
        tool.invoke({"resource": "x"})


def test_flaky_api_recovers_on_third_attempt():
    tool = FlakyApiTool(script=["transient", "transient", "ok"])
    # Attempts 0 and 1 raise; attempt 2 succeeds.
    out = tool.invoke({"resource": "order-1"}, attempt=2)
    assert out.payload == "live-data::order-1"
    assert out.source == "live"


def test_tool_json_schema_is_exposed():
    schema = DocSearchTool().input_json_schema()
    assert schema["properties"]["query"]["type"] == "string"
    assert "top_k" in schema["properties"]
