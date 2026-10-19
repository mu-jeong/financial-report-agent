"""Small shared Streamlit controls for portable Markdown exports."""

from __future__ import annotations

from typing import Any


_COPY_BUTTON_STYLE = """
<style>
[class*="st-key-markdown_export_"] [data-testid="stCode"] div:has(> [data-testid="stCodeCopyButton"]) {
    opacity: 1;
    transition: none;
}
[class*="st-key-markdown_export_"] [data-testid="stCodeCopyButton"] {
    transform: none;
    opacity: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.35rem;
    width: auto;
    min-height: 2.5rem;
    padding: 0.35rem 0.65rem;
    border: 1px solid currentColor;
    border-radius: 0.4rem;
    font: inherit;
    font-size: 0.85rem;
}
[class*="st-key-markdown_export_"] [data-testid="stCodeCopyButton"]::after {
    content: "복사";
}
[class*="st-key-markdown_export_"] [data-testid="stCodeCopyButton"][aria-label="Copied"]::after {
    content: "복사됨";
}
[class*="st-key-markdown_export_"] [data-testid="stCode"] pre {
    padding-top: 3.5rem;
}
</style>
"""


def render_markdown_export(
    st: Any,
    markdown: str,
    *,
    file_name: str,
    download_key: str,
    label: str = "답변 복사·저장",
) -> None:
    with st.expander(label, expanded=False):
        st.caption("본문 영역의 ‘복사’를 누르면 본문과 출처가 함께 복사됩니다. Markdown 파일로 저장할 수도 있습니다.")
        # Streamlit hides its native copy control until hover; expose it only here.
        st.html(_COPY_BUTTON_STYLE)
        with st.container(key=f"markdown_export_{download_key}"):
            st.code(markdown, language="markdown", height=320, wrap_lines=True)
        options = {
            "data": markdown.encode("utf-8"),
            "file_name": file_name,
            "mime": "text/markdown; charset=utf-8",
            "key": download_key,
            "on_click": "ignore",
        }
        st.download_button("Markdown 저장", **options)
