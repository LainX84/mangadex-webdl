"""Highlighted tags setting (display only): validation and normalisation."""

import pytest
from pydantic import ValidationError

from app.settings_service import AppSettings


def test_default_is_empty():
    assert AppSettings().highlighted_tags == []


def test_names_trimmed_deduplicated_and_colours_lowercased():
    s = AppSettings.model_validate(
        {
            "highlighted_tags": [
                {"name": "  Girls'   Love ", "color": "#FF00AA"},
                {"name": "girls' love", "color": "#000000"},
                {"name": "Genderswap", "color": "#7048e8"},
                {"name": "   ", "color": "#123456"},
            ]
        }
    )
    assert [(t.name, t.color) for t in s.highlighted_tags] == [("Girls' Love", "#ff00aa"), ("Genderswap", "#7048e8")]


@pytest.mark.parametrize("color", ["red", "#fff", "#12345g", "rgb(1,2,3)"])
def test_invalid_colour_rejected(color):
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"highlighted_tags": [{"name": "Romance", "color": color}]})


def test_round_trip_through_model_dump():
    s = AppSettings.model_validate({"highlighted_tags": [{"name": "Gyaru", "color": "#0ca678"}]})
    again = AppSettings.model_validate(s.model_dump())
    assert again.highlighted_tags[0].name == "Gyaru"
    assert again.model_dump()["highlighted_tags"] == [{"name": "Gyaru", "color": "#0ca678"}]
