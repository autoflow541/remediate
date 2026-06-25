"""PDF/UA-1 compliance metadata writer (Sprint 14).

Writes the three catalog-level entries that veraPDF checks on every document
before looking at anything else:

  Clause 7.1-2  — XMP stream with pdfuaid:part = 1
  Clause 7.1-3  — /MarkInfo with /Marked true and /Suspects false
  Clause 7.1-5  — /ViewerPreferences with /DisplayDocTitle true

Also ensures /Lang is set on the catalog (Clause 7.2-1 backup).

Called from writeback.py after the struct tree is committed but before save().
"""

from __future__ import annotations

import logging
import re

import pikepdf
from pikepdf import Dictionary, Name, Stream, String

log = logging.getLogger(__name__)

_XMP_TEMPLATE = """\
<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="PDF Accessibility Remediation Engine">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/">
      <pdfuaid:part>1</pdfuaid:part>
      <pdfuaid:amd>2005</pdfuaid:amd>
      <dc:title>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{title}</rdf:li>
        </rdf:Alt>
      </dc:title>
      <dc:language>
        <rdf:Bag>
          <rdf:li>{lang}</rdf:li>
        </rdf:Bag>
      </dc:language>
      <pdf:Producer>PDF Accessibility Remediation Engine</pdf:Producer>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

_XML_ESCAPE = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&apos;",
}


def _escape_xml(s: str) -> str:
    for char, entity in _XML_ESCAPE.items():
        s = s.replace(char, entity)
    return s


def _get_title(pdf: pikepdf.Pdf, manifest: dict) -> str:
    """Best-effort document title for XMP dc:title."""
    # 1. Manifest source title (set by AI or heading detection)
    src = manifest.get("source", {})
    title = (src.get("title") or "").strip()
    if title:
        return _escape_xml(title)

    # 2. PDF /Info /Title
    try:
        info = pdf.docinfo
        t = str(info.get("/Title", "")).strip()
        if t:
            return _escape_xml(t)
    except Exception:
        pass

    # 3. First H1 in manifest
    def _find_h1(nodes):
        for n in nodes or []:
            if n.get("tag") == "H1" and (n.get("text") or "").strip():
                return n["text"].strip()
            found = _find_h1(n.get("children"))
            if found:
                return found
        return ""
    h1 = _find_h1(manifest.get("nodes", []))
    if h1:
        return _escape_xml(h1)

    return "Untitled Document"


def _get_lang(pdf: pikepdf.Pdf, manifest: dict) -> str:
    """Document language tag for XMP dc:language."""
    # 1. Manifest
    lang = (manifest.get("document", {}).get("language") or "").strip()
    if lang:
        return _escape_xml(lang)
    # 2. PDF catalog /Lang
    try:
        lang = str(pdf.Root.get("/Lang", "")).strip()
        if lang:
            return _escape_xml(lang)
    except Exception:
        pass
    return "en"


def write_pdfua_metadata(pdf: pikepdf.Pdf, manifest: dict) -> dict:
    """Write all PDF/UA-1 catalog-level compliance entries.

    Returns a dict of what was written for the report.
    """
    written: dict[str, bool] = {}

    # ── 1. MarkInfo (/Marked true /Suspects false) ────────────────────────────
    try:
        pdf.Root.MarkInfo = Dictionary(Marked=True, Suspects=False)
        written["markInfo"] = True
        log.debug("pdf_ua_comply: MarkInfo written")
    except Exception as exc:
        log.warning("pdf_ua_comply: MarkInfo failed: %s", exc)
        written["markInfo"] = False

    # ── 2. ViewerPreferences (/DisplayDocTitle true) ──────────────────────────
    try:
        vp = pdf.Root.get("/ViewerPreferences")
        if vp is None:
            vp = Dictionary()
        vp.DisplayDocTitle = True
        pdf.Root.ViewerPreferences = vp
        written["displayDocTitle"] = True
    except Exception as exc:
        log.warning("pdf_ua_comply: ViewerPreferences failed: %s", exc)
        written["displayDocTitle"] = False

    # ── 3. Catalog /Lang (backup — autotag should have set it already) ────────
    try:
        existing_lang = str(pdf.Root.get("/Lang", "")).strip()
        if not existing_lang:
            lang = _get_lang(pdf, manifest)
            pdf.Root.Lang = String(lang)
            written["catalogLang"] = True
        else:
            written["catalogLang"] = False  # already present
    except Exception as exc:
        log.warning("pdf_ua_comply: catalog /Lang failed: %s", exc)

    # ── 4. XMP metadata stream (pdfuaid:part = 1) ────────────────────────────
    try:
        title = _get_title(pdf, manifest)
        lang  = _get_lang(pdf, manifest)
        xmp_bytes = _XMP_TEMPLATE.format(title=title, lang=lang).encode("utf-8")

        # Try to update existing metadata stream first
        existing = pdf.Root.get("/Metadata")
        if existing is not None:
            # Patch the existing stream: add pdfuaid namespace if missing
            try:
                raw = existing.read_bytes().decode("utf-8", errors="replace")
                if "pdfuaid:part" not in raw:
                    # Replace the whole stream with our template
                    existing.write(xmp_bytes)
                    existing.stream_dict["/Subtype"] = Name("/XML")
                    written["xmpMetadata"] = "patched"
                else:
                    written["xmpMetadata"] = "already_present"
            except Exception:
                existing.write(xmp_bytes)
                written["xmpMetadata"] = "replaced"
        else:
            meta_stream = Stream(pdf, xmp_bytes)
            meta_stream.stream_dict[Name("/Type")]    = Name("/Metadata")
            meta_stream.stream_dict[Name("/Subtype")] = Name("/XML")
            pdf.Root[Name("/Metadata")] = meta_stream
            written["xmpMetadata"] = "created"

        log.debug("pdf_ua_comply: XMP metadata %s (title=%r lang=%r)",
                  written["xmpMetadata"], title, lang)
    except Exception as exc:
        log.warning("pdf_ua_comply: XMP metadata failed: %s", exc)
        written["xmpMetadata"] = "failed"

    return written
