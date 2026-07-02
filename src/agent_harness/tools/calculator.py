"""Calculator + unit-conversion tool.

Deliberately does NOT use ``eval``. Arithmetic is dispatched over a fixed op
table and unit conversion over a fixed factor table, so there is no code-exec
surface. The safety gate adds a second, independent check on top of this.
"""

from __future__ import annotations

from ..classification import MissingContext, SchemaViolation
from ..schemas import CalcInput, CalcOutput
from .base import Tool

# (from_unit, to_unit) -> callable
_CONVERSIONS = {
    ("mi", "km"): lambda v: v * 1.609344,
    ("km", "mi"): lambda v: v / 1.609344,
    ("c", "f"): lambda v: v * 9.0 / 5.0 + 32.0,
    ("f", "c"): lambda v: (v - 32.0) * 5.0 / 9.0,
    ("lb", "kg"): lambda v: v * 0.45359237,
    ("kg", "lb"): lambda v: v / 0.45359237,
}


class CalculatorTool(Tool[CalcInput, CalcOutput]):
    name = "calc_units"
    input_model = CalcInput
    output_model = CalcOutput
    latency_ms = 3.0

    def run(self, request: CalcInput, attempt: int = 0) -> CalcOutput:
        if request.op == "convert":
            return self._convert(request)
        return self._arithmetic(request)

    def _arithmetic(self, request: CalcInput) -> CalcOutput:
        ops = request.operands
        if len(ops) < 2:
            raise MissingContext(
                f"op '{request.op}' needs at least two operands",
                detail=f"got {len(ops)} operand(s)",
            )
        acc = ops[0]
        for x in ops[1:]:
            if request.op == "add":
                acc += x
            elif request.op == "sub":
                acc -= x
            elif request.op == "mul":
                acc *= x
            elif request.op == "div":
                if x == 0:
                    raise SchemaViolation(
                        "division by zero", detail="operand 0 is not permitted for div"
                    )
                acc /= x
        return CalcOutput(value=round(acc, 6), unit=request.to_unit)

    def _convert(self, request: CalcInput) -> CalcOutput:
        if request.value is None or not request.from_unit or not request.to_unit:
            raise MissingContext(
                "convert requires value, from_unit and to_unit",
                detail=f"value={request.value} from={request.from_unit} to={request.to_unit}",
            )
        key = (request.from_unit.lower(), request.to_unit.lower())
        fn = _CONVERSIONS.get(key)
        if fn is None:
            raise SchemaViolation(
                f"unsupported conversion {request.from_unit}->{request.to_unit}",
                detail="no factor registered for this unit pair",
            )
        return CalcOutput(value=round(fn(request.value), 6), unit=request.to_unit)
