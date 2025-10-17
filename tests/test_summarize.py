"""Tests for adaptive summarisation of MorphoSource payloads."""
import pytest

from metadata_to_morphsource import router, summarize


@pytest.fixture
def media_payload():
    return {
        "media": [
            {
                "id": "0001",
                "title": "High-res Crocodile Skull CT",
                "description": "MicroCT volume of a crocodile skull",
                "permalink": "https://www.morphosource.org/media/0001",
            },
            {
                "id": "0002",
                "title": "Juvenile Crocodile CT",
                "description": "CT scan with soft-tissue detail",
                "permalink": "https://www.morphosource.org/media/0002",
            },
        ],
        "pages": {
            "total_count": 2,
            "total_pages": 1,
            "per_page": 12,
            "page": 1,
        },
    }


@pytest.fixture
def specimen_payload():
    return {
        "physical_objects": [
            {
                "uuid": "sp-1",
                "name": "Anolis specimen UF-1234",
                "object_number": "UF:1234",
                "taxonomy": "Anolis carolinensis",
                "permalink": "https://www.morphosource.org/objects/sp-1",
            },
            {
                "uuid": "sp-2",
                "name": "Anolis specimen UF-5678",
                "object_number": "UF:5678",
                "taxonomy": "Anolis sagrei",
            },
        ],
        "pages": {
            "total_count": 25,
            "total_pages": 3,
            "per_page": 12,
            "page": 2,
        },
    }


def test_media_summary_highlights_items(media_payload):
    request = router.QueryRequest(taxon="Crocodylia", intent="media", per_page=12, page=1)
    route = router.route_request(request)
    summary = summarize.summarize(media_payload, request=request, route=route).as_dict()

    assert "Found 2 media results" in summary["narrative"]
    assert "Crocodylia" in summary["narrative"]
    assert summary["spotlight"][0]["title"] == "High-res Crocodile Skull CT"
    assert summary["pagination"]["has_next"] is False
    assert summary["pagination"]["total_count"] == 2


def test_specimen_summary_adapts_to_pagination(specimen_payload):
    request = router.QueryRequest(taxon="Anolis", intent="specimens", per_page=12, page=2)
    route = router.route_request(request)
    summary = summarize.summarize(specimen_payload, request=request, route=route).as_dict()

    assert "Found 25 physical objects results" in summary["narrative"]
    assert "page 2" in summary["narrative"]
    assert summary["pagination"]["has_next"] is True
    assert summary["pagination"]["has_previous"] is True
    assert summary["spotlight"][0]["description"].startswith("Anolis")


def test_zero_results_summary_handles_empty_payload():
    request = router.QueryRequest(taxon="Serpentes", intent="media")
    summary = summarize.summarize({"media": []}, request=request).as_dict()

    assert summary["narrative"].startswith("No media results were found for Serpentes")
    assert summary["spotlight"] == []
    assert summary["pagination"]["total_count"] == 0
