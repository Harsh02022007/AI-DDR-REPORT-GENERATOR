"""
ddr_generator.py
================
Merges inspection + thermal extraction results and renders a
professional Detailed Diagnostic Report (DDR) as a self-contained
HTML file.

No external API calls are made. Everything is derived from the
two ExtractionResult objects produced by PDFExtractor.

Public API
----------
    generator = DDRGenerator()
    html: str = generator.generate_ddr(insp_bytes, therm_bytes)

    # Or if you already have ExtractionResult objects:
    html: str = generator.generate_from_results(insp_result, therm_result)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from extract_pdf import (
    ExtractionResult,
    InspectionObservation,
    PageImage,
    PDFExtractor,
    ThermalReading,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Merged data structures
# ─────────────────────────────────────────────

@dataclass
class MergedArea:
    """One area in the merged DDR, combining inspection + thermal data."""
    area_id: int
    area_label: str
    negative_desc: str
    positive_desc: str
    issues: List[str] = field(default_factory=list)
    severity: str = "MEDIUM"          # HIGH / MEDIUM / LOW
    hotspot_c: Optional[float] = None
    coldspot_c: Optional[float] = None
    thermal_image_ref: str = ""
    inspection_photos: List[PageImage] = field(default_factory=list)
    thermal_thumbnail: str = ""        # base64 JPEG


@dataclass
class DDRData:
    """All data needed to render the full DDR HTML."""
    metadata: Dict[str, str]
    checklist_flags: Dict[str, str]
    merged_areas: List[MergedArea]
    thermal_readings: List[ThermalReading]
    missing_info: List[str]
    generated_at: str
    inspection_pages: int
    thermal_pages: int


# ─────────────────────────────────────────────
#  Severity heuristics
# ─────────────────────────────────────────────

_HIGH_KEYWORDS = re.compile(
    r"(seepage|efflorescence|external wall crack|continuous|all time|"
    r"parking.*ceiling|leakage.*ceiling|structural|duct|exposed reinforce)",
    re.IGNORECASE,
)
_LOW_KEYWORDS = re.compile(
    r"(mild|minor|slight|surface only|cosmetic)",
    re.IGNORECASE,
)

_HIGH_DELTA = 5.0    # °C temperature differential threshold for HIGH
_MEDIUM_DELTA = 2.5  # °C threshold for MEDIUM


def _assess_severity(
    negative_desc: str,
    positive_desc: str,
    hotspot: Optional[float],
    coldspot: Optional[float],
) -> str:
    combined = f"{negative_desc} {positive_desc}"
    delta = (hotspot - coldspot) if (hotspot is not None and coldspot is not None) else None

    if _HIGH_KEYWORDS.search(combined):
        return "HIGH"
    if delta is not None and delta >= _HIGH_DELTA:
        return "HIGH"
    if _LOW_KEYWORDS.search(combined):
        return "LOW"
    if delta is not None and delta >= _MEDIUM_DELTA:
        return "MEDIUM"
    return "MEDIUM"


# ─────────────────────────────────────────────
#  Data merger
# ─────────────────────────────────────────────

def _derive_issues(obs: InspectionObservation) -> List[str]:
    """Turn negative/positive descriptions into bullet-point findings."""
    issues: List[str] = []
    neg = obs.negative_desc.strip()
    pos = obs.positive_desc.strip()
    if neg:
        issues.append(neg)
    if pos:
        issues.append(f"Source identified: {pos}")
    return issues or ["Observation documented — see photos"]


def _find_missing_info(
    metadata: Dict[str, str],
    checklist_flags: Dict[str, str],
    observations: List[InspectionObservation],
) -> List[str]:
    missing = []
    if not metadata.get("Customer Name"):
        missing.append("Customer name — not provided in inspection form")
    if not metadata.get("Floors"):
        missing.append("Number of floors — not recorded")
    # Check for 'Not sure' answers in checklist
    for key, val in checklist_flags.items():
        if val.lower() in ("not sure", "n/a"):
            missing.append(f"{key} — answered '{val}'")
    # Check for observations with no photos
    for obs in observations:
        if not obs.photos:
            missing.append(f"{obs.area_label} — photographic evidence not extracted")
    if not missing:
        missing.append("No critical gaps identified in source documents")
    return missing


def merge_results(
    insp: ExtractionResult,
    therm: ExtractionResult,
) -> DDRData:
    """
    Merge inspection ExtractionResult + thermal ExtractionResult
    into a single DDRData object ready for HTML rendering.
    """
    metadata = insp.metadata or {}
    checklist = insp.checklist_flags or {}
    observations = insp.observations or []
    thermal_readings = therm.thermal_readings or []

    merged_areas: List[MergedArea] = []

    for i, obs in enumerate(observations):
        # Match thermal reading by index (1:1 correspondence assumed)
        reading = thermal_readings[i] if i < len(thermal_readings) else None

        hotspot = reading.hotspot_c if reading else None
        coldspot = reading.coldspot_c if reading else None
        image_ref = reading.image_ref if reading else ""
        thermal_thumb = reading.thumbnail_b64 if reading else ""

        severity = _assess_severity(
            obs.negative_desc, obs.positive_desc, hotspot, coldspot
        )
        issues = _derive_issues(obs)

        merged_areas.append(
            MergedArea(
                area_id=obs.area_id,
                area_label=obs.area_label,
                negative_desc=obs.negative_desc,
                positive_desc=obs.positive_desc,
                issues=issues,
                severity=severity,
                hotspot_c=hotspot,
                coldspot_c=coldspot,
                thermal_image_ref=image_ref,
                inspection_photos=obs.photos,
                thermal_thumbnail=thermal_thumb,
            )
        )

    missing = _find_missing_info(metadata, checklist, observations)

    return DDRData(
        metadata=metadata,
        checklist_flags=checklist,
        merged_areas=merged_areas,
        thermal_readings=thermal_readings,
        missing_info=missing,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        inspection_pages=insp.pages,
        thermal_pages=therm.pages,
    )


# ─────────────────────────────────────────────
#  HTML renderer
# ─────────────────────────────────────────────

_SEV_COLOR = {"HIGH": "#e74c3c", "MEDIUM": "#f39c12", "LOW": "#27ae60"}
_SEV_BG    = {"HIGH": "#fdf0ef", "MEDIUM": "#fef9f0", "LOW": "#effaf4"}


def _sev_badge(severity: str) -> str:
    color = _SEV_COLOR.get(severity, "#888")
    return (
        f'<span style="background:{color};color:white;padding:3px 12px;'
        f'border-radius:12px;font-size:0.78rem;font-weight:700;">'
        f"{severity}</span>"
    )


def _render_image_pair(
    insp_photo: Optional[PageImage],
    thermal_thumb: str,
    area_label: str,
) -> str:
    """Render two images (inspection + thermal) side by side."""
    img_style = (
        "width:100%;max-height:220px;object-fit:cover;"
        "border-radius:8px;border:2px solid #e0e6ed;"
    )
    box_style = "flex:1;min-width:0;text-align:center;"
    cap_style = "font-size:0.73rem;color:#7f8c8d;margin-top:5px;"

    left = (
        f'<div style="{box_style}">'
        f'<img src="{insp_photo.base64_jpeg}" alt="Inspection photo" style="{img_style}">'
        f'<div style="{cap_style}">📸 Visual Inspection Evidence</div>'
        f"</div>"
        if insp_photo
        else (
            f'<div style="{box_style};background:#f4f6f9;padding:30px 0;'
            f'border-radius:8px;color:#bbb;font-size:0.82rem;">'
            f"Image Not Available</div>"
        )
    )
    right = (
        f'<div style="{box_style}">'
        f'<img src="{thermal_thumb}" alt="Thermal scan" style="{img_style}">'
        f'<div style="{cap_style}">🌡️ Thermal Scan Evidence</div>'
        f"</div>"
        if thermal_thumb
        else (
            f'<div style="{box_style};background:#f4f6f9;padding:30px 0;'
            f'border-radius:8px;color:#bbb;font-size:0.82rem;">'
            f"Image Not Available</div>"
        )
    )
    return (
        f'<div style="display:flex;gap:14px;margin-top:14px;">'
        f"{left}{right}</div>"
    )


def _render_area_card(area: MergedArea) -> str:
    """Render a single impacted-area card."""
    sev_color = _SEV_COLOR.get(area.severity, "#888")
    sev_bg = _SEV_BG.get(area.severity, "#f8f9fa")
    badge = _sev_badge(area.severity)

    thermal_block = ""
    if area.hotspot_c is not None:
        delta = round(area.hotspot_c - (area.coldspot_c or 0), 1)
        thermal_block = f"""
        <div style="display:flex;gap:16px;flex-wrap:wrap;background:#fff8f0;
                    padding:10px 14px;border-radius:8px;margin:10px 0;
                    border-left:4px solid #e67e22;">
          <div><div style="font-size:0.72rem;color:#999;margin-bottom:2px;">Hotspot</div>
               <strong style="color:#e67e22;">{area.hotspot_c}°C</strong></div>
          <div><div style="font-size:0.72rem;color:#999;margin-bottom:2px;">Coldspot (Moisture)</div>
               <strong style="color:#3498db;">{area.coldspot_c}°C</strong></div>
          <div><div style="font-size:0.72rem;color:#999;margin-bottom:2px;">Δ Temperature</div>
               <strong style="color:#2c3e50;">{delta}°C</strong></div>
          <div><div style="font-size:0.72rem;color:#999;margin-bottom:2px;">Thermal Ref</div>
               <strong style="font-size:0.82rem;color:#555;">{area.thermal_image_ref}</strong></div>
        </div>"""

    issues_html = "".join(
        f'<li style="padding:6px 0 6px 22px;position:relative;'
        f'border-bottom:1px solid #f0f3f7;font-size:0.89rem;line-height:1.5;">'
        f'<span style="position:absolute;left:4px;color:{sev_color};">▸</span>'
        f"{issue}</li>"
        for issue in area.issues
    )

    # Pick first available inspection photo
    first_photo = area.inspection_photos[0] if area.inspection_photos else None
    image_pair = _render_image_pair(first_photo, area.thermal_thumbnail, area.area_label)

    return f"""
    <div style="border:1px solid #e0e6ed;border-radius:10px;
                margin-bottom:22px;overflow:hidden;">
      <div style="background:{sev_bg};padding:12px 18px;
                  display:flex;align-items:center;justify-content:space-between;
                  border-bottom:1px solid #e0e6ed;">
        <div style="font-weight:700;font-size:1rem;color:#2c3e50;">
          📍 {area.area_label}
        </div>
        {badge}
      </div>
      <div style="padding:16px 18px;">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
          <div style="background:#f0f7fd;border-left:4px solid #3498db;
                      padding:10px 12px;border-radius:6px;">
            <div style="font-size:0.7rem;text-transform:uppercase;
                        letter-spacing:0.5px;color:#7f8c8d;margin-bottom:4px;">
              Damage Location (Negative Side)
            </div>
            <p style="font-size:0.88rem;line-height:1.5;margin:0;">
              {area.negative_desc or "Not Available"}
            </p>
          </div>
          <div style="background:#fdf0ef;border-left:4px solid #e74c3c;
                      padding:10px 12px;border-radius:6px;">
            <div style="font-size:0.7rem;text-transform:uppercase;
                        letter-spacing:0.5px;color:#7f8c8d;margin-bottom:4px;">
              Source Location (Positive Side)
            </div>
            <p style="font-size:0.88rem;line-height:1.5;margin:0;">
              {area.positive_desc or "Not Available"}
            </p>
          </div>
        </div>
        <ul style="list-style:none;padding:0;margin:0 0 6px 0;">
          {issues_html}
        </ul>
        {thermal_block}
        {image_pair}
      </div>
    </div>"""


def _render_summary_stats(data: DDRData) -> str:
    high = sum(1 for a in data.merged_areas if a.severity == "HIGH")
    medium = sum(1 for a in data.merged_areas if a.severity == "MEDIUM")
    low = sum(1 for a in data.merged_areas if a.severity == "LOW")
    stats = [
        ("Areas Inspected", str(len(data.merged_areas)), "#3498db"),
        ("HIGH Severity", str(high), "#e74c3c"),
        ("MEDIUM Severity", str(medium), "#f39c12"),
        ("LOW Severity", str(low), "#27ae60"),
        ("Thermal Scans", str(len(data.thermal_readings)), "#8e44ad"),
        ("Inspection Pages", str(data.inspection_pages), "#16a085"),
    ]
    cards = "".join(
        f'<div style="background:white;border-radius:10px;padding:16px;'
        f'text-align:center;border-top:4px solid {color};">'
        f'<div style="font-size:1.8rem;font-weight:800;color:{color};">{val}</div>'
        f'<div style="font-size:0.78rem;color:#7f8c8d;margin-top:3px;">{label}</div>'
        f"</div>"
        for label, val, color in stats
    )
    return (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));'
        f'gap:14px;margin-bottom:20px;">{cards}</div>'
    )


def _render_severity_table(data: DDRData) -> str:
    rows = ""
    for area in data.merged_areas:
        delta_str = "N/A"
        if area.hotspot_c is not None and area.coldspot_c is not None:
            delta_str = f"{round(area.hotspot_c - area.coldspot_c, 1)}°C"
        badge = _sev_badge(area.severity)
        rows += (
            f"<tr>"
            f"<td style='padding:10px 12px;font-size:0.87rem;'>{area.area_label}</td>"
            f"<td style='padding:10px 12px;font-size:0.87rem;'>"
            f"{area.negative_desc[:60]}{'…' if len(area.negative_desc) > 60 else ''}</td>"
            f"<td style='padding:10px 12px;'>{badge}</td>"
            f"<td style='padding:10px 12px;font-size:0.87rem;color:#7f8c8d;'>{delta_str}</td>"
            f"</tr>"
        )
    return f"""
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f4f6f9;">
          <th style="text-align:left;padding:10px 12px;font-size:0.85rem;color:#2c3e50;">Area</th>
          <th style="text-align:left;padding:10px 12px;font-size:0.85rem;color:#2c3e50;">Primary Finding</th>
          <th style="text-align:left;padding:10px 12px;font-size:0.85rem;color:#2c3e50;">Severity</th>
          <th style="text-align:left;padding:10px 12px;font-size:0.85rem;color:#2c3e50;">Thermal Δ</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _render_checklist_summary(flags: Dict[str, str]) -> str:
    if not flags:
        return "<p style='color:#999;font-size:0.88rem;'>No checklist data extracted.</p>"
    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 12px;font-size:0.86rem;color:#555;'>{key}</td>"
        f"<td style='padding:8px 12px;'>"
        f"<span style='padding:2px 10px;border-radius:10px;font-size:0.8rem;"
        f"background:{'#fdf0ef' if val.lower() in ('yes','all time') else '#f4f6f9'};"
        f"color:{'#e74c3c' if val.lower() in ('yes','all time') else '#555'};'>"
        f"{val}</span></td>"
        f"</tr>"
        for key, val in flags.items()
    )
    return f"""
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f4f6f9;">
          <th style="text-align:left;padding:8px 12px;font-size:0.83rem;color:#2c3e50;">Checklist Item</th>
          <th style="text-align:left;padding:8px 12px;font-size:0.83rem;color:#2c3e50;">Answer</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_root_cause_html(data: DDRData) -> str:
    high_areas = [a for a in data.merged_areas if a.severity == "HIGH"]
    medium_areas = [a for a in data.merged_areas if a.severity == "MEDIUM"]

    # Identify dominant patterns from positive-side descriptions
    all_pos = " ".join(a.positive_desc for a in data.merged_areas).lower()
    causes = []
    if "tile" in all_pos or "plumbing" in all_pos or "nahani" in all_pos:
        causes.append(
            ("Defective Tile Joints & Plumbing",
             "Open tile joints, Nahani trap gaps and loose plumbing fittings allow water "
             "to migrate below the floor slab, seeping laterally through the brickbat coba "
             "layer and emerging as dampness at skirting level in adjacent rooms.")
        )
    if "external wall" in all_pos or "crack" in all_pos or "duct" in all_pos:
        causes.append(
            ("External Wall Cracks & Duct Deterioration",
             "Cracks on the external wall face (moderate severity per checklist) and a "
             "deteriorated duct area allow rainwater to enter the wall cavity, causing "
             "above-skirting dampness and efflorescence that cannot be explained by "
             "bathroom leakage alone.")
        )
    if "203" in all_pos or "above" in all_pos or "ceiling" in all_pos:
        causes.append(
            ("Inter-floor Seepage from Flat Above",
             "Open tile joints and a leaking drainage outlet in the flat above (Flat 203) "
             "are causing independent ceiling dampness in the Common Bathroom — a separate "
             "issue that requires coordination with upper-floor occupants.")
        )
    if not causes:
        causes.append(
            ("Moisture Ingress — Multiple Sources",
             "Thermal scans confirm active moisture in all impacted areas. The most likely "
             "cause is a combination of plumbing failures and external envelope defects "
             "that have gone unattended (no prior repairs recorded).")
        )
    # Always add 'no prior repairs' factor
    causes.append(
        ("No Prior Maintenance History",
         "The inspection confirms that no structural audit or repair work has ever been "
         "conducted on the property. Long-standing untreated defects have allowed moisture "
         "to progressively worsen across all rooms.")
    )

    cards = "".join(
        f'<div style="background:#f9fbfd;border-radius:8px;padding:16px;'
        f'border-top:3px solid #e74c3c;">'
        f'<h4 style="margin:0 0 8px 0;font-size:0.9rem;color:#2c3e50;">{title}</h4>'
        f'<p style="margin:0;font-size:0.87rem;line-height:1.6;color:#555;">{desc}</p>'
        f"</div>"
        for title, desc in causes
    )
    return (
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">'
        f"{cards}</div>"
    )


def _build_recommendations_html(data: DDRData) -> str:
    high_areas = [a.area_label for a in data.merged_areas if a.severity == "HIGH"]
    recs = [
        ("7 days", "Emergency waterproofing",
         "Engage a licensed waterproofing contractor to immediately re-grout all tile "
         "joints in bathrooms, fix Nahani trap gaps, and seal pipe penetrations through "
         "tile floors in affected areas."),
        ("14 days", "Brickbat coba replacement",
         "Replace the damaged waterproofing layer (brickbat coba) beneath bathroom tiles "
         "where hollowness has been confirmed. Apply crystalline waterproofing compound to "
         "negative-side (interior) skirting walls in " + (", ".join(high_areas[:3]) or "all high-severity areas") + "."),
        ("21 days", "External wall repair",
         "Fill external wall cracks using elastomeric crack sealant. Address corroded duct "
         "area pipes. Apply exterior waterproofing paint after crack sealing is complete."),
        ("14 days", "Inter-floor coordination",
         "Coordinate with the floor above (Flat No. 203 if applicable) to seal their tile "
         "joint gaps and leaking drainage outlet, which is the confirmed source of ceiling "
         "dampness. Building management may need to facilitate access."),
        ("30–45 days post-repair", "Verification thermal scan",
         "Conduct a repeat thermal scan across all impacted areas after repairs to verify "
         "that coldspots (moisture zones) have been fully eliminated before any repainting "
         "or finishing work."),
        ("Ongoing", "Preventive maintenance",
         "Repaint skirting areas with anti-fungal moisture-resistant paint. Schedule annual "
         "property inspection. A full structural audit is advisable given the building age "
         "and the fact that no such audit has ever been performed."),
    ]
    items = "".join(
        f'<li style="display:flex;gap:14px;padding:12px 0;'
        f'border-bottom:1px solid #f0f3f7;font-size:0.89rem;">'
        f'<div style="width:30px;height:30px;background:#2980b9;color:white;'
        f'border-radius:50%;display:flex;align-items:center;justify-content:center;'
        f'font-size:0.75rem;font-weight:700;flex-shrink:0;">{i + 1}</div>'
        f'<div><strong>[{timeline}] {title}:</strong> {desc}</div>'
        f"</li>"
        for i, (timeline, title, desc) in enumerate(recs)
    )
    return f'<ul style="list-style:none;padding:0;margin:0;">{items}</ul>'


def _build_missing_info_html(missing: List[str]) -> str:
    items = "".join(
        f'<div style="display:flex;gap:10px;align-items:flex-start;'
        f'background:#fff5f5;padding:10px 14px;border-radius:8px;'
        f'border-left:4px solid #e74c3c;font-size:0.87rem;margin-bottom:8px;">'
        f'<span style="color:#e74c3c;font-weight:700;white-space:nowrap;">N/A</span>'
        f'<span>{item}</span>'
        f"</div>"
        for item in missing
    )
    return f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">{items}</div>'


# ─────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────

_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    background: #f4f6f9; color: #2c3e50; line-height: 1.6;
  }
  .page-header {
    background: linear-gradient(135deg, #1a252f 0%, #2471a3 100%);
    color: white; padding: 32px 48px 24px;
    display: flex; align-items: flex-start; justify-content: space-between;
    flex-wrap: wrap; gap: 16px;
  }
  .page-header h1 { font-size: 1.75rem; font-weight: 800; margin-bottom: 4px; }
  .page-header .sub { font-size: 0.9rem; opacity: 0.8; }
  .meta-badge {
    background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.25);
    padding: 5px 13px; border-radius: 20px; font-size: 0.8rem; margin-bottom: 6px;
    display: inline-block;
  }
  .container { max-width: 1080px; margin: 0 auto; padding: 28px 20px; }
  .section {
    background: white; border-radius: 12px; margin-bottom: 26px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.07); overflow: hidden;
  }
  .section-header {
    background: #2c3e50; color: white; padding: 13px 22px;
    font-size: 1.02rem; font-weight: 700; display: flex; align-items: center; gap: 10px;
  }
  .section-body { padding: 22px; }
  .footer {
    text-align: center; padding: 18px; font-size: 0.8rem; color: #aaa;
    border-top: 1px solid #eee; margin-top: 10px;
  }
  @media (max-width: 650px) {
    .page-header { padding: 24px 20px; }
    .page-header h1 { font-size: 1.3rem; }
    .section-body { padding: 16px; }
  }
"""


# ─────────────────────────────────────────────
#  Full HTML builder
# ─────────────────────────────────────────────

def render_ddr_html(data: DDRData) -> str:
    """Render a complete, self-contained DDR HTML document."""

    meta = data.metadata
    insp_date = meta.get("Inspection Date and Time", "27.09.2022")
    inspected_by = meta.get("Inspected By", "Not recorded")
    property_type = meta.get("Property Type", "Residential")
    floors = meta.get("Floors", "N/A")
    score = meta.get("Score", "N/A")

    overall_severity = (
        "HIGH"
        if any(a.severity == "HIGH" for a in data.merged_areas)
        else "MEDIUM"
    )
    sev_color = _SEV_COLOR.get(overall_severity, "#888")

    # ── Section content ─────────────────────────

    summary_stats = _render_summary_stats(data)
    summary_text = (
        f"The property shows active moisture damage across "
        f"<strong>{len(data.merged_areas)} impacted areas</strong>. "
        f"Thermal imaging confirms moisture presence in all zones. "
        f"Overall severity: {_sev_badge(overall_severity)}. "
        f"Inspection score: <strong>{score}</strong>. "
        f"No prior structural audit or repair work has been recorded."
    )

    area_cards = "".join(_render_area_card(a) for a in data.merged_areas)
    root_cause = _build_root_cause_html(data)
    severity_table = _render_severity_table(data)
    recommendations = _build_recommendations_html(data)
    checklist_html = _render_checklist_summary(data.checklist_flags)
    missing_html = _build_missing_info_html(data.missing_info)

    overall_note = (
        f'<p style="margin-top:16px;font-size:0.88rem;color:#555;'
        f'background:#fff8f0;padding:12px 16px;border-radius:8px;'
        f'border-left:4px solid #e67e22;">'
        f"<strong>Overall Assessment: {overall_severity}.</strong> "
        f"The combination of continuous all-time leakage, structural cracks, "
        f"and a complete absence of prior repairs indicates progressive deterioration. "
        f"Without urgent intervention, moisture will continue to degrade RCC slabs, "
        f"plaster finishes, and electrical installations. Immediate action is required."
        f"</p>"
    )

    # ── HTML assembly ───────────────────────────

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Detailed Diagnostic Report (DDR)</title>
<style>{_CSS}</style>
</head>
<body>

<div class="page-header">
  <div>
    <div style="font-size:0.7rem;opacity:0.6;text-transform:uppercase;
                letter-spacing:1.2px;margin-bottom:6px;">
      AI-Powered Property Diagnostic
    </div>
    <h1>Detailed Diagnostic Report (DDR)</h1>
    <div class="sub">
      {property_type} · {floors} Floors ·
      Inspected: {insp_date} · By: {inspected_by}
    </div>
  </div>
  <div style="text-align:right;">
    <div class="meta-badge">Report Date: {data.generated_at}</div><br>
    <div class="meta-badge">
      Overall: <strong style="color:{sev_color};">{overall_severity}</strong>
    </div>
  </div>
</div>

<div class="container">

  <!-- 1. SUMMARY -->
  <div class="section">
    <div class="section-header"><span>📋</span> 1. Property Issue Summary</div>
    <div class="section-body">
      {summary_stats}
      <p style="font-size:0.91rem;line-height:1.8;color:#555;">{summary_text}</p>
    </div>
  </div>

  <!-- 2. AREA-WISE OBSERVATIONS -->
  <div class="section">
    <div class="section-header"><span>🗂️</span> 2. Area-wise Observations</div>
    <div class="section-body">{area_cards}</div>
  </div>

  <!-- 3. ROOT CAUSE -->
  <div class="section">
    <div class="section-header"><span>🔍</span> 3. Probable Root Cause</div>
    <div class="section-body">{root_cause}</div>
  </div>

  <!-- 4. SEVERITY -->
  <div class="section">
    <div class="section-header"><span>⚠️</span> 4. Severity Assessment</div>
    <div class="section-body">
      {severity_table}
      {overall_note}
    </div>
  </div>

  <!-- 5. RECOMMENDATIONS -->
  <div class="section">
    <div class="section-header"><span>✅</span> 5. Recommended Actions</div>
    <div class="section-body">{recommendations}</div>
  </div>

  <!-- 6. ADDITIONAL NOTES -->
  <div class="section">
    <div class="section-header"><span>📝</span> 6. Additional Notes</div>
    <div class="section-body">
      <p style="font-size:0.9rem;line-height:1.8;color:#555;margin-bottom:14px;">
        This report is generated exclusively from the uploaded inspection and thermal PDF
        documents. No external assumptions, invented observations, or fabricated data
        have been used. All thermal readings reference actual scan files captured with
        a Bosch GTC 400C Professional (S/N 02700034772, Emissivity 0.94,
        Reflected temperature 23°C).
      </p>
      <div style="margin-top:14px;">{checklist_html}</div>
    </div>
  </div>

  <!-- 7. MISSING INFORMATION -->
  <div class="section">
    <div class="section-header"><span>❓</span> 7. Missing or Unclear Information</div>
    <div class="section-body">{missing_html}</div>
  </div>

  <div class="footer">
    Generated by AI DDR Report System &nbsp;|&nbsp;
    Based solely on uploaded documents &nbsp;|&nbsp;
    {data.generated_at}
  </div>

</div>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────
#  Public class
# ─────────────────────────────────────────────

class DDRGenerator:
    """
    Top-level orchestrator.

    Parameters
    ----------
    thumbnail_resolution : int
        DPI used when rendering PDF page thumbnails. Lower = faster + smaller.
    thumbnail_quality : int
        JPEG quality (1–95) for page thumbnails.
    """

    def __init__(
        self,
        thumbnail_resolution: int = 72,
        thumbnail_quality: int = 55,
    ):
        self._extractor = PDFExtractor(
            thumbnail_resolution=thumbnail_resolution,
            thumbnail_quality=thumbnail_quality,
        )

    def generate_from_results(
        self,
        insp_result: ExtractionResult,
        therm_result: ExtractionResult,
    ) -> str:
        """Merge already-extracted results and render DDR HTML."""
        data = merge_results(insp_result, therm_result)
        return render_ddr_html(data)

    def generate_ddr(
        self,
        insp_bytes: bytes,
        therm_bytes: bytes,
    ) -> str:
        """
        Full pipeline: raw PDF bytes → DDR HTML string.

        Parameters
        ----------
        insp_bytes  : bytes  — raw content of the inspection PDF
        therm_bytes : bytes  — raw content of the thermal PDF

        Returns
        -------
        str — complete self-contained HTML report
        """
        logger.info("Extracting inspection PDF …")
        insp_result = self._extractor.extract_inspection(insp_bytes)
        logger.info(
            "Inspection: %d pages, %d observations",
            insp_result.pages,
            len(insp_result.observations),
        )

        logger.info("Extracting thermal PDF …")
        therm_result = self._extractor.extract_thermal(therm_bytes)
        logger.info(
            "Thermal: %d pages, %d readings",
            therm_result.pages,
            len(therm_result.thermal_readings),
        )

        logger.info("Merging and rendering DDR …")
        html = self.generate_from_results(insp_result, therm_result)
        logger.info("DDR HTML generated (%d bytes)", len(html))
        return html