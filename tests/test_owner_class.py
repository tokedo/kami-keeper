"""SPEC P3a — classify() is the observation-term definition; render() is
the only formatter and its output is the closed set."""

from __future__ import annotations

import itertools

from keeper.owner_class import OwnerClass, classify, is_observation_name, render


def test_definition():
    assert classify(listed=True, max_threshold=999, owner_hostile_in_window=True, kami_hostile_in_window=True) is OwnerClass.FRIEND
    assert classify(listed=False, max_threshold=0, owner_hostile_in_window=False, kami_hostile_in_window=False) is OwnerClass.HARMLESS
    assert classify(listed=False, max_threshold=10, owner_hostile_in_window=False, kami_hostile_in_window=False) is OwnerClass.ARMED_PASSIVE
    assert classify(listed=False, max_threshold=10, owner_hostile_in_window=True, kami_hostile_in_window=False) is OwnerClass.HOSTILE
    assert classify(listed=False, max_threshold=0, owner_hostile_in_window=False, kami_hostile_in_window=True) is OwnerClass.HOSTILE
    # the standing list binds without a kill and without a window; a
    # kill still outranks it and `listed` still wins over both
    assert classify(listed=False, max_threshold=0, owner_hostile_in_window=False, kami_hostile_in_window=False, standing=True) is OwnerClass.STANDING
    assert classify(listed=False, max_threshold=5, owner_hostile_in_window=True, kami_hostile_in_window=False, standing=True) is OwnerClass.HOSTILE
    assert classify(listed=True, max_threshold=5, owner_hostile_in_window=False, kami_hostile_in_window=False, standing=True) is OwnerClass.FRIEND


def test_every_input_combination_renders_into_the_closed_set():
    """The closed set is FIVE names and the input space has the
    `standing` dimension (the keeper's `standingOwners`)."""
    seen = set()
    for listed, thr, kw, ke, st in itertools.product((True, False), (0, 5), (True, False), (True, False), (True, False)):
        name = render(classify(listed=listed, max_threshold=thr, owner_hostile_in_window=kw, kami_hostile_in_window=ke, standing=st), 24)
        assert is_observation_name(name)
        seen.add(name)
    assert seen == {"listed", "no-threshold", "threshold-no-kill", "standing", "killed-here-24h"}
    assert render(OwnerClass.HOSTILE, 6) == "killed-here-6h" and render(OwnerClass.STANDING, 6) == "standing"
    assert not is_observation_name("hostile") and not is_observation_name("killed-here-")
