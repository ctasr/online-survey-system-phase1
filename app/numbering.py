def question_label(position):
    return str(position)


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
