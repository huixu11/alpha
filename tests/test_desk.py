import base64

import pytest

from honest_alpha import agent, desk

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
EV = {"rules": {"cost_bps": 15, "stress_bps": 30, "dsr_min": 0.95, "holdout_start": "2022-12-30", "holdout_looks": 3,
                "search_window": ["2015-06-30", "2022-12-29"]}, "rules_sha256": "6d1b1514546d67eb" + "0" * 48}


def test_screenshot_must_be_an_image():
    assert desk._image(PNG)[0] == "image/png"
    with pytest.raises(ValueError):
        desk._image("data:text/html;base64," + base64.b64encode(b"<script>").decode())
    with pytest.raises(ValueError):
        desk._image("data:image/png," + "not base64")


def test_desk_answers_under_the_same_gate_without_a_researcher_style():
    system = desk.DeskHunt.system(object(), EV)
    assert "A person at the desk" in system and "not how it is judged" in system
    assert "6d1b1514546d67eb" in system and "cannot name a stock" in system
    assert not any(style in system for style in agent.STYLES.values())


def test_desk_proposes_from_the_same_space():
    ideas = desk.DESK_TOOL["input_schema"]["properties"]["ideas"]
    assert ideas == agent.PROPOSE_TOOL["input_schema"]["properties"]["ideas"]
