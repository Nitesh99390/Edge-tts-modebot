from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup


@dataclass
class Chapter:
    number: int
    title: str
    text: str


def _local_name(tag: str) -> str:
    """Return XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _clean_text(text: str) -> str:
    """Clean extracted book text without destroying paragraph structure."""
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Remove trailing/leading spaces from every line.
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)

    # Collapse excessive blank lines.
    cleaned = []
    previous_blank = False

    for line in lines:
        if not line:
            if not previous_blank:
                cleaned.append("")
            previous_blank = True
        else:
            cleaned.append(line)
            previous_blank = False

    return "\n".join(cleaned).strip()


def _read_xml(zf: zipfile.ZipFile, name: str):
    data = zf.read(name)
    return ET.fromstring(data)


def _find_opf(zf: zipfile.ZipFile) -> str:
    """
    Find the EPUB OPF file using META-INF/container.xml.
    """
    if "META-INF/container.xml" not in zf.namelist():
        raise ValueError("Invalid EPUB: META-INF/container.xml is missing.")

    container = _read_xml(zf, "META-INF/container.xml")

    for element in container.iter():
        if _local_name(element.tag) == "rootfile":
            full_path = element.attrib.get("full-path")
            if full_path:
                return posixpath.normpath(full_path)

    raise ValueError("Invalid EPUB: could not locate the OPF file.")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str):
    """
    Read the EPUB manifest and spine.

    Returns:
        manifest: {item_id: {href, media_type}}
        spine: [item_id, ...]
    """
    opf = _read_xml(zf, opf_path)

    manifest = {}
    spine = []

    for element in opf.iter():
        name = _local_name(element.tag)

        if name == "item":
            item_id = element.attrib.get("id")
            href = element.attrib.get("href")
            media_type = element.attrib.get("media-type", "")

            if item_id and href:
                manifest[item_id] = {
                    "href": href,
                    "media_type": media_type,
                }

        elif name == "itemref":
            idref = element.attrib.get("idref")
            if idref:
                spine.append(idref)

    return manifest, spine


def _extract_title(soup: BeautifulSoup, fallback: str) -> str:
    """
    Get a useful chapter title.
    """
    # Prefer visible headings.
    for tag_name in ("h1", "h2", "h3", "h4"):
        heading = soup.find(tag_name)
        if heading:
            title = heading.get_text(" ", strip=True)
            if title:
                return title

    # Then document title.
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        if title:
            return title

    return fallback


def _extract_html_text(raw_html: bytes, fallback_title: str):
    """
    Extract readable text from one XHTML/HTML chapter.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # Remove things that should never become audiobook text.
    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "header",
            "footer",
            "form",
        ]
    ):
        tag.decompose()

    title = _extract_title(soup, fallback_title)

    # EPUB chapter body.
    body = soup.body if soup.body else soup

    # Preserve paragraph-like boundaries.
    for tag in body.find_all(
        [
            "p",
            "div",
            "section",
            "article",
            "blockquote",
            "li",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "dl",
            "dt",
            "dd",
            "table",
            "tr",
            "td",
            "th",
            "caption",
            "figure",
            "figcaption",
            "pre",
            "hr",
        ]
    ):
        if tag.name == "br":
            tag.replace_with("\n")
        else:
            tag.insert_before("\n")
            tag.insert_after("\n")

    # Block tags already have newlines inserted around them, so join the
    # strings with no separator. A "\n" separator would split sentences
    # wherever an inline tag such as <em> or <span> appears.
    text = body.get_text()
    text = _clean_text(text)

    return title, text


def _read_text_file(path: Path) -> str:
    """
    Decode a TXT file. Handles UTF-8 (with or without BOM), UTF-16 files
    (common when saved from Windows tools) and Windows-1252.
    """
    data = path.read_bytes()

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")

    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace")


def _resolve_href(href, opf_dir, names, lower_names):
    """
    Map a manifest href to a file name inside the EPUB zip.

    Hrefs are URL-encoded (for example caf%C3%A9.xhtml) and may carry a
    #fragment. Returns None when no matching file exists.
    """
    href = href.split("#", 1)[0]

    for candidate in (href, unquote(href)):
        path = posixpath.normpath(posixpath.join(opf_dir, candidate))

        if path in names:
            return path

        # Some books use a different letter case in their hrefs.
        if path.lower() in lower_names:
            return lower_names[path.lower()]

    return None


def extract_epub(path, progress_callback=None):
    """
    Extract chapters from an EPUB.

    progress_callback can be a callable receiving:
        current, total, title

    Example:
        extract_epub(path, progress_callback=my_callback)
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError("EPUB file does not exist.")

    if not zipfile.is_zipfile(path):
        raise ValueError("The supplied file is not a valid EPUB.")

    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        lower_names = {name.lower(): name for name in names}

        # ------------------------------------------------------------
        # 1. Locate OPF
        # ------------------------------------------------------------
        opf_path = _find_opf(zf)
        opf_dir = posixpath.dirname(opf_path)

        # ------------------------------------------------------------
        # 2. Read manifest + spine
        # ------------------------------------------------------------
        manifest, spine = _parse_opf(zf, opf_path)

        if not spine:
            raise ValueError("The EPUB contains no readable spine.")

        chapters = []

        total = len(spine)

        # ------------------------------------------------------------
        # 3. Read only the files in spine order
        # ------------------------------------------------------------
        for index, idref in enumerate(spine, start=1):
            item = manifest.get(idref)

            if not item:
                continue

            media_type = item["media_type"].lower()

            # Only process XHTML/HTML documents.
            if (
                "html" not in media_type
                and "xhtml" not in media_type
            ):
                continue

            document_path = _resolve_href(
                item["href"],
                opf_dir,
                names,
                lower_names,
            )

            if document_path is None:
                continue

            fallback_title = f"Chapter {len(chapters) + 1}"

            # Tell caller which chapter we're processing.
            if progress_callback:
                try:
                    progress_callback(
                        index,
                        total,
                        fallback_title,
                    )
                except Exception:
                    pass

            try:
                raw_html = zf.read(document_path)

                title, text = _extract_html_text(
                    raw_html,
                    fallback_title,
                )

            except Exception as exc:
                # Don't kill the whole book because one malformed
                # XHTML file cannot be parsed.
                print(
                    f"[EPUB] Failed to read {document_path}: {exc}"
                )
                continue

            # Ignore empty/non-content files.
            if len(text.strip()) < 20:
                continue

            chapters.append(
                Chapter(
                    number=len(chapters) + 1,
                    title=title,
                    text=text,
                )
            )

        if not chapters:
            raise ValueError(
                "No readable chapters were found in this EPUB."
            )

        return chapters


def extract_txt(path):
    """
    Extract a TXT file.

    If the file contains headings such as:
        Chapter 1
        Chapter 2
        Chapter 3

    it will be split into chapters.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError("TXT file does not exist.")

    raw = _read_text_file(path)

    raw = _clean_text(raw)

    if not raw:
        raise ValueError("The TXT file is empty.")

    pattern = re.compile(
        r"(?im)^(chapter\s+\d+[^\n]*|chap\.\s*\d+[^\n]*)$"
    )

    matches = list(pattern.finditer(raw))

    if len(matches) >= 2:
        chapters = []

        # Text before the first heading (prologue, foreword, title page) must not be lost.
        preamble = _clean_text(raw[: matches[0].start()])

        if preamble:
            chapters.append(
                Chapter(
                    number=1,
                    title="Introduction",
                    text=preamble,
                )
            )

        for i, match in enumerate(matches):
            start = match.start()

            if i + 1 < len(matches):
                end = matches[i + 1].start()
            else:
                end = len(raw)

            block = raw[start:end].strip()
            lines = block.splitlines()

            title = (
                lines[0].strip()
                if lines
                else f"Chapter {i + 1}"
            )

            text = "\n".join(lines[1:]).strip()

            if text:
                chapters.append(
                    Chapter(
                        number=len(chapters) + 1,
                        title=title,
                        text=_clean_text(text),
                    )
                )

        if chapters:
            return chapters

    return [
        Chapter(
            number=1,
            title=path.stem,
            text=raw,
        )
    ]


def extract_book(path, progress_callback=None):
    """
    Automatically select EPUB or TXT parser.
    """
    path = Path(path)

    suffix = path.suffix.lower()

    if suffix == ".epub":
        return extract_epub(
            path,
            progress_callback=progress_callback,
        )

    if suffix == ".txt":
        return extract_txt(path)

    raise ValueError(
        "Supported files are EPUB and TXT."
    )
