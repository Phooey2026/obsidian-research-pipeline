#!/usr/bin/env python3
"""
tts_server.py — On-demand text-to-speech for the Obsidian Capital dashboard
===========================================================================
Small standalone HTTP service (port 8646) that turns Atlas's weekly Outlook
briefing and Jansky's executive briefing into spoken audio via an OpenRouter
TTS model.  It is NOT part of the weekly run — audio is generated only when
you press the play button on the dashboard, so nothing is spent unless you
ask for it.

Model + key follow the same Hermes convention as weekly_macro.py:
    model    ← ~/.hermes/profiles/tts_agent/config.yaml   (model.default)
    API key  ← ~/.hermes/profiles/tts_agent/.env, then ~/.hermes/.env,
               then the OPENROUTER_API_KEY environment variable
Change the model any time with Hermes; nothing is hardcoded here.

Everything else lives in an optional "tts" block of obsidian_config.json:
    "tts": {
      "profile": "tts_agent",
      "port": 8646,
      "voices": {"atlas": "Zephyr", "jansky": "Orus"},
      "instructions": "",          # optional delivery/style prompt
      "chunk_chars": 3000,         # max characters per TTS request
      "gap_ms": 350,               # silence inserted between chunks
      "keep_days": 90,             # audio older than this is pruned at start
      "pronunciations": {}         # extra "written": "spoken" replacements
    }

Endpoints (all JSON unless noted; CORS open — LAN only):
    GET  /api/tts/health
    GET  /api/tts/status?report=atlas|jansky
    POST /api/tts/generate   {"report": "...", "force": false}
    GET  /api/tts/voices?report=...&refresh=1
    POST /api/tts/voice      {"report": "...", "voice": "..."}
    GET  /audio/<name>.mp3   (audio/mpeg, supports Range for iPad/Safari)

Usage:
    python3 tts_server.py                 # run the service
    python3 tts_server.py --show atlas    # print the exact script that would
                                          # be spoken + estimate (no API call)
"""

from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

# ─── Paths / constants ───────────────────────────────────────────────────────
BASE_DIR    = os.environ.get("STOCK_BASE_DIR", "/home/jay/stock_dashboard")
REPORT_DIR  = f"{BASE_DIR}/reports"
AUDIO_DIR   = f"{REPORT_DIR}/audio"
WORK_DIR    = f"{AUDIO_DIR}/.work"
CONFIG_PATH = f"{BASE_DIR}/obsidian_config.json"
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))

# TTS_API_BASE exists so the service can be pointed at a mock for testing.
API_BASE    = os.environ.get("TTS_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
SAMPLE_RATE = 24000          # Gemini TTS returns 24 kHz, 16-bit, mono PCM
BYTES_PER_S = SAMPLE_RATE * 2

REPORTS = {
    "atlas": {
        "label": "Atlas — Weekly Economic Briefing",
        "intro": "",
    },
    "jansky": {
        "label": "Jansky — Executive Briefing",
        "intro": "Executive briefing from Jansky.",
    },
}

DEFAULT_TTS_CONFIG = {
    "profile":        "tts_agent",
    "port":           8646,
    "voices":         {"atlas": "Zephyr", "jansky": "Orus"},
    "instructions":   "",
    "chunk_chars":    3000,
    "gap_ms":         350,
    "keep_days":      90,
    "pronunciations": {},
}

# Used only when OpenRouter's models API does not return a voice list.
GEMINI_FALLBACK_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]

# Acronyms kept in capitals when an ALL-CAPS heading is converted to sentence case.
KEEP_CAPS = {
    "CPI", "PCE", "PPI", "GDP", "FOMC", "FED", "SOFR", "WTI", "OPEC", "ETF",
    "ETFS", "CCC", "AI", "US", "USD", "FRED", "BLS", "EPS", "PE", "SEC",
}

# Light defaults only — Gemini already handles most financial lingo well.
DEFAULT_PRONUNCIATIONS = {
    "YoY": "year over year",
    "MoM": "month over month",
    "QoQ": "quarter over quarter",
    "WoW": "week over week",
}

_CFG_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ─── Configuration ───────────────────────────────────────────────────────────
def _read_obsidian_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"⚠ Could not read obsidian_config.json: {e}")
        return {}


def load_tts_config() -> dict:
    """Defaults, overridden by the optional "tts" block of obsidian_config.json."""
    cfg = json.loads(json.dumps(DEFAULT_TTS_CONFIG))
    user = _read_obsidian_config().get("tts", {})
    if isinstance(user, dict):
        for k, v in user.items():
            if k == "voices" and isinstance(v, dict):
                cfg["voices"].update(v)
            else:
                cfg[k] = v
    return cfg


def save_voice(report: str, voice: str) -> None:
    """Persist a voice choice into obsidian_config.json (atomic write)."""
    with _CFG_LOCK:
        data = _read_obsidian_config()
        tts = data.setdefault("tts", {})
        tts.setdefault("voices", {})[report] = voice
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)


def _parse_env_file(path: str) -> dict:
    pairs = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                pairs[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return pairs


def load_profile(profile: str) -> dict:
    """
    Read the TTS model and OpenRouter key from the Hermes profile, using the
    same precedence as weekly_macro.load_atlas_config().
    Returns {"model", "api_key", "profile_dir"} — empty strings if missing.
    """
    pdir = os.path.join(HERMES_HOME, "profiles", profile)
    model = ""
    cfg_path = os.path.join(pdir, "config.yaml")
    if os.path.exists(cfg_path):
        try:
            import yaml  # present on the Jetson; regex fallback below if not
            conf = yaml.safe_load(open(cfg_path, "r", encoding="utf-8")) or {}
            m = conf.get("model")
            if isinstance(m, dict):
                model = str(m.get("default") or "")
            elif isinstance(m, str):
                model = m
        except Exception:
            try:
                lines = open(cfg_path, "r", encoding="utf-8").read().splitlines()
                for n, ln in enumerate(lines):
                    m = re.match(r"^model[ \t]*:[ \t]*(.*)$", ln)
                    if not m:
                        continue
                    inline = m.group(1).split("#")[0].strip()
                    if inline:                              # model: some/slug
                        model = inline
                        break
                    for sub in lines[n + 1:]:               # model:\n  default: slug
                        if sub.strip() and not sub.startswith((" ", "\t")):
                            break
                        d = re.match(r"^[ \t]+default[ \t]*:[ \t]*(.+)$", sub)
                        if d:
                            model = d.group(1).split("#")[0].strip()
                            break
                    break
            except Exception:
                pass
    model = model.strip().strip("\"'")
    if model.startswith("openrouter/"):
        model = model[len("openrouter/"):]

    prof_env    = _parse_env_file(os.path.join(pdir, ".env"))
    default_env = _parse_env_file(os.path.join(HERMES_HOME, ".env"))
    api_key = (prof_env.get("OPENROUTER_API_KEY")
               or default_env.get("OPENROUTER_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY", ""))
    return {"model": model, "api_key": api_key, "profile_dir": pdir}


# ─── Source text ─────────────────────────────────────────────────────────────
def extract_briefing(synthesis: str) -> str:
    """
    Pull just the EXECUTIVE BRIEFING out of Jansky's full synthesis (which also
    holds cross-pipeline bullets and ACTION/REASON recommendations).
    """
    lines = synthesis.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if len(ln.strip()) < 90 and re.search(r"EXECUTIVE\s+BRIEFING", ln, re.I):
            start = i + 1
            break
    end = len(lines)
    for i in range(start, len(lines)):
        ln = lines[i]
        if len(ln.strip()) < 100 and re.search(
                r"CROSS[-\s]PIPELINE|PIPELINE\s+RECOMMENDATIONS|OVERALL\s+POSTURE",
                ln, re.I):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def read_report_text(report: str) -> str:
    """Raw (markdown) text for a report. Raises FileNotFoundError/ValueError."""
    if report == "atlas":
        path = f"{BASE_DIR}/macro_summary.md"
        if not os.path.exists(path):
            raise FileNotFoundError("macro_summary.md not found — run weekly_macro.py")
        return open(path, "r", encoding="utf-8").read()
    if report == "jansky":
        path = f"{BASE_DIR}/jansky_latest.json"
        if not os.path.exists(path):
            raise FileNotFoundError("jansky_latest.json not found — run jansky_review.py")
        data = json.load(open(path, "r", encoding="utf-8"))
        synth = data.get("executive_summary", "") or ""
        text = extract_briefing(synth)
        if len(text) < 200:
            raise ValueError("Could not find an executive briefing in jansky_latest.json")
        return text
    raise ValueError(f"unknown report {report!r}")


# ─── Text preparation ────────────────────────────────────────────────────────
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_SYMBOLS = re.compile(
    "[←-⇿⌀-⏿■-➿⬀-⯿️\U0001F000-\U0001FAFF]")


def _is_caps_heading(s: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", s)
    return len(letters) >= 4 and letters.isupper() and len(s) < 100


def _decap(s: str, keep: set) -> str:
    words = []
    for i, w in enumerate(s.split()):
        core = re.sub(r"[^A-Za-z]", "", w)
        if core.upper() in keep:
            words.append(w)
        else:
            words.append(w.lower())
    out = " ".join(words)
    return out[:1].upper() + out[1:] if out else out


def _iso_date(m: re.Match) -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{_MONTHS[mo - 1]} {d}, {y}"
    return m.group(0)


def _short_date(m: re.Match) -> str:
    return f"{m.group(1)} {_MONTHS[int(m.group(2)) - 1]} {m.group(3)}"


def _inline_clean(s: str, pron: dict) -> str:
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)        # markdown links
    s = re.sub(r"\[([^\]]*)\]", r"\1", s)                   # [tags] -> text
    s = s.replace("`", "")
    s = re.sub(r"\*\*|__", "", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", s)
    s = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", _iso_date, s)
    s = re.sub(r"(?i)\b(as of|on|since|through|by|from|until|before|after|of)\s+"
               r"(1[0-2]|[1-9])/(3[01]|[12]\d|[1-9])\b(?![/\d])", _short_date, s)
    s = re.sub(r"\s*[—–-]+\s*$", "", s)                    # dangling dash ("J.B. —")
    s = s.replace("→", " to ").replace("≥", " at least ").replace("≤", " at most ")
    s = _SYMBOLS.sub("", s)
    s = re.sub(r"(?<=[\d%])\s*[–]\s*(?=[\d$])", " to ", s)  # 3.75%–4.00%
    s = re.sub(r"(?<=[\d%])-(?=\$?\d)", " to ", s)            # 4-8 weeks, 3.5%-4%
    s = re.sub(r"(?<!\w)~(?=\$?\d)", "about ", s)
    s = re.sub(r"(\d)\s*bps?\b(?=\s+(?:hike|cut|move|increase|rise|decline|drop|gap|"
               r"spread|shift|adjustment|premium|jump|surprise)\b)", r"\1 basis point", s)
    s = re.sub(r"(\d)\s*bps\b", r"\1 basis points", s)
    s = re.sub(r"(\d)\s*bp\b", r"\1 basis points", s)
    s = re.sub(r"\bbps\b", "basis points", s)
    s = re.sub(r"(?<![\w.])\+(?=\$?\d[\d.,]*\s*basis points)", "plus ", s)
    s = re.sub(r"(?<![\w.])\+(?=\$?\d)", "", s)           # "+4.02%" -> "4.02%"
    s = re.sub(r"\b(\d+)Y\b", r"\1-year", s)
    s = s.replace("%", " percent").replace("&", " and ")
    for written in sorted(pron, key=len, reverse=True):
        s = re.sub(rf"(?<![\w]){re.escape(written)}(?![\w])", pron[written], s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return s.strip()


def prepare_paragraphs(raw: str, report: str, cfg: dict) -> list[str]:
    """Markdown/report text -> list of speakable paragraphs."""
    pron = dict(DEFAULT_PRONUNCIATIONS)
    pron.update(cfg.get("pronunciations") or {})
    keep = set(KEEP_CAPS) | {k.upper() for k in pron}
    paras: list[str] = []
    in_fence = False
    for line in raw.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s:
            continue
        if re.fullmatch(r"[=\-_*~#\s|:]{3,}", s):                 # dividers / table rules
            continue
        if s.startswith("|"):                                      # table row
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [c for c in cells if c]
            if not cells:
                continue
            s = ", ".join(cells) + "."
            paras.append(_inline_clean(s, pron))
            continue
        heading = False
        m = re.match(r"^#{1,6}\s*(.*)$", s)
        if m:
            s, heading = m.group(1), True
        bullet = bool(re.match(r"^\s*[-*•]\s+", s))
        s = re.sub(r"^\s*[-*•]\s+", "", s)                         # bullets
        n = re.match(r"^\d+[.)]\s+(.*)$", s)                       # "1. TITLE"
        if n and (_is_caps_heading(n.group(1)) or heading):
            s, heading = n.group(1), True
        elif n:
            s = n.group(1)
        s = re.sub(r"\*\*|__", "", s)
        if _is_caps_heading(s):
            s, heading = _decap(s, keep), True
        s = _inline_clean(s, pron)
        if not s or not re.search(r"[A-Za-z0-9]", s):
            continue
        if (heading or bullet) and not re.search(r"[.!?:]$", s):
            s += "."
        paras.append(s)
    intro = REPORTS[report]["intro"]
    if intro:
        paras.insert(0, intro)
    return paras


def chunk_paragraphs(paras: list[str], limit: int) -> list[str]:
    """Pack paragraphs into chunks <= limit chars, splitting only at paragraph
    (then sentence, then word) boundaries."""
    pieces: list[str] = []
    for p in paras:
        if len(p) <= limit:
            pieces.append(p)
            continue
        cur = ""
        for sent in re.split(r"(?<=[.!?])\s+", p):
            while len(sent) > limit:                               # pathological
                cut = sent.rfind(" ", 0, limit) or limit
                cut = cut if cut > 0 else limit
                if cur:
                    pieces.append(cur)
                    cur = ""
                pieces.append(sent[:cut])
                sent = sent[cut:].lstrip()
            if cur and len(cur) + 1 + len(sent) > limit:
                pieces.append(cur)
                cur = sent
            else:
                cur = f"{cur} {sent}".strip()
        if cur:
            pieces.append(cur)
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + 2 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


# ─── OpenRouter ──────────────────────────────────────────────────────────────
class TTSFatal(Exception):
    """Non-retryable API failure (bad key, no credits, bad voice, ...)."""


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Obsidian Capital TTS",
    }


def synth_chunk(text: str, voice: str, model: str, api_key: str,
                instructions: str = "", retries: int = 3,
                on_retry=None) -> tuple[bytes, str]:
    """One /audio/speech call -> (pcm_bytes, generation_id)."""
    body = {"model": model, "input": text, "voice": voice, "response_format": "pcm"}
    if instructions:
        body["instructions"] = instructions
    last = ""
    for attempt in range(1, retries + 1):
        wait = 5.0 * attempt
        try:
            r = requests.post(f"{API_BASE}/audio/speech", json=body,
                              headers=_headers(api_key), timeout=(15, 600))
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and "json" not in ctype.lower():
                data = r.content
                if len(data) >= 2400:
                    if len(data) % 2:
                        data = data[:-1]
                    return data, r.headers.get("X-Generation-Id", "")
                last = f"audio response too short ({len(data)} bytes)"
            else:
                snippet = r.text[:300].replace("\n", " ")
                last = f"HTTP {r.status_code}: {snippet}"
                if r.status_code in (400, 401, 402, 403, 404):
                    raise TTSFatal(last)
                if r.status_code == 429:
                    wait = max(wait, float(r.headers.get("Retry-After", 0) or 0))
        except TTSFatal:
            raise
        except requests.RequestException as e:
            last = f"network error: {e}"
        if attempt < retries:
            if on_retry:
                on_retry(attempt + 1, last)
            time.sleep(wait)
    raise RuntimeError(last or "TTS request failed")


_MODELS_CACHE: dict = {"t": 0.0, "model": "", "data": None}


def _normalize_voices(raw) -> list[str]:
    out = []
    for v in raw or []:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            name = v.get("id") or v.get("name") or v.get("voice")
            if name:
                out.append(str(name))
    return out


def fetch_model_info(model: str, api_key: str, refresh: bool = False) -> dict:
    """Pricing + supported voices for the model, from OpenRouter's models API
    (cached for an hour). Never raises."""
    now = time.time()
    c = _MODELS_CACHE
    if (not refresh and c["data"] is not None and c["model"] == model
            and now - c["t"] < 3600):
        return c["data"]
    info = {"pricing": None, "voices": [], "error": ""}
    hdrs = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    for url in (f"{API_BASE}/models?output_modalities=all", f"{API_BASE}/models"):
        try:
            r = requests.get(url, headers=hdrs, timeout=20)
            r.raise_for_status()
            for entry in r.json().get("data", []):
                if model in (entry.get("id"), entry.get("canonical_slug")):
                    info["pricing"] = entry.get("pricing")
                    info["voices"] = _normalize_voices(entry.get("supported_voices"))
                    info["error"] = ""
                    break
            if info["pricing"] is not None or info["voices"]:
                break
            info["error"] = "model not found in OpenRouter models list"
        except Exception as e:
            info["error"] = str(e)[:200]
    failed = info["pricing"] is None and not info["voices"]
    # A failed lookup is only cached for a minute so it retries soon.
    c.update({"t": now - 3540 if failed else now, "model": model, "data": info})
    return info


def estimate(words: int, chars: int, pricing: dict | None) -> tuple[float, float | None]:
    """(minutes of audio, rough USD). Calibrated on a real Gemini TTS probe
    (Oct 27 2026): ~15 characters of text per second of audio, ~25 audio
    tokens per second, ~4 chars per text token. The actual cost is read back
    from OpenRouter after each generation."""
    minutes = chars / 15.0 / 60.0
    cost = None
    try:
        if pricing:
            p_in = float(pricing.get("prompt") or 0)
            p_out = float(pricing.get("completion") or 0)
            if p_in or p_out:
                cost = (chars / 4.0) * p_in + (minutes * 60 * 25) * p_out
    except (TypeError, ValueError):
        cost = None
    return minutes, cost


# ─── Jobs ────────────────────────────────────────────────────────────────────
class Job:
    def __init__(self, report: str, key: str, total: int):
        self.report, self.key, self.total = report, key, total
        self.state = "running"
        self.done = 0
        self.message = "Starting…"
        self.error = ""
        self.started = time.time()


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _content_key(model: str, voice: str, instructions: str, text: str) -> str:
    h = hashlib.sha256("\x1f".join([model, voice, instructions, text]).encode("utf-8"))
    return h.hexdigest()[:16]


def _find_audio(report: str, key: str) -> str | None:
    hits = sorted(glob.glob(f"{AUDIO_DIR}/{report}_*_{key}.mp3"))
    return hits[-1] if hits else None


def _meta_path(mp3: str) -> str:
    return mp3[:-4] + ".json"


def _fetch_actual_cost(meta_path: str, gen_ids: list[str], api_key: str) -> None:
    """Best effort: ask OpenRouter what the generations cost and store it."""
    try:
        time.sleep(8)
        total, found = 0.0, 0
        for gid in gen_ids:
            if not gid:
                continue
            r = requests.get(f"{API_BASE}/generation", params={"id": gid},
                             headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
            if r.status_code == 200:
                cost = (r.json().get("data") or {}).get("total_cost")
                if cost is not None:
                    total += float(cost)
                    found += 1
        if found:
            meta = json.load(open(meta_path, "r", encoding="utf-8"))
            meta["cost_usd"] = round(total, 6)
            meta["cost_chunks_found"] = found
            json.dump(meta, open(meta_path, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass


def run_job(job: Job, chunks: list[str], voice: str, model: str, api_key: str,
            instructions: str, gap_ms: int, text_chars: int) -> None:
    work = f"{WORK_DIR}/{job.report}_{job.key}"
    try:
        os.makedirs(work, exist_ok=True)
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is not installed on this machine")
        gen_ids: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            cpath = f"{work}/chunk_{i:03d}.pcm"
            ipath = f"{work}/chunk_{i:03d}.id"
            if os.path.exists(cpath) and os.path.getsize(cpath) > 0:   # resume
                job.done = i
                if os.path.exists(ipath):
                    gen_ids.append(open(ipath).read().strip())
                continue
            job.message = f"Generating part {i} of {len(chunks)}"

            def _retry(n, why, i=i):
                job.message = f"Part {i}: retrying (attempt {n}) — {why[:80]}"

            pcm, gid = synth_chunk(chunk, voice, model, api_key, instructions,
                                   on_retry=_retry)
            with open(cpath + ".tmp", "wb") as f:
                f.write(pcm)
            os.replace(cpath + ".tmp", cpath)
            open(ipath, "w").write(gid)
            gen_ids.append(gid)
            job.done = i
            log(f"{job.report}: part {i}/{len(chunks)} ok "
                f"({len(pcm) / BYTES_PER_S:.0f}s audio)")

        job.message = "Encoding MP3…"
        gap = b"\x00" * (int(SAMPLE_RATE * gap_ms / 1000) * 2)
        allpcm = f"{work}/all.pcm"
        total_bytes = 0
        with open(allpcm, "wb") as out:
            for i in range(1, len(chunks) + 1):
                if i > 1:
                    out.write(gap)
                    total_bytes += len(gap)
                data = open(f"{work}/chunk_{i:03d}.pcm", "rb").read()
                out.write(data)
                total_bytes += len(data)

        stamp = datetime.date.today().strftime("%Y%m%d")
        final = f"{AUDIO_DIR}/{job.report}_{stamp}_{job.key}.mp3"
        part = final + ".part"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", "1", "-i", allpcm, "-codec:a", "libmp3lame", "-q:a", "4",
             "-f", "mp3", part],
            capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(part):
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[:200]}")
        os.replace(part, final)

        meta = {
            "report": job.report, "voice": voice, "model": model,
            "chars": text_chars, "chunks": len(chunks),
            "duration_s": round(total_bytes / BYTES_PER_S, 1),
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "generation_ids": gen_ids,
        }
        json.dump(meta, open(_meta_path(final), "w", encoding="utf-8"), indent=2)
        shutil.rmtree(work, ignore_errors=True)
        job.state, job.message = "done", "Ready"
        log(f"{job.report}: finished {os.path.basename(final)} "
            f"({meta['duration_s']:.0f}s)")
        threading.Thread(target=_fetch_actual_cost,
                         args=(_meta_path(final), gen_ids, api_key), daemon=True).start()
    except TTSFatal as e:
        job.state, job.error = "error", str(e)
        job.message = f"Stopped: {e}"
        log(f"{job.report}: FATAL {e}")
    except Exception as e:
        job.state, job.error = "error", str(e)
        job.message = f"Failed: {e}"
        log(f"{job.report}: ERROR {e}")


# ─── Request logic ───────────────────────────────────────────────────────────
class Prepared:
    """Everything derived from the current report text + config."""

    def __init__(self, report: str):
        self.report = report
        self.cfg = load_tts_config()
        self.profile = load_profile(self.cfg["profile"])
        self.model = self.profile["model"]
        self.voice = self.cfg["voices"].get(report) or ""
        self.instructions = self.cfg.get("instructions") or ""
        self.error = ""
        self.paras: list[str] = []
        self.chunks: list[str] = []
        self.text = ""
        try:
            raw = read_report_text(report)
            self.paras = prepare_paragraphs(raw, report, self.cfg)
            self.chunks = chunk_paragraphs(self.paras, int(self.cfg["chunk_chars"]))
            self.text = "\n\n".join(self.paras)
        except (FileNotFoundError, ValueError) as e:
            self.error = str(e)
        self.key = _content_key(self.model, self.voice, self.instructions, self.text)
        self.words = len(self.text.split())
        self.chars = len(self.text)


def status_for(report: str) -> dict:
    if report not in REPORTS:
        return {"state": "error", "message": f"unknown report {report!r}"}
    p = Prepared(report)
    base = {"report": report, "label": REPORTS[report]["label"], "voice": p.voice,
            "model": p.model}
    if p.error:
        return {**base, "state": "unavailable", "message": p.error}
    if not p.model or not p.profile["api_key"]:
        need = []
        if not p.model:
            need.append(f"model.default in the Hermes '{p.cfg['profile']}' profile")
        if not p.profile["api_key"]:
            need.append("OPENROUTER_API_KEY in that profile's .env (or ~/.hermes/.env)")
        return {**base, "state": "unconfigured",
                "message": "Not configured: set " + " and ".join(need)}

    info = fetch_model_info(p.model, p.profile["api_key"])
    minutes, cost = estimate(p.words, p.chars, info.get("pricing"))
    base.update({"words": p.words, "chars": p.chars, "chunks": len(p.chunks),
                 "est_minutes": round(minutes, 1),
                 "est_cost": None if cost is None else round(cost, 4)})

    mp3 = _find_audio(report, p.key)
    if mp3:
        meta = {}
        try:
            meta = json.load(open(_meta_path(mp3), "r", encoding="utf-8"))
        except Exception:
            pass
        return {**base, "state": "ready", "audio_url": f"/audio/{os.path.basename(mp3)}",
                "duration_s": meta.get("duration_s"), "cost_usd": meta.get("cost_usd"),
                "generated_at": meta.get("generated_at")}
    with JOBS_LOCK:
        job = JOBS.get(p.key)
    if job and job.state == "running":
        return {**base, "state": "running", "done": job.done, "total": job.total,
                "message": job.message}
    if job and job.state == "error":
        return {**base, "state": "error", "done": job.done, "total": job.total,
                "message": job.message}
    return {**base, "state": "idle"}


def start_generation(report: str, force: bool = False) -> dict:
    if report not in REPORTS:
        return {"state": "error", "message": f"unknown report {report!r}"}
    p = Prepared(report)
    if p.error:
        return {"state": "unavailable", "message": p.error}
    if not p.model or not p.profile["api_key"]:
        return status_for(report)
    if not p.voice:
        return {"state": "error", "message": "No voice selected"}
    if not p.chunks:
        return {"state": "error", "message": "Nothing to read in this report"}

    with JOBS_LOCK:
        job = JOBS.get(p.key)
        if job and job.state == "running":
            return status_for(report)
        existing = _find_audio(report, p.key)
        if existing and not force:
            return status_for(report)
        if force:
            for path in glob.glob(f"{AUDIO_DIR}/{report}_*_{p.key}.*"):
                os.remove(path)
            shutil.rmtree(f"{WORK_DIR}/{report}_{p.key}", ignore_errors=True)
        job = Job(report, p.key, len(p.chunks))
        JOBS[p.key] = job
    log(f"{report}: generating {len(p.chunks)} part(s), {p.chars} chars, "
        f"voice={p.voice}, model={p.model}")
    threading.Thread(
        target=run_job,
        args=(job, p.chunks, p.voice, p.model, p.profile["api_key"],
              p.instructions, int(p.cfg["gap_ms"]), p.chars),
        daemon=True).start()
    return status_for(report)


def voices_for(report: str, refresh: bool) -> dict:
    cfg = load_tts_config()
    prof = load_profile(cfg["profile"])
    current = cfg["voices"].get(report, "")
    voices, source = [], "none"
    if prof["model"]:
        info = fetch_model_info(prof["model"], prof["api_key"], refresh=refresh)
        voices = info.get("voices") or []
        source = "openrouter" if voices else "none"
        if not voices and prof["model"].startswith("google/gemini"):
            voices, source = list(GEMINI_FALLBACK_VOICES), "fallback"
    if current and current.lower() not in [v.lower() for v in voices]:
        voices = [current] + voices
    return {"report": report, "current": current, "voices": voices, "source": source,
            "model": prof["model"]}


# ─── Housekeeping ────────────────────────────────────────────────────────────
def prune_old(keep_days: int) -> None:
    cutoff = time.time() - keep_days * 86400
    n = 0
    names = "|".join(REPORTS)
    for path in glob.glob(f"{AUDIO_DIR}/*"):
        if (os.path.isfile(path)
                and re.fullmatch(rf"(?:{names})_\d{{8}}_[0-9a-f]{{16}}\.(?:mp3|json)",
                                 os.path.basename(path))
                and os.path.getmtime(path) < cutoff):
            os.remove(path)
            n += 1
    for path in glob.glob(f"{WORK_DIR}/*"):
        if os.path.isdir(path) and os.path.getmtime(path) < time.time() - 7 * 86400:
            shutil.rmtree(path, ignore_errors=True)
            n += 1
    if n:
        log(f"Pruned {n} old audio item(s)")


# ─── HTTP ────────────────────────────────────────────────────────────────────
AUDIO_NAME_RE = re.compile(rf"(?:{'|'.join(REPORTS)})_\d{{8}}_[0-9a-f]{{16}}\.mp3")
VOICE_RE = re.compile(r"^[A-Za-z0-9_\-: .]{1,64}$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ObsidianTTS/1.0"

    def log_message(self, fmt, *args):          # keep the journal quiet
        if "/api/tts/status" not in (args[0] if args else ""):
            log(f"{self.address_string()} {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")

    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        report = (q.get("report") or [""])[0]
        try:
            if u.path == "/api/tts/health":
                return self._json({"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))})
            if u.path == "/api/tts/status":
                return self._json(status_for(report))
            if u.path == "/api/tts/voices":
                if report not in REPORTS:
                    return self._json({"error": "unknown report"}, 400)
                refresh = (q.get("refresh") or ["0"])[0] == "1"
                return self._json(voices_for(report, refresh))
            if u.path.startswith("/audio/"):
                return self._serve_audio(u.path[len("/audio/"):])
        except Exception as e:
            log(f"GET {u.path} failed: {e}")
            return self._json({"state": "error", "message": str(e)}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._read_json()
        report = str(body.get("report", ""))
        try:
            if u.path == "/api/tts/generate":
                if report not in REPORTS:
                    return self._json({"error": "unknown report"}, 400)
                return self._json(start_generation(report, bool(body.get("force"))))
            if u.path == "/api/tts/voice":
                voice = str(body.get("voice", "")).strip()
                if report not in REPORTS or not VOICE_RE.match(voice):
                    return self._json({"error": "bad report or voice"}, 400)
                known = {v.lower(): v for v in voices_for(report, False)["voices"]}
                save_voice(report, known.get(voice.lower(), voice))
                log(f"{report}: voice set to {known.get(voice.lower(), voice)}")
                return self._json(status_for(report))
        except Exception as e:
            log(f"POST {u.path} failed: {e}")
            return self._json({"state": "error", "message": str(e)}, 500)
        self._json({"error": "not found"}, 404)

    def _serve_audio(self, name: str):
        if not AUDIO_NAME_RE.fullmatch(name):
            return self._json({"error": "not found"}, 404)
        path = os.path.join(AUDIO_DIR, name)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                   # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self._cors()
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(65536, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ─── CLI ─────────────────────────────────────────────────────────────────────
def show(report: str) -> int:
    p = Prepared(report)
    if p.error:
        print(f"✗ {p.error}")
        return 1
    print(f"Report: {REPORTS[report]['label']}")
    print(f"Model:  {p.model or '(not set in Hermes profile)'}   Voice: {p.voice}")
    print("=" * 70)
    print(p.text)
    print("=" * 70)
    info = ({"pricing": None} if not (p.model and p.profile["api_key"])
            else fetch_model_info(p.model, p.profile["api_key"]))
    minutes, cost = estimate(p.words, p.chars, info.get("pricing"))
    sizes = ", ".join(str(len(c)) for c in p.chunks)
    print(f"{p.words} words · {p.chars} chars · {len(p.chunks)} chunk(s) [{sizes}]")
    print(f"≈ {minutes:.1f} min of audio · rough cost "
          f"{'unknown' if cost is None else f'${cost:.3f}'}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Obsidian Capital TTS service")
    ap.add_argument("--show", choices=sorted(REPORTS),
                    help="print the cleaned script + estimate for a report and exit")
    ap.add_argument("--port", type=int, help="override port (default 8646)")
    args = ap.parse_args()
    if args.show:
        sys.exit(show(args.show))

    cfg = load_tts_config()
    port = args.port or int(cfg["port"])
    os.makedirs(AUDIO_DIR, exist_ok=True)
    prune_old(int(cfg["keep_days"]))
    prof = load_profile(cfg["profile"])
    log(f"Obsidian TTS service — port {port}")
    log(f"  base dir : {BASE_DIR}")
    log(f"  profile  : {cfg['profile']}  model={prof['model'] or '(unset)'}  "
        f"key={'present' if prof['api_key'] else 'MISSING'}")
    log(f"  ffmpeg   : {'ok' if shutil.which('ffmpeg') else 'MISSING'}")
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")


if __name__ == "__main__":
    main()
