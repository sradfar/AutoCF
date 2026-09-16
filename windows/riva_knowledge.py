"""Curated AutoCF answers used by Riva before API-document retrieval."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


FAQS = (
    {
        "id": "first_step",
        "title": "Starting an AutoCF model",
        "aliases": (
            "what is the first step to run", "first step to run", "how do i start",
            "how do i begin", "start a new model", "start autocf",
        ),
        "keywords": ("first", "step", "start", "begin", "run"),
        "pages": ("project", "event"),
        "answer": (
            "For a new model, select Start new model, choose its working directory, and "
            "select the event. Then define the Study Area and continue through Terrain, "
            "Roughness & Infiltration, Forcing, and SFINCS Model. Use Build & Run only "
            "after the configuration is valid. If you already have a YAML, use Import "
            "existing project instead."
        ),
    },
    {
        "id": "workflow",
        "title": "AutoCF workflow order",
        "aliases": ("what is the workflow", "workflow order", "what comes next", "next step"),
        "keywords": ("workflow", "order", "next", "steps"),
        "pages": (),
        "answer": (
            "The normal order is Event & Time, Study Area, Terrain, Roughness & "
            "Infiltration, Forcing, SFINCS Model, Build & Run, Evaluation, Attribution & "
            "Analysis, and Results. Imported projects can go directly to the page that "
            "needs review."
        ),
    },
    {
        "id": "new_or_import",
        "title": "New model versus imported project",
        "aliases": ("new model or import", "import existing project", "open existing yaml"),
        "keywords": ("new", "import", "existing", "yaml"),
        "pages": ("project",),
        "answer": (
            "Use Start new model for the guided event-first workflow. Use Import existing "
            "project when a YAML already describes the model. Importing preserves that "
            "project's paths and does not force it into the new-model sequence."
        ),
    },
)

FAQS += (
    {
        "id": "draft_save",
        "title": "Saving an incomplete model",
        "aliases": ("can i save a draft", "save incomplete model", "configuration invalid"),
        "keywords": ("save", "draft", "incomplete", "invalid"),
        "pages": ("project",),
        "answer": (
            "You can save an incomplete YAML as a draft. AutoCF shows missing settings, "
            "but final scientific validation is enforced when you start Build & Run. "
            "Resolve the listed issue on its owning page before execution."
        ),
    },
    {
        "id": "study_area",
        "title": "Study-area setup",
        "aliases": ("set the study area", "select study area", "use huc"),
        "keywords": ("study", "area", "domain", "huc", "boundary"),
        "pages": ("study",),
        "answer": (
            "Define one computational domain by drawing or importing a boundary, or by "
            "selecting HUC polygons. Review the map, then select Save study area & continue. "
            "Adjacent selected HUCs are dissolved into one model boundary."
        ),
    },
)

FAQS += (
    {
        "id": "vertical_datum_handling",
        "title": "Vertical-datum handling",
        "aliases": (
            "how does autocf handle datums", "does autocf convert vertical datum",
            "how is the gapfill datum converted", "datum conversion",
        ),
        "keywords": ("vertical", "datum", "convert", "alignment", "gapfill"),
        "pages": ("terrain", "results"),
        "answer": (
            "AutoCF keeps Source 1 in its documented native vertical datum and uses that as the "
            "model datum. If Source 2 differs, automatic mode first tries AutoCF's isolated Transformez "
            "grid helper. It runs separately and does not modify the Python 3.11 model runtime. If the "
            "helper is inaccessible or cannot cover the requested conversion, AutoCF uses "
            "the older robust overlap-derived offset and labels that result approximate. The clipped "
            "converted raster is cached, and the chosen method, fallback reason, and uncertainty are "
            "saved in vertical_datum_provenance.json. AutoCF never silently relabels Source 1."
        ),
    },
    {
        "id": "terrain_source_datums",
        "title": "Built-in terrain source datums",
        "aliases": (
            "what datum is cudem", "what datum is 3dep", "what datum is gebco",
            "what datum is gedtm30", "what datum is copernicus dem",
        ),
        "keywords": ("cudem", "3dep", "gebco", "gedtm30", "copernicus", "datum"),
        "pages": ("terrain",),
        "answer": (
            "For the built-in products, AutoCF treats NOAA CUDEM and USGS 3DEP as NAVD88; "
            "GEDTM30 and Copernicus GLO-30 as EGM2008; and GEBCO as assumed MSL with a warning "
            "that its compiled sources can vary locally. A user raster must declare its native datum."
        ),
    },
    {
        "id": "terrain_date_and_provenance",
        "title": "Terrain age and source provenance",
        "aliases": (
            "is terrain historical", "terrain acquisition date", "which dem filled this cell",
            "terrain source provenance",
        ),
        "keywords": ("terrain", "date", "historical", "provenance", "source", "cell"),
        "pages": ("terrain", "results"),
        "answer": (
            "Enter an acquisition date when it is known. AutoCF warns if it is later than the modeled "
            "event, because that is modern terrain rather than a historical reconstruction. Product "
            "edition years are not assumed to be acquisition dates. After preparation, the simulation "
            "reports include a cell-level source map: 1 means Source 1, 2 means Source 2, and 0 means no data."
        ),
    },
)

FAQS += (
    {
        "id": "terrain_order",
        "title": "Terrain-source precedence",
        "aliases": ("terrain source order", "which terrain source wins", "topobathy precedence"),
        "keywords": ("terrain", "topobathy", "source", "precedence", "gapfill"),
        "pages": ("terrain",),
        "answer": (
            "Terrain Source 1 has priority where it contains valid elevations. Source 2 "
            "fills remaining gaps subject to the configured valid-elevation limits. "
            "Preparing terrain again invalidates dependent model state after a source change."
        ),
    },
    {
        "id": "surface",
        "title": "Roughness and infiltration",
        "aliases": ("what is roughness", "how is manning selected", "configure infiltration"),
        "keywords": ("roughness", "manning", "infiltration", "landcover"),
        "pages": ("surface",),
        "answer": (
            "Roughness converts land-cover classes into Manning coefficients used by SFINCS. "
            "Infiltration removes water according to the selected settings. Prepare the "
            "surface layers again after changing either source."
        ),
    },
)

FAQS += (
    {
        "id": "forcing",
        "title": "Forcing setup",
        "aliases": ("set forcing", "what forcing do i need", "prepare forcing"),
        "keywords": ("forcing", "rain", "wind", "pressure", "ocean", "river"),
        "pages": ("forcing",),
        "answer": (
            "Configure precipitation, wind, pressure, ocean boundary, and river discharge "
            "as required by the event. Preview atmospheric forcing and review validation "
            "messages before building."
        ),
    },
)

FAQS += (
    {
        "id": "build_run",
        "title": "Build versus run",
        "aliases": ("difference between build and run", "build or run", "what does build do"),
        "keywords": ("build", "run", "difference", "execute"),
        "pages": ("run",),
        "answer": (
            "Build prepares inputs and creates the SFINCS model. Run executes the prepared "
            "model. The desktop Run action reuses valid stages and rebuilds stale or missing "
            "stages before simulation."
        ),
    },
)

FAQS += (
    {
        "id": "results_view",
        "title": "Results page organization",
        "aliases": ("what is in results", "results page", "where are my results", "duplicate results"),
        "keywords": ("results", "files", "duplicate", "formats"),
        "pages": ("results",),
        "answer": (
            "Results shows curated PNG or JPG, CSV, and JSON products grouped by workflow. "
            "Files with the same stem are one product. NetCDF, TIFF, cache, log, and "
            "scenario files remain on disk but are hidden from this view."
        ),
    },
    {
        "id": "qwen_optional",
        "title": "Optional local Qwen model",
        "aliases": ("do i need qwen", "what does qwen do", "is qwen optional", "without qwen"),
        "keywords": ("qwen", "optional", "model", "download"),
        "pages": (),
        "answer": (
            "Qwen is optional. Without it, Riva still answers curated AutoCF questions and "
            "interprets saved result tables deterministically. When installed, Qwen turns "
            "selected technical documentation into more natural wording."
        ),
    },
)

FAQS += (
    {
        "id": "rmse",
        "title": "RMSE interpretation",
        "aliases": ("what is rmse", "what does rmse mean", "explain rmse"),
        "keywords": ("rmse", "root", "mean", "square", "error"),
        "pages": ("evaluation", "results"),
        "answer": (
            "RMSE is the root mean squared difference between modeled and observed values. "
            "It is measured in metres here, penalizes large errors more than MAE, and is "
            "better when lower. Check bias, correlation, sample count, datum mode, and plots too."
        ),
    },
    {
        "id": "bias",
        "title": "Bias interpretation",
        "aliases": ("what is bias", "what does bias mean", "positive bias", "negative bias"),
        "keywords": ("bias", "overpredict", "underpredict"),
        "pages": ("evaluation", "results"),
        "answer": (
            "Bias is mean modeled minus observed error. Positive bias means average "
            "overprediction; negative bias means average underprediction. Bias near zero alone does not "
            "prove good timing or small individual errors."
        ),
    },
)

FAQS += (
    {
        "id": "shapley",
        "title": "Shapley attribution",
        "aliases": ("what is shapley", "explain shapley", "shapley value"),
        "keywords": ("shapley", "driver", "allocation", "increment"),
        "pages": ("analysis", "results"),
        "answer": (
            "Shapley attribution allocates FULL minus NONE among coastal, precipitation, "
            "and river drivers while accounting for interactions. The three values should "
            "sum to FULL minus NONE; they are not absolute FULL-scenario totals."
        ),
    },
    {
        "id": "dis",
        "title": "Driver Impact Shift",
        "aliases": ("what is dis", "what is dsi", "explain dis", "driver impact shift"),
        "keywords": ("dis", "dsi", "driver", "impact", "shift"),
        "pages": ("analysis", "results"),
        "answer": (
            "AutoCF reports DIS, or Driver Impact Shift. It is a driver's impact share minus "
            "its flooded-area share in percentage points. Positive is consequence-heavy; "
            "negative is footprint-heavy."
        ),
    },
)

FAQS += (
    {
        "id": "interaction",
        "title": "Factorial interactions",
        "aliases": ("what is interaction", "factorial interaction", "driver synergy"),
        "keywords": ("interaction", "factorial", "synergy", "antagonistic"),
        "pages": ("analysis", "results"),
        "answer": (
            "Factorial interaction terms measure non-additive behavior between drivers. "
            "Positive terms are synergistic and negative terms are antagonistic for the "
            "stated metric and threshold."
        ),
    },
    {
        "id": "amplification",
        "title": "Compound amplification",
        "aliases": ("what is compound amplification", "explain amplification"),
        "keywords": ("compound", "amplification", "combined", "driver"),
        "pages": ("analysis", "results"),
        "answer": (
            "Compound amplification is FULL depth minus the maximum depth from any "
            "single-driver scenario. Positive values show where the combined event exceeds "
            "every single driver. Use thresholded areas to avoid emphasizing tiny differences."
        ),
    },
)

PAGE_ANSWERS = {
    "settings": "Configure the local execution backend, CPU use, application folders, and optional software. Check system status before running a model.",
    "project": "Set the project name and run/cache locations, save or import YAML, and review configuration validity.",
    "event": "Choose the event and simulation period. New models begin here after their working directory is selected.",
    "study": "Define and save the computational domain using a boundary, drawing, or selected HUC polygons.",
    "terrain": "Choose terrain sources and precedence, then prepare and inspect the clipped topobathy preview.",
    "surface": "Configure land-cover roughness and infiltration, then prepare the surface rasters.",
    "forcing": "Configure precipitation, wind, pressure, ocean, and river inputs and inspect forcing previews.",
    "sfincs": "Review the hydraulic mode, grid, output intervals, and advanced SFINCS parameters before execution.",
    "run": "Build the prepared SFINCS workspace and run the model. Final validation is enforced here.",
    "evaluation": "Compare modeled outputs with observations and inspect station metrics, flood maps, and plots.",
    "analysis": "Prepare factorial scenarios, run missing simulations, generate attribution reports, or run single-model exposure analysis.",
    "results": "Browse curated figures, tables, and JSON reports; technical model and scenario files are intentionally hidden.",
}


def _rule_database_path() -> Path:
    """Use the editable package database when running source or the desktop EXE."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "windows" / "riva_qa_database.json")
    candidates.append(Path(__file__).with_name("riva_qa_database.json"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


RULE_DATABASE_PATH = _rule_database_path()
RULE_DATABASE: dict[str, Any] = {}
RULE_DATABASE_ERROR = ""
REFERENCES: dict[str, dict[str, Any]] = {}
CASE_STUDIES: tuple[dict[str, Any], ...] = ()


def _load_rule_database(path: Path = RULE_DATABASE_PATH) -> dict[str, Any]:
    """Load and minimally validate the portable Riva Q&A database."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported or missing Riva database schema_version")
    for name in ("references", "questions", "case_studies"):
        if not isinstance(document.get(name), list):
            raise ValueError(f"Riva database field {name!r} must be a list")
    reference_ids = {
        str(item.get("id", "")) for item in document["references"]
        if isinstance(item, dict)
    }
    if "" in reference_ids:
        raise ValueError("every Riva reference needs an id")
    for question in document["questions"]:
        if not isinstance(question, dict):
            raise ValueError("every Riva question must be an object")
        required = ("id", "title", "aliases", "keywords", "answer", "references")
        missing = [name for name in required if not question.get(name)]
        if missing:
            raise ValueError(
                f"Riva question {question.get('id', '<unknown>')!r} is missing {missing}"
            )
        unknown = set(map(str, question["references"])) - reference_ids
        if unknown:
            raise ValueError(
                f"Riva question {question['id']!r} has unknown references {sorted(unknown)}"
            )
    return document


def _database_faqs(document: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    references = {str(item["id"]): item for item in document["references"]}
    output: list[dict[str, Any]] = []
    for item in document["questions"]:
        cited = [references[str(ref_id)] for ref_id in item["references"]]
        evidence = "; ".join(
            f"{ref['citation']} ({ref['url']})" for ref in cited
        )
        answer = str(item["answer"]).strip() + "\n\nEvidence: " + evidence
        output.append({
            "id": str(item["id"]),
            "title": str(item["title"]),
            "aliases": tuple(map(str, item["aliases"])),
            "keywords": tuple(map(str, item["keywords"])),
            "pages": tuple(map(str, item.get("pages", ()))),
            "answer": answer,
            "source": str(cited[0]["url"]),
            "certainty": str(item.get("certainty", "")),
            "reference_ids": tuple(map(str, item["references"])),
        })
    return tuple(output)


try:
    RULE_DATABASE = _load_rule_database()
    REFERENCES = {
        str(item["id"]): item for item in RULE_DATABASE["references"]
    }
    CASE_STUDIES = tuple(RULE_DATABASE["case_studies"])
    FAQS += _database_faqs(RULE_DATABASE)
except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
    # Preserve Riva's original deterministic answers even if the database
    # file is missing or damaged.
    RULE_DATABASE_ERROR = str(exc)
