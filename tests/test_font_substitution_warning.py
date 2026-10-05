"""A substituted font must be visible in the parse result, not only in a deduplicated log line.

ezdxf resolves once per distinct font face, and the worker's ``_OncePerMessage`` filter discards the
repeated "no default font found" records, so nothing downstream could tell that a drawing's raster was
produced with a monospace stub instead of the requested face.

The end-to-end substitution was observed on the Windows host that logged 79,611 of them; the Linux
runner resolves the same face another way, so these tests pin the mechanism (the hook is reached on
the live render path, only the last-resort font counts, the warning text) instead of relying on the
platform's font resolution.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

ezdxf = pytest.importorskip("ezdxf")

from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg  # noqa: E402
from ezdxf.fonts import fonts as ezfonts  # noqa: E402

from aec_intelligence.operational import parsers  # noqa: E402


class MonospaceFont:  # the shape of the library's last-resort stub
    pass


class TrueTypeFont:
    pass


def _doc_with_text():
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


def test_the_hook_is_reached_by_the_real_renderer():
    """The wrapper must be on the live path, not merely installed on the module."""
    seen = []
    with parsers._watch_font_substitutions():
        watch_wrapper = ezfonts.make_font  # parsers' wrapper; it delegates to the library's resolver

        def counting(face, cap_height, *args, **kwargs):
            seen.append(face)
            return watch_wrapper(face, cap_height, *args, **kwargs)

        ezfonts.make_font = counting
        try:
            _render(_doc_with_text())
        finally:
            ezfonts.make_font = watch_wrapper
    assert seen, "rendering text must resolve at least one font through the wrapped resolver"


def test_only_the_last_resort_font_counts_as_a_substitution():
    watch = parsers._FontSubstitutionWatch()
    watch._resolver(lambda face, cap_height: MonospaceFont())(
        SimpleNamespace(family="NanumSquare", filename="NanumSquareR.ttf"), 2.5)
    assert watch.missing == {"NanumSquare"}

    watch._resolver(lambda face, cap_height: TrueTypeFont())(
        SimpleNamespace(family="Malgun Gothic", filename="malgun.ttf"), 2.5)
    assert watch.missing == {"NanumSquare"}, "a font that resolved must not be reported"


def test_a_face_without_a_family_is_reported_by_its_filename():
    watch = parsers._FontSubstitutionWatch()
    watch._resolver(lambda face, cap_height: MonospaceFont())(
        SimpleNamespace(family=None, filename="SomeFont.ttf"), 2.5)
    assert watch.missing == {"SomeFont.ttf"}


def test_the_warning_names_the_fonts_and_is_silent_when_nothing_was_substituted():
    assert parsers.font_substitution_warning("A-101", set()) is None
    text = parsers.font_substitution_warning("A-101", {"NanumSquare"})
    assert text and "A-101" in text and "NanumSquare" in text and "substitute font" in text
    assert parsers.font_substitution_warning("A-101", {"a", "b", "c", "d"}).count("(+1 more)") == 1
