"""Machine check for arithmetic steps: the model's claimed result vs. an exact evaluation."""
import ast
import math
import operator
import re

_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:e[-+]?\d+)?|-?\.\d+", re.I)


class NotArithmetic(ValueError):
    pass


def _clean(expr: str) -> str:
    e = expr.replace("×", "*").replace("÷", "/").replace("^", "**").replace("−", "-")
    e = re.sub(r"(?<=\d),(?=\d{3}\b)", "", e)  # thousands separators
    return e.strip().rstrip("=").strip()


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise NotArithmetic("exponent too large to check")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    raise NotArithmetic("not pure arithmetic")


def evaluate(expr: str) -> float:
    try:
        return _eval(ast.parse(_clean(expr), mode="eval"))
    except (SyntaxError, ZeroDivisionError, OverflowError) as e:
        raise NotArithmetic(str(e)) from e


def to_number(text) -> float | None:
    """The single number a result/answer states ('84', '$1,200', 'x = 3.5 km'); None if zero or several."""
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text)
    try:
        return float(evaluate(s))
    except NotArithmetic:
        pass
    nums = _NUM.findall(s)
    return float(nums[0].replace(",", "")) if len(nums) == 1 else None


def matches(computed: float, claimed: str) -> bool:
    value = to_number(claimed)
    if value is None:
        return False
    if math.isclose(computed, value, rel_tol=1e-6, abs_tol=1e-9):
        return True
    m = re.search(r"\.(\d+)", str(claimed))  # accept a correctly rounded value
    return bool(m) and round(computed, len(m.group(1))) == value


def fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.10g}"


def check_step(step: dict) -> str | None:
    """None if fine (or nothing to check); otherwise what is wrong."""
    expr, claimed = step.get("expression"), step.get("result")
    if not expr or claimed in (None, ""):
        return None
    try:
        computed = evaluate(str(expr))
    except NotArithmetic:
        return None
    if matches(computed, str(claimed)):
        return None
    return f"{expr} = {fmt(computed)}, not {claimed}"
