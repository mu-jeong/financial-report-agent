from apps.gui import export_views


class _Expander:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeStreamlit:
    def __init__(self):
        self.expanders = []
        self.captions = []
        self.codes = []
        self.downloads = []

    def expander(self, label, **kwargs):
        self.expanders.append((label, kwargs))
        return _Expander()

    def caption(self, text):
        self.captions.append(text)

    def code(self, text, **kwargs):
        self.codes.append((text, kwargs))

    def html(self, _text, **_kwargs):
        pass

    def container(self, **_kwargs):
        return _Expander()

    def download_button(self, label, **kwargs):
        self.downloads.append((label, kwargs))
        return False


def test_markdown_export_uses_native_copy_and_download_without_rerun_callback():
    st = _FakeStreamlit()
    markdown = "# 한글 리포트\n\n본문 [1]\n"

    export_views.render_markdown_export(
        st,
        markdown,
        file_name="한글-report.md",
        download_key="download-key",
    )

    assert st.expanders == [("답변 복사·저장", {"expanded": False})]
    assert st.codes == [(markdown, {
        "language": "markdown",
        "height": 320,
        "wrap_lines": True,
    })]
    assert st.downloads == [("Markdown 저장", {
        "data": markdown.encode("utf-8"),
        "file_name": "한글-report.md",
        "mime": "text/markdown; charset=utf-8",
        "key": "download-key",
        "on_click": "ignore",
    })]
