"""Tests for instruction_parser."""

from __future__ import annotations

from mise.services.instruction_parser import parse_instructions


def test_parses_simple_numbered_steps() -> None:
    blob = "1. Preheat oven\n2. Mix ingredients\n3. Bake"
    sections = parse_instructions(blob)
    assert len(sections) == 1
    assert sections[0].name is None
    assert sections[0].steps == ["Preheat oven", "Mix ingredients", "Bake"]


def test_strips_step_prefixes() -> None:
    blob = "Step 1: Preheat\nStep 2. Mix\n3) Bake\n• Cool"
    sections = parse_instructions(blob)
    assert sections[0].steps == ["Preheat", "Mix", "Bake", "Cool"]


def test_handles_sections() -> None:
    blob = "Marinade:\n1. Mix soy sauce\n2. Add ginger\n\nCooking:\n1. Sear meat\n2. Add sauce"
    sections = parse_instructions(blob)
    assert [s.name for s in sections] == ["Marinade", "Cooking"]
    assert sections[0].steps == ["Mix soy sauce", "Add ginger"]
    assert sections[1].steps == ["Sear meat", "Add sauce"]


def test_handles_unsectioned_followed_by_section() -> None:
    blob = "Wash veggies\nMarinade:\n1. Mix soy"
    sections = parse_instructions(blob)
    assert [s.name for s in sections] == [None, "Marinade"]
    assert sections[0].steps == ["Wash veggies"]


def test_drops_blank_lines() -> None:
    blob = "1. Preheat\n\n\n2. Bake"
    sections = parse_instructions(blob)
    assert sections[0].steps == ["Preheat", "Bake"]


def test_empty_blob_returns_empty() -> None:
    assert parse_instructions("") == []
    assert parse_instructions("   \n\n") == []


def test_preserves_step_text_after_strip() -> None:
    blob = "1. Mix 2 cups of flour"
    sections = parse_instructions(blob)
    # The "2 cups" embedded in step text should NOT be stripped — only
    # leading numbering goes.
    assert sections[0].steps == ["Mix 2 cups of flour"]
