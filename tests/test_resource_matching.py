from app.services.resource_matching import matches_place_name


def test_place_matching_accepts_official_suffix_variants() -> None:
    assert matches_place_name(
        "蒙古部落敕勒川草原文化展示中心",
        "敕勒川草原文化旅游区",
    )


def test_place_matching_does_not_match_unrelated_places() -> None:
    assert not matches_place_name("大召寺", "内蒙古博物院")
