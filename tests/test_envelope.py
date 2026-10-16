from spain_open_data_mcp.core.envelope import Check, confidence_from


def test_confidence_is_the_weakest_input():
    assert confidence_from([]) == "high"
    assert confidence_from([Check(kind="cross_check", severity="info", message="")]) == "high"
    assert confidence_from([Check(kind="estimate", severity="info", message="")]) == "medium"
    assert confidence_from([Check(kind="thin_sample", severity="warning", message="")]) == "medium"
    assert confidence_from([Check(kind="stale", severity="critical", message="")]) == "low"
    assert confidence_from([], floor="medium") == "medium"


def test_confidence_line_says_what_it_means():
    from spain_open_data_mcp.core.envelope import render_footer

    text = render_footer([], [], "medium")
    assert text.splitlines()[-1].startswith("Confidence: medium (") and "estimate" in text
