"""Tests for the MorphoSource URL builder helpers."""
from metadata_to_morphsource import url_builder


def test_media_ct_scan_template_matches_prompt_example():
    template = url_builder.media_ct_scan("Reptilia")
    expected = (
        "https://www.morphosource.org/api/media?"
        "f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography"
        "&locale=en&search_field=all_fields"
        "&q=Reptilia"
    )
    assert template.url == expected


def test_media_ct_scan_open_access_template_matches_prompt_example():
    template = url_builder.media_ct_scan("Crocodylia", open_access=True)
    expected = (
        "https://www.morphosource.org/api/media?"
        "f%5Bmodality%5D%5B%5D=MicroNanoXRayComputedTomography"
        "&f%5Bvisibility%5D%5B%5D=Open"
        "&locale=en&search_field=all_fields"
        "&q=Crocodylia"
    )
    assert template.url == expected


def test_specimen_count_template_matches_prompt_example():
    template = url_builder.specimens_count("Serpentes")
    expected = (
        "https://www.morphosource.org/api/physical-objects?"
        "f%5Btaxonomy_gbif%5D%5B%5D=Serpentes"
        "&locale=en"
        "&per_page=1&page=1&taxonomy_gbif=Serpentes"
    )
    assert template.url == expected


def test_specimen_browse_template_matches_prompt_example():
    template = url_builder.specimens_browse("Serpentes")
    expected = (
        "https://www.morphosource.org/api/physical-objects?"
        "f%5Btaxonomy_gbif%5D%5B%5D=Serpentes"
        "&locale=en"
        "&per_page=12&page=1&taxonomy_gbif=Serpentes"
    )
    assert template.url == expected


def test_physical_objects_url_no_object_type():
    url = url_builder.specimens_browse("Squamata").url
    assert "object_type" not in url
    assert "f%5Btaxonomy_gbif%5D%5B%5D=Squamata" in url
    assert "taxonomy_gbif=Squamata" in url


def test_media_ct_scan_respects_custom_pagination():
    template = url_builder.media_ct_scan("Anolis", per_page=24, page=2)
    params = template.as_params()
    assert params["q"] == "Anolis"
    assert params["per_page"] == "24"
    assert params["page"] == "2"
