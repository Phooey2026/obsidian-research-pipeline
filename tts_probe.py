#!/usr/bin/env python3
"""
tts_probe.py — one-shot check of the TTS route before trusting the service
==========================================================================
Run this ONCE on the Jetson. It reads the model + key from the tts_agent
Hermes profile (same as tts_server.py), makes a single short TTS request
(a few cents at most), and reports what actually came back:

    python3 tts_probe.py                    # default sample, Atlas's voice
    python3 tts_probe.py --voice Orus       # try another voice
    python3 tts_probe.py --list-voices      # voices OpenRouter reports for the model
    python3 tts_probe.py --text "Your own sentence here."

It writes reports/audio/probe.mp3 so you can listen to it, and prints the
audio length, HTTP status/headers and (best effort) the real cost.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import requests

import tts_server as T

SAMPLE = ("Good afternoon. This is a short test of the Obsidian Capital briefing voice. "
          "The Fed held rates at three point eight eight percent, headline CPI rose "
          "four point oh two percent year over year, and the ten year yield is four "
          "point nine six percent. Core PCE is running up forty-one basis points "
          "from its February low.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", help="voice name (default: Atlas's configured voice)")
    ap.add_argument("--text", default=SAMPLE)
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args()

    cfg = T.load_tts_config()
    prof = T.load_profile(cfg["profile"])
    voice = args.voice or cfg["voices"].get("atlas", "Zephyr")

    print(f"Hermes profile : {cfg['profile']}  ({prof['profile_dir']})")
    print(f"Model          : {prof['model'] or '(NOT SET — hermes --profile ' + cfg['profile'] + ' config set model.default <slug>)'}")
    print(f"API key        : {'found' if prof['api_key'] else 'MISSING (OPENROUTER_API_KEY in the profile .env)'}")
    print(f"ffmpeg         : {'ok' if T.shutil.which('ffmpeg') else 'MISSING'}")
    print(f"Endpoint       : {T.API_BASE}/audio/speech")
    if not prof["model"] or not prof["api_key"]:
        return 1

    info = T.fetch_model_info(prof["model"], prof["api_key"], refresh=True)
    print(f"Pricing (listed): {info.get('pricing')}")
    vlist = info.get("voices") or []
    print(f"Voices (OpenRouter): {len(vlist)} listed"
          + (f"  — {error_note(info)}" if not vlist else ""))
    if args.list_voices:
        for v in vlist or T.GEMINI_FALLBACK_VOICES:
            print("  ", v)
        if not vlist:
            print("   (OpenRouter returned none — above is the built-in Gemini list)")
        return 0

    print(f"\nRequesting {len(args.text)} chars with voice '{voice}' …")
    t0 = time.time()
    try:
        r = requests.post(f"{T.API_BASE}/audio/speech",
                          json={"model": prof["model"], "input": args.text,
                                "voice": voice, "response_format": "pcm",
                                **({"instructions": cfg["instructions"]}
                                   if cfg.get("instructions") else {})},
                          headers=T._headers(prof["api_key"]), timeout=(15, 300))
    except requests.RequestException as e:
        print(f"✗ Network error: {e}")
        return 1
    dt = time.time() - t0
    ctype = r.headers.get("Content-Type", "")
    print(f"HTTP {r.status_code}  ·  Content-Type: {ctype}  ·  {len(r.content)} bytes  ·  {dt:.1f}s")
    gid = r.headers.get("X-Generation-Id", "")
    print(f"X-Generation-Id: {gid or '(none)'}")
    if r.status_code != 200 or "json" in ctype.lower():
        print(f"✗ Not audio. Body: {r.text[:500]}")
        return 1

    pcm = r.content[: len(r.content) // 2 * 2]
    secs = len(pcm) / T.BYTES_PER_S
    print(f"Audio          : {secs:.1f} s at {T.SAMPLE_RATE} Hz, 16-bit mono "
          f"(assumed — if it sounds sped-up or slowed, tell me)")

    os.makedirs(T.AUDIO_DIR, exist_ok=True)
    pcm_path = f"{T.AUDIO_DIR}/probe.pcm"
    mp3_path = f"{T.AUDIO_DIR}/probe.mp3"
    open(pcm_path, "wb").write(pcm)
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar",
                        str(T.SAMPLE_RATE), "-ac", "1", "-i", pcm_path,
                        "-codec:a", "libmp3lame", "-q:a", "4", "-f", "mp3", mp3_path],
                       capture_output=True, text=True)
    os.remove(pcm_path)
    if p.returncode != 0:
        print(f"✗ ffmpeg failed: {p.stderr[:300]}")
        return 1
    print(f"Saved          : {mp3_path}   (play it to check the voice)")

    if gid:
        print("\nLooking up the real cost (can take a few seconds to appear) …")
        for _ in range(4):
            time.sleep(4)
            try:
                g = requests.get(f"{T.API_BASE}/generation", params={"id": gid},
                                 headers={"Authorization": f"Bearer {prof['api_key']}"},
                                 timeout=20)
                if g.status_code == 200:
                    d = g.json().get("data") or {}
                    print(f"Cost           : ${d.get('total_cost')}   "
                          f"(tokens: prompt={d.get('tokens_prompt')}, "
                          f"completion={d.get('tokens_completion')})")
                    print(f"→ {len(args.text)} chars ≈ {secs:.1f}s of audio; scale up from this "
                          f"for a full briefing.")
                    return 0
            except requests.RequestException:
                pass
        print("Cost lookup not available yet — check the OpenRouter activity page.")
    return 0


def error_note(info: dict) -> str:
    return info.get("error") or "model has no supported_voices field"


if __name__ == "__main__":
    sys.exit(main())
