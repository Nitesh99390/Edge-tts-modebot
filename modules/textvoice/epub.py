from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, FeatureNotFound

logger = logging.getLogger(__name__)

# Zip bomb / resource limits for Oracle Cloud VMs
MAX_EPUB_UNCOMPRESSED_BYTES = 150 * 1024 * 1024  # 150 MB total text/assets
MAX_CHAPTER_RAW_BYTES = 15 * 1024 * 1024         # 15 MB per XHTML spine item
MAX_TXT_BYTES = 50 * 1024 * 1024                 # 50 MB plain text file

# Recognizes English, Hindi (अध्याय / भाग / खंड), Hinglish, Roman numerals, Prologues & Epilogues
_TXT_CHAPTER_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:"
    r"(?:chapter|chap\.?|ch\.?|part|book|volume|vol\.?|act|section|episode)\s+"
    r"(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|forty|fifty)[^\n]{0,120}"
    r"|"
    r"(?:अध्याय|भाग|खंड|पर्व|प्रकरण)\s*(?:\d+|[\u0966-\u096f]+)[^\n]{0,120}"
    r"|"
    r"(?:prologue|epilogue|foreword|preface|introduction|interlude|"
    r"प्रस्तावना|भूमिका|उपसंहार)(?:\s*[:\-–—]\s*[^\n]{0,100})?"
    r")[ \t]*$"
)


@dataclass
class Chapter:
    number: int
    title: str
    text: str


def _local_name(tag: str) -> str:
    """Return XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _make_soup(raw_bytes: bytes) -> BeautifulSoup:
    """Use fast C-based lxml parser when installed, falling back to html.parser."""
    try:
        return BeautifulSoup(raw_bytes, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(raw_bytes, "html.parser")


def _clean_text(text: str) -> str:
    """Clean extracted book text without destroying paragraph structure."""
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)

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


def _safe_zip_read(zf: zipfile.ZipFile, name: str, max_bytes: int = MAX_CHAPTER_RAW_BYTES) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > max_bytes:
        raise ValueError(
            f"Archive entry {name!r} is too large ({info.file_size} bytes)."
        )
    return zf.read(name)


def _read_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    data = _safe_zip_read(zf, name)
    # Strip UTF-8 BOM if present before XML parsing
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return ET.fromstring(data)


def _find_opf(zf: zipfile.ZipFile) -> str:
    """Find the EPUB OPF file using META-INF/container.xml (or fallback scan)."""
    names = set(zf.namelist())
    lower_map = {n.lower(): n for n in names}

    container_name = lower_map.get("meta-inf/container.xml")
    if container_name:
        container = _read_xml(zf, container_name)
        for element in container.iter():
            if _local_name(element.tag) == "rootfile":
                full_path = element.attrib.get("full-path")
                if full_path:
                    norm = posixpath.normpath(unquote(full_path)).lstrip("/")
                    if norm in names:
                        return norm
                    if norm.lower() in lower_map:
                        return lower_map[norm.lower()]

    # Fallback: locate any .opf file in the archive
    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name

    raise ValueError("Invalid EPUB: could not locate the OPF package file.")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str):
    """
    Read the EPUB manifest and spine.

    Returns:
        manifest: {item_id: {href, media_type, properties}}
        spine: [item_id, ...]
        toc_id: optional NCX item id
    """
    opf = _read_xml(zf, opf_path)

    manifest: dict[str, dict[str, str]] = {}
    spine: list[str] = []
    toc_id: str | None = None

    for element in opf.iter():
        name = _local_name(element.tag)

        if name == "spine":
            toc_id = element.attrib.get("toc")

        elif name == "item":
            item_id = element.attrib.get("id")
            href = element.attrib.get("href")
            media_type = element.attrib.get("media-type", "")
            properties = element.attrib.get("properties", "")

            if item_id and href:
                manifest[item_id] = {
                    "href": href,
                    "media_type": media_type,
                    "properties": properties,
                }

        elif name == "itemref":
            # Skip non-linear auxiliary items only if marked linear="no" and spine has main items
            idref = element.attrib.get("idref")
            linear = element.attrib.get("linear", "yes").strip().lower()
            if idref:
                spine.append((idref, linear != "no"))

    # Prefer linear items; if all were marked non-linear, keep all
    linear_spine = [idref for idref, is_linear in spine if is_linear]
    final_spine = linear_spine if linear_spine else [idref for idref, _ in spine]

    return manifest, final_spine, toc_id


def _resolve_href(href: str, base_dir: str, names: set[str], lower_names: dict[str, str]) -> str | None:
    """
    Map a manifest/TOC href to a file name inside the EPUB zip.
    Handles URL-encoding, #fragments, leading slashes, and relative '../' paths.
    """
    href = href.split("#", 1)[0].strip()
    if not href:
        return None

    for candidate in (href, unquote(href)):
        candidate_clean = candidate.replace("\\", "/")
        if candidate_clean.startswith("/"):
            joined = posixpath.normpath(candidate_clean.lstrip("/"))
        elif base_dir:
            joined = posixpath.normpath(posixpath.join(base_dir, candidate_clean))
        else:
            joined = posixpath.normpath(candidate_clean)

        joined = joined.lstrip("/")
        if joined.startswith(".."):
            continue

        if joined in names:
            return joined

        if joined.lower() in lower_names:
            return lower_names[joined.lower()]

    return None


def _parse_toc_titles(
    zf: zipfile.ZipFile,
    opf_dir: str,
    manifest: dict[str, dict[str, str]],
    toc_id: str | None,
    names: set[str],
    lower_names: dict[str, str],
) -> dict[str, str]:
    """
    Extract human-readable chapter titles from EPUB3 nav document or EPUB2 toc.ncx.
    Returns mapping {zip_internal_path: chapter_title}.
    """
    titles: dict[str, str] = {}

    # 1. EPUB 3 Navigation Document (properties contains "nav")
    for item in manifest.values():
        props = item.get("properties", "").split()
        if "nav" in props:
            nav_path = _resolve_href(item["href"], opf_dir, names, lower_names)
            if not nav_path:
                continue
            nav_dir = posixpath.dirname(nav_path)
            try:
                soup = _make_soup(_safe_zip_read(zf, nav_path))
                for link in soup.find_all("a", href=True):
                    label = link.get_text(" ", strip=True)
                    target = _resolve_href(link["href"], nav_dir, names, lower_names)
                    if target and label and target not in titles:
                        titles[target] = label
            except Exception as exc:
                logger.debug("Could not parse EPUB3 nav %s: %s", nav_path, exc)

    # 2. EPUB 2 NCX Table of Contents
    ncx_item = manifest.get(toc_id) if toc_id else None
    if not ncx_item:
        for item in manifest.values():
            if "dtbncx" in item.get("media_type", "").lower() or item["href"].lower().endswith(".ncx"):
                ncx_item = item
                break

    if ncx_item:
        ncx_path = _resolve_href(ncx_item["href"], opf_dir, names, lower_names)
        if ncx_path:
            ncx_dir = posixpath.dirname(ncx_path)
            try:
                ncx_root = _read_xml(zf, ncx_path)
                for nav_point in ncx_root.iter():
                    if _local_name(nav_point.tag) != "navPoint":
                        continue
                    label_text = ""
                    content_src = ""
                    for child in nav_point:
                        cname = _local_name(child.tag)
                        if cname == "navLabel":
                            for sub in child.iter():
                                if _local_name(sub.tag) == "text" and sub.text:
                                    label_text = sub.text.strip()
                                    break
                        elif cname == "content":
                            content_src = child.attrib.get("src", "")
                    if label_text and content_src:
                        target = _resolve_href(content_src, ncx_dir, names, lower_names)
                        if target and target not in titles:
                            titles[target] = label_text
            except Exception as exc:
                logger.debug("Could not parse EPUB2 NCX %s: %s", ncx_path, exc)

    return titles


def _extract_title(soup: BeautifulSoup, toc_title: str | None, fallback: str) -> str:
    """
    Get a useful chapter title.
    Priority: visible h1-h4 heading -> TOC title -> <title> (if meaningful) -> fallback.
    """
    for tag_name in ("h1", "h2", "h3", "h4"):
        heading = soup.find(tag_name)
        if heading:
            title = heading.get_text(" ", strip=True)
            if title and len(title) <= 160:
                return title

    if toc_title:
        return toc_title

    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        if title and title.lower() not in {"unknown", "untitled", "index", "document"}:
            return title

    return fallback


def _extract_html_text(raw_html: bytes, toc_title: str | None, fallback_title: str) -> tuple[str, str]:
    """
    Extract readable text from one XHTML/HTML chapter.
    Note: We intentionally DO NOT decompose <header> because EPUB3 novels wrap
    chapter titles inside <header><h1>Chapter 1</h1></header>!
    """
    soup = _make_soup(raw_html)

    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "math",
            "nav",
            "footer",
            "form",
            "button",
            "rt",
            "rp",
        ]
    ):
        tag.decompose()

    # Also skip EPUB3 navigation blocks hidden inside regular chapters
    for nav_aside in soup.find_all(attrs={"epub:type": re.compile(r"\b(toc|page-list|landmarks)\b")}):
        nav_aside.decompose()

    title = _extract_title(soup, toc_title, fallback_title)

    body = soup.body if soup.body else soup

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

    text = body.get_text()
    text = _clean_text(text)

    return title, text


def _read_text_file(path: Path) -> str:
    """
    Decode a TXT file safely. Handles UTF-8 (with or without BOM), UTF-16 LE/BE,
    UTF-32, Windows-1252, and Latin-1.
    """
    size = path.stat().st_size
    if size > MAX_TXT_BYTES:
        raise ValueError("TXT file is too large (over 50 MB).")

    data = path.read_bytes()

    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32", errors="replace")

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace")


def extract_epub(path: str | Path, progress_callback=None) -> list[Chapter]:
    """
    Extract chapters from an EPUB safely and accurately.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError("EPUB file does not exist.")

    if not zipfile.is_zipfile(path):
        raise ValueError("The supplied file is not a valid EPUB archive.")

    with zipfile.ZipFile(path, "r") as zf:
        total_uncompressed = sum(info.file_size for info in zf.infolist())
        if total_uncompressed > MAX_EPUB_UNCOMPRESSED_BYTES * 3:
            raise ValueError(
                "EPUB archive uncompressed size exceeds safety limits."
            )

        names = set(zf.namelist())
        lower_names = {name.lower(): name for name in names}

        opf_path = _find_opf(zf)
        opf_dir = posixpath.dirname(opf_path)

        manifest, spine, toc_id = _parse_opf(zf, opf_path)
        if not spine:
            raise ValueError("The EPUB contains no readable spine.")

        toc_titles = _parse_toc_titles(
            zf, opf_dir, manifest, toc_id, names, lower_names
        )

        chapters: list[Chapter] = []
        seen_paths: set[str] = set()
        accumulated_text_bytes = 0
        total = len(spine)

        for index, idref in enumerate(spine, start=1):
            item = manifest.get(idref)
            if not item:
                continue

            # Skip standalone EPUB3 nav table-of-contents document
            props = item.get("properties", "").split()
            if "nav" in props and len(spine) > 1:
                continue

            media_type = item["media_type"].lower()
            href_lower = item["href"].split("#", 1)[0].lower()
            is_html = (
                "html" in media_type
                or "xhtml" in media_type
                or href_lower.endswith((".xhtml", ".html", ".htm", ".xml"))
            )
            if not is_html:
                continue

            document_path = _resolve_href(
                item["href"],
                opf_dir,
                names,
                lower_names,
            )
            if document_path is None or document_path in seen_paths:
                continue

            seen_paths.add(document_path)
            fallback_title = f"Chapter {len(chapters) + 1}"
            toc_title = toc_titles.get(document_path)

            if progress_callback:
                try:
                    progress_callback(index, total, toc_title or fallback_title)
                except Exception:
                    pass

            try:
                raw_html = _safe_zip_read(zf, document_path)
                title, text = _extract_html_text(
                    raw_html,
                    toc_title,
                    fallback_title,
                )
            except Exception as exc:
                logger.warning("Failed to read EPUB item %s: %s", document_path, exc)
                continue

            # Keep short chapters/prologues (at least 3 chars of real content), skip empty covers
            stripped = text.strip()
            if len(stripped) < 3:
                continue

            accumulated_text_bytes += len(stripped.encode("utf-8"))
            if accumulated_text_bytes > MAX_EPUB_UNCOMPRESSED_BYTES:
                raise ValueError("Book text exceeds maximum supported size.")

            chapters.append(
                Chapter(
                    number=len(chapters) + 1,
                    title=title,
                    text=stripped,
                )
            )

        if not chapters:
            raise ValueError("No readable chapters were found in this EPUB.")

        return chapters


def extract_txt(path: str | Path) -> list[Chapter]:
    """
    Extract a TXT file into chapters (supports English, Roman numerals, Prologues,
    and Hindi अध्याय / भाग / खंड headings).
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError("TXT file does not exist.")

    raw = _clean_text(_read_text_file(path))
    if not raw:
        raise ValueError("The TXT file is empty.")

    matches = list(_TXT_CHAPTER_PATTERN.finditer(raw))

    if len(matches) >= 1:
        chapters: list[Chapter] = []

        preamble = _clean_text(raw[: matches[0].start()])
        if preamble and len(matches) >= 2:
            chapters.append(
                Chapter(
                    number=1,
                    title="Introduction",
                    text=preamble,
                )
            )
        elif preamble and len(matches) == 1:
            # Single chapter heading with preamble before it
            chapters.append(
                Chapter(
                    number=1,
                    title="Introduction",
                    text=preamble,
                )
            )

        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)

            block = raw[start:end].strip()
            lines = block.splitlines()

            title = lines[0].strip() if lines else f"Chapter {i + 1}"
            text = _clean_text("\n".join(lines[1:]))

            if text:
                chapters.append(
                    Chapter(
                        number=len(chapters) + 1,
                        title=title,
                        text=text,
                    )
                )

        if len(chapters) >= 2 or (len(chapters) == 1 and len(matches) == 1 and not preamble):
            return chapters

    return [
        Chapter(
            number=1,
            title=path.stem.replace("_", " ").strip() or "Book",
            text=raw,
        )
    ]


def extract_book(path: str | Path, progress_callback=None) -> list[Chapter]:
    """Automatically select EPUB or TXT parser."""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".epub":
        return extract_epub(path, progress_callback=progress_callback)

    if suffix == ".txt":
        return extract_txt(path)

    raise ValueError("Supported files are EPUB and TXT.")
