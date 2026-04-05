from __future__ import annotations

from malt_crm.bootstrap.html import render_settings_html


def test_settings_html_escapes_error_message() -> None:
    page = render_settings_html(
        error_message='<em>oops</em>',
        remember_placeholder="x",
        openai_placeholder="y",
    )
    assert "<em>oops</em>" not in page
    assert "&lt;em&gt;oops&lt;/em&gt;" in page
