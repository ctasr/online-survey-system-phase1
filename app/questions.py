import secrets
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


QUESTION_TYPES = {
    "single_choice": "单选题",
    "multiple_choice": "多选题",
    "text": "文本填空题",
    "number": "数字填空题",
}

MAX_NUMBER_INTEGER_DIGITS = 1000
MAX_NUMBER_FRACTION_DIGITS = 1000
MAX_NUMBER_INPUT_LENGTH = 2050


def decimal_digit_counts(value):
    """Count expanded places without rounding or expanding scientific notation."""
    if value.is_zero():
        return 1, 0
    _, digits, exponent = value.as_tuple()
    trailing = 0
    for digit in reversed(digits):
        if digit:
            break
        trailing += 1
    return max(1, value.adjusted() + 1), max(0, -exponent - trailing)


def definition_content(question):
    """Compare question definitions without changing stored numeric representations."""
    metadata = {"id", "version", "active", "jumps", "library_question_id",
                "library_root_id", "library_version", "library_source_question_version"}
    content = {key: deepcopy(value) for key, value in question.items() if key not in metadata}
    if content.get("type") == "number":
        content["number_statistics"] = content.get("number_statistics") or "distribution"
        for key in ("min_value", "max_value"):
            if content.get(key) is not None:
                content[key] = Decimal(str(content[key]))
    return content


def _nonnegative_int(raw, label):
    if not raw:
        return None
    if not raw.isdecimal():
        raise ValueError(f"{label}必须是非负整数。")
    return int(raw)


def _finite_decimal(raw, label):
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise ValueError(f"{label}必须是数字。")
    try:
        text = str(raw).strip()
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{label}超出数字处理范围。") from error
    if not text:
        return None
    if len(text) > MAX_NUMBER_INPUT_LENGTH:
        raise ValueError(f"{label}输入过长，请缩短后重试。")
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"{label}必须是数字。") from error
    if not value.is_finite():
        raise ValueError(f"{label}必须是有限数字。")
    integer_digits, fraction_digits = decimal_digit_counts(value)
    if integer_digits > MAX_NUMBER_INTEGER_DIGITS or fraction_digits > MAX_NUMBER_FRACTION_DIGITS:
        raise ValueError(f"{label}超出数字处理范围，整数和小数部分各不能超过 1000 位。")
    # A zero with an extreme exponent is still zero; keep only its calculation form.
    if value.is_zero():
        return Decimal(0)
    return value


def build_question(form):
    kind = form.get("type", "")
    title = form.get("title", "").strip()
    if kind not in QUESTION_TYPES:
        raise ValueError("请选择正确的题型。")
    if not title:
        raise ValueError("题目内容不能为空。")

    question = {
        "id": secrets.token_hex(8),
        "type": kind,
        "title": title,
        "required": form.get("required") == "on",
    }
    if kind in ("single_choice", "multiple_choice"):
        options = [line.strip() for line in form.get("options", "").splitlines() if line.strip()]
        if len(options) < 2 or len(options) != len(set(options)):
            raise ValueError("选项至少需要两个，且不能重复。")
        question["options"] = options
        if kind == "multiple_choice":
            minimum = _nonnegative_int(form.get("min_select", "").strip(), "最少选几项")
            maximum = _nonnegative_int(form.get("max_select", "").strip(), "最多选几项")
            exact = _nonnegative_int(form.get("exact_select", "").strip(), "需要选几项")
            if exact is not None and (minimum is not None or maximum is not None):
                raise ValueError("固定选择数量时，不能同时设置最少和最多选几项。")
            if exact is not None and (exact < 1 or exact > len(options)):
                raise ValueError("需要选择的项数应在 1 到选项总数之间。")
            if exact is not None:
                question["required"] = True
            if minimum is not None and minimum > len(options):
                raise ValueError("最少选择的项数不能超过选项总数。")
            if maximum is not None and maximum > len(options):
                raise ValueError("最多选择的项数不能超过选项总数。")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("最少选择的项数不能大于最多选择的项数。")
            if question["required"] and maximum == 0:
                raise ValueError("必答题至少要允许选择 1 项。")
            question["min_select"] = minimum
            question["max_select"] = maximum
            question["exact_select"] = exact
    elif kind == "text":
        minimum = _nonnegative_int(form.get("min_length", "").strip(), "最少字数")
        maximum = _nonnegative_int(form.get("max_length", "").strip(), "最多字数")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("最少字数不能大于最多字数。")
        if question["required"] and maximum == 0:
            raise ValueError("必答题的最多字数不能为 0。")
        question["min_length"] = minimum
        question["max_length"] = maximum
    else:
        minimum = _finite_decimal(form.get("min_value", "").strip(), "最小值")
        maximum = _finite_decimal(form.get("max_value", "").strip(), "最大值")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("最小值不能大于最大值。")
        integer_only = form.get("integer_only") == "on"
        if (integer_only and minimum is not None and maximum is not None
                and minimum.to_integral_value(rounding=ROUND_CEILING)
                > maximum.to_integral_value(rounding=ROUND_FLOOR)):
            raise ValueError("这个数值范围内没有可填写的整数。")
        question["min_value"] = str(minimum) if minimum is not None else None
        question["max_value"] = str(maximum) if maximum is not None else None
        question["integer_only"] = integer_only
        statistics_mode = form.get("number_statistics", "distribution")
        if statistics_mode not in ("distribution", "measure"):
            raise ValueError("数字题统计方式不正确。")
        question["number_statistics"] = statistics_mode
    return question


def validate_answer(question, form):
    key = "q_" + question["id"]
    kind = question["type"]
    title = question["title"]

    if kind == "multiple_choice":
        values = form.getlist(key)
        if not values:
            if question["required"]:
                raise ValueError("你还未作答本题。")
            return None
        if len(values) != len(set(values)) or any(value not in question["options"] for value in values):
            raise ValueError(f"“{title}”包含无效选项。")
        count = len(values)
        exact = question.get("exact_select")
        if exact is not None and count != exact:
            raise ValueError(f"“{title}”请选择 {exact} 项。")
        minimum = question.get("min_select")
        maximum = question.get("max_select")
        if minimum is not None and count < minimum:
            raise ValueError(f"“{title}”至少选 {minimum} 项。")
        if maximum is not None and count > maximum:
            raise ValueError(f"“{title}”最多选 {maximum} 项。")
        return values

    values = form.getlist(key)
    if len(values) > 1:
        raise ValueError(f"“{title}”的提交格式不正确。")
    value = values[0].strip() if values else ""
    if not value:
        if question["required"]:
            raise ValueError("你还未作答本题。")
        return None

    if kind == "single_choice":
        if value not in question["options"]:
            raise ValueError(f"“{title}”包含无效选项。")
    elif kind == "text":
        minimum = question.get("min_length")
        maximum = question.get("max_length")
        if minimum is not None and len(value) < minimum:
            raise ValueError(f"“{title}”至少输入 {minimum} 字。")
        if maximum is not None and len(value) > maximum:
            raise ValueError(f"“{title}”最多输入 {maximum} 字。")
    elif kind == "number":
        number = _finite_decimal(value, f"“{title}”")
        if question.get("integer_only") and number != number.to_integral_value():
            raise ValueError(f"“{title}”必须输入整数。")
        minimum = question.get("min_value")
        maximum = question.get("max_value")
        if minimum is not None and number < Decimal(minimum):
            raise ValueError(f"“{title}”不能小于 {minimum}。")
        if maximum is not None and number > Decimal(maximum):
            raise ValueError(f"“{title}”不能大于 {maximum}。")
    else:
        raise ValueError("问卷题型不正确。")
    return value
