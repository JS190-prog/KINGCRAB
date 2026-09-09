import pytest

from crabagent.kinetic_contract import compile_king_plan


@pytest.mark.parametrize("format", ["{name}", "{name}:", "## {name}", "**{name}:**", "**{name}**:"])
def test_king_headings_preserve_action_body_without_markup(format):
    body = "\n\n".join(format.format(name=name) + "\n" + value for name, value in [
        ("GOAL_RESTATEMENT", "Create the bounded artifact."),
        ("SUBGOALS", "- Check observed evidence."),
        ("CONSTRAINTS", "- One new file only."),
        ("SUCCESS_CHECKS", "- Verify its digest."),
        ("NEXT_ACTION", "QUEEN: bind the observed evidence."),
    ])
    plan = compile_king_plan(objective="Create the artifact", goal_plan={}, goal_graph={}, model_text=body)
    assert plan["next_action"] == "QUEEN: bind the observed evidence."
    assert plan["goal_restatement"] == "Create the bounded artifact."
    assert plan["subgoals"] == ["Check observed evidence."]


def test_king_inline_action_heading_preserves_its_body():
    plan = compile_king_plan(objective="Test", goal_plan={}, goal_graph={}, model_text="NEXT_ACTION: Read the source.")
    assert plan["next_action"] == "Read the source."
