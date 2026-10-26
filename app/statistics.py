from collections import Counter
from decimal import Decimal, ROUND_HALF_UP, localcontext
from .numbering import question_positions
from .questions import _finite_decimal, decimal_digit_counts


def display_number(value):
    integer_digits, _ = decimal_digit_counts(value)
    with localcontext() as context:
        context.prec = max(28, integer_digits + 3)
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return numeric_label(rounded)


def display_percent(count, total):
    return display_number(Decimal(count) * 100 / total) if total else "0"


def numeric_label(number):
    """Format a numeric value exactly, without changing the stored raw answer."""
    if number.is_zero():
        return "0"
    label = format(number, "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def survey_overview(responses):
    anonymous = sum(response.get("respondent_id", "missing") is None for response in responses)
    named = sum(response.get("respondent_id", "missing") not in (None, "missing") for response in responses)
    times = [response["submitted_at"] for response in responses if response.get("submitted_at")]
    return {"anonymous": anonymous, "named": named,
            "first_submission": min(times) if times else None,
            "last_submission": max(times) if times else None}


def summarize_responses(questions, responses, question_history=None, return_overview=False):
    results = []
    by_id = {}
    numeric_values = {}
    history_by_id = {}
    reached_by_question = {question["id"]: 0 for question in questions}
    used_versions = set()
    response_count = anonymous = named = 0
    first_submission = last_submission = None
    for old in question_history or []:
        history_by_id.setdefault(old["id"], []).append(old)
    positions = question_positions(questions)
    for question in questions:
        versions = sorted(history_by_id.get(question["id"], []) + [question],
                          key=lambda item: item.get("version", 1))
        for definition in versions:
            key = (definition["id"], definition.get("version", 1))
            result = {
                "index": positions[question["id"]],
                "question": definition,
                "historical": definition is not question or not question.get("active", True),
                "current_definition": definition is question,
                "reached_count": 0,
                "blank_count": 0,
                "answered_count": 0,
                "invalid_count": 0,
                "option_counts": {option: 0 for option in definition.get("options", [])},
                "option_percentages": {},
                "values": [],
                "average": None,
                "median": None,
                "minimum": None,
                "maximum": None,
                "text_frequencies": [],
                "number_frequencies": [],
                "distinct_count": 0,
            }
            results.append(result)
            by_id[key] = result
            if definition["type"] == "number":
                numeric_values[key] = []

    for response_index, response in enumerate(responses):
        response_count += 1
        respondent = response.get("respondent_id", "missing")
        anonymous += respondent is None
        named += respondent not in (None, "missing")
        submitted = response.get("submitted_at")
        if submitted:
            first_submission = min(first_submission, submitted) if first_submission else submitted
            last_submission = max(last_submission, submitted) if last_submission else submitted
        reached_here = set()
        for answer in response.get("answers", []):
            key = (answer.get("question_id"), answer.get("question_version", 1))
            used_versions.add(key)
            result = by_id.get(key)
            if result is None:
                continue
            result["reached_count"] += 1
            reached_here.add(result["question"]["id"])
            value = answer.get("value")
            if value is None or value == "" or value == []:
                result["blank_count"] += 1
                continue
            kind = result["question"]["type"]
            if kind == "single_choice":
                if not isinstance(value, str) or value not in result["option_counts"]:
                    continue
                result["answered_count"] += 1
                result["option_counts"][value] += 1
            elif kind == "multiple_choice":
                if not isinstance(value, list):
                    continue
                chosen = set(value) & result["option_counts"].keys()
                if not chosen:
                    continue
                result["answered_count"] += 1
                for option in chosen:
                    result["option_counts"][option] += 1
            elif kind == "text":
                if not isinstance(value, str):
                    continue
                result["answered_count"] += 1
                result["values"].append(value)
            elif kind == "number":
                try:
                    number = _finite_decimal(value, "数字回答")
                except ValueError:
                    result["invalid_count"] += 1
                    continue
                if number is None:
                    result["invalid_count"] += 1
                    continue
                result["answered_count"] += 1
                result["values"].append(value)
                numeric_values[(result["question"]["id"], result["question"].get("version", 1))].append(number)
        for question_id in reached_here:
            reached_by_question[question_id] += 1

    for question_id, numbers in numeric_values.items():
        if numbers:
            result = by_id[question_id]
            frequencies = Counter(numbers)
            result["number_frequencies"] = [(numeric_label(value), count) for value, count in
                                           sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))]
            ordered = sorted(numbers)
            middle = len(ordered) // 2
            sizes = [decimal_digit_counts(number) for number in numbers]
            with localcontext() as context:
                context.prec = max(28, max(size[0] for size in sizes)
                                   + max(size[1] for size in sizes) + len(str(len(numbers))) + 4)
                average = sum(numbers, Decimal(0)) / len(numbers)
                median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
                by_id[question_id].update(
                    average=display_number(average), median=display_number(median),
                    minimum=numeric_label(ordered[0]),
                    maximum=numeric_label(ordered[-1]),
                )
    results = [result for result in results if (result["current_definition"] and not result["historical"]) or
               (result["question"]["id"], result["question"].get("version", 1)) in used_versions]
    for result in results:
        result["not_included_count"] = response_count - reached_by_question[result["question"]["id"]]
        if result["question"]["type"] in ("single_choice", "multiple_choice"):
            result["option_percentages"] = {
                option: display_percent(count, result["answered_count"])
                for option, count in result["option_counts"].items()
            }
        elif result["question"]["type"] == "text":
            frequencies = Counter(result["values"])
            result["distinct_count"] = len(frequencies)
            result["text_frequencies"] = sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))
    if return_overview:
        return results, {"response_count": response_count, "anonymous": anonymous, "named": named,
                         "first_submission": first_submission, "last_submission": last_submission}
    return results
