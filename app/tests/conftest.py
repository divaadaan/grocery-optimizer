"""Suite-wide guards.

Tests are deterministic: no live network, no live LLM. Post-ingestion deal
labelling (``app/services/deal_labeller.py``) calls a hosted model on a
background thread when ``DEAL_LABELLING_ENABLED`` is on -- its shipped default,
and possibly on in ``.env`` -- so it is switched off for every test. A test of
the labelling hook turns it back on and injects a fake runner.
"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _no_background_labelling(monkeypatch):
    monkeypatch.setattr(settings, "deal_labelling_enabled", False)
