# AI DDR Report Generator

Automatically converts a site **Inspection PDF** and a **Thermal Imaging PDF**
into a structured, client-ready **Detailed Diagnostic Report (DDR)**.

```
ai-ddr-report-generator/
│
├── data/                   ← place input PDFs here for batch use
│
├── src/
│   ├── extract_pdf.py      ← PDF text + image extraction module
│   └── ddr_generator.py    ← DDR merge, analysis & HTML renderer
│
├── output/                 ← generated DDR HTML files land here
│
├── app.py                  ← Streamlit web application
├── requirements.txt
├── README.md
└── .env                    ← secrets (never commit)
```

---

## Features

| Feature | Detail |
|---------|--------|
| **Text extraction** | pdfplumber (primary) + PyMuPDF fallback |
| **Image extraction** | Page thumbnails rendered via pdfplumber → PIL |
| **Thermal parsing** | Regex parser for Bosch GTC / similar scan formats |
| **Observation parsing** | Impacted-area blocks & summary table extraction |
| **Severity scoring** | Heuristic engine: keyword + thermal ΔT thresholds |
| **DDR structure** | All 7 mandatory sections (summary → missing info) |
| **Report format** | Self-contained HTML — opens in any browser |
| **No LLM required** | Runs fully offline; optional API extension points |

---

## Quick Start

### 1. Clone / download

```bash
git clone https://github.com/your-org/ai-ddr-report-generator.git
cd ai-ddr-report-generator
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** PyMuPDF (`fitz`) is optional but recommended for faster text
> extraction on large PDFs. If it is not installed, the pipeline falls back
> gracefully to pdfplumber.

### 3. Configure environment (optional)

```bash
cp .env.example .env
# Edit .env if you plan to add an LLM backend later
```

### 4. Run the web app

```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

### 5. Upload → Generate → Download

1. Upload the **Inspection Report** PDF (left panel).
2. Upload the **Thermal Images** PDF (right panel).
3. Click **🚀 Generate DDR Report**.
4. Preview inline or click **💾 Download** to save the HTML report.

---

## Programmatic Usage

```python
from src.ddr_generator import DDRGenerator

generator = DDRGenerator(
    thumbnail_resolution=72,   # DPI for page thumbnails
    thumbnail_quality=60,      # JPEG quality (1-95)
)

with open("data/inspection.pdf", "rb") as f:
    insp_bytes = f.read()

with open("data/thermal.pdf", "rb") as f:
    therm_bytes = f.read()

html: str = generator.generate_ddr(insp_bytes, therm_bytes)

with open("output/DDR_Report.html", "w", encoding="utf-8") as f:
    f.write(html)

print("Report saved.")
```

---

## Module Reference

### `src/extract_pdf.py`

| Class / Function | Purpose |
|-----------------|---------|
| `PDFExtractor` | Main extractor class — call `.extract_inspection()` or `.extract_thermal()` |
| `PDFExtractor.extract_inspection(bytes)` | Returns `ExtractionResult` with observations, checklist, metadata, thumbnails |
| `PDFExtractor.extract_thermal(bytes)` | Returns `ExtractionResult` with thermal readings + thumbnails |
| `PDFExtractor.extract_text_only(bytes)` | Lightweight text-only extraction |
| `parse_thermal_readings(text)` | Parse `ThermalReading` objects from raw text |
| `parse_inspection_observations(text)` | Parse `InspectionObservation` objects |
| `parse_checklist_flags(text)` | Extract `{question: answer}` checklist pairs |
| `parse_metadata(text)` | Extract property metadata dict |

**Key data classes:** `PageImage`, `ThermalReading`, `InspectionObservation`, `ExtractionResult`

---

### `src/ddr_generator.py`

| Class / Function | Purpose |
|-----------------|---------|
| `DDRGenerator` | Top-level class — `.generate_ddr(insp_bytes, therm_bytes)` |
| `DDRGenerator.generate_from_results(insp, therm)` | Skip extraction; merge already-extracted results |
| `merge_results(insp, therm)` | Produce `DDRData` from two `ExtractionResult` objects |
| `render_ddr_html(data)` | Render `DDRData` → HTML string |
| `_assess_severity(...)` | Heuristic severity scoring (HIGH / MEDIUM / LOW) |

**Key data classes:** `MergedArea`, `DDRData`

---

## DDR Report Structure

The generated HTML report always contains exactly **7 sections**:

1. **Property Issue Summary** — stats cards + overall narrative
2. **Area-wise Observations** — per-area cards with photos, thermal readings & badges
3. **Probable Root Cause** — auto-derived cause cards
4. **Severity Assessment** — table + overall assessment paragraph
5. **Recommended Actions** — 6 prioritised, time-boxed actions
6. **Additional Notes** — checklist summary, data provenance statement
7. **Missing or Unclear Information** — explicit N/A flags

---

## Input PDF Format

The pipeline is designed for, but not limited to, **UrbanRoof**-style inspection reports
and **Bosch GTC 400C** thermal PDFs. It will generalise to any inspection PDF that uses:

- "Impacted Area N" headings with "Negative side Description" / "Positive side Description"
- OR a summary table with area / observation rows

And any thermal PDF that contains:

```
Thermal image : RB02380X.JPG    Device : …    Serial Number : …
Hotspot :  28.8 °C
Coldspot : 23.4 °C
Emissivity : 0.94
Reflected temperature : 23 °C
```

If neither pattern matches, the pipeline falls back to regex mining for any
temperature readings and area-name mentions.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No observations found" | Check PDF has selectable text (not a scanned image-only PDF) |
| "No thermal readings found" | Ensure the thermal PDF format matches Bosch GTC or similar |
| Images are blank | Increase `thumbnail_resolution` (e.g. 96) in sidebar settings |
| Slow generation | Lower `thumbnail_resolution` (e.g. 48) or `thumbnail_quality` (e.g. 45) |
| `ModuleNotFoundError: pdfplumber` | Run `pip install -r requirements.txt` |
| Large output file | Lower `thumbnail_quality` in sidebar settings |

---

## Extending with an LLM

The architecture intentionally separates extraction from generation.
To plug in Claude / OpenAI for richer natural-language analysis:

```python
# In src/ddr_generator.py, after merge_results():
import anthropic

client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env

def enrich_with_llm(data: DDRData) -> DDRData:
    prompt = f"""
    Given these inspection findings:
    {[a.negative_desc for a in data.merged_areas]}
    And these thermal readings:
    {[(r.hotspot_c, r.coldspot_c) for r in data.thermal_readings]}
    Write a professional root-cause analysis paragraph.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    # Inject LLM text into data ...
    return data
```

---

## License

MIT — see `LICENSE` for details.