from spain_open_data_mcp.core.envelope import Check, confidence_from


def test_confidence_is_the_weakest_input():
    assert confidence_from([]) == "high"
    assert confidence_from([Check(kind="cross_check", severity="info", message="")]) == "high"
    assert confidence_from([Check(kind="estimate", severity="info", message="")]) == "medium"
    assert confidence_from([Check(kind="thin_sample", severity="warning", message="")]) == "medium"
    assert confidence_from([Check(kind="stale", severity="critical", message="")]) == "low"
    assert confidence_from([], floor="medium") == "medium"
