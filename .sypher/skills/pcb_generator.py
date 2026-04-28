#!/usr/bin/env python3
"""
pcb_generator.py — SYPHER PCB Forge
Advanced offline Code-to-Copper pipeline. Zero external APIs.

Pipeline:
  1. Execute SKiDL script (file or inline) → KiCad Netlist (.net)
  2. Parse netlist (XML or S-expression) → .kicad_pcb board file
  3. Auto-route with Freerouting (headless, auto-discovered)
  4. Export 3D STEP model via kicad-cli
  5. Export 2D SVG render via kicad-cli
  6. Emit JSON manifest to stdout

Usage:
  python3 pcb_generator.py --skidl circuit.py --output ./build
  python3 pcb_generator.py --skidl-code "$(cat circuit.py)" --output ./build
  python3 pcb_generator.py --skidl circuit.py --output ./build --freerouting ~/freerouting.jar
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Pin:
    number: str
    net: str = ""


@dataclass
class Component:
    ref: str
    value: str
    footprint: str
    pins: list[Pin] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return re.match(r"[A-Za-z]+", self.ref).group(0).upper()

    @property
    def pad_count(self) -> int:
        if self.pins:
            return len(self.pins)
        return _default_pad_count(self.prefix)


@dataclass
class Net:
    number: int
    name: str
    nodes: list[tuple[str, str]] = field(default_factory=list)  # (ref, pin)


def _default_pad_count(prefix: str) -> int:
    return {"R": 2, "C": 2, "L": 2, "D": 2, "Q": 3, "J": 2, "SW": 2,
            "U": 8, "IC": 8, "Y": 2, "F": 2, "LED": 2}.get(prefix, 2)


# ── Step 1: SKiDL execution ───────────────────────────────────────────────────

def run_skidl(script_path: Optional[Path], inline_code: Optional[str],
              output_dir: Path) -> Path:
    """Execute SKiDL (file or inline string); return path to generated .net file."""
    _log("Running SKiDL netlist generator...")

    env = os.environ.copy()
    # Ensure SKiDL can find KiCad libraries if installed
    for kicad_share in ["/usr/share/kicad", "/usr/local/share/kicad"]:
        if Path(kicad_share).exists():
            env.setdefault("KICAD_SYMBOL_DIR", f"{kicad_share}/symbols")
            env.setdefault("KICAD_FOOTPRINT_DIR", f"{kicad_share}/footprints")
            break

    if inline_code is not None:
        src_file = output_dir / "_sypher_skidl_circuit.py"
        src_file.write_text(inline_code)
        script_path = src_file

    if script_path is None or not script_path.exists():
        raise FileNotFoundError(f"SKiDL script not found: {script_path}")

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(output_dir),
        capture_output=True, text=True, env=env, timeout=120,
    )

    if result.returncode != 0:
        # Surface the actual SKiDL traceback
        raise RuntimeError(
            f"SKiDL execution failed (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout[-1000:]}\n"
            f"STDERR:\n{result.stderr[-2000:]}"
        )

    # SKiDL writes <circuit_name>.net to CWD — find the newest one
    nets = sorted(output_dir.glob("*.net"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not nets:
        raise FileNotFoundError(
            "SKiDL ran without error but produced no .net file. "
            "Ensure your SKiDL script calls generate_netlist() or skidl.ERC()."
        )

    netlist = nets[0]
    _log(f"Netlist generated: {netlist.name}  ({netlist.stat().st_size} bytes)")
    return netlist


# ── Step 2a: Netlist parsing ──────────────────────────────────────────────────

def parse_netlist(netlist: Path) -> tuple[list[Component], list[Net]]:
    """Parse KiCad netlist — handles both XML and S-expression formats."""
    text = netlist.read_text(errors="replace").strip()

    if text.startswith("<?xml") or text.startswith("<export"):
        return _parse_xml_netlist(text)
    else:
        return _parse_sexpr_netlist(text)


def _parse_xml_netlist(text: str) -> tuple[list[Component], list[Net]]:
    """Parse KiCad XML netlist (standard SKiDL output format)."""
    root = ET.fromstring(text)
    ns = {"": ""}  # KiCad XML has no namespace

    # Build net name lookup: code → name
    net_name: dict[str, str] = {}
    net_list: list[Net] = []
    for i, n in enumerate(root.findall(".//nets/net"), start=1):
        code = n.get("code", str(i))
        name = n.get("name", f"NET_{code}")
        net_name[code] = name
        nodes = [(nd.get("ref", ""), nd.get("pin", ""))
                 for nd in n.findall("node")]
        net_list.append(Net(number=i, name=name, nodes=nodes))

    # Build component list with pin→net assignments
    pin_to_net: dict[tuple[str, str], str] = {}
    for net in net_list:
        for ref, pin in net.nodes:
            pin_to_net[(ref, pin)] = net.name

    components: list[Component] = []
    for comp in root.findall(".//components/comp"):
        ref = comp.get("ref", "?")
        value = (comp.findtext("value") or "").strip()
        footprint = (comp.findtext("footprint") or "").strip()

        pins: list[Pin] = []
        for p in comp.findall(".//pin"):
            num = p.get("num", "")
            pins.append(Pin(number=num, net=pin_to_net.get((ref, num), "")))

        # If no explicit pins, infer from net nodes
        if not pins:
            for net in net_list:
                for r, pnum in net.nodes:
                    if r == ref:
                        pins.append(Pin(number=pnum, net=net.name))
            pins.sort(key=lambda p: _pin_sort_key(p.number))

        components.append(Component(ref=ref, value=value,
                                    footprint=footprint, pins=pins))

    return components, net_list


def _parse_sexpr_netlist(text: str) -> tuple[list[Component], list[Net]]:
    """Fallback parser for KiCad S-expression netlist format."""
    components: list[Component] = []
    net_list: list[Net] = []

    # Extract components
    for m in re.finditer(
        r'\(comp\s+\(ref\s+"?([^")\s]+)"?\)'
        r'(?:.*?\(value\s+"?([^")\s]*)"?\))?'
        r'(?:.*?\(footprint\s+"?([^")\s]*)"?\))?',
        text, re.DOTALL
    ):
        components.append(Component(
            ref=m.group(1),
            value=m.group(2) or "",
            footprint=m.group(3) or "",
        ))

    # Extract nets
    for i, m in enumerate(
        re.finditer(r'\(net\s+\(code\s+"?(\d+)"?\)\s+\(name\s+"?([^")\s]*)"?\)', text),
        start=1
    ):
        net_list.append(Net(number=int(m.group(1)), name=m.group(2)))

    # Assign pins from node entries
    pin_map: dict[str, list[Pin]] = {c.ref: c.pins for c in components}
    for net in net_list:
        for m in re.finditer(
            rf'net.*?code.*?{net.number}.*?(?:node\s+\(ref\s+"?([^")\s]+)"?\)'
            r'\s+\(pin\s+"?([^")\s]+)"?\))+',
            text, re.DOTALL
        ):
            ref, pin = m.group(1), m.group(2)
            net.nodes.append((ref, pin))
            if ref in pin_map:
                pin_map[ref].append(Pin(number=pin, net=net.name))

    return components, net_list


def _pin_sort_key(num: str) -> tuple:
    parts = re.split(r"(\d+)", num)
    return tuple(int(p) if p.isdigit() else p for p in parts)


# ── Step 2b: KiCad PCB generation ────────────────────────────────────────────

# Net → integer id lookup built during PCB write
_net_ids: dict[str, int] = {}

BOARD_W, BOARD_H = 120.0, 100.0      # mm
GRID = 2.54                           # component placement grid (mm)
COLS = 10                             # components per row


def netlist_to_kicad_pcb(netlist: Path, components: list[Component],
                          nets: list[Net], output_dir: Path) -> Path:
    pcb_path = output_dir / (netlist.stem + ".kicad_pcb")
    _log(f"Generating {pcb_path.name}  "
         f"({len(components)} components, {len(nets)} nets)...")

    global _net_ids
    _net_ids = {n.name: n.number for n in nets}

    lines: list[str] = []
    _w = lines.append

    # ── Header
    _w("(kicad_pcb")
    _w("  (version 20221018)")
    _w("  (generator sypher_forge)")
    _w("")

    # ── General settings
    _w("  (general")
    _w("    (thickness 1.6)")
    _w("    (legacy_teardrops no)")
    _w("  )")
    _w("")
    _w('  (paper "A4")')
    _w("")

    # ── Layer definitions
    _w("  (layers")
    for entry in [
        "(0 \"F.Cu\" signal)",
        "(31 \"B.Cu\" signal)",
        "(32 \"B.Adhes\" user \"B.Adhesive\")",
        "(33 \"F.Adhes\" user \"F.Adhesive\")",
        "(34 \"B.Paste\" user)",
        "(35 \"F.Paste\" user)",
        "(36 \"B.SilkS\" user \"B.Silkscreen\")",
        "(37 \"F.SilkS\" user \"F.Silkscreen\")",
        "(38 \"B.Mask\" user)",
        "(39 \"F.Mask\" user)",
        "(40 \"Dwgs.User\" user \"User.Drawings\")",
        "(44 \"Edge.Cuts\" user)",
        "(46 \"B.Courtyard\" user)",
        "(47 \"F.Courtyard\" user)",
        "(48 \"B.Fab\" user)",
        "(49 \"F.Fab\" user)",
    ]:
        _w(f"    {entry}")
    _w("  )")
    _w("")

    # ── Setup / design rules
    _w("  (setup")
    _w("    (pad_to_mask_clearance 0.1)")
    _w("    (allow_soldermask_bridges_in_footprints no)")
    _w("    (pcbplotparams")
    _w("      (layerselection 0x00010fc_ffffffff)")
    _w("      (outputdirectory \"./\")")
    _w("      (disableapertmacros no)")
    _w("      (usegerberextensions no)")
    _w("      (usegerberattributes yes)")
    _w("      (usegerberadvancedattributes yes)")
    _w("      (creategerberjobfile yes)")
    _w("      (dashed_line_dash_ratio 12.0)")
    _w("      (dashed_line_gap_ratio 3.0)")
    _w("      (svgprecision 4)")
    _w("      (plotframeref no)")
    _w("      (viasonmask no)")
    _w("      (mode 1)")
    _w("      (useauxorigin no)")
    _w("      (hpglpennumber 1)")
    _w("      (hpglpenspeed 20)")
    _w("      (hpglpendiameter 15.0)")
    _w("      (pdf_front_fp_property_popups yes)")
    _w("      (pdf_back_fp_property_popups yes)")
    _w("      (dxfpolygonmode yes)")
    _w("      (dxfimperialunits yes)")
    _w("      (dxfusepcbnewfont yes)")
    _w("      (psnegative no)")
    _w("      (psa4output no)")
    _w("      (plotreference yes)")
    _w("      (plotvalue yes)")
    _w("      (plotfptext yes)")
    _w("      (plotinvisibletext no)")
    _w("      (sketchpadsonfab no)")
    _w("      (subtractmaskfromsilk no)")
    _w("      (outputformat 1)")
    _w("      (mirror no)")
    _w("      (drillshape 0)")
    _w("      (scaleselection 1)")
    _w('      (outputdirectory "./")')
    _w("    )")
    _w("  )")
    _w("")

    # ── Net declarations
    _w('  (net (number 0) (name ""))')
    for net in nets:
        _w(f'  (net (number {net.number}) (name "{_esc(net.name)}"))')
    _w("")

    # ── Footprints
    for idx, comp in enumerate(components):
        col = idx % COLS
        row = idx // COLS
        x = round(10.0 + col * 12.0, 4)
        y = round(10.0 + row * 12.0, 4)
        _w(_footprint_sexpr(comp, x, y))

    # ── Board outline
    _w(f"  (gr_rect")
    _w(f"    (start 0 0)")
    _w(f"    (end {BOARD_W} {BOARD_H})")
    _w(f'    (layer "Edge.Cuts")')
    _w(f"    (width 0.05)")
    _w(f"  )")
    _w("")

    _w(")")  # close kicad_pcb

    pcb_path.write_text("\n".join(lines) + "\n")
    _log(f"PCB written: {pcb_path}  ({pcb_path.stat().st_size} bytes)")
    return pcb_path


def _footprint_sexpr(comp: Component, x: float, y: float) -> str:
    """Generate a valid kicad_pcb footprint S-expression for one component."""
    lines = []
    _w = lines.append

    fp_name = comp.footprint or f"Sypher:{comp.prefix}_Generic"
    pad_count = comp.pad_count
    prefix = comp.prefix

    _w(f'  (footprint "{_esc(fp_name)}" (layer "F.Cu")')
    _w(f"    (at {x} {y})")
    _w(f'    (descr "{_esc(comp.value)}")')
    _w(f'    (tags "sypher_generated")')
    _w(f'    (property "Reference" "{_esc(comp.ref)}" (at 0 -2.5) (layer "F.SilkS"))')
    _w(f'    (property "Value" "{_esc(comp.value)}" (at 0 2.5) (layer "F.Fab"))')

    # Courtyard
    cw = max(pad_count * 1.5, 3.0)
    ch = 2.5
    _w(f'    (fp_rect (start {-cw/2:.2f} {-ch/2:.2f}) (end {cw/2:.2f} {ch/2:.2f})'
       f' (layer "F.Courtyard") (width 0.05))')

    # Pads
    if prefix in ("R", "C", "L", "D", "LED", "F") and pad_count == 2:
        # SMD 0805 style — two pads, 1.8 mm apart
        for i, pin in enumerate(_get_pins(comp, 2)):
            px = -0.9 + i * 1.8
            net_clause = _net_clause(pin.net)
            _w(f'    (pad "{pin.number}" smd rect (at {px:.2f} 0) (size 1.2 1.5)'
               f' (layers "F.Cu" "F.Paste" "F.Mask"){net_clause})')

    elif prefix == "Q" and pad_count == 3:
        # SOT-23 style
        positions = [(-1.27, 0.65), (1.27, 0.65), (0, -1.15)]
        for i, (pin, (px, py)) in enumerate(zip(_get_pins(comp, 3), positions)):
            net_clause = _net_clause(pin.net)
            _w(f'    (pad "{pin.number}" smd roundrect (at {px:.2f} {py:.2f}) (size 0.9 1.3)'
               f' (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25){net_clause})')

    else:
        # Through-hole DIP / generic: pads on 2.54 mm pitch
        pitch = GRID
        total_w = (pad_count - 1) * pitch
        for i, pin in enumerate(_get_pins(comp, pad_count)):
            px = round(-total_w / 2 + i * pitch, 4)
            net_clause = _net_clause(pin.net)
            shape = "circle" if i == 0 else "oval"
            _w(f'    (pad "{pin.number}" thru_hole {shape} (at {px} 0) (size 1.6 1.6)'
               f' (drill 0.8) (layers "*.Cu" "*.Mask"){net_clause})')

    _w("  )")  # close footprint
    return "\n".join(lines)


def _get_pins(comp: Component, count: int) -> list[Pin]:
    if comp.pins:
        return comp.pins[:count]
    return [Pin(number=str(i + 1)) for i in range(count)]


def _net_clause(net_name: str) -> str:
    if not net_name or net_name not in _net_ids:
        return ""
    return f' (net {_net_ids[net_name]} "{_esc(net_name)}")'


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Step 3: Freerouting ───────────────────────────────────────────────────────

FREEROUTING_SEARCH = [
    Path("freerouting.jar"),
    Path.home() / "freerouting.jar",
    Path.home() / ".local/share/freerouting/freerouting.jar",
    Path("/usr/local/share/freerouting/freerouting.jar"),
    Path("/opt/freerouting/freerouting.jar"),
]


def autoroute(pcb_path: Path, freerouting_jar: Optional[Path],
              output_dir: Path) -> tuple[Path, str]:
    """
    Attempt headless autorouting with Freerouting.
    Returns (resulting_pcb_path, routing_status).
    """
    jar = freerouting_jar or _find_freerouting()

    if jar is None:
        _log("Freerouting not found — board will be unrouted. "
             "Place freerouting.jar in the project root or ~/freerouting.jar")
        return pcb_path, "unrouted"

    if not _java_available():
        _log("java not found on PATH — skipping Freerouting.")
        return pcb_path, "unrouted (java missing)"

    _log(f"Auto-routing with Freerouting: {jar}")
    routed = output_dir / (pcb_path.stem + "_routed.kicad_pcb")

    # Freerouting 1.7+ supports KiCad PCB format directly
    result = subprocess.run(
        [
            "java", "-jar", str(jar),
            "-board_file", str(pcb_path),
            "-output", str(routed),
            "-mp", "5",          # max passes
            "-host", "SYPHER",
        ],
        capture_output=True, text=True, timeout=600,
    )

    if result.returncode == 0 and routed.exists():
        _log(f"Routing complete: {routed.name}")
        return routed, "routed"

    # Fallback: Specctra DSN/SES flow (older Freerouting)
    _log("Direct KiCad routing failed — trying DSN/SES flow...")
    return _autoroute_dsn_ses(pcb_path, jar, output_dir)


def _autoroute_dsn_ses(pcb_path: Path, jar: Path,
                       output_dir: Path) -> tuple[Path, str]:
    """Export DSN → Freerouting → import SES back into PCB."""
    dsn = output_dir / (pcb_path.stem + ".dsn")
    ses = output_dir / (pcb_path.stem + ".ses")
    routed = output_dir / (pcb_path.stem + "_routed.kicad_pcb")

    # Export DSN via kicad-cli (requires kicad 7+)
    r = subprocess.run(
        ["kicad-cli", "pcb", "export", "specctradsn",
         "--output", str(dsn), str(pcb_path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0 or not dsn.exists():
        _log("kicad-cli DSN export failed — board will be unrouted.")
        return pcb_path, "unrouted"

    r = subprocess.run(
        ["java", "-jar", str(jar),
         "-de", str(dsn), "-do", str(ses), "-mp", "5"],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0 or not ses.exists():
        _log("Freerouting DSN routing failed — board will be unrouted.")
        return pcb_path, "unrouted"

    # Import SES back via kicad-cli
    r = subprocess.run(
        ["kicad-cli", "pcb", "import", "specctrases",
         "--input", str(ses), "--output", str(routed), str(pcb_path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode == 0 and routed.exists():
        _log(f"DSN/SES routing complete: {routed.name}")
        return routed, "routed (DSN/SES)"

    _log("SES import failed — board will be unrouted.")
    return pcb_path, "unrouted"


def _find_freerouting() -> Optional[Path]:
    for p in FREEROUTING_SEARCH:
        if p.exists():
            _log(f"Found Freerouting: {p}")
            return p
    return None


def _java_available() -> bool:
    try:
        subprocess.run(["java", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── Step 4: kicad-cli exports ─────────────────────────────────────────────────

def export_step(pcb_path: Path, output_dir: Path) -> Path:
    step = output_dir / (pcb_path.stem + ".step")
    _log("Exporting STEP model...")
    r = subprocess.run(
        ["kicad-cli", "pcb", "export", "step",
         "--subst-models",
         "--output", str(step),
         str(pcb_path)],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli STEP export failed:\n{r.stderr[-500:]}")
    if not step.exists():
        raise FileNotFoundError("kicad-cli reported success but .step file not found.")
    _log(f"STEP exported: {step.name}  ({step.stat().st_size} bytes)")
    return step


def export_svg(pcb_path: Path, output_dir: Path) -> Path:
    svg = output_dir / (pcb_path.stem + ".svg")
    _log("Exporting SVG render...")
    r = subprocess.run(
        ["kicad-cli", "pcb", "export", "svg",
         "--layers", "F.Cu,B.Cu,Edge.Cuts,F.SilkS,B.SilkS,F.Courtyard",
         "--output", str(svg),
         str(pcb_path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli SVG export failed:\n{r.stderr[-500:]}")
    if not svg.exists():
        raise FileNotFoundError("kicad-cli reported success but .svg file not found.")
    _log(f"SVG exported: {svg.name}  ({svg.stat().st_size} bytes)")
    return svg


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[SYPHER PCB] {msg}", file=sys.stderr, flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="SYPHER PCB Forge — offline Code-to-Copper pipeline"
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--skidl", type=Path, metavar="FILE",
                     help="Path to a SKiDL Python script")
    src.add_argument("--skidl-code", metavar="CODE",
                     help="Inline SKiDL source code (for AI-generated circuits)")
    src.add_argument("--netlist", type=Path, metavar="FILE",
                     help="Skip SKiDL — start from an existing .net file")

    ap.add_argument("--output", required=True, type=Path,
                    help="Output directory (created if absent)")
    ap.add_argument("--freerouting", type=Path, default=None,
                    help="Explicit path to freerouting.jar")
    ap.add_argument("--no-3d", action="store_true",
                    help="Skip STEP export (faster, no 3D models needed)")
    ap.add_argument("--board-width", type=float, default=BOARD_W,
                    help="Board width in mm (default 120)")
    ap.add_argument("--board-height", type=float, default=BOARD_H,
                    help="Board height in mm (default 100)")
    args = ap.parse_args()

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {"status": "ok", "warnings": []}

    try:
        # Step 1 — netlist
        if args.netlist:
            netlist = args.netlist
            _log(f"Using existing netlist: {netlist}")
        else:
            netlist = run_skidl(args.skidl, args.skidl_code, output_dir)
        result["netlist"] = str(netlist)

        # Step 2 — parse + PCB
        components, nets = parse_netlist(netlist)
        result["component_count"] = len(components)
        result["net_count"] = len(nets)

        if not components:
            result["warnings"].append("No components found in netlist — PCB will be empty.")

        pcb = netlist_to_kicad_pcb(netlist, components, nets, output_dir)
        result["kicad_pcb"] = str(pcb)

        # Step 3 — autoroute
        pcb, routing_status = autoroute(pcb, args.freerouting, output_dir)
        result["routing_status"] = routing_status
        result["kicad_pcb"] = str(pcb)  # update to routed path if changed

        # Step 4 — exports
        if not args.no_3d:
            try:
                step = export_step(pcb, output_dir)
                result["step_model"] = str(step)
            except Exception as e:
                result["warnings"].append(f"STEP export failed: {e}")
                result["step_model"] = None

        try:
            svg = export_svg(pcb, output_dir)
            result["svg_render"] = str(svg)
        except Exception as e:
            result["warnings"].append(f"SVG export failed: {e}")
            result["svg_render"] = None

    except Exception as e:
        result["status"] = "error"
        result["message"] = str(e)
        print(json.dumps(result, separators=(",", ":")))
        sys.exit(1)

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
