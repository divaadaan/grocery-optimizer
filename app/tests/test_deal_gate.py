"""Unit tests for the deal-source gate that holds the deferred compliance fix.

These tests encode an ACCEPTED ALLERGEN-SAFETY DEFERRAL. ``app/agents/dietary.py``
decides dietary compliance by casefold substring match, which is unsound against
raw flyer product names in the dangerous direction: ``prosciutto``, ``chorizo``,
``kielbasa``, ``gelatin``, ``lard`` and ``marzipan`` contain no forbidden term,
so a nut-free or vegetarian plan built from ingested deals can include them.

The fix is deferred by decision, not by oversight. The gate is what makes the
deferral safe, so these tests are the thing standing between a restricted user
and a flyer name the oracle cannot read. If one of them starts failing, the
answer is not to update the assertion.
"""
import pytest

from app.agents import dietary
from app.services.deal_gate import (
    CURATED_SOURCES,
    allowed_sources,
    filter_deals,
    ingested_deals_allowed,
    restrictions_are_active,
)

SEED_DEAL = {"product_name": "Chicken Breast Boneless", "source": "seed"}
INGESTED_DEAL = {"product_name": "MINA HALAL BONELESS CHICKEN BREAST", "source": "flipp"}
INGESTED_SNEAKY = {"product_name": "Prosciutto di Parma 100g", "source": "flipp"}
LEGACY_DEAL = {"product_name": "Pasta Penne"}  # pre-migration row: no source key


# --- the gap the gate exists to contain -------------------------------------

@pytest.mark.parametrize("product_name", [
    "Prosciutto di Parma",
    "Chorizo Sausage Sticks",   # 'sausage' IS matched - included as a control
    "Kielbasa Ring",
    "Capicollo Hot",
    "Pancetta Cubes",
    "Gelatin Dessert Mix",
    "Lard 454g",
    "Marzipan Bar",
])
def test_documents_which_flyer_names_the_oracle_misreads(product_name):
    """Characterisation, not aspiration: records today's oracle behaviour.

    Every name here is animal- or nut-derived. ``is_compliant`` returning True
    for any of them is a false negative. This test asserts the *current* state
    so the deferral is measurable; it is expected to need rewriting when
    dietary.py is fixed.
    """
    vegan_ok = dietary.is_compliant(product_name, ["vegan"])
    nut_free_ok = dietary.is_compliant(product_name, ["no_nuts"])
    # At least one restriction fails to catch it (chorizo is the one the
    # substring vocabulary happens to cover, via "sausage").
    assert vegan_ok or nut_free_ok, (
        f"{product_name!r} is now caught by both oracles - if dietary.py was "
        "fixed, revisit app/services/deal_gate.py and this test"
    )


def test_oracle_also_false_positives_on_safe_flyer_produce():
    # The harmless direction, recorded for completeness.
    assert dietary.is_compliant("Beefsteak Tomatoes", ["vegetarian"]) is False
    assert dietary.is_compliant("Hamburger Buns", ["vegetarian"]) is False
    assert dietary.is_compliant("Butternut Squash", ["no_nuts"]) is False


# --- the gate ---------------------------------------------------------------

def test_restrictions_are_active():
    assert restrictions_are_active(["vegan"]) is True
    assert restrictions_are_active([]) is False
    assert restrictions_are_active(None) is False
    assert restrictions_are_active(["", "  "]) is False


def test_unrestricted_user_sees_ingested_deals():
    # Nothing for the unsound oracle to get wrong, so no reason to withhold.
    assert ingested_deals_allowed([], allow_for_restricted=False) is True
    assert allowed_sources([], allow_for_restricted=False) is None


def test_restricted_user_is_withheld_ingested_deals_by_default():
    assert ingested_deals_allowed(["vegan"], allow_for_restricted=False) is False
    assert allowed_sources(["vegan"], allow_for_restricted=False) == sorted(CURATED_SOURCES)


def test_restricted_user_sees_ingested_deals_only_when_explicitly_waived():
    assert ingested_deals_allowed(["vegan"], allow_for_restricted=True) is True
    assert allowed_sources(["vegan"], allow_for_restricted=True) is None


def test_the_flag_defaults_to_off_in_settings():
    """The deferral must be opt-in. A default of True would ship the gap open."""
    from app.config import settings
    assert settings.ingested_deals_for_restricted_diets is False
    assert settings.deal_ingestion_enabled is False


# --- filtering --------------------------------------------------------------

def test_filter_withholds_ingested_deals_from_restricted_user():
    deals = [SEED_DEAL, INGESTED_DEAL, INGESTED_SNEAKY]
    kept = filter_deals(deals, ["vegetarian"], allow_for_restricted=False)
    assert kept == [SEED_DEAL]


def test_filter_is_a_passthrough_for_unrestricted_user():
    deals = [SEED_DEAL, INGESTED_DEAL]
    assert filter_deals(deals, [], allow_for_restricted=False) == deals


def test_filter_passes_everything_through_once_waived():
    deals = [SEED_DEAL, INGESTED_DEAL]
    assert filter_deals(deals, ["vegan"], allow_for_restricted=True) == deals


def test_legacy_rows_without_a_source_count_as_curated():
    """Pre-migration `deals` rows have no source column.

    Failing closed on them would empty the seed catalog for every restricted
    user, which breaks the app without making anyone safer - the seed names are
    exactly the hand-written ones the oracle IS sound against.
    """
    kept = filter_deals([LEGACY_DEAL, INGESTED_DEAL], ["vegan"], allow_for_restricted=False)
    assert kept == [LEGACY_DEAL]


def test_filter_does_not_mutate_its_input():
    deals = [SEED_DEAL, INGESTED_DEAL]
    filter_deals(deals, ["vegan"], allow_for_restricted=False)
    assert len(deals) == 2


def test_curated_sources_does_not_include_a_machine_source():
    assert "flipp" not in CURATED_SOURCES
    assert CURATED_SOURCES == frozenset({"seed", "manual"})
