"""Identification d'un film depuis une fiche web choisie par l'utilisateur."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urlsplit
from urllib.request import Request, urlopen

from movie import __version__
from movie.core.models import MovieError, MovieMetadata

_ALLOWED_HOSTS = {
    "themoviedb.org",
    "www.themoviedb.org",
}
_MAX_PAGE_BYTES = 3_000_000
_USER_AGENT = f"Movie/{__version__} (+local personal media organizer)"


def movie_search_url(query: str) -> str:
    """Construit une recherche TMDB utilisable sans compte ni clé API."""

    normalized = " ".join(query.replace("_", " ").split())
    return f"https://www.themoviedb.org/search/movie?query={quote_plus(normalized)}"


class MetadataClient:
    """Lit les métadonnées publiques d'une fiche TMDB."""

    def __init__(self, *, timeout_seconds: float = 15) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch(self, url: str) -> MovieMetadata:
        normalized_url = _validated_movie_url(url)
        request = Request(
            normalized_url,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                final_url = _validated_movie_url(response.geturl())
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise MovieError("La fiche choisie n'est pas une page HTML.")
                raw = response.read(_MAX_PAGE_BYTES + 1)
                if len(raw) > _MAX_PAGE_BYTES:
                    raise MovieError("La fiche du film est trop volumineuse.")
                charset = response.headers.get_content_charset() or "utf-8"
        except MovieError:
            raise
        except OSError as error:
            raise MovieError(f"Impossible de lire la fiche du film : {error}") from error

        return parse_movie_page(raw.decode(charset, errors="replace"), final_url)


def parse_movie_page(html: str, source_url: str) -> MovieMetadata:
    """Extrait un objet Movie JSON-LD, avec Open Graph comme solution de repli."""

    parser = _MetadataHtmlParser()
    parser.feed(html)
    structured = _find_movie_object(parser.structured_objects)

    raw_title = _text_value(structured.get("name")) if structured else None
    raw_title = raw_title or parser.meta.get("og:title")
    title = _clean_title(raw_title) if raw_title else None
    if not title:
        raise MovieError(
            "Impossible d'identifier le titre sur cette page. "
            "Colle le lien direct d'une fiche de film TMDB."
        )

    date = _publication_date(structured) if structured else None
    year = _extract_year(date or raw_title or title)
    summary = (
        _text_value(structured.get("description")) if structured else None
    ) or parser.meta.get("og:description")
    genres = _genre_values(structured.get("genre")) if structured else ()
    image = _image_value(structured.get("image")) if structured else None
    poster_url = image or parser.meta.get("og:image")
    if poster_url and urlsplit(poster_url).scheme != "https":
        poster_url = None

    return MovieMetadata(
        title=title,
        year=year,
        summary=summary.strip() if summary else None,
        genres=genres,
        source_url=source_url,
        poster_url=poster_url,
    )


class _MetadataHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.structured_objects: list[Any] = []
        self._in_json_ld = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.casefold(): value for name, value in attrs if value}
        if tag.casefold() == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if key and content:
                self.meta[key.casefold()] = content
        elif (
            tag.casefold() == "script"
            and attributes.get("type", "").casefold() == "application/ld+json"
        ):
            self._in_json_ld = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._in_json_ld:
            return
        self._in_json_ld = False
        try:
            payload = "".join(self._script_parts).strip()
            payload = re.sub(r"^/\*\s*<!\[CDATA\[\s*\*/", "", payload).strip()
            payload = re.sub(r"/\*\s*\]\]>\s*\*/$", "", payload).strip()
            self.structured_objects.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
        self._script_parts = []


def _validated_movie_url(value: str) -> str:
    url = value.strip()
    parsed = urlsplit(url)
    direct_movie_path = re.fullmatch(r"/movie/\d+(?:-[^/?#]+)?/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or direct_movie_path is None
    ):
        raise MovieError("Utilise le lien HTTPS direct d'une fiche de film TMDB.")
    return url


def _find_movie_object(objects: list[Any]) -> dict[str, Any] | None:
    for value in objects:
        for candidate in _walk_json(value):
            kind = candidate.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(item).casefold() == "movie" for item in kinds):
                return candidate
    return None


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _text_value(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _genre_values(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(
        item.strip() for item in values if isinstance(item, str) and item.strip()
    )


def _publication_date(value: dict[str, Any]) -> str | None:
    direct = _text_value(value.get("datePublished"))
    if direct:
        return direct
    events = value.get("releasedEvent")
    candidates = events if isinstance(events, list) else [events]
    for event in candidates:
        if isinstance(event, dict):
            date = _text_value(event.get("startDate"))
            if date:
                return date
    return None


def _image_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            image = _image_value(item)
            if image:
                return image
        return None
    if isinstance(value, dict):
        return _text_value(value.get("url") or value.get("contentUrl"))
    return None


def _extract_year(value: str) -> int | None:
    match = re.search(r"\b(18|19|20)\d{2}\b", value)
    return int(match.group(0)) if match else None


def _clean_title(value: str) -> str:
    title = re.sub(r"\s*[|—-]\s*(IMDb|The Movie Database.*)$", "", value).strip()
    return re.sub(r"\s*\((?:18|19|20)\d{2}\)\s*$", "", title).strip()
