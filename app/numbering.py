def question_label(position):
    return str(position)


def question_positions(questions):
    # Keep a fallback for historical answers to removed questions, while current
    # questions use the same consecutive positions as the filling page.
    positions = {question["id"]: index for index, question in enumerate(questions, 1)}
    positions.update({question["id"]: index for index, question in enumerate(
        (question for question in questions if question.get("active", True)), 1)})
    return positions


def option_style(survey):
    return survey.get("option_label_style", survey.get("numbering_style", "numeric"))


def option_label(position, style="numeric"):
    if style != "letters":
        return str(position)
    label = ""
    while position:
        position, remainder = divmod(position - 1, 26)
        label = chr(65 + remainder) + label
    return label
