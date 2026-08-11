from ltengine.token_budget import output_cap


def test_disabled_when_max_tokens_zero() -> None:
    assert output_cap(1000, 0, 3.0, 1.5, 64) is None


def test_static_ceiling_when_dynamic_knobs_unset() -> None:
    assert output_cap(1000, 16384, 0.0, 0.0, 64) == 16384


def test_dynamic_cap() -> None:
    # ceil(1000/3)=334; ceil(334*1.5)=501; max(64)->501; min(16384)->501
    assert output_cap(1000, 16384, 3.0, 1.5, 64) == 501


def test_floor_applies() -> None:
    # ceil(30/3)=10; ceil(10*1.5)=15; max(64)->64
    assert output_cap(30, 16384, 3.0, 1.5, 64) == 64


def test_ceiling_wins_over_floor() -> None:
    # huge input -> dynamic 50001 -> min(500) = 500 even though floor is 64
    assert output_cap(100_000, 500, 3.0, 1.5, 64) == 500
