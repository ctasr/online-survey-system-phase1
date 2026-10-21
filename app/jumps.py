import secrets
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR


NUMBER_OPERATORS = {
    "lt": "小于",
    "lte": "小于或等于",
    "eq": "等于",
    "gte": "大于或等于",
    "gt": "大于",
}
MULTI_MODES = {
    "any": "包含下方任意一项",
    "all": "包含下方所有选项",
    "exact": "只选择下方这些选项",
}


def build_jump_rule(questions, source_index, form):
    source = questions[source_index]
    if source["type"] not in ("single_choice", "multiple_choice", "number"):
        raise ValueError("只有单选题、多选题和数字填空题可以设置跳转。")
    target_id = form.get("target_id", "")
    if target_id not in {question["id"] for question in questions[source_index + 1:]}:
        raise ValueError("只能跳转到后面的题目。")
    rule = {"id": secrets.token_hex(8), "target_id": target_id}
    if source["type"] == "single_choice":
        option = form.get("option", "")
        if option not in source["options"]:
            raise ValueError("请选择有效的单选选项。")
        rule.update({"kind": "single_choice", "option": option})
    elif source["type"] == "multiple_choice":
        mode = form.get("mode", "")
        options = form.getlist("match_options")
        if mode not in MULTI_MODES:
            raise ValueError("请选择多选题的跳转条件。")
        if not options or len(options) != len(set(options)) or any(
            option not in source["options"] for option in options
        ):
            raise ValueError("请选择至少一个有效且不重复的多选选项。")
        rule.update({"kind": "multiple_choice", "mode": mode, "options": options})
    else:
        operator = form.get("operator", "")
        raw = form.get("threshold", "").strip()
        if operator not in NUMBER_OPERATORS:
            raise ValueError("请选择有效的数字比较方式。")
        try:
            threshold = Decimal(raw)
        except InvalidOperation as error:
            raise ValueError("设定的数字必须是有效数字。") from error
        if not threshold.is_finite():
            raise ValueError("设定的数字必须是有效数字。")
        if source.get("integer_only") and threshold != threshold.to_integral_value():
            raise ValueError("这道题只允许填写整数，设定的数字也必须是整数。")
        rule.update({"kind": "number", "operator": operator, "threshold": str(threshold)})
    return rule


def rule_matches(rule, value):
    if value is None:
        return False
    if rule["kind"] == "single_choice":
        return value == rule["option"]
    if rule["kind"] == "multiple_choice":
        selected = set(value)
        wanted = set(rule["options"])
        if rule["mode"] == "any":
            return bool(selected & wanted)
        if rule["mode"] == "all":
            return wanted <= selected
        return selected == wanted
    number = Decimal(value)
    threshold = Decimal(rule["threshold"])
    return {
        "lt": number < threshold,
        "lte": number <= threshold,
        "eq": number == threshold,
        "gte": number >= threshold,
        "gt": number > threshold,
    }[rule["operator"]]


def next_question_index(questions, index, value):
    source = questions[index]
    later = {question["id"]: position for position, question in enumerate(questions) if position > index}
    for rule in source.get("jumps", []):
        target_index = later.get(rule.get("target_id"))
        if target_index is not None and not rule_issue(source, rule, later) and rule_matches(rule, value):
            return target_index
    return index + 1


def rule_issue(question, rule, later):
    if rule.get("target_id") not in later:
        return "跳转目标已失效或不在本题之后"
    kind = question["type"]
    if rule.get("kind") != kind:
        return "条件与当前题型不匹配"
    if kind == "single_choice":
        if rule.get("option") not in question["options"]:
            return "匹配选项已不存在"
    elif kind == "multiple_choice":
        options = rule.get("options", [])
        if (rule.get("mode") not in MULTI_MODES or not isinstance(options, list)
                or not options or len(options) != len(set(options))
                or any(option not in question["options"] for option in options)):
            return "多选题的跳转条件已失效"
    elif kind == "number":
        if rule.get("operator") not in NUMBER_OPERATORS:
            return "数字比较方式已失效"
        try:
            threshold = Decimal(rule.get("threshold", ""))
        except (InvalidOperation, TypeError):
            return "设定的数字已失效"
        if not threshold.is_finite() or (question.get("integer_only") and threshold != threshold.to_integral_value()):
            return "设定的数字不符合这道题的要求"
    else:
        return "当前题型不支持跳转"
    return None


def _number_bounds(question, rules):
    lower = Decimal(question["min_value"]) if question.get("min_value") is not None else None
    upper = Decimal(question["max_value"]) if question.get("max_value") is not None else None
    lower_inclusive = upper_inclusive = True
    for rule in rules:
        value = Decimal(rule["threshold"])
        operator = rule["operator"]
        if operator in ("gt", "gte", "eq"):
            inclusive = operator != "gt"
            if lower is None or value > lower:
                lower, lower_inclusive = value, inclusive
            elif value == lower:
                lower_inclusive = lower_inclusive and inclusive
        if operator in ("lt", "lte", "eq"):
            inclusive = operator != "lt"
            if upper is None or value < upper:
                upper, upper_inclusive = value, inclusive
            elif value == upper:
                upper_inclusive = upper_inclusive and inclusive
    if question.get("integer_only"):
        minimum = None if lower is None else int(lower.to_integral_value(rounding=ROUND_CEILING))
        maximum = None if upper is None else int(upper.to_integral_value(rounding=ROUND_FLOOR))
        if minimum is not None and not lower_inclusive and Decimal(minimum) == lower:
            minimum += 1
        if maximum is not None and not upper_inclusive and Decimal(maximum) == upper:
            maximum -= 1
        return minimum is None or maximum is None or minimum <= maximum
    if lower is None or upper is None:
        return True
    return lower < upper or (lower == upper and lower_inclusive and upper_inclusive)


def _multi_clauses(rule, universe):
    wanted = set(rule["options"])
    if rule["mode"] == "any":
        return [({option}, set()) for option in wanted]
    if rule["mode"] == "all":
        return [(wanted, set())]
    return [(wanted, universe - wanted)]


def _multi_possible(question, rules):
    universe = set(question["options"])
    exact = question.get("exact_select")
    minimum = exact if exact is not None else max(1, question.get("min_select") or 0)
    maximum = exact if exact is not None else question.get("max_select")
    if maximum is None:
        maximum = len(universe)
    clauses = [(set(), set())]
    for rule in rules:
        clauses = [(required | more, excluded | forbidden)
                   for required, excluded in clauses
                   for more, forbidden in _multi_clauses(rule, universe)]
    for required, excluded in clauses:
        if required & excluded:
            continue
        if max(len(required), minimum) <= min(len(universe - excluded), maximum):
            return True
    return False


def rules_can_overlap(question, rules):
    kind = question["type"]
    if kind == "single_choice":
        return all(rule["option"] == rules[0]["option"] for rule in rules)
    if kind == "multiple_choice":
        return _multi_possible(question, rules)
    if kind == "number":
        return _number_bounds(question, rules)
    return False


def jump_warnings(questions):
    warnings = {}
    for index, question in enumerate(questions):
        rules = question.get("jumps", [])
        later = {item["id"] for item in questions[index + 1:]}
        found = []
        valid = []
        for number, rule in enumerate(rules, start=1):
            issue = rule_issue(question, rule, later)
            if issue:
                found.append(f"第 {number} 条规则：{issue}，填写时不会执行。")
            elif not rules_can_overlap(question, [rule]):
                found.append(f"第 {number} 条规则：按当前题目限制，不会有合法答案命中。")
            else:
                valid.append((number, rule))
        for position, (first_number, first) in enumerate(valid):
            for second_number, second in valid[position + 1:]:
                if rules_can_overlap(question, [first, second]):
                    consequence = (f"按第 {first_number} 条执行" if first["target_id"] != second["target_id"]
                                   else "两条跳向同一题，可考虑合并")
                    found.append(f"第 {first_number}、{second_number} 条规则可能同时命中；{consequence}。")
        if found:
            warnings[question["id"]] = found
    return warnings
