"""A substituted font must be visible in the parse result, not only in a deduplicated log line.

ezdxf resolves once per distinct font face, and the worker's ``_OncePerMessage`` filter discards the
repeated "no default font found" records, so nothing downstream could tell that a drawing's raster was
produced with a monospace stub instead of the requested face.
"""

from __future__ import annotations

import pytest

ezdxf = pytest.importorskip("ezdxf")

from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg  # noqa: E402
from ezdxf.fonts import fonts as ezfonts  # noqa: E402

from aec_intelligence.operational import parsers  # noqa: E402


def _doc_with_unresolvable_font():
    # The production case: a style naming a font file that is not installed (NanumSquareR.ttf in the
    # worker log). ezdxf then resolves it to its last-resort monospace stub.
    doc = ezdxf.new("R2010")
    doc.styles.add("MISSING", font="NanumSquareR.ttf")
    modelspace = doc.modelspace()
    modelspace.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100), (0, 0)])  # a real bounding box
    modelspace.add_text("문", height=8, dxfattribs={"style": "MISSING"}).set_placement((10, 10))
    return doc


def _render(doc):
    for sheet in doc.layouts:
        try:
            backend = svg.SVGBackend()
            Frontend(RenderContext(doc), backend).draw_layout(sheet, finalize=True)
            backend.get_string(layout.Page(0, 0))
        except ValueError as exc:  # an empty paperspace layout is not what this test is about
            assert "bounding box" in str(exc)


def test_the_resolver_is_wrapped_only_inside_the_block():
    original = ezfonts.make_font
    with parsers._watch_font_substitutions():
        assert ezfonts.make_font is not original
    assert ezfonts.make_font is original


def test_a_font_that_cannot_be_resolved_is_collected_from_a_real_render():
    with parsers._watch_font_substitutions() as watch:
        _render(_doc_with_unresolvable_font())
    assert watch.missing, "the substituted face must be reported, not lost in the log"


def test_the_warning_names_the_fonts_and_is_silent_when_nothing_was_substituted():
    assert parsers.font_substitution_warning("A-101", set()) is None
    text = parsers.font_substitution_warning("A-101", {"NanumSquare"})
    assert text and "A-101" in text and "NanumSquare" in text and "substitute font" in text
    assert parsers.font_substitution_warning("A-101", {"a", "b", "c", "d"}).count("(+1 more)") == 1
