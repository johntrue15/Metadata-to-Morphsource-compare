"""Command-line interface for MorphoSource helpers."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional
from urllib.parse import urlencode

from . import ensure_pipeline_imports


ensure_pipeline_imports()


def _load_pipeline():
    try:
        import query_formatter  # type: ignore
        import morphosource_api  # type: ignore
    except ImportError as exc:  # pragma: no cover - defensive path resolution
        raise RuntimeError(
            "Unable to import query pipeline modules. Install the project dependencies (requests, openai)."
        ) from exc
    return query_formatter, morphosource_api


API_BASE = "https://www.morphosource.org/api"


def _suppress_stdout(enabled: bool):
    if enabled:
        return contextlib.nullcontext()
    buffer = io.StringIO()
    return contextlib.redirect_stdout(buffer)


def _prepare_params(base: Optional[Mapping[str, Any]], *,
                    per_page: Optional[int] = None,
                    page: Optional[int] = None,
                    ensure_locale: bool = False) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if base:
        params.update(base)

    if per_page is not None:
        params['per_page'] = str(per_page)
    if page is not None:
        params['page'] = str(page)
    if ensure_locale and 'locale' not in params:
        params['locale'] = 'en'

    return params


def _build_request_url(endpoint: str, params: Mapping[str, Any]) -> str:
    query = urlencode(params, doseq=True)
    if query:
        return f"{API_BASE}/{endpoint}?{query}"
    return f"{API_BASE}/{endpoint}"


def _build_media_page_url(media: Mapping[str, Any]) -> Optional[str]:
    for key in ('slug', 'id'):
        value = media.get(key)
        if isinstance(value, str) and value:
            return f"https://www.morphosource.org/concern/media/{value}"
    return None


def _ensure_ct_modality(params: MutableMapping[str, Any]) -> None:
    modality_value = 'MicroNanoXRayComputedTomography'
    params.setdefault('modality', modality_value)
    params.setdefault('f[modality][]', modality_value)


def _ensure_specimen_filters(params: MutableMapping[str, Any]) -> None:
    """Ensure specimen-related taxonomy filters stay in sync."""

    array_key = 'f[taxonomy_gbif][]'
    taxonomy_value: Any = params.get('taxonomy_gbif')

    if taxonomy_value and array_key not in params:
        params[array_key] = taxonomy_value
        return

    array_value = params.get(array_key)
    if array_value and 'taxonomy_gbif' not in params:
        if isinstance(array_value, list):
            params['taxonomy_gbif'] = array_value[0]
        else:
            params['taxonomy_gbif'] = array_value


def _format_media_title(media: Mapping[str, Any], fallback: str) -> str:
    for key in ('title', 'name', 'label', 'slug', 'id'):
        value = media.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def count_specimens(args: argparse.Namespace) -> int:
    query_formatter, morphosource_api = _load_pipeline()
    formatted = query_formatter.format_query(args.query)
    params = _prepare_params(
        formatted.get('api_params'),
        per_page=args.per_page,
        page=args.page,
        ensure_locale=True,
    )
    _ensure_specimen_filters(params)

    query_info = {
        **formatted,
        'api_endpoint': 'physical-objects',
    }

    with _suppress_stdout(args.debug):
        result = morphosource_api.search_morphosource(
            params,
            query_info.get('formatted_query', args.query),
            query_info=query_info,
        )

    summary = result.get('summary', {})
    query_details = result.get('query_info', query_info)
    endpoint = query_details.get('api_endpoint', 'physical-objects')
    params = query_details.get('api_params', params)
    request_url = _build_request_url(endpoint, params)

    print("COUNT-SPECIMENS")
    print(f"original_query: {args.query}")
    print(f"formatted_query: {query_details.get('formatted_query', args.query)}")
    print(f"endpoint: {endpoint}")
    print(f"request_url: {request_url}")
    print(f"total_count: {summary.get('count', 0)}")

    return 0


def browse_ct(args: argparse.Namespace) -> int:
    query_formatter, morphosource_api = _load_pipeline()
    formatted = query_formatter.format_query(args.query)
    params = _prepare_params(
        formatted.get('api_params'),
        per_page=args.per_page,
        page=args.page,
        ensure_locale=True,
    )
    params.setdefault('search_field', 'all_fields')
    _ensure_ct_modality(params)

    if args.open_only:
        params.setdefault('f[visibility][]', 'Open')

    query_info = {
        **formatted,
        'api_endpoint': 'media',
    }

    with _suppress_stdout(args.debug):
        result = morphosource_api.search_morphosource(
            params,
            query_info.get('formatted_query', args.query),
            query_info=query_info,
        )

    summary = result.get('summary', {})
    query_details = result.get('query_info', query_info)
    endpoint = query_details.get('api_endpoint', 'media')
    params = query_details.get('api_params', params)
    request_url = _build_request_url(endpoint, params)

    media_items: Iterable[Mapping[str, Any]] = result.get('full_data', {}).get('media', [])  # type: ignore[assignment]
    media_list: List[Mapping[str, Any]] = list(media_items)

    print("BROWSE-CT")
    print(f"original_query: {args.query}")
    print(f"formatted_query: {query_details.get('formatted_query', args.query)}")
    print(f"endpoint: {endpoint}")
    print(f"request_url: {request_url}")
    print(f"page: {params.get('page', args.page)}")
    print(f"per_page: {params.get('per_page', args.per_page)}")
    print(f"returned: {len(media_list)}")

    for index, media in enumerate(media_list, 1):
        title = _format_media_title(media, f"Result {index}")
        media_url = _build_media_page_url(media) or "(no URL available)"
        print(f"{index}. {title} -> {media_url}")

    if args.csv:
        fieldnames = ['index', 'title', 'media_page_url']
        extra_keys = set()
        for media in media_list:
            extra_keys.update(str(key) for key in media.keys())
        fieldnames.extend(sorted(extra_keys))

        output_path = Path(args.csv)
        with output_path.open('w', newline='', encoding='utf-8') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for idx, media in enumerate(media_list, 1):
                row = {key: media.get(key) for key in extra_keys}
                row.update(
                    {
                        'index': idx,
                        'title': _format_media_title(media, f"Result {idx}"),
                        'media_page_url': _build_media_page_url(media),
                    }
                )
                writer.writerow(row)
        print(f"csv_export: {output_path}")

    if args.open_browser:
        webbrowser.open(request_url)

    return 0


def nl_query(args: argparse.Namespace) -> int:
    query_formatter, morphosource_api = _load_pipeline()
    formatted = query_formatter.format_query(args.query)
    params = _prepare_params(
        formatted.get('api_params'),
        per_page=args.per_page,
        page=args.page,
        ensure_locale=True,
    )
    endpoint = formatted.get('api_endpoint') or 'media'

    query_info = {
        **formatted,
        'api_endpoint': endpoint,
    }

    with _suppress_stdout(args.debug):
        result = morphosource_api.search_morphosource(
            params,
            query_info.get('formatted_query', args.query),
            query_info=query_info,
        )

    query_details = result.get('query_info', query_info)
    endpoint = query_details.get('api_endpoint', endpoint)
    params = query_details.get('api_params', params)
    request_url = _build_request_url(endpoint, params)

    payload = {
        'original_query': args.query,
        'formatted_query': query_details.get('formatted_query', args.query),
        'endpoint': endpoint,
        'request_url': request_url,
        'summary': result.get('summary', {}),
        'results': result.get('full_data', {}),
    }

    print(json.dumps(payload, indent=2))

    return 0


def _add_pagination_args(parser: argparse.ArgumentParser, *, default_page: int, default_per_page: int) -> None:
    parser.add_argument('--page', type=int, default=default_page, help='Result page number (default: %(default)s)')
    parser.add_argument('--per-page', type=int, default=default_per_page, help='Results per page (default: %(default)s)')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MorphoSource command-line interface")
    parser.add_argument('--debug', action='store_true', help='Display underlying pipeline output')

    subparsers = parser.add_subparsers(dest='command', required=True)

    count_parser = subparsers.add_parser('count-specimens', help='Count biological specimens that match a query')
    count_parser.add_argument('query', help='Natural language query or taxon name')
    _add_pagination_args(count_parser, default_page=1, default_per_page=1)
    count_parser.set_defaults(func=count_specimens)

    browse_parser = subparsers.add_parser('browse-ct', help='Browse CT media records that match a query')
    browse_parser.add_argument('query', help='Natural language query or taxon name')
    browse_parser.add_argument('--open', dest='open_browser', action='store_true', help='Open the generated search URL in a browser')
    browse_parser.add_argument('--open-only', action='store_true', help='Limit results to openly available media')
    browse_parser.add_argument('--csv', help='Optional path to export browse results as CSV')
    _add_pagination_args(browse_parser, default_page=1, default_per_page=12)
    browse_parser.set_defaults(func=browse_ct)

    nl_parser = subparsers.add_parser('nl-query', help='Run a natural language query and print the raw JSON response')
    nl_parser.add_argument('query', help='Natural language query text')
    _add_pagination_args(nl_parser, default_page=1, default_per_page=10)
    nl_parser.set_defaults(func=nl_query)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)  # type: ignore[misc]


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())

