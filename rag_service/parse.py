"""Four supported file formats to ordered, source-located blocks."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Block:
    kind: str
    text: str
    locator: dict
    heading_path: tuple[str, ...] = ()
    table_html: str | None = None


class ParseFailure(ValueError):
    pass


def parse_file(path: Path, media_type: str) -> tuple[list[Block], list[str]]:
    if media_type == "text/plain":
        return parse_txt(path.read_bytes()), []
    if media_type == "text/markdown":
        return parse_markdown(path.read_text(encoding="utf-8-sig")), []
    if media_type in ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return parse_docling(path, "pdf" if media_type == "application/pdf" else "docx")
    raise ParseFailure("UNSUPPORTED_FORMAT")


def parse_txt(raw: bytes) -> list[Block]:
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ParseFailure("INVALID_ENCODING") from exc
    blocks = [Block("paragraph", line.strip(), {"kind": "txt", "line_start": n, "line_end": n}) for n, line in enumerate(lines, 1) if line.strip()]
    if not blocks:
        raise ParseFailure("EMPTY_CONTENT")
    return blocks


def parse_markdown(text: str) -> list[Block]:
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = parser.parse(text)
    blocks: list[Block] = []
    headings: list[str] = []
    in_table = False
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            level = int(token.tag[1])
            title = tokens[index + 1].content.strip()
            headings = headings[: level - 1] + [title]
            continue
        if token.type == "table_open":
            in_table = True
        elif token.type == "table_close":
            in_table = False
        if token.type not in {"paragraph_open", "fence", "code_block", "table_open"} or (in_table and token.type == "paragraph_open"):
            continue
        if token.type == "paragraph_open":
            content = tokens[index + 1].content.strip()
            kind = "paragraph"
        elif token.type == "table_open":
            close = next((j for j in range(index + 1, len(tokens)) if tokens[j].type == "table_close" and tokens[j].level == token.level), index)
            content = "\n".join(t.content for t in tokens[index:close] if t.type == "inline" and t.content.strip())
            kind = "table"
        else:
            content = token.content.strip()
            kind = "code"
        if not content or not token.map:
            continue
        locator = {"kind": "markdown", "line_start": token.map[0] + 1, "line_end": token.map[1]}
        blocks.append(Block(kind, content, locator, tuple(headings), table_html=None))
    if not blocks:
        raise ParseFailure("EMPTY_CONTENT")
    return blocks


def parse_docling(path: Path, kind: str) -> tuple[list[Block], list[str]]:
    from docling.document_converter import DocumentConverter

    try:
        result = DocumentConverter().convert(path)
        document = result.document
    except Exception as exc:
        raise ParseFailure("PARSE_FAILED") from exc
    blocks: list[Block] = []
    warnings: list[str] = []
    headings: list[str] = []
    block_no = 0
    for item, level in document.iterate_items():
        label = str(getattr(item, "label", "")).lower()
        if "section_header" in label or "title" in label:
            title = str(getattr(item, "text", "")).strip()
            if title:
                headings = headings[:max(0, int(level) - 1)] + [title]
            continue
        table = "table" in label
        content = str(getattr(item, "text", "")).strip()
        table_html = None
        if table:
            try:
                table_html = item.export_to_html(doc=document)
            except Exception:
                warnings.append("TABLE_STRUCTURE_UNAVAILABLE")
            if not content and table_html:
                content = table_html
        if not content:
            continue
        block_no += 1
        if kind == "pdf":
            prov = getattr(item, "prov", None) or []
            pages = [int(p.page_no) for p in prov if getattr(p, "page_no", None)]
            if not pages:
                raise ParseFailure("PAGE_LOCATION_UNAVAILABLE")
            locator = {"kind": "pdf", "page_start": min(pages) if pages else None, "page_end": max(pages) if pages else None}
        else:
            locator = {"kind": "docx", "heading_path": headings.copy(), "block_start": block_no, "block_end": block_no}
        blocks.append(Block("table" if table else "paragraph", content, locator, tuple(headings), table_html))
    if not blocks:
        raise ParseFailure("EMPTY_CONTENT")
    return blocks, sorted(set(warnings))
