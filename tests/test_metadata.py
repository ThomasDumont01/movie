"""Tests de l'identification d'un film depuis une fiche web."""

from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

from movie.core.models import MovieError
from movie.metadata import (
    MetadataClient,
    _validated_movie_url,
    movie_search_url,
    parse_movie_page,
)

MOVIE_PAGE = """
<html><head>
<meta property="og:title" content="Titre de secours — IMDb">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Movie",
  "name": "Mon Film",
  "datePublished": "2019-12-18",
  "description": "Une aventure dans les étoiles.",
  "genre": ["Aventure", "Science-fiction"],
  "image": "https://image.example/affiche.jpg"
}
</script>
</head></html>
"""


class MetadataParserTests(TestCase):
    def test_extracts_structured_movie_metadata(self) -> None:
        metadata = parse_movie_page(MOVIE_PAGE, "https://www.imdb.com/title/tt0000001/")

        self.assertEqual(metadata.title, "Mon Film")
        self.assertEqual(metadata.year, 2019)
        self.assertEqual(metadata.release_date, "2019-12-18")
        self.assertEqual(metadata.genres, ("Aventure", "Science-fiction"))
        self.assertEqual(metadata.poster_url, "https://image.example/affiche.jpg")

    def test_open_graph_is_used_as_fallback(self) -> None:
        metadata = parse_movie_page(
            '<meta property="og:title" content="Film Test (2020) — IMDb">',
            "https://www.imdb.com/title/tt0000002/",
        )

        self.assertEqual(metadata.title, "Film Test")
        self.assertEqual(metadata.year, 2020)

    def test_page_without_title_is_rejected(self) -> None:
        with self.assertRaisesRegex(MovieError, "Impossible d'identifier"):
            parse_movie_page("<html></html>", "https://www.imdb.com/title/x/")

    def test_search_url_normalizes_disc_label(self) -> None:
        self.assertEqual(
            movie_search_url("THE_RISE  OF_SKYWALKER"),
            "https://www.themoviedb.org/search/movie?query=THE+RISE+OF+SKYWALKER",
        )

    def test_tmdb_cdata_and_release_event_are_supported(self) -> None:
        page = """
        <script type="application/ld+json">/* <![CDATA[ */
        {"@type":"Movie","name":"Film TMDB","genre":["Drame"],
         "releasedEvent":[{"startDate":"2021-05-03"}]}
        /* ]]> */</script>
        """

        metadata = parse_movie_page(
            page,
            "https://www.themoviedb.org/movie/1-film",
        )

        self.assertEqual(metadata.year, 2021)
        self.assertEqual(metadata.release_date, "2021-05-03")
        self.assertEqual(metadata.genres, ("Drame",))

    def test_only_direct_https_tmdb_movie_links_are_accepted(self) -> None:
        valid = "https://www.themoviedb.org/movie/181812-star-wars"
        self.assertEqual(_validated_movie_url(valid), valid)

        invalid_links = (
            "http://www.themoviedb.org/movie/181812-star-wars",
            "https://example.com/movie/181812-star-wars",
            "https://www.themoviedb.org/search/movie?query=star+wars",
            "https://www.themoviedb.org/tv/181812-star-wars",
        )
        for link in invalid_links:
            with self.subTest(link=link), self.assertRaises(MovieError):
                _validated_movie_url(link)

    def test_nested_graph_and_image_object_are_supported(self) -> None:
        page = """
        <script type="application/ld+json">
        {"@graph":[{"@type":"Movie","name":"Film imbriqué",
        "genre":"Drame","image":{"contentUrl":"https://image.tmdb.org/x.webp"}}]}
        </script>
        """
        metadata = parse_movie_page(
            page,
            "https://www.themoviedb.org/movie/2-film",
        )

        self.assertEqual(metadata.title, "Film imbriqué")
        self.assertEqual(metadata.genres, ("Drame",))
        self.assertEqual(metadata.poster_url, "https://image.tmdb.org/x.webp")

    def test_insecure_poster_is_discarded(self) -> None:
        page = '<meta property="og:title" content="Film"><meta property="og:image" content="http://image/cover.jpg">'
        metadata = parse_movie_page(
            page,
            "https://www.themoviedb.org/movie/3-film",
        )
        self.assertIsNone(metadata.poster_url)

    def test_tmdb_second_open_graph_image_is_the_original_fanart(self) -> None:
        page = """
        <meta property="og:title" content="Film">
        <meta property="og:image"
              content="https://media.themoviedb.org/t/p/w500/poster.jpg">
        <meta property="og:image"
              content="https://media.themoviedb.org/t/p/w780/backdrop.jpg">
        """

        metadata = parse_movie_page(
            page,
            "https://www.themoviedb.org/movie/3-film",
        )

        self.assertEqual(
            metadata.poster_url,
            "https://media.themoviedb.org/t/p/w500/poster.jpg",
        )
        self.assertEqual(
            metadata.fanart_url,
            "https://media.themoviedb.org/t/p/original/backdrop.jpg",
        )

    def test_non_tmdb_secondary_image_is_not_accepted_as_fanart(self) -> None:
        page = """
        <meta property="og:title" content="Film">
        <meta property="og:image" content="https://image.example/poster.jpg">
        <meta property="og:image" content="https://image.example/backdrop.jpg">
        """

        metadata = parse_movie_page(
            page,
            "https://www.themoviedb.org/movie/3-film",
        )

        self.assertIsNone(metadata.fanart_url)


class MetadataClientTests(TestCase):
    @patch("movie.metadata.urlopen")
    def test_fetch_reads_a_valid_tmdb_page(self, urlopen: MagicMock) -> None:
        urlopen.return_value.__enter__.return_value = _response(
            final_url="https://www.themoviedb.org/movie/1-film",
            content_type="text/html",
            content=MOVIE_PAGE.encode(),
        )

        metadata = MetadataClient(timeout_seconds=2).fetch(
            "https://www.themoviedb.org/movie/1-film"
        )

        self.assertEqual(metadata.title, "Mon Film")
        self.assertEqual(metadata.source_url, "https://www.themoviedb.org/movie/1-film")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)

    @patch("movie.metadata.urlopen")
    def test_non_html_response_is_rejected(self, urlopen: MagicMock) -> None:
        urlopen.return_value.__enter__.return_value = _response(
            final_url="https://www.themoviedb.org/movie/1-film",
            content_type="application/json",
            content=b"{}",
        )
        with self.assertRaisesRegex(MovieError, "page HTML"):
            MetadataClient().fetch("https://www.themoviedb.org/movie/1-film")

    @patch("movie.metadata._MAX_PAGE_BYTES", 3)
    @patch("movie.metadata.urlopen")
    def test_oversized_page_is_rejected(self, urlopen: MagicMock) -> None:
        urlopen.return_value.__enter__.return_value = _response(
            final_url="https://www.themoviedb.org/movie/1-film",
            content_type="text/html",
            content=b"1234",
        )
        with self.assertRaisesRegex(MovieError, "trop volumineuse"):
            MetadataClient().fetch("https://www.themoviedb.org/movie/1-film")

    @patch("movie.metadata.urlopen", side_effect=OSError("hors ligne"))
    def test_network_failure_is_reported(self, _urlopen: object) -> None:
        with self.assertRaisesRegex(MovieError, "hors ligne"):
            MetadataClient().fetch("https://www.themoviedb.org/movie/1-film")

    @patch("movie.metadata.urlopen")
    def test_redirect_outside_tmdb_is_rejected(self, urlopen: MagicMock) -> None:
        urlopen.return_value.__enter__.return_value = _response(
            final_url="https://example.com/movie/1-film",
            content_type="text/html",
            content=MOVIE_PAGE.encode(),
        )
        with self.assertRaisesRegex(MovieError, "fiche de film TMDB"):
            MetadataClient().fetch("https://www.themoviedb.org/movie/1-film")


def _response(*, final_url: str, content_type: str, content: bytes) -> MagicMock:
    response = MagicMock()
    response.geturl.return_value = final_url
    response.headers.get_content_type.return_value = content_type
    response.headers.get_content_charset.return_value = "utf-8"
    response.read.return_value = content
    return response
