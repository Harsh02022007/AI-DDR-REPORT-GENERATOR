"""
extract_pdf.py
==============
Handles all PDF extraction duties for the DDR pipeline:
  - Text extraction  (pdfplumber primary, PyMuPDF fallback)
  - Image extraction (pdfplumber → PIL → base64 JPEG)
  - Thermal-reading  parser
  - Inspection-observation parser

Dependencies: pdfplumber, Pillow, PyMuPDF (optional)
"""

from __future__ import annotations

import base64
import io
import logging
import re
import tempfile
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────

@dataclass
class PageImage:
    """A single image extracted from a PDF page."""
    page_number: int          # 1-based
    label: str                # human-readable label, e.g. "Photo 3"
    base64_jpeg: str          # data:image/jpeg;base64,…
    width: int = 0
    height: int = 0


@dataclass
class ThermalReading:
    """One Bosch thermal scan record."""
    scan_number: int          # page order in thermal PDF (1-based)
    image_ref: str            # e.g. "RB02380X"
    date: str                 # e.g. "27/09/22"
    hotspot_c: float          # °C
    coldspot_c: float         # °C
    emissivity: float
    reflected_temp_c: float
    thumbnail_b64: str = ""   # optional page thumbnail


@dataclass
class InspectionObservation:
    """One impacted-area block from the inspection PDF."""
    area_id: int              # 1-based impacted area number
    area_label: str           # e.g. "Impacted Area 1"
    negative_desc: str        # damage observed at this location
    positive_desc: str        # source / root-cause location
    photos: List[PageImage] = field(default_factory=list)


@dataclass
class ExtractionResult:
    """Everything extracted from one PDF."""
    raw_text: str
    pages: int
    observations: List[InspectionObservation] = field(default_factory=list)
    thermal_readings: List[ThermalReading] = field(default_factory=list)
    checklist_flags: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, str] = field(default_factory=dict)
    page_thumbnails: List[PageImage] = field(default_factory=list)


# ─────────────────────────────────────────────
#  Low-level helpers
# ─────────────────────────────────────────────

def _pil_to_b64_jpeg(pil_image, quality: int = 72) -> str:
    """Convert any PIL image → base64 JPEG data-URI string."""
    img = pil_image.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    raw = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{raw}"


def _extract_text_pdfplumber(pdf_path: str) -> Tuple[str, int]:
    """Extract full text from a PDF using pdfplumber."""
    import pdfplumber
    text_parts: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        for page in pdf.pages:
            t = page.extract_text() or ""
            text_parts.append(t)
    return "\n\n".join(text_parts), n_pages


def _extract_text_pymupdf(pdf_path: str) -> Tuple[str, int]:
    """Extract full text using PyMuPDF (fitz) as an alternative."""
    import fitz  # type: ignore
    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    parts = [page.get_text("text") for page in doc]
    doc.close()
    return "\n\n".join(parts), n_pages


def _extract_page_thumbnails_pdfplumber(
    pdf_path: str,
    resolution: int = 72,
    quality: int = 55,
    max_pages: int = 50,
) -> List[PageImage]:
    """Render every page as a thumbnail JPEG via pdfplumber → PIL."""
    import pdfplumber
    thumbnails: List[PageImage] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            try:
                pil_page = page.to_image(resolution=resolution)
                b64 = _pil_to_b64_jpeg(pil_page.original, quality=quality)
                thumbnails.append(
                    PageImage(
                        page_number=i + 1,
                        label=f"Page {i + 1}",
                        base64_jpeg=b64,
                        width=page.width,
                        height=page.height,
                    )
                )
            except Exception as exc:
                logger.warning("Thumbnail failed page %d: %s", i + 1, exc)
    return thumbnails


# ─────────────────────────────────────────────
#  Text parsers
# ─────────────────────────────────────────────

_THERMAL_HEADER_RE = re.compile(
    r"Thermal image\s*:\s*(\w+\.JPG)"
    r".*?(\d{2}/\d{2}/\d{2})"
    r".*?Hotspot\s*:\s*([\d.]+)\s*°C"
    r".*?Coldspot\s*:\s*([\d.]+)\s*°C"
    r".*?Emissivity\s*:\s*([\d.]+)"
    r".*?Reflected temperature\s*:\s*([\d.]+)\s*°C",
    re.DOTALL | re.IGNORECASE,
)

_HOTSPOT_INLINE_RE = re.compile(
    r"Hotspot\s*:\s*([\d.]+)\s*°C.*?Coldspot\s*:\s*([\d.]+)\s*°C"
    r".*?Emissivity\s*:\s*([\d.]+).*?Reflected temperature\s*:\s*([\d.]+)\s*°C",
    re.DOTALL | re.IGNORECASE,
)


def parse_thermal_readings(text: str) -> List[ThermalReading]:
    """
    Parse all thermal scan blocks from the raw text of the thermal PDF.
    Tries the full header form first; falls back to the inline form.
    """
    readings: List[ThermalReading] = []

    # Full-header matches (with image reference)
    for i, m in enumerate(re.finditer(_THERMAL_HEADER_RE, text), start=1):
        readings.append(
            ThermalReading(
                scan_number=i,
                image_ref=m.group(1).replace(".JPG", "").replace(".jpg", ""),
                date=m.group(2),
                hotspot_c=float(m.group(3)),
                coldspot_c=float(m.group(4)),
                emissivity=float(m.group(5)),
                reflected_temp_c=float(m.group(6)),
            )
        )

    # If full-header pattern found nothing, fall back to inline
    if not readings:
        for i, m in enumerate(re.finditer(_HOTSPOT_INLINE_RE, text), start=1):
            readings.append(
                ThermalReading(
                    scan_number=i,
                    image_ref=f"SCAN_{i:03d}",
                    date="",
                    hotspot_c=float(m.group(1)),
                    coldspot_c=float(m.group(2)),
                    emissivity=float(m.group(3)),
                    reflected_temp_c=float(m.group(4)),
                )
            )

    return readings


_AREA_BLOCK_RE = re.compile(
    r"Impacted Area\s+(\d+)\s*"
    r"Negative side Description\s+(.*?)\s*"
    r"(?:Negative side photographs.*?)?"
    r"Positive side Description\s+(.*?)\s*"
    r"(?:Positive side photographs|Impacted Area|\Z)",
    re.DOTALL | re.IGNORECASE,
)

_SUMMARY_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(Observed[^|]+?)\s+\d+\.\d+\s+(Observed[^|]+?)$",
    re.MULTILINE,
)


def parse_inspection_observations(text: str) -> List[InspectionObservation]:
    """
    Parse impacted-area observation blocks from inspection PDF text.
    Uses the 'Impacted Area N' pattern first, then the summary table.
    """
    observations: List[InspectionObservation] = []

    for m in re.finditer(_AREA_BLOCK_RE, text):
        area_id = int(m.group(1))
        neg = re.sub(r"\s+", " ", m.group(2)).strip()
        pos = re.sub(r"\s+", " ", m.group(3)).strip()
        # Remove trailing "Photo N" artefacts
        neg = re.sub(r"Photo\s+\d+.*$", "", neg, flags=re.IGNORECASE).strip()
        pos = re.sub(r"Photo\s+\d+.*$", "", pos, flags=re.IGNORECASE).strip()
        observations.append(
            InspectionObservation(
                area_id=area_id,
                area_label=f"Impacted Area {area_id}",
                negative_desc=neg,
                positive_desc=pos,
            )
        )

    # Fallback: mine the SUMMARY TABLE section
    if not observations:
        for m in re.finditer(_SUMMARY_ROW_RE, text):
            area_id = int(m.group(1))
            observations.append(
                InspectionObservation(
                    area_id=area_id,
                    area_label=f"Impacted Area {area_id}",
                    negative_desc=re.sub(r"\s+", " ", m.group(2)).strip(),
                    positive_desc=re.sub(r"\s+", " ", m.group(3)).strip(),
                )
            )

    return sorted(observations, key=lambda o: o.area_id)


def parse_checklist_flags(text: str) -> Dict[str, str]:
    """
    Extract key checklist responses as {question: answer} pairs.
    Captures yes/no/moderate/N/A-style values that follow a colon or a tab.
    """
    flags: Dict[str, str] = {}
    pattern = re.compile(
        r"(Leakage during|Leakage due to concealed plumbing"
        r"|Tiles Broken|Loose Plumbing"
        r"|Algae fungus|Cracks observed on RCC"
        r"|Are there any major or minor cracks"
        r"|Internal WC.Bath.Balcony leakage"
        r"|Existing type of paint)"
        r"[^A-Za-z0-9\n]+(Yes|No|All time|Moderate|N/A|Not sure)",
        re.IGNORECASE,
    )
    for m in re.finditer(pattern, text):
        key = re.sub(r"\s+", " ", m.group(1)).strip()
        flags[key] = m.group(2).strip()
    return flags


def parse_metadata(text: str) -> Dict[str, str]:
    """Extract property metadata from inspection text."""
    meta: Dict[str, str] = {}
    fields = {
        "Inspection Date and Time": r"Inspection Date and Time[:\s]+([\d./:\s]+IST)",
        "Inspected By": r"Inspected By[:\s]+([A-Za-z\s&]+)\n",
        "Property Type": r"Property Type[:\s]+([A-Za-z\s]+)\n",
        "Floors": r"Floors[:\s]+(\d+)",
        "Score": r"Score\s+([\d.]+%)",
        "Flagged items": r"Flagged items\s+(\d+)",
        "Previous Structural audit": r"Previous Structural audit done\s+(Yes|No)",
        "Previous Repair work": r"Previous Repair work done\s+(Yes|No)",
        "Impacted Areas": r"Impacted Areas/Rooms\s+([A-Za-z,\s]+)\n",
    }
    for key, pattern in fields.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            meta[key] = re.sub(r"\s+", " ", m.group(1)).strip()
    return meta


# ─────────────────────────────────────────────
#  Page-image assignment helpers
# ─────────────────────────────────────────────

def assign_thumbnails_to_observations(
    observations: List[InspectionObservation],
    thumbnails: List[PageImage],
    photo_pages: Optional[List[int]] = None,
) -> None:
    """
    Heuristically assigns page thumbnails to observation objects.
    photo_pages: 0-based page indices that contain photos (optional hint).
    If not provided, pages 2-8 (0-indexed) are assumed to hold photos.
    """
    if not thumbnails:
        return
    if photo_pages is None:
        # Pages 3–9 in the sample doc contain impacted-area photos (0-indexed: 2–8)
        photo_pages = list(range(2, min(9, len(thumbnails))))

    photo_thumbs = [t for t in thumbnails if (t.page_number - 1) in photo_pages]

    # Distribute roughly equally across observations
    n = len(observations)
    if n == 0:
        return
    chunk = max(1, len(photo_thumbs) // n)
    for i, obs in enumerate(observations):
        start = i * chunk
        end = start + chunk if i < n - 1 else len(photo_thumbs)
        obs.photos = photo_thumbs[start:end]


def assign_thumbnails_to_readings(
    readings: List[ThermalReading],
    thumbnails: List[PageImage],
) -> None:
    """Assign one page thumbnail per thermal reading (1:1 mapping)."""
    for i, reading in enumerate(readings):
        if i < len(thumbnails):
            reading.thumbnail_b64 = thumbnails[i].base64_jpeg


# ─────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────

class PDFExtractor:
    """
    Unified extractor for both inspection and thermal PDFs.

    Usage
    -----
    extractor = PDFExtractor()
    insp_result  = extractor.extract_inspection(insp_bytes)
    therm_result = extractor.extract_thermal(therm_bytes)
    """

    def __init__(
        self,
        thumbnail_resolution: int = 72,
        thumbnail_quality: int = 55,
        max_thumbnail_pages: int = 30,
    ):
        self.thumbnail_resolution = thumbnail_resolution
        self.thumbnail_quality = thumbnail_quality
        self.max_thumbnail_pages = max_thumbnail_pages

    # ── internal ──────────────────────────────

    def _bytes_to_tempfile(self, pdf_bytes: bytes) -> str:
        """Write bytes to a named temp file and return its path."""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.write(pdf_bytes)
        tmp.close()
        return tmp.name

    def _extract_text(self, pdf_path: str) -> Tuple[str, int]:
        """Try pdfplumber, fall back to PyMuPDF, fall back to empty string."""
        try:
            return _extract_text_pdfplumber(pdf_path)
        except Exception as e:
            logger.warning("pdfplumber text extraction failed: %s", e)
        try:
            return _extract_text_pymupdf(pdf_path)
        except Exception as e:
            logger.warning("PyMuPDF text extraction failed: %s", e)
        return "", 0

    def _extract_thumbnails(self, pdf_path: str) -> List[PageImage]:
        """Render page thumbnails; return empty list on failure."""
        try:
            return _extract_page_thumbnails_pdfplumber(
                pdf_path,
                resolution=self.thumbnail_resolution,
                quality=self.thumbnail_quality,
                max_pages=self.max_thumbnail_pages,
            )
        except Exception as e:
            logger.warning("Thumbnail extraction failed: %s", e)
            return []

    # ── public methods ─────────────────────────

    def extract_inspection(self, pdf_bytes: bytes) -> ExtractionResult:
        """
        Extract text, observations, checklist flags, metadata,
        and page thumbnails from an inspection PDF.
        """
        pdf_path = self._bytes_to_tempfile(pdf_bytes)
        try:
            text, n_pages = self._extract_text(pdf_path)
            thumbnails = self._extract_thumbnails(pdf_path)

            observations = parse_inspection_observations(text)
            checklist = parse_checklist_flags(text)
            metadata = parse_metadata(text)

            # Assign page thumbnails to observations
            # Pages 3–9 (0-indexed 2–8) contain impacted-area photos in the sample
            photo_page_indices = list(range(2, min(9, len(thumbnails))))
            assign_thumbnails_to_observations(observations, thumbnails, photo_page_indices)

            return ExtractionResult(
                raw_text=text,
                pages=n_pages,
                observations=observations,
                checklist_flags=checklist,
                metadata=metadata,
                page_thumbnails=thumbnails,
            )
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass

    def extract_thermal(self, pdf_bytes: bytes) -> ExtractionResult:
        """
        Extract thermal readings and page thumbnails from a thermal PDF.
        """
        pdf_path = self._bytes_to_tempfile(pdf_bytes)
        try:
            text, n_pages = self._extract_text(pdf_path)
            thumbnails = self._extract_thumbnails(pdf_path)

            readings = parse_thermal_readings(text)
            assign_thumbnails_to_readings(readings, thumbnails)

            return ExtractionResult(
                raw_text=text,
                pages=n_pages,
                thermal_readings=readings,
                page_thumbnails=thumbnails,
            )
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass

    def extract_text_only(self, pdf_bytes: bytes) -> str:
        """
        Lightweight extraction: returns just the raw text string.
        Useful for quick classification / debugging.
        """
        pdf_path = self._bytes_to_tempfile(pdf_bytes)
        try:
            text, _ = self._extract_text(pdf_path)
            return text
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass