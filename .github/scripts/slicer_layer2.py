#!/usr/bin/env python3
"""
Layer 2 — Literature-guided deep morphometric analysis.

Takes Layer 1 outputs (mesh metrics, landmarks, screenshots), searches
the literature for landmark protocols and measurement standards, then
uses the LLM to generate a custom Slicer analysis script that runs
publication-quality measurements informed by real papers.

Usage:
    python3 slicer_layer2.py \
        --layer1-json /tmp/slicer_deep_analysis/deep_analysis.json \
        --research-topic "optic nerve morphology in chameleons" \
        --output-dir /tmp/slicer_layer2
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# Add script dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from literature_search import search_literature, Paper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("Layer2")

SLICER_BIN = "/Applications/Slicer.app/Contents/MacOS/Slicer"


def _load_dotenv():
    search = Path(__file__).resolve().parent
    for _ in range(5):
        env_file = search / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
            return
        search = search.parent


_load_dotenv()


# ---------------------------------------------------------------------------
# LLM helper (standalone, doesn't need Slicer)
# ---------------------------------------------------------------------------


def _call_llm(system: str, user: str, max_tokens: int = 4000, json_mode: bool = False) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4")
    if not api_key:
        log.warning("OPENAI_API_KEY not set")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai not installed")
        return None

    client = OpenAI(api_key=api_key)
    kwargs = {"model": model, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]}

    is_reasoning = model.lower().startswith(("o1", "o3", "o4", "gpt-5"))
    if is_reasoning:
        kwargs["max_completion_tokens"] = max_tokens
        if json_mode:
            kwargs["messages"][0]["content"] += "\nRespond with valid JSON only."
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = 0.4
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        return (content or "").strip()
    except Exception as exc:
        log.error("LLM call failed: %s", exc)
        return None


def _call_vision(images: list[Path], prompt: str, max_tokens: int = 2000) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(api_key=api_key)
    content = [{"type": "text", "text": prompt}]
    for img_path in images[:6]:
        b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    try:
        resp = client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.error("Vision call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Step 1: Literature search
# ---------------------------------------------------------------------------


def find_relevant_papers(layer1: dict, research_topic: str) -> list[Paper]:
    specimen = layer1.get("specimen", {})
    taxon = specimen.get("taxonomy", "")
    element = specimen.get("element", "")

    anatomy_terms = ["skull", "cranial", "morphology"]
    if "orbit" in element.lower() or "optic" in research_topic.lower():
        anatomy_terms.extend(["optic nerve", "orbit", "optic foramen"])
    if "landmark" in research_topic.lower():
        anatomy_terms.append("landmarks")

    log.info("Searching literature for %s + %s", taxon, anatomy_terms)

    papers = search_literature(
        taxon=taxon,
        anatomy_terms=anatomy_terms,
        research_topic=research_topic,
        layer1_data=layer1,
        max_pubmed=15,
        max_scholar=8,
    )
    return papers


# ---------------------------------------------------------------------------
# Step 2: LLM reads literature and designs measurement protocol
# ---------------------------------------------------------------------------

_PROTOCOL_SYSTEM = """You are a morphometrics research assistant. Given:
1. Layer 1 analysis of a 3D specimen mesh (dimensions, landmarks, screenshots)
2. Relevant papers from the literature

Your job is to design a detailed measurement protocol for deeper analysis.

Return a JSON object with:
{
  "protocol_name": "short name for this protocol",
  "rationale": "why these measurements matter for the research topic",
  "landmarks": [
    {
      "name": "landmark name (anatomical)",
      "description": "how to identify this point on the mesh",
      "approximate_coords": [x, y, z],
      "source_paper": "Author et al. (Year)",
      "anatomical_region": "e.g. orbital, neurocranial, dermatocranial"
    }
  ],
  "measurements": [
    {
      "name": "measurement name",
      "type": "distance|angle|ratio|area",
      "landmarks_used": ["landmark1", "landmark2"],
      "description": "what this measures anatomically",
      "source_paper": "Author et al. (Year)",
      "expected_value": "if reported in literature, the value for this species"
    }
  ],
  "slicer_analyses": [
    {
      "name": "analysis name",
      "module": "SlicerMorph module or VTK filter to use",
      "description": "what to compute",
      "parameters": {}
    }
  ],
  "research_questions": [
    "specific questions these measurements can answer"
  ]
}"""


def design_measurement_protocol(layer1: dict, papers: list[Paper], research_topic: str) -> dict:
    layer1_summary = json.dumps({
        "specimen": layer1.get("specimen"),
        "distances": layer1.get("distances"),
        "shape_ratios": layer1.get("shape_ratios"),
        "landmarks": layer1.get("landmarks"),
        "surface_complexity": layer1.get("surface_complexity"),
        "curvature": layer1.get("curvature"),
        "bilateral_asymmetry": layer1.get("bilateral_asymmetry"),
        "pca_shape": {k: v for k, v in layer1.get("pca_shape", {}).items() if k != "principal_axes"},
    }, indent=2)

    paper_summaries = []
    for p in papers[:15]:
        entry = f"- {p.authors[:40]} ({p.year}) \"{p.title}\""
        if p.abstract:
            entry += f"\n  Abstract: {p.abstract[:300]}"
        if p.keywords:
            entry += f"\n  Keywords: {', '.join(p.keywords[:5])}"
        paper_summaries.append(entry)
    papers_text = "\n".join(paper_summaries)

    user_msg = (
        f"Research topic: {research_topic}\n\n"
        f"Layer 1 analysis:\n{layer1_summary}\n\n"
        f"Relevant papers:\n{papers_text}\n\n"
        f"Design a measurement protocol that builds on Layer 1 and is "
        f"informed by the literature. Include specific anatomical landmarks, "
        f"distances, angles, and SlicerMorph analyses."
    )

    log.info("Designing measurement protocol with LLM...")
    content = _call_llm(_PROTOCOL_SYSTEM, user_msg, max_tokens=4000, json_mode=True)

    if content:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            for o, c in [("{", "}"), ("[", "]")]:
                s, e = content.find(o), content.rfind(c)
                if s != -1 and e > s:
                    try:
                        return json.loads(content[s:e + 1])
                    except json.JSONDecodeError:
                        pass
            log.warning("Could not parse protocol JSON")

    return _fallback_protocol(layer1)


def _fallback_protocol(layer1: dict) -> dict:
    """Hardcoded fallback when LLM is unavailable."""
    existing_lm = {lm["name"]: lm["position"] for lm in layer1.get("landmarks", [])}
    return {
        "protocol_name": "Basic cranial morphometrics",
        "rationale": "Standard measurements for comparative skull analysis",
        "landmarks": [
            {"name": n, "approximate_coords": c, "description": "From Layer 1",
             "source_paper": "Auto-detected", "anatomical_region": "cranial"}
            for n, c in existing_lm.items()
        ],
        "measurements": [
            {"name": "skull_length", "type": "distance", "landmarks_used": ["snout_tip", "occipital_condyle"],
             "description": "Maximum anteroposterior length", "source_paper": "Standard"},
            {"name": "skull_width", "type": "distance", "landmarks_used": ["left_lateral_max", "right_lateral_max"],
             "description": "Maximum mediolateral width", "source_paper": "Standard"},
            {"name": "skull_height", "type": "distance", "landmarks_used": ["ventral_margin", "parietal_crest_apex"],
             "description": "Maximum dorsoventral height", "source_paper": "Standard"},
        ],
        "slicer_analyses": [],
        "research_questions": ["How does this specimen compare to published measurements?"],
    }


# ---------------------------------------------------------------------------
# Step 3: Generate Slicer Python script from protocol
# ---------------------------------------------------------------------------

_SLICER_SCRIPT_SYSTEM = """You are a 3D Slicer Python script generator. Given a measurement protocol
(landmarks, distances, analyses), generate a complete Python script that runs inside 3D Slicer.

The script must:
1. Load the mesh file (path provided)
2. Place landmarks at the specified approximate coordinates, refined by finding the closest mesh surface point
3. Compute all specified measurements (distances, angles, ratios)
4. Take screenshots showing the landmarks and measurements
5. Export results as JSON
6. Call slicer.app.exit(0) at the end

Available libraries inside Slicer: slicer, vtk, numpy, json, os
Use renderWindow.SetOffScreenRendering(1) for headless rendering.
The mesh is a PLY file with vertex colors (bone segmentation).

Output ONLY the Python script, no markdown fences or explanation."""


def generate_slicer_script(protocol: dict, ply_path: str, output_dir: str) -> str:
    user_msg = (
        f"Mesh file: {ply_path}\n"
        f"Output directory: {output_dir}\n\n"
        f"Protocol:\n{json.dumps(protocol, indent=2)}\n\n"
        f"Generate the Slicer Python script."
    )

    content = _call_llm(_SLICER_SCRIPT_SYSTEM, user_msg, max_tokens=6000)
    if content:
        # Strip markdown fences if present
        if "```" in content:
            parts = content.split("```")
            for part in parts[1::2]:
                if part.startswith("python"):
                    part = part[6:]
                return part.strip()
        return content

    return _fallback_slicer_script(protocol, ply_path, output_dir)


def _fallback_slicer_script(protocol: dict, ply_path: str, output_dir: str) -> str:
    """Generate a basic Slicer script without LLM."""
    landmarks_code = ""
    for lm in protocol.get("landmarks", []):
        coords = lm.get("approximate_coords", [0, 0, 0])
        name = lm["name"]
        landmarks_code += f'    markups.AddControlPoint(vtk.vtkVector3d({coords[0]}, {coords[1]}, {coords[2]}), "{name}")\n'

    measurements_code = ""
    for m in protocol.get("measurements", []):
        if m.get("type") == "distance" and len(m.get("landmarks_used", [])) == 2:
            p1, p2 = m["landmarks_used"]
            measurements_code += f'    d = np.linalg.norm(lm_coords.get("{p1}", np.zeros(3)) - lm_coords.get("{p2}", np.zeros(3)))\n'
            measurements_code += f'    results["{m["name"]}"] = round(float(d), 2)\n'
            measurements_code += f'    print(f"  {m["name"]}: {{d:.2f}} mm")\n'

    return textwrap.dedent(f'''\
        import slicer
        import vtk
        import numpy as np
        import json
        import os

        OUTPUT_DIR = "{output_dir}"
        PLY_PATH = "{ply_path}"
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        print("Layer 2: Literature-guided analysis")
        success, node = slicer.util.loadModel(PLY_PATH, returnNode=True)
        mesh = node.GetMesh()
        bounds = [0]*6
        node.GetBounds(bounds)
        center = [(bounds[0]+bounds[1])/2, (bounds[2]+bounds[3])/2, (bounds[4]+bounds[5])/2]
        print(f"Loaded: {{node.GetName()}} | {{mesh.GetNumberOfPoints():,}} pts")

        markups = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Layer2Landmarks")
        md = markups.GetDisplayNode()
        if md:
            md.SetGlyphScale(2.5)
            md.SetTextScale(3.5)

        # Place landmarks
    {landmarks_code}
        # Build coordinate dict
        lm_coords = {{}}
        for i in range(markups.GetNumberOfControlPoints()):
            name = markups.GetNthControlPointLabel(i)
            pos = [0,0,0]
            markups.GetNthControlPointPosition(i, pos)
            lm_coords[name] = np.array(pos)

        results = {{}}
        print("\\nMeasurements:")
    {measurements_code}
        # Screenshot
        slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUp3DView)
        threeDWidget = slicer.app.layoutManager().threeDWidget(0)
        threeDView = threeDWidget.threeDView()
        rw = threeDView.renderWindow()
        rw.SetOffScreenRendering(1)
        rw.SetSize(1600, 1200)
        renderer = rw.GetRenderers().GetFirstRenderer()
        renderer.SetBackground(0.05, 0.05, 0.1)
        camera = renderer.GetActiveCamera()
        camera.SetPosition(center[0]+80, center[1]-80, center[2]+60)
        camera.SetFocalPoint(*center)
        camera.SetViewUp(0, 0, 1)
        renderer.ResetCameraClippingRange()
        rw.Render()

        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(rw)
        w2i.SetScale(1)
        w2i.ReadFrontBufferOff()
        w2i.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(os.path.join(OUTPUT_DIR, "layer2_landmarks.png"))
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()

        with open(os.path.join(OUTPUT_DIR, "layer2_measurements.json"), "w") as f:
            json.dump(results, f, indent=2)
        print(f"\\nResults saved to {{OUTPUT_DIR}}")
        slicer.app.exit(0)
    ''')


# ---------------------------------------------------------------------------
# Step 4: Run the generated script in Slicer
# ---------------------------------------------------------------------------


def run_slicer_script(script_path: str, timeout: int = 120) -> tuple[bool, str]:
    log.info("Running Slicer script: %s", script_path)
    try:
        result = subprocess.run(
            [SLICER_BIN, "--no-splash", "--python-script", script_path],
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout + result.stderr
        # Filter noise
        clean = "\n".join(
            line for line in output.split("\n")
            if not line.startswith("Error #1") and not line.startswith("libpng")
            and not line.startswith("Switch to module")
        )
        return result.returncode == 0, clean
    except subprocess.TimeoutExpired:
        log.error("Slicer script timed out after %ds", timeout)
        return False, "Timeout"
    except Exception as exc:
        log.error("Slicer execution failed: %s", exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Step 5: Compare results to literature and produce report
# ---------------------------------------------------------------------------


def synthesize_layer2_report(
    layer1: dict,
    protocol: dict,
    slicer_results: dict,
    papers: list[Paper],
    vision_analysis: str,
    research_topic: str,
    output_dir: Path,
) -> str:
    """Use LLM to produce a comprehensive Layer 2 report."""
    system = (
        "You are writing the results section of a morphometric research paper. "
        "Given the specimen analysis, measurement protocol, actual measurements, "
        "relevant literature, and visual analysis of the specimen, produce a "
        "comprehensive Markdown report that:\n"
        "1. Summarizes all measurements with proper anatomical terminology\n"
        "2. Compares to published values where available\n"
        "3. Identifies novel findings\n"
        "4. Suggests next steps (what Layer 3 multi-specimen comparison should do)\n"
        "5. Assesses research suitability for the stated topic"
    )

    paper_refs = "\n".join(
        f"- {p.authors[:30]} ({p.year}) {p.title[:80]}" for p in papers[:10]
    )

    user = (
        f"Research topic: {research_topic}\n\n"
        f"Specimen: {json.dumps(layer1.get('specimen', {}), indent=2)}\n\n"
        f"Protocol: {protocol.get('protocol_name', 'Standard')}\n"
        f"Rationale: {protocol.get('rationale', '')}\n\n"
        f"Measurements:\n{json.dumps(slicer_results, indent=2)}\n\n"
        f"Layer 1 metrics:\n{json.dumps(layer1.get('distances', {}), indent=2)}\n"
        f"Shape ratios: {json.dumps(layer1.get('shape_ratios', {}), indent=2)}\n"
        f"Surface complexity: {json.dumps(layer1.get('surface_complexity', {}), indent=2)}\n\n"
        f"Visual analysis:\n{vision_analysis or 'Not available'}\n\n"
        f"Literature:\n{paper_refs}\n\n"
        f"Research questions: {json.dumps(protocol.get('research_questions', []))}"
    )

    report = _call_llm(system, user, max_tokens=4000)
    if not report:
        report = f"## Layer 2 Report\n\nMeasurements: {json.dumps(slicer_results, indent=2)}"

    report_path = output_dir / "layer2_report.md"
    report_path.write_text(report, encoding="utf-8")
    log.info("Layer 2 report: %s", report_path)
    return report


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def run_layer2(layer1_path: str, research_topic: str, output_dir: str) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # Load Layer 1
    log.info("Loading Layer 1 data: %s", layer1_path)
    layer1 = json.loads(Path(layer1_path).read_text())
    ply_path = layer1.get("specimen", {}).get("name", "")
    # Reconstruct PLY path from Layer 1 data
    ply_search = Path("/Users/m4macmini/Documents/GitHub/Metadata-to-Morphsource-compare/data")
    ply_files = list(ply_search.rglob("*.ply"))
    ply_path = str(ply_files[0]) if ply_files else ""
    log.info("PLY file: %s", ply_path)

    # Step 1: Literature search
    log.info("=" * 60)
    log.info("STEP 1: Literature Search")
    log.info("=" * 60)
    papers = find_relevant_papers(layer1, research_topic)
    log.info("Found %d papers", len(papers))

    papers_json = output / "literature.json"
    papers_json.write_text(json.dumps([p.to_dict() for p in papers], indent=2))

    # Step 2: Design protocol
    log.info("=" * 60)
    log.info("STEP 2: Design Measurement Protocol")
    log.info("=" * 60)
    protocol = design_measurement_protocol(layer1, papers, research_topic)
    log.info("Protocol: %s (%d landmarks, %d measurements)",
             protocol.get("protocol_name", "?"),
             len(protocol.get("landmarks", [])),
             len(protocol.get("measurements", [])))

    protocol_path = output / "protocol.json"
    protocol_path.write_text(json.dumps(protocol, indent=2))

    # Step 3: Generate Slicer script
    log.info("=" * 60)
    log.info("STEP 3: Generate Slicer Script")
    log.info("=" * 60)
    slicer_script = generate_slicer_script(protocol, ply_path, str(output))

    script_path = output / "layer2_slicer_script.py"
    script_path.write_text(slicer_script)
    log.info("Script generated: %s (%d lines)", script_path, slicer_script.count("\n"))

    # Step 4: Execute in Slicer
    log.info("=" * 60)
    log.info("STEP 4: Execute in 3D Slicer")
    log.info("=" * 60)
    success, slicer_output = run_slicer_script(str(script_path), timeout=180)
    log.info("Slicer execution: %s", "SUCCESS" if success else "FAILED")
    if slicer_output:
        for line in slicer_output.strip().split("\n")[-20:]:
            log.info("  Slicer: %s", line)

    (output / "slicer_output.log").write_text(slicer_output)

    # Load Slicer results
    slicer_results = {}
    measurements_file = output / "layer2_measurements.json"
    if measurements_file.exists():
        slicer_results = json.loads(measurements_file.read_text())
        log.info("Slicer measurements: %s", json.dumps(slicer_results, indent=2))

    # Step 5: Vision analysis of screenshots
    log.info("=" * 60)
    log.info("STEP 5: GPT Vision Analysis")
    log.info("=" * 60)
    screenshots = sorted(output.glob("*.png"))
    vision_analysis = ""
    if screenshots:
        vision_analysis = _call_vision(
            screenshots[:6],
            "Analyze these 3D Slicer renderings of a Chamaeleo calyptratus skull. "
            "Identify anatomical features, landmark positions, bone segments, "
            "and any notable morphological characteristics relevant to optic "
            "nerve and orbital morphology research.",
        ) or ""
        log.info("Vision analysis: %d chars", len(vision_analysis))
        (output / "vision_analysis.txt").write_text(vision_analysis)

    # Step 6: Synthesize report
    log.info("=" * 60)
    log.info("STEP 6: Synthesize Report")
    log.info("=" * 60)
    report = synthesize_layer2_report(
        layer1, protocol, slicer_results, papers,
        vision_analysis, research_topic, output,
    )

    # Final output
    final = {
        "layer": 2,
        "research_topic": research_topic,
        "specimen": layer1.get("specimen"),
        "papers_found": len(papers),
        "protocol": protocol,
        "slicer_success": success,
        "measurements": slicer_results,
        "layer1_distances": layer1.get("distances"),
        "shape_ratios": layer1.get("shape_ratios"),
        "screenshots": [str(p) for p in screenshots],
        "report_path": str(output / "layer2_report.md"),
    }
    (output / "layer2_output.json").write_text(json.dumps(final, indent=2))
    log.info("Layer 2 complete. Output: %s", output)

    return final


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Layer 2: Literature-guided deep analysis")
    parser.add_argument("--layer1-json", required=True, help="Path to Layer 1 deep_analysis.json")
    parser.add_argument("--research-topic", default="cranial morphology and optic nerve specialization",
                        help="Research topic for literature search")
    parser.add_argument("--output-dir", default="/tmp/slicer_layer2", help="Output directory")
    args = parser.parse_args()

    run_layer2(args.layer1_json, args.research_topic, args.output_dir)


if __name__ == "__main__":
    main()
