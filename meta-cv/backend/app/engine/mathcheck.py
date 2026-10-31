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


# ---------- Number tracing ----------
# The arithmetic check only proves expression == result. Tracing proves the operands are not invented:
# every number a step uses must come from the problem text (question + verified premises), from an
# earlier machine-checked step, or be a small everyday constant. "1921 - 1897 = 24" passes the
# arithmetic check but fails here when the verified premise says 1879.

SAFE_CONSTANTS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 24, 52, 60, 100, 360, 365, 366, 1000, 3600)
_TEXT_NUM = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\d)(?!(?:st|nd|rd|th)\b)")
_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_SMALL.update({w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)})
_SCALES = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_SPECIAL = {"dozen": 12, "half": 0.5, "twice": 2, "double": 2, "triple": 3, "quarter": 0.25, "hundred": 100,
            **_SCALES}


def numbers_in(text: str) -> list[str]:
    """Numbers written with digits ('1,200', '3.5', '15%'), not ordinals or digits inside words ('1st', 'x2')."""
    return _TEXT_NUM.findall(str(text))


def word_numbers(text: str) -> list[float]:
    """Numbers written as words: 'twelve', 'twenty five', 'two hundred and five', 'a dozen', 'half'."""
    out: list[float] = []
    total = cur = 0
    active = False

    def flush():
        nonlocal total, cur, active
        if active:
            out.append(total + cur)
        total = cur = 0
        active = False

    for w in re.findall(r"[a-z]+|[^a-z\s]", str(text).lower()):  # punctuation ends a number ("five, six")
        if w in _SMALL:
            cur += _SMALL[w]
            active = True
        elif w == "hundred" and active:
            cur *= 100
        elif w in _SCALES and active:
            total += cur * _SCALES[w]
            cur = 0
        elif w == "and" and active:
            continue
        else:
            flush()
            if w in _SPECIAL:
                out.append(_SPECIAL[w])
    flush()
    return out


def operands(expr: str) -> list[float]:
    """The literal numbers in a pure-arithmetic expression."""
    try:
        tree = ast.parse(_clean(expr), mode="eval")
    except SyntaxError as e:
        raise NotArithmetic(str(e)) from e
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) in (int, float)]


def _pool(values) -> list[float]:
    """Each source number also counts as a percentage and a percentage change (15 -> 0.15, 1.15, 0.85)."""
    out = []
    for v in values:
        v = abs(float(v))
        out += [v, v / 100, v * 100, 1 + v / 100, 1 - v / 100]
    return out


def _traced(n, pool: list[float]) -> bool:
    s = n if isinstance(n, str) else fmt(abs(n))
    return any(matches(a, s) for a in pool)


def untraced_number(steps: list[dict], sources: str) -> tuple[int, str] | None:
    """(index, why) for the first step that uses a number with no source; None if every number traces.

    sources: the problem text the solution may draw on (question, verified premises, earlier verified answers).
    A step's own machine-checked result becomes a source for the steps after it.
    """
    pool = _pool([to_number(n) for n in numbers_in(sources)] + word_numbers(sources) + list(SAFE_CONSTANTS))
    for i, s in enumerate(steps):
        expr = str(s.get("expression") or "")
        value = None
        if expr:
            try:
                value = evaluate(expr)
                ops = operands(expr)
            except NotArithmetic:
                ops = numbers_in(expr)  # not checkable as arithmetic, but its numbers still need a source
            for x in ops:
                if not _traced(x, pool):
                    shown = x if isinstance(x, str) else fmt(x)
                    return i, (f"{shown} in {expr} does not come from the question, a verified premise "
                               "or an earlier step")
        for n in numbers_in(s.get("text", "")):
            if _traced(n, pool) or (value is not None and matches(value, n)):
                continue
            return i, (f"the step states {n}, which does not come from the question, a verified premise "
                       "or a machine-checked calculation")
        if value is not None:
            pool += _pool([value])
    return None
