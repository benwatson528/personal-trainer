"""Equipment inventory loading and how it reaches the prompts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from personal_trainer.equipment import Equipment, Plate, load_equipment

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inventory_path(tmp_path: Path) -> Path:
    p = tmp_path / "equipment.yaml"
    p.write_text(yaml.safe_dump({
        "bars": {"barbell": 20, "ez_bar": 20},
        "plates": [{"weight": 20, "count": 2}, {"weight": 1.25, "count": 2}],
        "dumbbells": [24, 16, 8],
        "other": ["Kettlebells: 16, 24kg"],
    }))
    return p


def test_loads_the_real_config_file():
    """The shipped inventory must parse — it is what the athlete edits."""
    equipment = load_equipment(PROJECT_ROOT / "config" / "equipment.yaml")
    assert not equipment.is_empty
    assert equipment.bars["barbell"] == 20
    assert equipment.dumbbells, "no dumbbells listed"
    assert equipment.plates, "no plates listed"


def test_missing_file_yields_empty_inventory(tmp_path: Path):
    """Absence of the file must degrade quietly, not crash a workout."""
    equipment = load_equipment(tmp_path / "nope.yaml")
    assert equipment.is_empty
    assert "No equipment inventory" in equipment.describe()


def test_loads_all_sections(inventory_path: Path):
    equipment = load_equipment(inventory_path)
    assert equipment.bars == {"barbell": 20.0, "ez_bar": 20.0}
    assert equipment.dumbbells == [24.0, 16.0, 8.0]
    assert equipment.other == ["Kettlebells: 16, 24kg"]
    assert len(equipment.plates) == 2


def test_dumbbells_sorted_deduplicated_and_numeric():
    """The LLM needs one clean ascending-scale list, not whatever order was typed."""
    equipment = Equipment(dumbbells=[16.0, 24.0, 8.0, 16.0])
    assert equipment.dumbbells == [24.0, 16.0, 8.0]


def test_plates_accept_bare_numbers_as_well_as_dicts(tmp_path: Path):
    """Writing `- 20` instead of `- {weight: 20}` should still work."""
    equipment = _from_dict(tmp_path, {"plates": [20, 2.5]})
    assert equipment.plates == [Plate(20.0, 2), Plate(2.5, 2)]


def test_plate_count_defaults_to_a_pair(tmp_path: Path):
    equipment = _from_dict(tmp_path, {"plates": [{"weight": 20}]})
    assert equipment.plates == [Plate(20.0, 2)]


def _from_dict(tmp_path: Path, data: dict) -> Equipment:
    p = tmp_path / "equipment.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_equipment(p)


def test_malformed_file_does_not_crash(tmp_path: Path):
    p = tmp_path / "equipment.yaml"
    p.write_text("plates: [unclosed\n")
    assert load_equipment(p).is_empty


def test_describe_gives_the_model_the_actual_constraint(inventory_path: Path):
    """Weights are chosen by the LLM, so the numbers must actually be in the prompt."""
    equipment = load_equipment(inventory_path)
    text = equipment.describe()
    assert "24, 16, 8" in text, "dumbbell weights missing from prompt"
    assert "20kg x2" in text, "plate inventory missing from prompt"
    assert "1.25kg x2" in text
    assert "barbell 20kg" in text
    assert "ez_bar 20kg" in text
    assert "Kettlebells" in text
    # The rule that actually stops it inventing weights.
    assert "not listed" in text


def test_describe_brief_omits_plates(inventory_path: Path):
    """Chat gets a compact summary; plate maths belongs to the generator."""
    equipment = load_equipment(inventory_path)
    brief = equipment.describe_brief()
    assert "24, 16, 8" in brief
    assert "20kg x2" not in brief
    assert "1.25" not in brief


def test_describe_brief_is_empty_without_an_inventory():
    assert Equipment().describe_brief() == ""


def test_generation_prompt_carries_the_equipment(strong_service, stub_llm):
    """End to end: the inventory reaches the generator's prompt, not just the loader."""
    from test_strong_generation import (
        VALID_CSV, prescription_response, validation_response,
    )
    from conftest import make_llm_response

    stub_llm.responses = [
        prescription_response(),
        make_llm_response("```csv\n" + VALID_CSV + "```"),
        validation_response(),
    ]
    result = strong_service.generate("jnt_2", "what next?")

    assert result.success, result.errors
    # The prescription call carries the equipment block.
    assert "Equipment available" in stub_llm.calls[0]["messages"][-1]["content"]
    # So does the CSV-writing call.
    assert "Equipment available" in stub_llm.calls[1]["messages"][-1]["content"]
    # And the system prompt states the constraint.
    assert "loadable with the equipment" in stub_llm.calls[1]["messages"][0]["content"]