"""Rule-order bin assignment for local sorting profiles."""

from server.routers import sorting_profiles


def test_misc_can_share_the_default_category_bin(monkeypatch):
    categories = [[[[], []]]]
    saved = []
    monkeypatch.setattr(sorting_profiles, "_current_bin_categories", lambda: categories)
    monkeypatch.setattr(sorting_profiles, "_apply_and_persist_bin_categories", saved.append)

    count = sorting_profiles._preassign_bins_from_rules({
        "rules": [{"id": "zakje1"}, {"id": "overig"}],
        "default_category_id": "overig",
        "misc_shares_default_bin": True,
    })

    assert count == 2
    assert categories == [[[ ["zakje1"], ["overig", "misc"] ]]]
    assert saved == [categories]


def test_misc_is_not_auto_assigned_without_profile_opt_in(monkeypatch):
    categories = [[[[], []]]]
    monkeypatch.setattr(sorting_profiles, "_current_bin_categories", lambda: categories)
    monkeypatch.setattr(sorting_profiles, "_apply_and_persist_bin_categories", lambda _: None)

    sorting_profiles._preassign_bins_from_rules({
        "rules": [{"id": "zakje1"}, {"id": "overig"}],
        "default_category_id": "overig",
    })

    assert categories == [[[ ["zakje1"], ["overig"] ]]]
