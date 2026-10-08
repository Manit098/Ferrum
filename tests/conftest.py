"""Shared fixtures: keep tests away from the real ferrum config file
and from any FERRUM_* variables in the developer's environment."""

import pytest

from ferrum.config import INT_FIELDS, STR_FIELDS


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    target = tmp_path / "ferrum-config.json"
    monkeypatch.setattr("ferrum.config.config_file_path", lambda: target)
    for var in list(STR_FIELDS.values()) + list(INT_FIELDS.values()):
        monkeypatch.delenv(var, raising=False)
    return target
