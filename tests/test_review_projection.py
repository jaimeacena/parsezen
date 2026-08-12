from __future__ import annotations

from parsezen.review_projection import private_review_content, project_review_text


def test_review_projection_hides_complete_private_images_and_restores_exact_source() -> None:
    source = (
        "Antes.\n\n"
        "<!-- PZDOC PDF PAGE 3 -->\n\n"
        "![](<__parsezen_resources__/pdf/page-0003-image-01.jpg>)\n\n"
        "Después.\n"
        "<!-- Comentario interno: conservar la imagen -->\n"
        "PZDOC: 123...\n"
        "Comentario interno: conservar este ancla\n"
    )

    projection = project_review_text(source)

    assert "![]" not in projection.visible_text
    assert "()" not in projection.visible_text
    assert "PZDOC" not in projection.visible_text
    assert "__parsezen_resources__" not in projection.visible_text
    assert "Comentario interno" not in projection.visible_text
    assert "(" not in projection.visible_text
    assert ")" not in projection.visible_text
    private = private_review_content(source)
    assert "PZDOC: 123..." in private
    assert "Comentario interno: conservar este ancla" in private
    assert projection.restore(projection.visible_text) == source


def test_review_projection_restores_private_fragments_after_visible_edit() -> None:
    source = (
        "Texto original.\n\n"
        "![Figura](__parsezen_resources__/images/figure.png)\n\n"
        "<!-- PZDOC EPUB ANCHOR section-one -->\n"
    )
    projection = project_review_text(source)

    restored = projection.restore(projection.visible_text.replace("original", "corregido"))

    assert restored == (
        "Texto corregido.\n\n"
        "![Figura](__parsezen_resources__/images/figure.png)\n\n"
        "<!-- PZDOC EPUB ANCHOR section-one -->\n"
    )


def test_review_projection_restores_large_repetitive_edits_without_full_document_diff() -> None:
    private = "<!-- PZDOC EPUB ANCHOR repeated-section -->"
    source = f"{'a' * 10_000}{private}{'b' * 10_000}"
    projection = project_review_text(source)

    edited = f"x{projection.visible_text[:10_000]}B{projection.visible_text[10_001:]}"

    assert projection.restore(edited) == f"x{'a' * 10_000}{private}B{'b' * 9_999}"
