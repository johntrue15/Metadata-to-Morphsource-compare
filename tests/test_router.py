"""Router tests covering primary and fallback URL plans."""
import pytest

from metadata_to_morphsource import router, url_builder


@pytest.fixture
def default_request():
    return router.QueryRequest(
        taxon="Anolis",
        intent="media",
        modality="ct",
        open_access=False,
        count_only=False,
        per_page=12,
        page=1,
    )


def test_media_request_returns_ct_primary_and_specimen_fallback(default_request):
    decision = router.route_request(default_request)

    assert decision.primary == url_builder.media_ct_scan("Anolis")
    assert decision.fallbacks[0] == url_builder.specimens_browse("Anolis", per_page=12, page=1)


def test_open_access_media_request_uses_open_template():
    request = router.QueryRequest(
        taxon="Crocodylia",
        intent="media",
        modality="ct",
        open_access=True,
        count_only=False,
    )
    decision = router.route_request(request)

    assert decision.primary == url_builder.media_ct_scan("Crocodylia", open_access=True)
    assert decision.fallbacks[0].url.endswith("taxonomy_gbif=Crocodylia")


def test_specimen_count_request_prioritises_count_template():
    request = router.QueryRequest(
        taxon="Serpentes",
        intent="specimens",
        count_only=True,
    )
    decision = router.route_request(request)

    assert decision.primary == url_builder.specimens_count("Serpentes")
    assert decision.fallbacks[0] == url_builder.specimens_browse("Serpentes")


def test_unknown_intent_defaults_to_media_plan():
    request = router.QueryRequest(
        taxon="Reptilia",
        intent="unknown",
        count_only=False,
    )
    decision = router.route_request(request)

    assert decision.primary == url_builder.media_ct_scan("Reptilia")
    urls = decision.urls()
    assert urls[0].startswith(url_builder.MORPHOSOURCE_BASE)
    # Fallback should include specimen browse for coverage
    assert any("physical-objects" in url for url in urls[1:])
