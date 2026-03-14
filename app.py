"""
app.py
======
Streamlit front-end for the AI DDR Report Generator.

Run
---
    streamlit run app.py

Environment
-----------
All secrets are read from .env (loaded automatically via python-dotenv).
No API keys are required for the core DDR pipeline.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

import streamlit as st

# ── path setup so src/ modules are importable when running from project root ──
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# ── optional .env loader ──
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from ddr_generator import DDRGenerator  # noqa: E402  (after path setup)

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Page config (must be first Streamlit call)
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="AI DDR Report Generator",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ─────────────────────────────────────────────
#  Custom CSS
# ─────────────────────────────────────────────
st.markdown(
    """
    <style>
      /* Remove default top padding */
      .block-container { padding-top: 1.5rem !important; }

      /* Upload area */
      [data-testid="stFileUploader"] {
        border: 2px dashed #3498db !important;
        border-radius: 10px !important;
        padding: 10px !important;
      }

      /* Primary button */
      div.stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #1a252f 0%, #2980b9 100%);
        color: white;
        font-size: 1.05rem;
        font-weight: 700;
        padding: 0.65rem 2.5rem;
        border: none;
        border-radius: 8px;
        width: 100%;
        transition: opacity 0.2s;
      }
      div.stButton > button[kind="primary"]:hover { opacity: 0.88; }

      /* Sidebar styling */
      [data-testid="stSidebar"] { background: #1a252f; }
      [data-testid="stSidebar"] * { color: #ecf0f1 !important; }
      [data-testid="stSidebar"] .stMarkdown h3 { color: #3498db !important; }

      /* Success / info boxes */
      .stAlert { border-radius: 8px !important; }

      /* Section header cards */
      .step-card {
        background: white; border-radius: 10px;
        padding: 14px 18px; margin-bottom: 12px;
        border-left: 5px solid #3498db;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
      }
      .step-card h4 { margin: 0 0 4px 0; font-size: 0.95rem; color: #2c3e50; }
      .step-card p  { margin: 0; font-size: 0.85rem; color: #7f8c8d; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────
#  Session state helpers
# ─────────────────────────────────────────────
def _init_state() -> None:
    for key, default in [
        ("ddr_html", None),
        ("ddr_filename", ""),
        ("last_generated", ""),
        ("generation_time", 0.0),
        ("error_msg", ""),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default


# ─────────────────────────────────────────────
#  Sidebar
# ─────────────────────────────────────────────
def _render_sidebar() -> dict:
    with st.sidebar:
        st.markdown("## 🏠 DDR Generator")
        st.markdown("---")

        st.markdown("### ⚙️ Settings")
        thumbnail_res = st.select_slider(
            "Thumbnail resolution (DPI)",
            options=[48, 60, 72, 96],
            value=72,
            help="Higher = better image quality but slower processing.",
        )
        thumbnail_quality = st.slider(
            "JPEG quality",
            min_value=40, max_value=85, value=60, step=5,
            help="Higher = sharper images but larger output file.",
        )

        st.markdown("---")
        st.markdown("### 📋 How It Works")
        for step, desc in [
            ("1. Upload PDFs", "Inspection report + thermal scan PDF"),
            ("2. Auto-extract", "Text, observations & thermal data parsed"),
            ("3. Merge & analyse", "Findings correlated, severity assessed"),
            ("4. Generate DDR", "Professional HTML report produced"),
            ("5. Download", "Self-contained HTML — open in any browser"),
        ]:
            st.markdown(
                f'<div class="step-card"><h4>{step}</h4><p>{desc}</p></div>',
                unsafe_allow_html=True,
            )

        st.markdown("---")
        st.markdown(
            "<small style='opacity:0.55;'>Powered by pdfplumber + Pillow.<br>"
            "No API keys required.</small>",
            unsafe_allow_html=True,
        )

    return {
        "thumbnail_resolution": thumbnail_res,
        "thumbnail_quality": thumbnail_quality,
    }


# ─────────────────────────────────────────────
#  File upload section
# ─────────────────────────────────────────────
def _render_upload_section():
    st.markdown("## 📂 Upload Source Documents")
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 📋 Inspection Report")
        st.caption("UrbanRoof / field inspection PDF with impacted-area observations")
        insp_file = st.file_uploader(
            "Upload inspection PDF",
            type="pdf",
            key="insp_upload",
            label_visibility="collapsed",
        )
        if insp_file:
            size_kb = len(insp_file.getvalue()) / 1024
            st.success(f"✓ {insp_file.name}  ({size_kb:.1f} KB)")

    with col2:
        st.markdown("#### 🌡️ Thermal Report")
        st.caption("Bosch GTC or similar thermal imaging PDF with temperature data")
        therm_file = st.file_uploader(
            "Upload thermal PDF",
            type="pdf",
            key="therm_upload",
            label_visibility="collapsed",
        )
        if therm_file:
            size_kb = len(therm_file.getvalue()) / 1024
            st.success(f"✓ {therm_file.name}  ({size_kb:.1f} KB)")

    return insp_file, therm_file


# ─────────────────────────────────────────────
#  Generation logic
# ─────────────────────────────────────────────
def _generate_ddr(
    insp_bytes: bytes,
    therm_bytes: bytes,
    thumbnail_resolution: int,
    thumbnail_quality: int,
) -> str:
    generator = DDRGenerator(
        thumbnail_resolution=thumbnail_resolution,
        thumbnail_quality=thumbnail_quality,
    )
    return generator.generate_ddr(insp_bytes, therm_bytes)


# ─────────────────────────────────────────────
#  Results section
# ─────────────────────────────────────────────
def _render_results() -> None:
    if not st.session_state.ddr_html:
        return

    html = st.session_state.ddr_html
    filename = st.session_state.ddr_filename
    gen_time = st.session_state.generation_time

    st.markdown("---")
    st.markdown("## 📄 Generated Report")

    # Metrics row
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Status", "✅ Ready")
    col2.metric("Generation time", f"{gen_time:.1f}s")
    col3.metric("Report size", f"{len(html) / 1024:.0f} KB")
    col4.metric("Generated at", st.session_state.last_generated)

    # Download button
    st.download_button(
        label="💾 Download DDR Report (HTML)",
        data=html,
        file_name=filename,
        mime="text/html",
        use_container_width=True,
    )

    # Preview
    with st.expander("👁️ Preview Report (scrollable)", expanded=True):
        st.components.v1.html(html, height=900, scrolling=True)


# ─────────────────────────────────────────────
#  Error display
# ─────────────────────────────────────────────
def _render_error() -> None:
    if st.session_state.error_msg:
        st.error(f"❌ {st.session_state.error_msg}")
        with st.expander("🔍 Troubleshooting tips"):
            st.markdown(
                """
                **Common causes:**
                - PDF is scanned/image-only → text extraction yields nothing.
                  *Fix: ensure PDFs have selectable text.*
                - Corrupted or password-protected PDF.
                - Very large PDF (>50 MB) → try compressing first.
                - pdfplumber not installed → run `pip install -r requirements.txt`.
                """
            )


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
def main() -> None:
    _init_state()

    # Sidebar
    settings = _render_sidebar()

    # Page header
    st.markdown(
        """
        <div style="background:linear-gradient(135deg,#1a252f 0%,#2980b9 100%);
                    color:white;padding:22px 28px;border-radius:12px;margin-bottom:24px;">
          <h1 style="margin:0;font-size:1.9rem;">🏠 AI DDR Report Generator</h1>
          <p style="margin:6px 0 0;opacity:0.8;font-size:0.95rem;">
            Upload an Inspection PDF + Thermal PDF → Receive a structured
            Detailed Diagnostic Report with images, thermal data &amp; recommendations.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Upload section
    insp_file, therm_file = _render_upload_section()

    # Generate button
    st.markdown("<br>", unsafe_allow_html=True)
    both_ready = insp_file is not None and therm_file is not None
    generate_clicked = st.button(
        "🚀 Generate DDR Report",
        type="primary",
        disabled=not both_ready,
        use_container_width=True,
    )

    if not both_ready:
        st.info("⬆️ Please upload **both** PDF files above to enable report generation.")

    # Generation
    if generate_clicked and both_ready:
        st.session_state.error_msg = ""
        st.session_state.ddr_html = None

        progress_bar = st.progress(0, text="Starting …")
        status_text  = st.empty()

        steps = [
            (15, "📖 Extracting inspection PDF …"),
            (40, "🌡️ Extracting thermal PDF …"),
            (65, "🔗 Merging and correlating findings …"),
            (85, "🎨 Rendering HTML report …"),
            (100, "✅ Done!"),
        ]

        try:
            insp_bytes  = insp_file.getvalue()
            therm_bytes = therm_file.getvalue()

            t0 = time.time()
            for pct, msg in steps[:-1]:
                progress_bar.progress(pct, text=msg)
                status_text.caption(msg)

            html = _generate_ddr(
                insp_bytes,
                therm_bytes,
                settings["thumbnail_resolution"],
                settings["thumbnail_quality"],
            )

            progress_bar.progress(100, text=steps[-1][1])
            status_text.empty()

            elapsed = round(time.time() - t0, 1)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"DDR_Report_{ts}.html"

            # Persist to output/ folder as well
            out_dir = ROOT / "output"
            out_dir.mkdir(exist_ok=True)
            (out_dir / filename).write_text(html, encoding="utf-8")

            st.session_state.ddr_html = html
            st.session_state.ddr_filename = filename
            st.session_state.last_generated = datetime.now().strftime("%H:%M:%S")
            st.session_state.generation_time = elapsed

        except Exception as exc:
            logger.exception("DDR generation failed")
            st.session_state.error_msg = f"{type(exc).__name__}: {exc}"
            progress_bar.empty()
            status_text.empty()

    # Results / error
    _render_error()
    _render_results()


if __name__ == "__main__":
    main()