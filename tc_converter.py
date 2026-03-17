#!/usr/bin/env python3
"""
AFL Timecode Converter
Converts between BTGL/BTGR file-elapsed and BCAST countdown references.

Run:  python3 tc_converter.py
Open: http://localhost:8765

To allow others on your network to connect, run:
      python3 tc_converter.py --network
Then share: http://<your-ip>:8765
"""

import os, sys, json, re, subprocess, threading, io, socket, argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

PORT = 8765

# Broadcast clock crop presets (left, top, right, bottom as fraction of frame)
BROADCAST_PRESETS = {
    "fox":   {"name": "FOX Footy",     "crop": (0.115, 0.917, 0.22,  1.0)},
    "seven": {"name": "Seven Network", "crop": (0.41,  0.932, 0.565, 1.0)},
}

OCR_INTERVAL = 8  # seconds between sampled frames during calibration

# ── Global state ──────────────────────────────────────────────────────────────

def _empty_calibration():
    return {"status": "idle", "progress": 0, "total": 0, "message": "", "lookup": []}

state = {
    "media_folder":       str(Path(__file__).parent),
    "broadcast":          "fox",
    "quarter":            None,          # currently selected quarter e.g. "Q1"
    "available_quarters": [],            # e.g. ["Q1","Q2","Q3","Q4"]
    "files":              {},
    "startup_errors":     [],
    "calibrations":       {},            # {quarter: calibration_dict}
    "calibration":        _empty_calibration(),  # always points at current quarter's cal
}

# ── Platform detection ────────────────────────────────────────────────────────

import platform
IS_WINDOWS = platform.system() == "Windows"
IS_MAC     = platform.system() == "Darwin"

# ── System checks ─────────────────────────────────────────────────────────────

def find_executable(name):
    """Search PATH and common install locations for an executable."""
    # Plain name first (works if it's in PATH)
    candidates = [name]
    if IS_MAC:
        candidates += [
            f"/opt/homebrew/bin/{name}",   # Apple Silicon Homebrew
            f"/usr/local/bin/{name}",      # Intel Mac Homebrew
            f"/usr/bin/{name}",
        ]
    elif IS_WINDOWS:
        candidates += [
            # Common Windows install locations
            rf"C:\ffmpeg\bin\{name}.exe",
            rf"C:\Program Files\ffmpeg\bin\{name}.exe",
            rf"C:\Program Files (x86)\ffmpeg\bin\{name}.exe",
            os.path.join(os.environ.get("USERPROFILE",""), "ffmpeg", "bin", f"{name}.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA",""), "Microsoft", "WinGet",
                         "Packages", "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
                         "ffmpeg-*", "bin", f"{name}.exe"),
        ]
    else:  # Linux
        candidates += [f"/usr/bin/{name}", f"/usr/local/bin/{name}"]

    for p in candidates:
        # Skip glob-style paths on Windows
        if "*" in p:
            continue
        try:
            r = subprocess.run([p, "-version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return p
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
    return None

FFPROBE = find_executable("ffprobe")
FFMPEG  = find_executable("ffmpeg")

# On Windows, pytesseract needs to know where tesseract.exe lives
def configure_tesseract_windows():
    if not IS_WINDOWS:
        return
    common_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.environ.get("LOCALAPPDATA",""),
                     "Programs", "Tesseract-OCR", "tesseract.exe"),
    ]
    try:
        import pytesseract
        for p in common_paths:
            if os.path.exists(p):
                pytesseract.pytesseract.tesseract_cmd = p
                return
    except ImportError:
        pass

configure_tesseract_windows()

def _install_instructions():
    """Return platform-appropriate install instructions."""
    if IS_WINDOWS:
        return {
            "ffmpeg": {
                "fix":     "Download ffmpeg from https://www.gyan.dev/ffmpeg/builds/ — extract the zip, then add the 'bin' folder to your Windows PATH. Or run in Command Prompt: winget install Gyan.FFmpeg",
                "fix_cmd": "winget install Gyan.FFmpeg",
            },
            "pillow": {
                "fix":     "Open Command Prompt and run: pip install Pillow",
                "fix_cmd": "pip install Pillow",
            },
            "tesseract": {
                "fix":     "Download the Tesseract installer from https://github.com/UB-Mannheim/tesseract/wiki — run it, then restart this app. Or: winget install UB-Mannheim.TesseractOCR",
                "fix_cmd": "winget install UB-Mannheim.TesseractOCR",
            },
        }
    else:  # Mac / Linux
        pip = "pip3" if IS_MAC else "pip3"
        brew = "brew" if IS_MAC else "apt-get"
        return {
            "ffmpeg": {
                "fix":     f"Open Terminal and run: {'brew install ffmpeg' if IS_MAC else 'sudo apt install ffmpeg'}",
                "fix_cmd": "brew install ffmpeg" if IS_MAC else "sudo apt install ffmpeg",
            },
            "pillow": {
                "fix":     f"Open Terminal and run: {pip} install Pillow",
                "fix_cmd": f"{pip} install Pillow",
            },
            "tesseract": {
                "fix":     f"Open Terminal and run: {'brew install tesseract' if IS_MAC else 'sudo apt install tesseract-ocr'} && {pip} install pytesseract",
                "fix_cmd": f"{'brew install tesseract' if IS_MAC else 'sudo apt install tesseract-ocr'} && {pip} install pytesseract",
            },
        }

def check_dependencies():
    inst   = _install_instructions()
    errors = []
    if not FFPROBE:
        errors.append({
            "type":    "missing_ffprobe",
            "title":   "ffprobe not found",
            "detail":  "ffprobe (part of ffmpeg) is required to read timecode from video files.",
            "fix":     inst["ffmpeg"]["fix"],
            "fix_cmd": inst["ffmpeg"]["fix_cmd"],
        })
    # Pillow and Tesseract are only needed for OCR calibration — not checked at startup
    return errors


def check_ocr_dependencies():
    """Check OCR-specific dependencies — called only when calibration is attempted."""
    inst   = _install_instructions()
    errors = []
    try:
        from PIL import Image
    except ImportError:
        errors.append({
            "type":    "missing_pillow",
            "title":   "Pillow not installed",
            "detail":  "Pillow is required for OCR calibration.",
            "fix":     inst["pillow"]["fix"],
            "fix_cmd": inst["pillow"]["fix_cmd"],
        })
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
    except Exception:
        errors.append({
            "type":    "missing_tesseract",
            "title":   "Tesseract OCR not found",
            "detail":  "Tesseract is required for OCR calibration.",
            "fix":     inst["tesseract"]["fix"],
            "fix_cmd": inst["tesseract"]["fix_cmd"],
        })
    return errors

# ── Timecode helpers ──────────────────────────────────────────────────────────

def tc_str_to_seconds(tc, fps=25):
    parts = tc.strip().split(":")
    h, m, s, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    return ((h * 3600 + m * 60 + s) * fps + f) / fps

def seconds_to_tc_str(total_seconds, fps=25):
    total_frames = round(total_seconds * fps)
    f = total_frames % fps
    total_secs = total_frames // fps
    s = total_secs % 60
    m = (total_secs // 60) % 60
    h = total_secs // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

def elapsed_seconds_to_str(secs):
    total_frames = round(secs * 25)
    f = total_frames % 25
    total_secs = total_frames // 25
    s = total_secs % 60
    m = total_secs // 60
    return f"{m:02d}:{s:02d}:{f:02d}"

def parse_elapsed_input(text):
    text = text.strip()
    if re.match(r"^\d{1,2}:\d{2}:\d{2}:\d{2}$", text):
        return None
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 25
    m = re.match(r"^(\d+):(\d{2})$", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"^(\d+(?:\.\d+)?)$", text)
    if m:
        return float(m.group(1))
    return None

def parse_tod_input(text):
    text = text.strip()
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2}):(\d{2})$", text)
    if m:
        h, mn, s, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return h * 3600 + mn * 60 + s + f / 25
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})$", text)
    if m:
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return h * 3600 + mn * 60 + s
    return None

# ── File detection ────────────────────────────────────────────────────────────

QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

def detect_quarter(filename):
    """Return the quarter label (Q1–Q4) found in a filename, or None."""
    name = filename.upper()
    for q in QUARTERS:
        if q in name:
            return q
    return None

def detect_file_type(filename, quarter=None):
    """Return BTGL or BCAST if the filename matches, filtered by quarter if given."""
    name = filename.upper()
    if quarter and quarter.upper() not in name:
        return None
    if "BTGL" in name or "BTGR" in name:
        return "BTGL"
    if "BCAST" in name:
        return "BCAST"
    return None

def scan_media_folder(quarter=None):
    """Scan folder for video files. If quarter given, only return files for that quarter.
    Also returns the list of all available quarters found in the folder."""
    folder = Path(state["media_folder"])
    if not folder.exists():
        return {}, [], f"Folder not found: {folder}"
    extensions = {".mp4", ".mov", ".mxf", ".avi", ".mkv"}
    try:
        entries = [p for p in folder.iterdir() if p.suffix.lower() in extensions]
    except PermissionError:
        return {}, [], f"Permission denied reading folder: {folder}"

    # Detect which quarters exist in the folder
    available = sorted({detect_quarter(p.name) for p in entries if detect_quarter(p.name)})

    # If no quarter specified, auto-select the first available
    target = quarter or (available[0] if available else None)

    found = {}
    errors = []
    for p in entries:
        if p.suffix.lower() not in extensions:
            continue
        ftype = detect_file_type(p.name, target)
        if ftype is None:
            continue
        info, err = read_file_tc(p)
        if info:
            found[ftype] = info
            found[ftype]["path"] = str(p)
        else:
            errors.append(f"{p.name}: {err}")
    return found, available, "; ".join(errors) if errors else None

def apply_scan(files, available, scan_err, quarter=None):
    """Update global state after a folder scan. Quarter defaults to first available."""
    state["available_quarters"] = available
    state["files"] = files
    # Set quarter to the explicitly requested one, or keep current if still valid, else first available
    if quarter and quarter in available:
        new_q = quarter
    elif state["quarter"] in available:
        new_q = state["quarter"]
    elif available:
        new_q = available[0]
    else:
        new_q = None
    state["quarter"] = new_q
    # Ensure calibration dict exists for this quarter and point state["calibration"] at it
    if new_q:
        if new_q not in state["calibrations"]:
            state["calibrations"][new_q] = _empty_calibration()
        state["calibration"] = state["calibrations"][new_q]
    else:
        state["calibration"] = _empty_calibration()
    return scan_err

def switch_quarter(quarter):
    """Switch to a different quarter — re-scan files and swap calibration."""
    files, available, scan_err = scan_media_folder(quarter)
    apply_scan(files, available, scan_err, quarter)
    return scan_err

def read_file_tc(path):
    if not FFPROBE:
        return None, "ffprobe not installed"
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30
        )
        if not result.stdout.strip():
            return None, f"ffprobe returned no output (stderr: {result.stderr[:200]})"
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        tc = None
        fps = 25
        for s in data["streams"]:
            if s.get("codec_type") == "video":
                tc = s.get("tags", {}).get("timecode")
                r = s.get("r_frame_rate", "25/1")
                num, den = r.split("/")
                fps = round(int(num) / int(den))
                break
        if tc is None:
            return None, "no timecode track found in file"
        return {
            "tc_str":     tc,
            "start_tc_s": tc_str_to_seconds(tc, fps),
            "duration_s": duration,
            "fps":        fps,
            "filename":   path.name,
        }, None
    except Exception as e:
        return None, str(e)

# ── OCR calibration ───────────────────────────────────────────────────────────

def get_clock_crop():
    preset = state["broadcast"]
    return BROADCAST_PRESETS.get(preset, BROADCAST_PRESETS["fox"])["crop"]

def ocr_jpeg_bytes(jpeg_bytes):
    """OCR a JPEG frame and return countdown seconds, or None."""
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(jpeg_bytes))
        w, h = img.size
        l, t, r, b = get_clock_crop()
        crop = img.crop((int(l * w), int(t * h), int(r * w), h))
        crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS).convert("L")
        text = pytesseract.image_to_string(
            crop, config="--psm 7 -c tessedit_char_whitelist=0123456789:"
        )
        m = re.search(r"(\d{1,2}):(\d{2})", text)
        if m:
            mins, secs = int(m.group(1)), int(m.group(2))
            if secs < 60 and mins <= 30:
                return mins * 60 + secs
        return None
    except Exception:
        return None

def extract_frame_jpeg(video_path, position_s):
    """Extract a single JPEG frame at position_s seconds using ffmpeg."""
    if not FFMPEG:
        return None
    try:
        cmd = [FFMPEG, "-ss", str(position_s), "-i", video_path,
               "-frames:v", "1", "-f", "image2pipe",
               "-vcodec", "mjpeg", "-q:v", "5", "pipe:1"]
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        return result.stdout if result.returncode == 0 and result.stdout else None
    except Exception:
        return None

def run_calibration():
    cal = state["calibration"]
    cal["status"]   = "running"
    cal["lookup"]   = []
    cal["progress"] = 0

    ocr_errors = check_ocr_dependencies()
    if ocr_errors:
        cal["status"]  = "error"
        cal["message"] = ocr_errors[0]["title"] + " — " + ocr_errors[0]["detail"] + "  Fix: " + ocr_errors[0]["fix_cmd"]
        return

    bcast = state["files"].get("BCAST")
    if not bcast:
        cal["status"]  = "error"
        cal["message"] = "BCAST file not loaded"
        return
    if not FFMPEG:
        cal["status"]  = "error"
        cal["message"] = "ffmpeg not found — needed to extract frames"
        return

    path     = bcast["path"]
    duration = bcast["duration_s"]
    times    = list(range(0, int(duration), OCR_INTERVAL))
    cal["total"]   = len(times)
    cal["message"] = f"Scanning {len(times)} frames from BCAST..."

    lookup = []
    for i, t in enumerate(times):
        cal["progress"] = i
        cal["message"]  = f"Scanning frame {i+1}/{len(times)}  ({t//60}:{t%60:02d} elapsed)"
        jpeg = extract_frame_jpeg(path, t)
        if jpeg:
            cd = ocr_jpeg_bytes(jpeg)
            if cd is not None:
                lookup.append({"file_elapsed_s": float(t), "countdown_s": cd})

    if not lookup:
        cal["status"]  = "error"
        cal["message"] = ("OCR found no clock readings. "
                          "Check broadcast preset (FOX vs Seven) or use manual sync below.")
        return

    # Clean the lookup table.
    # Rules:
    #   1. Countdown must not increase by more than 5s (would be physically impossible)
    #   2. Countdown must not DROP faster than real time — the clock runs at most 1s per
    #      real second, so across an OCR_INTERVAL gap the max possible drop is
    #      OCR_INTERVAL seconds. We allow 2× tolerance for OCR noise, plus a small buffer.
    #      A sudden drop of (say) 600s in 8s of file time is an OCR misread and must go.
    lookup.sort(key=lambda x: x["file_elapsed_s"])
    cleaned = [lookup[0]]
    for entry in lookup[1:]:
        prev      = cleaned[-1]
        time_gap  = entry["file_elapsed_s"] - prev["file_elapsed_s"]
        cd_change = entry["countdown_s"] - prev["countdown_s"]   # negative = clock went down
        max_drop  = (time_gap * 2) + 15   # generous tolerance for OCR noise
        # Keep entry only if clock went up by ≤5s (noise) OR down by a physically plausible amount
        if -max_drop <= cd_change <= 5:
            cleaned.append(entry)

    cal["lookup"]   = cleaned
    cal["status"]   = "done"
    cal["progress"] = len(times)
    cal["message"]  = (f"Done — {len(cleaned)} valid readings from {len(times)} frames. "
                       f"({len(times)-len(cleaned)} discarded as OCR noise)")

# ── Conversion engine ─────────────────────────────────────────────────────────

def countdown_to_bcast_elapsed(countdown_s):
    lookup = state["calibration"]["lookup"]
    if not lookup:
        return None

    # Scan ALL segments and collect every match — the countdown is monotonically
    # non-increasing, so a given value should appear only once. But if a phantom
    # OCR reading slipped through cleaning we want the LAST (latest) match, since
    # low countdown values (near end of quarter) belong at the end of the file.
    match_elapsed = None
    best, best_diff = None, float("inf")

    for i in range(len(lookup) - 1):
        a, b = lookup[i], lookup[i + 1]
        hi = max(a["countdown_s"], b["countdown_s"])
        lo = min(a["countdown_s"], b["countdown_s"])
        if lo <= countdown_s <= hi and a["countdown_s"] != b["countdown_s"]:
            t = (a["countdown_s"] - countdown_s) / (a["countdown_s"] - b["countdown_s"])
            match_elapsed = a["file_elapsed_s"] + t * (b["file_elapsed_s"] - a["file_elapsed_s"])
            # Don't return immediately — keep scanning for a later match
        diff = abs(lookup[i]["countdown_s"] - countdown_s)
        if diff < best_diff:
            best_diff = diff
            best = lookup[i]

    last = lookup[-1]
    diff = abs(last["countdown_s"] - countdown_s)
    if diff < best_diff:
        best_diff = diff
        best = last

    # Prefer an interpolated match; fall back to nearest entry if within 30s
    if match_elapsed is not None:
        return match_elapsed
    return best["file_elapsed_s"] if best and best_diff <= 30 else None

def file_elapsed_to_tod(ftype, elapsed_s):
    info = state["files"].get(ftype)
    return info["start_tc_s"] + elapsed_s if info else None

def tod_to_file_elapsed(ftype, tod_s):
    info = state["files"].get(ftype)
    if not info:
        return None
    e = tod_s - info["start_tc_s"]
    return e if 0 <= e <= info["duration_s"] else None

def make_result(ftype, elapsed_s):
    info = state["files"].get(ftype)
    if not info or elapsed_s is None:
        return {"available": False, "reason": f"{ftype} not loaded"}
    if elapsed_s < 0:
        return {"available": False, "reason": "Before start of file"}
    if elapsed_s > info["duration_s"]:
        dur = elapsed_seconds_to_str(info["duration_s"])
        return {"available": False, "reason": f"After end of file (duration {dur})"}

    tod_s = file_elapsed_to_tod(ftype, elapsed_s)
    result = {
        "available":    True,
        "file_elapsed": elapsed_seconds_to_str(elapsed_s),
        "embedded_tc":  seconds_to_tc_str(tod_s, info["fps"]),
        "elapsed_s":    elapsed_s,
        "filename":     info["filename"],
    }
    if ftype == "BCAST" and state["calibration"]["status"] == "done":
        lookup = state["calibration"]["lookup"]
        near = min(lookup, key=lambda x: abs(x["file_elapsed_s"] - elapsed_s), default=None)
        if near and abs(near["file_elapsed_s"] - elapsed_s) <= OCR_INTERVAL + 2:
            cd = near["countdown_s"]
            result["bcast_countdown"] = f"{int(cd//60)}:{int(cd%60):02d}"
        else:
            result["bcast_countdown"] = None
    return result

def convert(mode, value):
    if mode == "btgl":
        e = parse_elapsed_input(value)
        if e is None:
            return {"error": f'Cannot parse "{value}" — use MM:SS or MM:SS:FF'}
        tod = file_elapsed_to_tod("BTGL", e)
        return {"results": {
            "BTGL":  make_result("BTGL",  e),
            "BCAST": make_result("BCAST", tod_to_file_elapsed("BCAST", tod)),
        }}
    elif mode == "bcast":
        e = parse_elapsed_input(value)
        if e is None:
            return {"error": f'Cannot parse "{value}" — use MM:SS'}
        if state["calibration"]["status"] != "done":
            return {"error": "Calibration not complete — run calibration or use manual sync first"}
        bcast_e = countdown_to_bcast_elapsed(e)
        if bcast_e is None:
            return {"error": f'Could not map countdown {int(e//60)}:{int(e%60):02d} to a frame position'}
        tod = file_elapsed_to_tod("BCAST", bcast_e)
        return {"results": {
            "BTGL":  make_result("BTGL",  tod_to_file_elapsed("BTGL",  tod)),
            "BCAST": make_result("BCAST", bcast_e),
        }}
    elif mode == "tod":
        tod = parse_tod_input(value)
        if tod is None:
            return {"error": f'Cannot parse "{value}" — use HH:MM:SS or HH:MM:SS:FF'}
        return {"results": {
            "BTGL":  make_result("BTGL",  tod_to_file_elapsed("BTGL",  tod)),
            "BCAST": make_result("BCAST", tod_to_file_elapsed("BCAST", tod)),
        }}
    return {"error": "Unknown mode"}

# ── Network helpers ───────────────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def browse_for_folder():
    """Open a native Windows folder-picker dialog. Returns (path, error)."""
    if not IS_WINDOWS:
        return None, "Folder picker is only available on Windows"
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()                          # hide the blank Tk window
        root.wm_attributes("-topmost", True)     # dialog appears on top
        initial = state["media_folder"] if os.path.isdir(state["media_folder"]) else "/"
        folder = filedialog.askdirectory(
            title="Select match media folder",
            initialdir=initial,
        )
        root.destroy()
        return (folder, None) if folder else (None, "No folder selected")
    except Exception as e:
        return None, str(e)

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AFL Timecode Converter</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #0f1117; color: #e2e8f0; min-height: 100vh; }

.header { background: linear-gradient(135deg,#1a1f2e,#16213e);
  border-bottom: 1px solid #2d3748; padding: 18px 28px;
  display: flex; align-items: center; gap: 14px; }
.logo { width:40px; height:40px; background: linear-gradient(135deg,#e8a020,#f4c842);
  border-radius:9px; display:flex; align-items:center; justify-content:center;
  font-size:18px; font-weight:900; color:#0f1117; flex-shrink:0; }
.header h1 { font-size:1.25rem; font-weight:700; color:#f7fafc; }
.header p  { font-size:0.78rem; color:#718096; margin-top:1px; }

.main { max-width:1120px; margin:0 auto; padding:22px 20px; }

/* ── Alerts ── */
.alert { border-radius:10px; padding:14px 16px; margin-bottom:16px; }
.alert-err  { background:#3d1c1c; border:1px solid #6b2222; color:#fc8181; }
.alert-warn { background:#2d2a1c; border:1px solid #6b5a1e; color:#fbd38d; }
.alert-title { font-weight:700; font-size:0.88rem; margin-bottom:4px; }
.alert-body  { font-size:0.8rem; line-height:1.5; }
.alert code  { background:rgba(255,255,255,0.1); padding:2px 6px; border-radius:4px;
  font-family:monospace; font-size:0.85em; user-select:all; cursor:pointer; }

/* ── Cards / Panels ── */
.panel { background:#1a1f2e; border:1px solid #2d3748; border-radius:12px;
  padding:18px 20px; margin-bottom:18px; }
.panel-title { font-size:0.9rem; font-weight:700; color:#e2e8f0; margin-bottom:12px;
  display:flex; align-items:center; gap:8px; }
.badge { font-size:0.65rem; font-weight:700; padding:2px 7px; border-radius:20px;
  letter-spacing:.05em; text-transform:uppercase; }
.badge-ok   { background:#1c3d2f; color:#68d391; }
.badge-warn { background:#3d2d1c; color:#fbd38d; }
.badge-err  { background:#3d1c1c; color:#fc8181; }

/* ── File cards ── */
.file-row { display:flex; gap:10px; margin-bottom:18px; flex-wrap:wrap; }
.fc { flex:1; min-width:150px; background:#0f1117; border:1px solid #2d3748;
  border-radius:10px; padding:12px 14px; }
.fc.missing { opacity:.5; border-style:dashed; }
.fc-label { font-size:.68rem; font-weight:700; letter-spacing:.07em;
  text-transform:uppercase; margin-bottom:4px; }
.fc-BTGL .fc-label { color:#63b3ed; }
.fc-BCAST .fc-label { color:#fbd38d; }
.fc-DIRTY .fc-label { color:#fc8181; }
.fc-name { font-size:.72rem; color:#718096; margin-bottom:5px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.fc-tc   { font-family:monospace; font-size:.82rem; color:#68d391; }
.fc-dur  { font-size:.72rem; color:#718096; margin-top:2px; }

/* ── Inputs ── */
.row { display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap; }
.field { flex:1; min-width:180px; }
.field label { display:block; font-size:.73rem; color:#718096; margin-bottom:5px; }
.inp {
  width:100%; padding:9px 12px; background:#0f1117; border:1px solid #2d3748;
  border-radius:8px; color:#e2e8f0; font-size:.92rem; font-family:monospace;
  outline:none; transition:border-color .15s; }
.inp:focus { border-color:#e8a020; }
.hint { font-size:.7rem; color:#4a5568; margin-top:4px; }
.btn { padding:9px 18px; border-radius:8px; border:none; cursor:pointer;
  font-size:.83rem; font-weight:700; transition:all .15s; white-space:nowrap; }
.btn-primary  { background:linear-gradient(135deg,#e8a020,#f4c842); color:#0f1117; }
.btn-primary:hover  { opacity:.9; transform:translateY(-1px); }
.btn-primary:disabled { opacity:.4; cursor:not-allowed; transform:none; }
.btn-secondary { background:#2d3748; color:#e2e8f0; }
.btn-secondary:hover { background:#3d4a5e; }
.btn-sm { padding:6px 12px; font-size:.75rem; }

/* ── Folder bar ── */
.folder-bar { display:flex; gap:8px; align-items:center; margin-bottom:14px; }
.folder-bar .inp { flex:1; }
.folder-hint { font-size:.7rem; color:#4a5568; margin-top:5px; }

/* ── Drop zone ── */
.drop-zone { border:2px dashed #2d3748; border-radius:10px; padding:20px;
  text-align:center; transition:all .2s; cursor:pointer; }
.drop-zone:hover, .drop-zone.dragover { border-color:#e8a020; background:#1c1a10; }
.drop-zone p  { font-size:.82rem; color:#718096; line-height:1.6; }
.drop-zone strong { color:#e2e8f0; }

/* ── Manual sync details ── */
.manual-details { background:#1a1f2e; border:1px solid #2d3748; border-radius:12px;
  padding:0; margin-top:18px; }
.manual-details[open] { padding-bottom:18px; }
.manual-summary { list-style:none; padding:14px 20px; cursor:pointer;
  font-size:.82rem; font-weight:700; color:#a0aec0;
  display:flex; align-items:center; gap:8px; border-radius:12px;
  user-select:none; }
.manual-summary::-webkit-details-marker { display:none; }
.manual-summary::before { content:'▸'; font-size:.75rem; color:#4a5568; transition:transform .15s; }
.manual-details[open] .manual-summary::before { transform:rotate(90deg); }
.manual-summary:hover { color:#e2e8f0; }
.manual-body { padding:0 20px; }

/* ── Quarter selector ── */
.qtr-bar  { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  background:#1a1f2e; border:1px solid #2d3748; border-radius:10px;
  padding:11px 16px; margin-bottom:14px; }
.qtr-lbl  { font-size:.72rem; color:#718096; font-weight:600;
  text-transform:uppercase; letter-spacing:.07em; flex-shrink:0; }
.qtr-btns { display:flex; gap:6px; flex-wrap:wrap; }
.qtr-btn  { padding:5px 16px; border-radius:7px; border:1px solid #2d3748;
  background:transparent; color:#718096; cursor:pointer;
  font-size:.82rem; font-weight:700; transition:all .15s; letter-spacing:.04em; }
.qtr-btn.active   { border-color:#63b3ed; background:#1a2d3e; color:#63b3ed; }
.qtr-btn.cal-done { border-color:#2f6846; }
.qtr-btn.active.cal-done { border-color:#63b3ed; }
.qtr-btn:hover:not(.active):not([disabled]) { border-color:#4a5568; color:#a0aec0; }
.qtr-btn[disabled] { opacity:.3; cursor:not-allowed; }
.qtr-cal-tick { font-size:.65rem; color:#68d391; margin-left:3px; }

/* ── Broadcast selector ── */
.bcast-opts { display:flex; gap:8px; flex-wrap:wrap; }
.bcast-opt  { padding:7px 14px; border-radius:8px; border:1px solid #2d3748;
  background:transparent; color:#718096; cursor:pointer;
  font-size:.8rem; font-weight:600; transition:all .15s; }
.bcast-opt.active { border-color:#e8a020; background:#2d2a1c; color:#f4c842; }
.bcast-opt:hover:not(.active) { border-color:#4a5568; color:#a0aec0; }

/* ── Calibration ── */
.cal-status { font-size:.8rem; padding:8px 12px; border-radius:8px;
  background:#2d3748; margin-top:10px; }
.cal-status.done    { background:#1c3d2f; color:#68d391; border:1px solid #2f6846; }
.cal-status.running { background:#2d2a1c; color:#fbd38d; border:1px solid #6b5a1e; }
.cal-status.error   { background:#3d1c1c; color:#fc8181; border:1px solid #6b2222; }
.prog-bar  { height:4px; background:#2d3748; border-radius:2px; margin-top:8px; }
.prog-fill { height:100%; background:linear-gradient(90deg,#e8a020,#f4c842);
  border-radius:2px; transition:width .3s; }
.divider { border:none; border-top:1px solid #2d3748; margin:14px 0; }

/* ── Converter ── */
.tabs { display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }
.tab  { padding:7px 14px; border-radius:8px; border:1px solid #2d3748;
  background:transparent; color:#718096; cursor:pointer;
  font-size:.8rem; font-weight:600; transition:all .15s; }
.tab.active       { border-color:#e8a020; background:#2d2a1c; color:#f4c842; }
.tab:hover:not(.active) { border-color:#4a5568; color:#a0aec0; }

/* ── Results ── */
.results { display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }
.rc { flex:1; min-width:230px; background:#0f1117; border:1px solid #2d3748;
  border-radius:10px; padding:14px 16px; }
.rc.active { border-color:#3d4a5e; }
.rc.na     { opacity:.45; }
.rc-label-row { display:flex; justify-content:space-between; align-items:center;
  margin-bottom:10px; }
.rc-label { font-size:.68rem; font-weight:700; letter-spacing:.07em; text-transform:uppercase; }
.rc-BTGL .rc-label { color:#63b3ed; }
.rc-BCAST .rc-label { color:#fbd38d; }
.rc-DIRTY .rc-label { color:#fc8181; }
.rc-fname { font-size:.68rem; color:#4a5568; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; max-width:150px; }
.rc-row { margin-bottom:8px; }
.rc-row-label { font-size:.66rem; color:#718096; text-transform:uppercase;
  letter-spacing:.06em; margin-bottom:2px; }
.rc-val { font-family:monospace; font-size:1rem; color:#e2e8f0;
  background:#1a1f2e; padding:7px 10px; border-radius:6px;
  cursor:pointer; display:flex; justify-content:space-between; align-items:center;
  border:1px solid transparent; transition:border-color .15s; user-select:all; }
.rc-val:hover  { border-color:#4a5568; }
.rc-val.copied { border-color:#68d391; color:#68d391; }
.rc-val .copy-hint { font-size:.62rem; color:#4a5568; font-family:sans-serif; user-select:none; }
.rc-na-msg { font-size:.8rem; color:#4a5568; font-style:italic; }
.countdown-val { color:#fbd38d; }
.empty-msg { width:100%; text-align:center; color:#4a5568;
  font-size:.88rem; padding:32px; }
</style>
</head>
<body>
<div class="header">
  <div class="logo">TC</div>
  <div>
    <h1>AFL Timecode Converter</h1>
    <p>BTGL/BTGR file elapsed &nbsp;·&nbsp; BCAST countdown &nbsp;·&nbsp; Time of Day</p>
  </div>
</div>
<div class="main">

  <!-- Folder path — always at the top -->
  <div class="folder-bar">
    <input type="text" class="inp" id="folderPath" placeholder="Paste folder path or click Browse…" />
    <button class="btn btn-secondary" id="browseBtn" onclick="browseFolder()" style="display:none">Browse…</button>
    <button class="btn btn-secondary" onclick="setFolder()">Scan Folder</button>
  </div>
  <div class="folder-hint" style="margin-bottom:14px">Select the match folder containing your Q1–Q4 files</div>

  <!-- Startup errors -->
  <div id="startupErrors"></div>

  <!-- Quarter selector (hidden until multiple quarters are detected) -->
  <div class="qtr-bar" id="quarterBar" style="display:none">
    <span class="qtr-lbl">Quarter</span>
    <div class="qtr-btns" id="quarterBtns"></div>
  </div>

  <!-- File status -->
  <div class="file-row" id="fileRow">
    <div class="fc missing"><div class="fc-label">Loading…</div></div>
  </div>

  <!-- Settings: broadcast preset only -->
  <div class="panel">
    <div class="panel-title">⚙ Broadcast Preset</div>
    <div class="bcast-opts" id="bcastOpts">
      <button class="bcast-opt active" data-b="fox"   onclick="setBroadcast('fox')">FOX Footy</button>
      <button class="bcast-opt"        data-b="seven" onclick="setBroadcast('seven')">Seven Network</button>
    </div>
  </div>

  <!-- Calibration -->
  <div class="panel">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
      <div style="flex:1">
        <div class="panel-title">📡 BCAST Countdown Calibration</div>
        <p style="font-size:.78rem;color:#718096;line-height:1.55">
          Since the AFL clock stops during stoppages it's non-linear, so we OCR sample frames from BCAST
          to map the visible countdown to exact file positions. Takes ~2–3 min. Runs in the background.
        </p>
      </div>
      <button class="btn btn-primary" id="calBtn" onclick="startCal()" style="flex-shrink:0">Run Calibration</button>
    </div>
    <div class="cal-status" id="calStatus">Not calibrated — BCAST countdown conversion unavailable</div>
    <div class="prog-bar" id="progBar" style="display:none"><div class="prog-fill" id="progFill" style="width:0"></div></div>
  </div>

  <!-- Converter -->
  <div class="panel">
    <div class="panel-title">🔄 Convert</div>
    <div class="tabs">
      <button class="tab active" id="tab-btgl"  onclick="setMode('btgl')">📁 BTGL / BTGR Elapsed</button>
      <button class="tab"        id="tab-bcast" onclick="setMode('bcast')">⏱ BCAST Countdown</button>
      <button class="tab"        id="tab-tod"   onclick="setMode('tod')">🕐 Time of Day</button>
    </div>

    <div id="inp-btgl" class="row">
      <div class="field">
        <label>Time elapsed in BTGL / BTGR file</label>
        <input type="text" class="inp" id="val-btgl" placeholder="MM:SS  or  MM:SS:FF">
        <div class="hint">e.g. 14:23 — how far you've scrubbed into the BTGL or BTGR file</div>
      </div>
      <button class="btn btn-primary" onclick="doConvert('btgl')">Convert →</button>
    </div>

    <div id="inp-bcast" class="row" style="display:none">
      <div class="field">
        <label>BCAST visible countdown clock</label>
        <input type="text" class="inp" id="val-bcast" placeholder="MM:SS (time remaining)">
        <div class="hint">e.g. 12:34 — what the on-screen clock shows (requires calibration)</div>
      </div>
      <button class="btn btn-primary" onclick="doConvert('bcast')">Convert →</button>
    </div>

    <div id="inp-tod" class="row" style="display:none">
      <div class="field">
        <label>Time-of-day timecode (embedded in any file)</label>
        <input type="text" class="inp" id="val-tod" placeholder="HH:MM:SS  or  HH:MM:SS:FF">
        <div class="hint">e.g. 19:06:43 — the embedded time-of-day TC visible in your NLE for any of the files</div>
      </div>
      <button class="btn btn-primary" onclick="doConvert('tod')">Convert →</button>
    </div>

    <div class="results" id="results">
      <div class="empty-msg">Enter a time above and press Convert</div>
    </div>
  </div>

  <!-- Manual sync — collapsible at the bottom -->
  <details class="manual-details">
    <summary class="manual-summary">Manual sync (instant alternative to calibration)</summary>
    <div class="manual-body">
      <p style="font-size:.75rem;color:#718096;margin-bottom:12px;line-height:1.5;margin-top:4px">
        Open BCAST in your NLE, find any moment where you can read both the file position and the visible
        clock. Enter both below to build a linear sync without running OCR calibration.
      </p>
      <div class="row">
        <div class="field">
          <label>BCAST file elapsed at sync point (MM:SS)</label>
          <input type="text" class="inp" id="syncElapsed" placeholder="e.g. 02:14">
        </div>
        <div class="field">
          <label>Countdown clock at that moment (MM:SS)</label>
          <input type="text" class="inp" id="syncCountdown" placeholder="e.g. 19:27">
        </div>
        <div class="field">
          <label>Quarter clock duration (MM:SS)</label>
          <input type="text" class="inp" id="syncQtrDur" placeholder="e.g. 30:00">
        </div>
        <button class="btn btn-secondary" onclick="applyManual()">Apply</button>
      </div>
    </div>
  </details>

</div><!-- /main -->
<script>
let mode = 'btgl', calPoll = null;
let currentQuarter = null;
let calDoneQuarters = {};  // tracks which quarters have been fully calibrated

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  const r = await fetch('/api/init');
  const d = await r.json();
  if (d.is_windows) document.getElementById('browseBtn').style.display = '';
  renderErrors(d.startup_errors);
  renderQuarters(d.available_quarters, d.quarter);
  renderFiles(d.files, d.scan_error);
  renderCal(d.calibration);
  if (d.media_folder) document.getElementById('folderPath').value = d.media_folder;
  if (d.broadcast)    setActiveBroadcast(d.broadcast);
}

async function browseFolder() {
  const btn = document.getElementById('browseBtn');
  btn.disabled = true;
  btn.textContent = 'Opening…';
  try {
    const r = await fetch('/api/browse_folder');
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    document.getElementById('folderPath').value = d.media_folder || '';
    renderErrors(d.startup_errors);
    renderQuarters(d.available_quarters, d.quarter);
    renderFiles(d.files, d.scan_error);
    renderCal(d.calibration);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Browse…';
  }
}

function renderErrors(errs) {
  const el = document.getElementById('startupErrors');
  if (!errs || !errs.length) { el.innerHTML=''; return; }
  el.innerHTML = errs.map(e => `
    <div class="alert alert-err">
      <div class="alert-title">⚠ ${e.title}</div>
      <div class="alert-body">${e.detail}<br>
        ${e.fix_cmd ? `Fix: <code onclick="copyText('${e.fix_cmd}')">${e.fix_cmd}</code> (click to copy)` : e.fix}
      </div>
    </div>`).join('');
}

function renderFiles(files, scanErr) {
  const row = document.getElementById('fileRow');
  const types = ['BTGL','BCAST'];
  const colors = {BTGL:'#63b3ed', BCAST:'#fbd38d'};
  const descs  = {BTGL:'BTG Left/Right · elapsed TC', BCAST:'Broadcast · countdown clock'};
  row.innerHTML = types.map(t => {
    const f = files && files[t];
    if (!f) return `<div class="fc fc-${t} missing">
      <div class="fc-label" style="color:${colors[t]}">${t}</div>
      <div class="fc-name">Not found</div>
      <div class="fc-dur">${descs[t]}</div>
    </div>`;
    const dur = Math.floor(f.duration_s/60)+':'+String(Math.floor(f.duration_s%60)).padStart(2,'0');
    return `<div class="fc fc-${t}">
      <div class="fc-label" style="color:${colors[t]}">${t} <span class="badge badge-ok">LOADED</span></div>
      <div class="fc-name" title="${f.filename}">${f.filename}</div>
      <div class="fc-tc">${f.tc_str}</div>
      <div class="fc-dur">${dur} &nbsp;·&nbsp; ${descs[t]}</div>
    </div>`;
  }).join('');
  if (scanErr) {
    row.insertAdjacentHTML('afterend',
      `<div class="alert alert-warn"><div class="alert-title">File scan issue</div>
      <div class="alert-body">${scanErr}</div></div>`);
  }
}

function renderCal(cal) {
  const el  = document.getElementById('calStatus');
  const btn = document.getElementById('calBtn');
  const pb  = document.getElementById('progBar');
  const pf  = document.getElementById('progFill');
  if (cal.status === 'done') {
    el.className='cal-status done'; el.textContent='✓ '+cal.message;
    btn.textContent='Re-calibrate'; btn.disabled=false; pb.style.display='none';
    // Mark current quarter as calibrated and refresh quarter buttons
    if (currentQuarter) {
      calDoneQuarters[currentQuarter] = true;
      refreshQuarterTicks();
    }
  } else if (cal.status === 'running') {
    el.className='cal-status running'; el.textContent='⏳ '+cal.message;
    btn.disabled=true; pb.style.display='block';
    if (cal.total>0) pf.style.width=Math.round(cal.progress/cal.total*100)+'%';
  } else if (cal.status === 'error') {
    el.className='cal-status error'; el.textContent='✗ '+cal.message;
    btn.disabled=false; pb.style.display='none';
  } else {
    el.className='cal-status'; btn.disabled=false; pb.style.display='none';
    el.textContent='Not calibrated — BCAST countdown conversion unavailable';
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function setFolder() {
  const path = document.getElementById('folderPath').value.trim();
  if (!path) return;
  const r = await fetch('/api/set_folder', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})});
  const d = await r.json();
  calDoneQuarters = {};  // reset — new folder means fresh calibrations
  renderErrors(d.startup_errors);
  renderQuarters(d.available_quarters, d.quarter);
  renderFiles(d.files, d.scan_error);
  renderCal(d.calibration);
}

// ── Quarter selector ──────────────────────────────────────────────────────────
function renderQuarters(available, current) {
  currentQuarter = current || null;
  const bar  = document.getElementById('quarterBar');
  const btns = document.getElementById('quarterBtns');
  // Only show the bar when more than one quarter exists in the folder
  if (!available || available.length < 2) { bar.style.display='none'; return; }
  bar.style.display = 'flex';
  btns.innerHTML = ['Q1','Q2','Q3','Q4'].map(q => {
    const avail  = available.includes(q);
    const active = q === current;
    const done   = calDoneQuarters[q];
    const cls    = ['qtr-btn',
      active  ? 'active'   : '',
      done    ? 'cal-done' : '',
    ].filter(Boolean).join(' ');
    const tick = done ? `<span class="qtr-cal-tick">✓</span>` : '';
    return avail
      ? `<button class="${cls}" onclick="setQuarter('${q}')">${q}${tick}</button>`
      : `<button class="qtr-btn" disabled title="${q} not found in folder">${q}</button>`;
  }).join('');
}

function refreshQuarterTicks() {
  // Update the ✓ tick marks without re-rendering the whole bar
  document.querySelectorAll('.qtr-btn').forEach(btn => {
    const q = btn.textContent.replace('✓','').trim();
    if (calDoneQuarters[q]) {
      btn.classList.add('cal-done');
      if (!btn.querySelector('.qtr-cal-tick'))
        btn.insertAdjacentHTML('beforeend', '<span class="qtr-cal-tick">✓</span>');
    }
  });
}

async function setQuarter(q) {
  const r = await fetch('/api/set_quarter', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({quarter:q})});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  renderQuarters(d.available_quarters, d.quarter);
  renderFiles(d.files, d.scan_error);
  renderCal(d.calibration);
  // Clear previous results — they belong to the old quarter
  document.getElementById('results').innerHTML =
    '<div class="empty-msg">Enter a time above and press Convert</div>';
}

function setBroadcast(type) {
  setActiveBroadcast(type);
  fetch('/api/set_broadcast', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({type})});
}

function setActiveBroadcast(type) {
  document.querySelectorAll('.bcast-opt').forEach(b => {
    b.classList.toggle('active', b.dataset.b === type);
  });
}

// ── Folder drag-and-drop onto page ────────────────────────────────────────────
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => {
  e.preventDefault();
  const items = e.dataTransfer.items;
  for (const item of items) {
    const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
    if (entry && entry.isDirectory) {
      // Can't get full path from browser for security reasons — show guidance
      const inp = document.getElementById('folderPath');
      inp.placeholder = 'Drag folder shows name only — please type the full path';
      inp.style.borderColor = '#e8a020';
      setTimeout(()=>inp.style.borderColor='',2000);
      return;
    }
  }
});

// ── Calibration ───────────────────────────────────────────────────────────────
async function startCal() {
  document.getElementById('calBtn').disabled = true;
  await fetch('/api/calibrate', {method:'POST'});
  calPoll = setInterval(async () => {
    const r = await fetch('/api/calibration_status');
    const d = await r.json();
    renderCal(d);
    if (['done','error','idle'].includes(d.status)) clearInterval(calPoll);
  }, 1500);
}

async function applyManual() {
  const elapsed   = document.getElementById('syncElapsed').value.trim();
  const countdown = document.getElementById('syncCountdown').value.trim();
  const qtrDur    = document.getElementById('syncQtrDur').value.trim();
  if (!elapsed || !countdown || !qtrDur) { alert('Fill in all three fields.'); return; }
  const r = await fetch('/api/manual_calibrate', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({elapsed, countdown, qtr_duration: qtrDur})});
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  renderCal(d.calibration);
}

// ── Converter ─────────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  ['btgl','bcast','tod'].forEach(x => {
    document.getElementById('inp-'+x).style.display = x===m ? 'flex' : 'none';
    document.getElementById('tab-'+x).classList.toggle('active', x===m);
  });
  document.getElementById('results').innerHTML =
    '<div class="empty-msg">Enter a time above and press Convert</div>';
}

document.addEventListener('keydown', e => { if(e.key==='Enter') doConvert(mode); });

async function doConvert(m) {
  const inputId = m === 'tod' ? 'val-tod' : 'val-'+m;
  const val = document.getElementById(inputId).value.trim();
  if (!val) return;
  const r = await fetch('/api/convert', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:m, value:val})});
  const d = await r.json();
  if (d.error) {
    document.getElementById('results').innerHTML =
      `<div class="empty-msg" style="color:#fc8181">⚠ ${d.error}</div>`;
    return;
  }
  renderResults(d.results);
}

function renderResults(results) {
  const c = document.getElementById('results');
  c.innerHTML = ['BTGL','BCAST'].map(t => {
    const r = results[t];
    if (!r) return `<div class="rc rc-${t} na">
      <div class="rc-label-row"><span class="rc-label">${t}</span></div>
      <div class="rc-na-msg">File not loaded</div></div>`;
    if (!r.available) return `<div class="rc rc-${t} na">
      <div class="rc-label-row"><span class="rc-label">${t}</span></div>
      <div class="rc-na-msg">${r.reason}</div></div>`;

    const countdown = (t==='BCAST' && r.bcast_countdown)
      ? `<div class="rc-row">
          <div class="rc-row-label">Countdown clock</div>
          <div class="rc-val countdown-val" onclick="copyVal(this)">${r.bcast_countdown}
            <span class="copy-hint">copy</span></div></div>` : '';

    return `<div class="rc rc-${t} active">
      <div class="rc-label-row">
        <span class="rc-label">${t}</span>
        <span class="rc-fname" title="${r.filename}">${r.filename||''}</span>
      </div>
      <div class="rc-row">
        <div class="rc-row-label">File Elapsed (MM:SS:FF)</div>
        <div class="rc-val" onclick="copyVal(this)">${r.file_elapsed}
          <span class="copy-hint">copy</span></div></div>
      <div class="rc-row">
        <div class="rc-row-label">Embedded TC (HH:MM:SS:FF)</div>
        <div class="rc-val" onclick="copyVal(this)">${r.embedded_tc}
          <span class="copy-hint">copy</span></div></div>
      ${countdown}
    </div>`;
  }).join('');
}

function copyVal(el) {
  const text = el.childNodes[0].textContent.trim();
  navigator.clipboard.writeText(text).then(()=>{
    el.classList.add('copied');
    setTimeout(()=>el.classList.remove('copied'), 1200);
  });
}
function copyText(t) {
  navigator.clipboard.writeText(t);
}

init();
</script>
</body>
</html>"""

# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def full_state(self, scan_err=None):
        """Standard response payload sent after any state-changing operation."""
        return {
            "files":               {k: {kk: vv for kk, vv in v.items() if kk != "path"}
                                    for k, v in state["files"].items()},
            "scan_error":          scan_err,
            "media_folder":        state["media_folder"],
            "broadcast":           state["broadcast"],
            "startup_errors":      state["startup_errors"],
            "quarter":             state["quarter"],
            "available_quarters":  state["available_quarters"],
            "calibration":         {k: v for k, v in state["calibration"].items() if k != "lookup"},
            "is_windows":          IS_WINDOWS,
        }

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/init":
            files, available, scan_err = scan_media_folder()
            apply_scan(files, available, scan_err)
            self.send_json(self.full_state(scan_err))
            return

        if path == "/api/calibration_status":
            cal = state["calibration"]
            self.send_json({k: v for k, v in cal.items() if k != "lookup"})
            return

        if path == "/api/browse_folder":
            folder, err = browse_for_folder()
            if folder:
                state["media_folder"] = folder
                state["calibrations"] = {}
                files, available, scan_err = scan_media_folder()
                apply_scan(files, available, scan_err)
                self.send_json(self.full_state(scan_err))
            else:
                self.send_json({"error": err})
            return

        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        raw  = self.read_body()
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        # ── Set folder ──
        if path == "/api/set_folder":
            folder = payload.get("path", "").strip()
            if folder:
                state["media_folder"] = folder
                state["calibrations"] = {}   # reset all calibrations for new folder
                files, available, scan_err = scan_media_folder()
                apply_scan(files, available, scan_err)
                self.send_json(self.full_state(scan_err))
            else:
                self.send_json({"error": "No path provided"})
            return

        # ── Set quarter ──
        if path == "/api/set_quarter":
            quarter = payload.get("quarter", "").strip().upper()
            if quarter in state["available_quarters"]:
                files, available, scan_err = scan_media_folder(quarter)
                apply_scan(files, available, scan_err, quarter)
                self.send_json(self.full_state(scan_err))
            else:
                self.send_json({"error": f"Quarter {quarter} not available"})
            return

        # ── Set broadcast preset ──
        if path == "/api/set_broadcast":
            btype = payload.get("type", "fox")
            if btype in BROADCAST_PRESETS:
                state["broadcast"] = btype
            self.send_json({"ok": True})
            return

        # ── Start OCR calibration ──
        if path == "/api/calibrate":
            if state["calibration"]["status"] != "running":
                threading.Thread(target=run_calibration, daemon=True).start()
            self.send_json({"started": True})
            return

        # ── Manual calibration sync ──
        if path == "/api/manual_calibrate":
            try:
                def parse_ms(s):
                    m = re.match(r"^(\d+):(\d{2})$", s.strip())
                    if not m: raise ValueError(f"Bad MM:SS: {s}")
                    return int(m.group(1)) * 60 + int(m.group(2))
                sync_e  = parse_ms(payload.get("elapsed", ""))
                sync_cd = parse_ms(payload.get("countdown", ""))
                qtr     = parse_ms(payload.get("qtr_duration", ""))
                game_start = sync_e - (qtr - sync_cd)
                bcast    = state["files"].get("BCAST")
                max_e    = bcast["duration_s"] if bcast else 1800
                lookup   = []
                for s in range(0, int(max_e) + 1, 15):
                    elapsed_game = s - game_start
                    cd = max(0.0, float(qtr - elapsed_game)) if elapsed_game >= 0 else float(qtr)
                    lookup.append({"file_elapsed_s": float(s), "countdown_s": cd})
                state["calibration"]["lookup"]  = lookup
                state["calibration"]["status"]  = "done"
                state["calibration"]["message"] = (
                    f"Manual sync applied (linear). "
                    f"Sync: {sync_e//60}:{sync_e%60:02d} elapsed = {sync_cd//60}:{sync_cd%60:02d} remaining."
                )
                self.send_json({"calibration": {k: v for k, v in state["calibration"].items() if k != "lookup"}})
            except Exception as e:
                self.send_json({"error": str(e)})
            return

        # ── OCR a single frame (sent as raw JPEG from client) ──
        if path == "/api/ocr_frame":
            cd = ocr_jpeg_bytes(raw)
            self.send_json({"countdown_s": cd})
            return

        # ── Convert ──
        if path == "/api/convert":
            result = convert(payload.get("mode"), payload.get("value", ""))
            self.send_json(result)
            return

        self.send_response(404); self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", action="store_true",
                        help="Bind to 0.0.0.0 so others on your network can connect")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    host = "0.0.0.0" if args.network else "127.0.0.1"
    port = args.port

    state["startup_errors"] = check_dependencies()

    print(f"\n{'='*55}")
    print(f"  AFL Timecode Converter")
    print(f"{'='*55}")

    if state["startup_errors"]:
        for e in state["startup_errors"]:
            print(f"\n  ⚠  {e['title']}")
            print(f"     {e['fix']}")
    else:
        print(f"  ffprobe : {FFPROBE}")
        print(f"  ffmpeg  : {FFMPEG}")

    files, available, err = scan_media_folder()
    apply_scan(files, available, err)
    print(f"\n  Media folder: {state['media_folder']}")
    print(f"  Quarters found: {available or 'none'}")
    for t, info in files.items():
        print(f"  {t:6s} → {info['filename']}  (TC: {info['tc_str']})")
    if not files:
        print("  No media files detected yet — set the folder in the app.")

    local_ip = get_local_ip()
    print(f"\n  Open in browser: http://localhost:{port}")
    if args.network:
        print(f"  Network access:  http://{local_ip}:{port}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*55}\n")

    server = HTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
