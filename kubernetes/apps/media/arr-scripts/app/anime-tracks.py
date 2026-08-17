#!/usr/bin/env python3
"""Default MKV tracks to Japanese audio + English dialogue subtitles.

Jellyfin cannot tell a "Signs & Songs" track from full dialogue — both are
tagged eng — and forced beats default in several clients
(jellyfin/jellyfin-android#1729). Fixing container flags is the only
reliable route. mkvpropedit rewrites header bits only; no re-encode.

Two entry points:
  pipeline  — Sonarr/Radarr Custom Script, reads env, acts on one file
  batch     — anime-tracks.py --apply /data/media/TV-Anime
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SIGNS = re.compile(r"sign|song|s&s|forced|karaoke|op/ed|typeset", re.I)
JPN = {"jpn", "ja", "jp"}
ENG = {"eng", "en"}
UND = {"und"}


def lang(t):
    p = t["properties"]
    return (p.get("language_ietf") or p.get("language") or "und").lower().split("-")[0]


def title(t):
    return t["properties"].get("track_name", "") or ""


def flag(t, name):
    return bool(t["properties"].get(name, False))


def plan(path):
    """Return ([(selector, flag, value)], note) or (None, reason)."""
    r = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None, "not a readable mkv"
    info = json.loads(r.stdout)

    audio = [t for t in info["tracks"] if t["type"] == "audio"]
    subs = [t for t in info["tracks"] if t["type"] == "subtitles"]
    edits, notes = [], []

    def pick(langs):
        return next((i for i, t in enumerate(audio)
                     if lang(t) in langs and not flag(t, "flag_commentary")), None)

    want_a, from_und = pick(JPN), False
    if want_a is None:
        want_a, from_und = pick(UND), True
    if want_a is None:
        notes.append("no jpn/und audio")
    else:
        for i, t in enumerate(audio):
            want = 1 if i == want_a else 0
            if int(t["properties"].get("default_track", False)) != want:
                edits.append((f"track:a{i + 1}", "flag-default", want))
        # Jellyfin will not honour an und track as the default, so retag it —
        # but only when it beat a real language, never a lone untagged track.
        if from_und and len(audio) > 1:
            edits.append((f"track:a{want_a + 1}", "language", "jpn"))
            notes.append(f"audio=a{want_a + 1} (und -> jpn)")
        elif from_und:
            notes.append(f"audio=a{want_a + 1} (REVIEW: only und audio, not retagged)")
        else:
            notes.append(f"audio=a{want_a + 1}")

    # Rank on the track name before the language tag: a dialogue track in any
    # language beats a signs/songs one, since groups routinely mistag dialogue
    # as jpn. Language only breaks ties between equally dialogue-like tracks.
    def sub_rank(i):
        t = subs[i]
        return (bool(SIGNS.search(title(t))),
                flag(t, "flag_hearing_impaired"),
                0 if lang(t) in ENG else 1 if lang(t) in JPN | UND else 2,
                i)

    cand = [i for i, t in enumerate(subs) if not flag(t, "flag_commentary")]
    if not cand:
        notes.append("no subs")
    else:
        want_s = min(cand, key=sub_rank)
        if SIGNS.search(title(subs[want_s])):
            notes.append("REVIEW: only signs-like subs")
        elif lang(subs[want_s]) not in ENG:
            notes.append(f"REVIEW: non-eng dialogue subs ({lang(subs[want_s])})")
        for i, t in enumerate(subs):
            want = 1 if i == want_s else 0
            if int(t["properties"].get("default_track", False)) != want:
                edits.append((f"track:s{i + 1}", "flag-default", want))
            # Forced beats default in several clients — clear it everywhere.
            if int(t["properties"].get("forced_track", False)) != 0:
                edits.append((f"track:s{i + 1}", "flag-forced", 0))
        notes.append(f"subs=s{want_s + 1} ({title(subs[want_s]) or lang(subs[want_s])})")

    return edits, "; ".join(notes)


def process(path, apply_changes, quiet=False):
    path = Path(path)
    if path.suffix.lower() != ".mkv":
        return False
    edits, note = plan(path)
    if edits is None:
        print(f"  skip  {path.name}: {note}")
        return False
    if not edits:
        if not quiet:
            print(f"  ok    {path.name} [{note}]")
        return False
    print(f"  {'set ' if apply_changes else 'plan'}  {path.name} [{note}]")
    if not apply_changes:
        return True
    cmd = ["mkvpropedit", str(path)]
    for sel, prop, val in edits:
        cmd += ["--edit", sel, "--set", f"{prop}={val}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAIL  {path.name}: {r.stderr.strip().splitlines()[-1:]}")
        return False
    return True


# Sonarr/Radarr pass variables through .NET StringDictionary, which lowercases
# every key — the process sees sonarr_eventtype, not the documented CamelCase.
ENV = {k.lower(): v for k, v in os.environ.items()}


def from_env():
    """Paths to process when invoked as an ARR Custom Script, else None."""
    ev = ENV.get("sonarr_eventtype") or ENV.get("radarr_eventtype")
    if not ev:
        return None
    if ev != "Download":  # Test, Grab, Rename, health events
        print(f"event {ev}: nothing to do")
        sys.exit(0)
    if ENV.get("sonarr_eventtype"):
        if ENV.get("sonarr_series_type", "").lower() != "anime":
            print("not an anime series, skipping")
            sys.exit(0)
        paths = ENV.get("sonarr_episodefile_path") or ""
        extra = ENV.get("sonarr_episodefile_paths") or ""
    else:
        paths = ENV.get("radarr_moviefile_path") or ""
        extra = ENV.get("radarr_moviefile_paths") or ""
    found = [p for p in ([paths] + extra.split("|")) if p]
    return list(dict.fromkeys(found))


def main():
    env_paths = from_env()
    if env_paths is not None:
        for p in env_paths:
            try:
                process(p, apply_changes=True)
            except Exception as e:  # a bad file must never fail the import
                print(f"  ERROR {p}: {e}")
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", type=Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="hide already-correct files")
    a = ap.parse_args()

    # No env, no args: an ARR probing the script. Never fail its validation.
    if not a.roots:
        print("no ARR event and no paths given; nothing to do")
        return 0

    files = sorted(f for r in a.roots for f in r.rglob("*.mkv"))
    print(f"scanning {len(files)} files")
    n = sum(process(f, a.apply, a.quiet) for f in files)
    print(f"\n{'changed' if a.apply else 'would change'}: {n} / {len(files)}")
    if not a.apply and n:
        print("dry run — re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
