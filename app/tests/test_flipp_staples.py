"""Guardrails on the staple list and the Flipp client's politeness settings.

The staple list is the ingestion scoping lever: one HTTP request per term per
postal code, against an endpoint that owes us nothing. These tests keep an
edit to ``app/services/flipp_staples.py`` from quietly turning a refresh into a
crawl, and keep the client's retry/throttle behaviour from regressing. No
network: the HTTP tests drive a fake session.
"""
import pytest
import requests

from app.services.flipp_client import (
    RETRYABLE_STATUS,
    DealProviderError,
    FlippSearchClient,
)
from app.services.flipp_staples import (
    CATEGORIES,
    MAX_STAPLES,
    STAPLES,
    category_for_term,
    staple_terms,
)


# --- the staple list --------------------------------------------------------

def test_staples_is_not_empty():
    assert len(STAPLES) > 0


def test_staples_is_bounded():
    """A refresh is one request per term. 60 terms x 1.5s is already 90s."""
    assert len(STAPLES) <= MAX_STAPLES


def test_terms_are_unique():
    terms = staple_terms()
    assert len(terms) == len(set(terms))


def test_terms_are_lowercase_and_trimmed():
    for term in staple_terms():
        assert term == term.strip().casefold(), term


def test_terms_are_long_enough_to_be_meaningful():
    """The endpoint answers an empty q with the entire national flyer corpus
    (mattresses, tool accessories, diapers), and near-random results for one-
    and two-letter queries."""
    for term in staple_terms():
        assert len(term) >= 3, term


def test_every_category_is_declared():
    for staple in STAPLES:
        assert staple.category in CATEGORIES, staple


def test_category_lookup_is_case_insensitive():
    assert category_for_term("Chicken Breast") == "Meat & Poultry"
    assert category_for_term("  milk ") == "Dairy & Eggs"


def test_unknown_term_has_no_category():
    # Legal: scripts/ingest_deals.py --terms allows ad-hoc terms.
    assert category_for_term("dragon fruit") is None
    assert category_for_term("") is None


def test_the_list_spans_the_categories_a_meal_plan_needs():
    covered = {s.category for s in STAPLES}
    assert {"Produce", "Meat & Poultry", "Dairy & Eggs", "Pantry"} <= covered


# --- client politeness ------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"items": []}
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("not json")
        return self._payload


_BAD_JSON = object()


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout,
                           "headers": headers or {}})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(responses, **kwargs):
    slept = []
    kwargs.setdefault("min_interval", 0.0)
    client = FlippSearchClient(
        session=FakeSession(responses),
        sleep=slept.append,
        **kwargs,
    )
    return client, slept


def test_sends_a_user_agent_and_a_timeout():
    client, _ = _client([FakeResponse(payload={"items": [{"name": "x"}]})])
    client.search("milk", "M5V3A8")

    call = client._session.calls[0]
    assert call["headers"]["User-Agent"]
    assert "grocery-optimizer" in call["headers"]["User-Agent"]
    assert call["timeout"] == client.timeout
    assert call["timeout"] > 0


def test_sends_the_term_and_postal_code_as_query_params():
    client, _ = _client([FakeResponse()])
    client.search("ground beef", "M5V 3A8")
    assert client._session.calls[0]["params"] == {
        "q": "ground beef", "postal_code": "M5V 3A8"
    }


def test_empty_term_is_refused_without_a_request():
    client, _ = _client([])
    with pytest.raises(ValueError):
        client.search("   ", "M5V3A8")
    assert client._session.calls == []


def test_throttles_between_requests():
    client, slept = _client([FakeResponse(), FakeResponse()], min_interval=5.0)
    client.search("milk", "M5V3A8")
    client.search("eggs", "M5V3A8")
    # Second call had to wait out the interval.
    assert any(0 < s <= 5.0 for s in slept), slept


def test_retries_on_transport_error_then_succeeds():
    client, slept = _client(
        [requests.ConnectionError("reset"), FakeResponse(payload={"items": [{"a": 1}]})],
        max_retries=2,
    )
    assert client.search("milk", "M5V3A8") == [{"a": 1}]
    assert len(client._session.calls) == 2
    assert slept, "should have backed off before retrying"


def test_backoff_grows():
    client, _ = _client([FakeResponse()])
    delays = [client._backoff(i) for i in range(4)]
    assert delays[0] < delays[-1]
    assert all(d <= 30.0 for d in delays)


@pytest.mark.parametrize("status_code", sorted(RETRYABLE_STATUS))
def test_retries_on_server_side_statuses(status_code):
    client, _ = _client(
        [FakeResponse(status_code=status_code), FakeResponse(payload={"items": []})],
        max_retries=1,
    )
    assert client.search("milk", "M5V3A8") == []
    assert len(client._session.calls) == 2


def test_does_not_retry_a_permanent_rejection():
    """403/404 means stop asking. Retrying a refusal is the rude case."""
    client, _ = _client([FakeResponse(status_code=403)], max_retries=3)
    with pytest.raises(DealProviderError):
        client.search("milk", "M5V3A8")
    assert len(client._session.calls) == 1


def test_gives_up_after_max_retries():
    client, _ = _client([requests.Timeout("slow")] * 3, max_retries=2)
    with pytest.raises(DealProviderError):
        client.search("milk", "M5V3A8")
    assert len(client._session.calls) == 3  # 1 attempt + 2 retries


def test_non_json_body_is_an_error_not_an_empty_result():
    client, _ = _client([FakeResponse(payload=_BAD_JSON)])
    with pytest.raises(DealProviderError):
        client.search("milk", "M5V3A8")


def test_missing_items_key_yields_an_empty_list_not_a_crash():
    # Defends against a silent provider shape change.
    client, _ = _client([FakeResponse(payload={"ecom_items": [], "flyers": []})])
    assert client.search("milk", "M5V3A8") == []


def test_non_dict_entries_in_items_are_dropped():
    client, _ = _client([FakeResponse(payload={"items": [{"a": 1}, None, "x"]})])
    assert client.search("milk", "M5V3A8") == [{"a": 1}]


def test_client_defaults_come_from_settings():
    from app.config import settings

    client = FlippSearchClient(session=FakeSession([]))
    assert client.url == settings.flipp_search_url
    assert "backflipp.wishabi.com" in client.url
    assert client.min_interval >= 1.0, "do not ship a sub-second request interval"
    assert client.timeout > 0
    assert client.source == "flipp"
