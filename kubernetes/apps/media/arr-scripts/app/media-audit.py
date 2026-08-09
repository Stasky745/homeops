#!/usr/bin/env python3
"""Report what in the library would fail Jellyfin direct play, and why.

Read-only. Writes nothing, changes nothing.

Direct play fails on three independent axes, and any one of them forces work:
  video     — client can't decode the codec/profile
  audio     — no track the client can decode, so ffmpeg transcodes audio
  subtitles — image or ASS subs must be burned in, forcing a *video* transcode

Prints a distribution so the fix can be chosen from evidence rather than from
whichever file happened to break last.
"""

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".webm"}

# Codecs a mainstream client (browser, TV app, phone) plays without transcoding.
# Deliberately conservative: the goal is to find what forces work, not to model
# every client. HEVC is "maybe" — Chrome largely refuses it, Safari/TVs accept it.
VIDEO_OK = {"h264"}
VIDEO_MAYBE = {"hevc", "vp9", "av1"}
AUDIO_OK = {"aac", "mp3", "ac3", "opus", "vorbis", "flac"}   # flac: stereo only, checked below
SUBS_OK = {"subrip", "srt", "webvtt", "mov_text", "ass_text"}  # text, rendered client-side
SUBS_BURN = {"hdmv_pgs_subtitle", "pgs", "dvd_subtitle", "vobsub", "ass", "ssa"}


def find_ffprobe():
    """PATH first, then Jellyfin's bundled build which is not on PATH."""
    for c in ("ffprobe", "/usr/lib/jellyfin-ffmpeg/ffprobe"):
        p = shutil.which(c) or (c if Path(c).exists() else None)
        if p:
            return p
    return None


FFPROBE = find_ffprobe()


def probe(path):
    """Return {'video': [...], 'audio': [...], 'subs': [...]} or None."""
    if FFPROBE:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, text=True)
        if r.returncode != 0:
            return None
        streams = json.loads(r.stdout).get("streams", [])
        out = {"video": [], "audio": [], "subs": []}
        for s in streams:
            t = s.get("codec_type")
            if t == "video" and s.get("disposition", {}).get("attached_pic") != 1:
                out["video"].append({
                    "codec": s.get("codec_name", "?"),
                    "profile": (s.get("profile") or "").lower(),
                    "bits": int(s.get("bits_per_raw_sample") or 8),
                })
            elif t == "audio":
                out["audio"].append({
                    "codec": s.get("codec_name", "?"),
                    "channels": int(s.get("channels") or 0),
                })
            elif t == "subtitle":
                out["subs"].append({"codec": s.get("codec_name", "?")})
        return out

    # Fallback: mkvmerge, MKV only
    r = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    out = {"video": [], "audio": [], "subs": []}
    for t in json.loads(r.stdout).get("tracks", []):
        p = t.get("properties", {})
        codec = (p.get("codec_id") or "").lower()
        if t["type"] == "video":
            out["video"].append({
                "codec": "h264" if "avc" in codec else "hevc" if "hevc" in codec else codec,
                "profile": "", "bits": 8,
            })
        elif t["type"] == "audio":
            out["audio"].append({"codec": codec.split("/")[-1].lower(),
                                 "channels": int(p.get("audio_channels") or 0)})
        elif t["type"] == "subtitles":
            out["subs"].append({"codec": codec.split("/")[-1].lower()})
    return out


def classify(info):
    """Return (video_verdict, audio_verdict, subs_verdict, labels)."""
    labels = {}

    v = info["video"][0] if info["video"] else None
    if not v:
        vv, labels["video"] = "fail", "no video stream"
    else:
        ten_bit = v["bits"] >= 10 or "10" in v["profile"]
        name = f"{v['codec']} {'10-bit' if ten_bit else '8-bit'}"
        labels["video"] = name
        if v["codec"] in VIDEO_OK and not ten_bit:
            vv = "ok"
        elif v["codec"] in VIDEO_OK and ten_bit:
            vv = "fail"          # Hi10p: no HW decode anywhere, transcodes on most clients
        elif v["codec"] in VIDEO_MAYBE:
            vv = "maybe"
        else:
            vv = "fail"

    compatible = [a for a in info["audio"]
                  if a["codec"] in AUDIO_OK and not (a["codec"] == "flac" and a["channels"] > 2)]
    av = "ok" if compatible else "fail"
    labels["audio"] = (",".join(sorted({a["codec"] for a in info["audio"]})) or "none")

    burn = [s for s in info["subs"] if s["codec"] in SUBS_BURN]
    default_burn = bool(burn)
    sv = "fail" if default_burn else "ok"
    labels["subs"] = (",".join(sorted({s["codec"] for s in info["subs"]})) or "none")

    return vv, av, sv, labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", type=Path)
    ap.add_argument("--verbose", action="store_true", help="list every failing file")
    a = ap.parse_args()

    files = sorted(f for r in a.roots for f in r.rglob("*") if f.suffix.lower() in EXTS)
    print(f"scanning {len(files)} files under {', '.join(map(str, a.roots))}")
    print(f"probe: {FFPROBE or 'mkvmerge (MKV only — bit depth unavailable)'}\n")

    vids, auds, subs = Counter(), Counter(), Counter()
    verdicts = Counter()
    blockers = Counter()
    unreadable = 0
    failing = []

    for f in files:
        info = probe(f)
        if info is None:
            unreadable += 1
            continue
        vv, av, sv, lab = classify(info)
        vids[f"{lab['video']:<16} {vv}"] += 1
        auds[f"{lab['audio']:<24} {av}"] += 1
        subs[f"{lab['subs']:<24} {sv}"] += 1

        bad = [n for n, r in (("video", vv), ("audio", av), ("subtitles", sv)) if r == "fail"]
        for b in bad:
            blockers[b] += 1
        if not bad and "maybe" not in (vv, av, sv):
            verdicts["direct play"] += 1
        elif bad:
            verdicts["will transcode"] += 1
            failing.append((f, bad, lab))
        else:
            verdicts["client-dependent"] += 1

    def dump(title, counter):
        print(f"{title}")
        for k, n in counter.most_common():
            print(f"  {n:>6}  {k}")
        print()

    dump("VIDEO", vids)
    dump("AUDIO   (is there any client-decodable track?)", auds)
    dump("SUBTITLES   (image/ASS subs force burn-in → video transcode)", subs)

    total = sum(verdicts.values())
    print("VERDICT")
    for k, n in verdicts.most_common():
        pct = (100 * n / total) if total else 0
        print(f"  {n:>6}  {pct:5.1f}%  {k}")
    if unreadable:
        print(f"  {unreadable:>6}         unreadable")
    print()

    if blockers:
        print("BLOCKERS  (a file can have more than one)")
        for k, n in blockers.most_common():
            print(f"  {n:>6}  {k}")
        print()
        top = blockers.most_common(1)[0]
        print(f"Biggest win: fixing {top[0]} would address {top[1]} files.")

    if a.verbose and failing:
        print("\nFAILING FILES")
        for f, bad, lab in failing[:200]:
            print(f"  {','.join(bad):<20} {lab['video']:<14} {lab['audio']:<20} {f.name}")
        if len(failing) > 200:
            print(f"  … and {len(failing) - 200} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
