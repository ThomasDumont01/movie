"""Tests des règles d'édition directe des fichiers Matroska."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from xml.etree import ElementTree

from movie.core.models import MovieMetadata
from movie.matroska import _Attachment, _update_global_tags


class MatroskaTagTests(TestCase):
    def test_managed_tags_are_replaced_without_losing_unknown_tags(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            tags = Path(temporary_directory) / "tags.xml"
            tags.write_text(
                """<?xml version="1.0"?>
<Tags><Tag><Targets />
<Simple><Name>TITLE</Name><String>Ancien titre</String></Simple>
<Simple><Name>DATE_RELEASED</Name><String>1999</String></Simple>
<Simple><Name>RATING</Name><String>8.5</String></Simple>
</Tag></Tags>
""",
                encoding="utf-8",
            )

            _update_global_tags(
                tags,
                MovieMetadata(
                    title="Nouveau titre",
                    summary="Résumé",
                    genres=("Archive",),
                ),
            )

            root = ElementTree.parse(tags).getroot()
            values = {
                simple.findtext("Name"): simple.findtext("String")
                for simple in root.findall("./Tag/Simple")
            }
            self.assertEqual(values["TITLE"], "Nouveau titre")
            self.assertEqual(values["RATING"], "8.5")
            self.assertNotIn("DATE_RELEASED", values)
            self.assertEqual(values["DESCRIPTION"], "Résumé")

    def test_fanart_detection_accepts_filename_and_french_description(self) -> None:
        by_name = _Attachment(1, "fanart.jpg", "image/jpeg", "")
        by_description = _Attachment(2, "image.jpg", "image/jpeg", "Arrière-plan")
        unrelated = _Attachment(3, "document.pdf", "application/pdf", "Fanart")

        self.assertTrue(by_name.is_fanart)
        self.assertTrue(by_description.is_fanart)
        self.assertFalse(unrelated.is_image)
