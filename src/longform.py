"""
Long-form clipper: turn pending VODs into short candidate clips.

Per VOD, per run:
  1. scan the next chunk (chat if available, otherwise audio)
  2. rank the moments
  3. download ONLY those windows (yt-dlp --download-sections), never the
     whole 8-hour video — that's what keeps this inside a free cloud runner
  4. measure each window's audio lift; real moments get a score boost
  5. hand the windows to the normal render stage as clips

Growing VODs (a streamer still live, e.g. a 100% marathon) are scanned
incrementally: we remember how far we got and pick up from there next run.
"""
import datetime as dt
import pathlib
import re
import subprocess

import chat
import highlights
from common import WORK, cfg, db, log

SAFE = re.compile(r"[^A-Za-z0-9_.-]")


_PLAYLISTS = {}


def _hls_playlist(url):
    """Twitch/Kick VODs are HLS: a playlist of ~2-10 s video segments."""
    import requests
    from urllib.parse import urljoin
    if url not in _PLAYLISTS:
        out = subprocess.run(["yt-dlp", "-g", "-f", "b[height<=1080]/b", url],
                             capture_output=True, text=True, timeout=120, check=True)
        _PLAYLISTS[url] = out.stdout.strip().splitlines()[0]
    m3u8 = _PLAYLISTS[url]
    text = requests.get(m3u8, timeout=30).text
    if "#EXT-X-STREAM-INF" in text:          # master playlist: take first variant
        variant = next(l for l in text.splitlines() if l and not l.startswith("#"))
        m3u8 = urljoin(m3u8, variant)
        _PLAYLISTS[url] = m3u8
        text = requests.get(m3u8, timeout=30).text
    segs, t, dur = [], 0.0, None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((t, dur, urljoin(m3u8, line)))
            t, dur = t + dur, None
    return segs


def hls_cut(url, start, end, dst):
    """Download ONLY the segments covering [start, end] — seconds, not minutes,
    even 10 hours into a marathon stream."""
    import requests
    import tempfile
    segs = [s for s in _hls_playlist(url) if s[0] + s[1] > start and s[0] < end]
    if not segs:
        raise RuntimeError(f"no HLS segments for {start:.0f}-{end:.0f}")
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, (_, _, seg_url) in enumerate(segs):
            part = pathlib.Path(tmp) / f"{i:04d}.ts"
            r = requests.get(seg_url, timeout=60)
            r.raise_for_status()
            part.write_bytes(r.content)
            parts.append(part)
        listing = pathlib.Path(tmp) / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-ss", f"{start - segs[0][0]:.2f}",
             "-t", f"{end - start:.2f}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-c:a", "aac", str(dst)],
            check=True, capture_output=True, text=True, timeout=300)
    return dst


def cut_window(src, start, end, dst):
    """Fetch only [start, end] of a video. Twitch/Kick: grab the exact HLS
    segments; anything else (YouTube): yt-dlp sections; files: ffmpeg."""
    dst = pathlib.Path(dst)
    if re.match(r"https?://(www\.)?(twitch\.tv|kick\.com)/", str(src)):
        try:
            return hls_cut(str(src), start, end, dst)
        except Exception as e:  # noqa: BLE001
            log.warning("segment cut failed (%s); trying yt-dlp", e)
    if re.match(r"https?://", str(src)):
        subprocess.run(
            ["yt-dlp", "-q", "--no-warnings",
             "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
             "--download-sections", f"*{start:.0f}-{end:.0f}",
             "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
             "-o", str(dst), str(src)],
            check=True, capture_output=True, text=True, timeout=240,
        )
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t",
             f"{end - start:.2f}", "-i", str(src), "-c:v", "libx264",
             "-preset", "veryfast", "-c:a", "aac", str(dst)],
            check=True, capture_output=True, text=True, timeout=600,
        )
    return dst


def remote_duration(url):
    out = subprocess.run(["yt-dlp", "--print", "duration", "--skip-download", url],
                         capture_output=True, text=True, timeout=120)
    try:
        return float(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0.0


def media_url(url):
    """ffmpeg can't read a YouTube/Kick watch page; resolve the audio stream."""
    if not re.match(r"https?://", str(url)):
        return url
    out = subprocess.run(["yt-dlp", "-g", "-f", "ba/b", url],
                         capture_output=True, text=True, timeout=120, check=True)
    return out.stdout.strip().splitlines()[0]


def cached_youtube_chat(v):
    """A 24h stream's chat is huge — download it once, reuse for every chunk."""
    import json
    cache = WORK / "render" / (SAFE.sub("_", v["vod_id"]) + ".chat.json")
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text())]
    comments = chat.youtube_chat(v["url"])
    cache.write_text(json.dumps(comments))
    return comments


def scouted_today(conn, streamer):
    return conn.execute(
        "SELECT COUNT(*) FROM clips WHERE lane='scout' AND streamer=? "
        "AND date(first_seen)=date('now')", (streamer,)).fetchone()[0]


def find_windows(v, start, end, lf):
    """Chat first; fall back to audio when there is no chat replay."""
    common = dict(top_n=lf["max_moments_per_chunk"], offset=0.0,
                  max_len=lf.get("max_len_s", 59))
    comments = []
    try:
        if v["platform"] == "twitch":
            comments = chat.twitch_chat(v["vod_id"].split(":", 1)[1], start, end)
        elif v["platform"] == "youtube":
            comments = cached_youtube_chat(v)
    except Exception as e:  # noqa: BLE001
        log.warning("chat unavailable for %s (%s) — using audio", v["vod_id"], e)

    if len(comments) >= lf["min_chat_messages"]:
        # Offsets are normally absolute VOD seconds. If a trimmed download came
        # back relative to the trim point, every offset sits below `start`.
        if start > 0 and max(t for t, _ in comments) < start:
            comments = [(t + start, m) for t, m in comments]
        shifted = [(t - start, m) for t, m in comments if start <= t < end]
        ws = highlights.chat_spikes(
            shifted, end - start, min_ratio=lf["chat_min_ratio"],
            pre_roll=lf["pre_roll_s"], clip_len=lf["clip_len_s"],
            **{**common, "offset": start})
        return ws
    levels = highlights.loudness_per_second(media_url(v["url"]), start, end - start)
    return highlights.audio_peaks(levels, clip_len=lf["clip_len_s"],
                                  **{**common, "offset": start})


def process_vod(conn, v, conf, room=None):
    lf = conf["longform"]
    sc = conf["scouting"]
    dur = v["duration"] or 0
    if v["platform"] in ("local",) and not dur:
        from transform import probe
        dur = probe(v["url"])["duration"]
    elif not dur and v["platform"] != "twitch":
        dur = remote_duration(v["url"])
        if dur:
            conn.execute("UPDATE vods SET duration=? WHERE vod_id=?",
                         (dur, v["vod_id"]))

    # leave a safety margin on a still-growing VOD
    horizon = dur - (lf["live_margin_s"] if v["is_live"] else 0)
    start = v["processed_to"] or 0
    if horizon - start < lf["min_chunk_s"]:
        # Not enough new footage. A finished VOD is done; a live one goes to
        # the back of the line so it can't hog a slot every cycle.
        conn.execute("UPDATE vods SET state=?, updated_at=CURRENT_TIMESTAMP "
                     "WHERE vod_id=?",
                     ("scanning" if v["is_live"] else "done", v["vod_id"]))
        conn.commit()
        return 0
    in_game = v["in_game"] if "in_game" in v.keys() else None
    if v["is_live"] and in_game == 0:
        # Streamer switched away from GTA VI (e.g. a zombies break mid-marathon):
        # skip the new footage — campaigns pay for GTA only, and it's off-niche.
        log.info("skip %s: %s is live but not in GTA VI right now", v["vod_id"], v["streamer"])
        conn.execute("UPDATE vods SET processed_to=?, state='scanning', "
                     "updated_at=CURRENT_TIMESTAMP WHERE vod_id=?", (horizon, v["vod_id"]))
        conn.commit()
        return 0
    end = min(horizon, start + lf["max_chunk_s"])
    log.info("scan %s [%s] %s %.0f→%.0fs", v["vod_id"], v["lane"],
             v["streamer"], start, end)

    windows = find_windows(v, start, end, lf)

    # Skip the intro / just-chatting part at the start of a stream (2026-11-21)
    skip = lf.get("skip_first_s", 0)
    if skip:
        n0 = len(windows)
        windows = [w for w in windows if w.start >= skip]
        if len(windows) < n0:
            log.info("  dropped %d moments in the first %ds (intro)", n0 - len(windows), skip)

    limit = v["max_clips"] or lf["max_clips_per_chunk"]
    if v["lane"] == "publish":
        from common import source_by_id
        limit *= int((source_by_id(v["source_id"]) or {}).get("priority", 1))
    if room is not None:
        limit = min(limit, room)      # only cut what this cycle can render
    if v["lane"] == "scout":
        limit = max(0, sc["clips_per_streamer_per_day"] -
                    scouted_today(conn, v["streamer"]))
    made = 0
    floor = lf.get("min_score", 0) if v["lane"] == "publish" else 0
    tries = limit + lf.get("extra_tries", 3)   # a few spares for clips under the floor
    for w in windows[:tries]:
        if made >= limit:
            break
        cid = f"{v['vod_id']}@{int(w.start)}"
        if conn.execute("SELECT 1 FROM clips WHERE clip_id=?", (cid,)).fetchone():
            continue
        dst = WORK / "raw" / (SAFE.sub("_", cid) + ".mp4")
        try:
            cut_window(v["url"], w.start, w.end, dst)
            lift = highlights.audio_lift(dst) if w.kind == "vod_chat" else w.audio_lift
        except Exception as e:  # noqa: BLE001
            log.error("window download failed %s: %s", cid, e)
            continue
        score = round(w.score * (1 + max(0.0, lift) / 20), 2)
        if score < floor:
            log.info("  %s score %.1f under floor %s — dropped", cid, score, floor)
            dst.unlink(missing_ok=True)
            continue
        conn.execute(
            """INSERT INTO clips (clip_id, source_id, streamer, platform, title,
                url, views, duration, created_at, score, state, lane, source_kind,
                chat_ratio, audio_lift, vod_id, vod_start, chat_top)
               VALUES (?,?,?,?,?,?,?,?,?,?,'downloaded',?,?,?,?,?,?,?)""",
            (cid, v["source_id"], v["streamer"], "vod", v["title"], str(dst), 0,
             w.length, dt.datetime.now(dt.timezone.utc).isoformat(), score,
             v["lane"], w.kind, w.ratio, lift, v["vod_id"], w.start, w.top_chat),
        )
        made += 1

    done = (not v["is_live"]) and end >= horizon - 1
    conn.execute(
        "UPDATE vods SET processed_to=?, clips_made=clips_made+?, state=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE vod_id=?",
        (end, made, "done" if done else "scanning", v["vod_id"]))
    conn.commit()
    log.info("  -> %d windows found, %d clips cut", len(windows), made)
    return made


class VodTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise VodTimeout()


def run():
    """One bad VOD must never freeze a cycle (posting runs after this stage).
    Each VOD gets a time budget, and the whole stage gets one too."""
    import signal
    import time
    conf = cfg()
    lf = conf["longform"]
    conn = db()
    conn.row_factory = __import__("sqlite3").Row
    from common import backlog_full
    full = backlog_full(conn, conf)
    if full:
        # Footage isn't skipped: the VODs keep their place and get scanned
        # (best moments only) once the queue drains.
        log.info("backlog brake: %d clips already queued — not cutting new ones", full)
        return
    # publish lane first: those are the clips that can make money
    lanes = ("publish", "scout") if conf["scouting"].get("enabled") else ("publish",)
    from common import source_by_id
    todo = conn.execute(
        "SELECT * FROM vods WHERE state IN ('new','scanning') AND lane IN (%s) "
        "ORDER BY CASE lane WHEN 'publish' THEN 0 ELSE 1 END, updated_at"
        % ",".join("?" * len(lanes)), lanes).fetchall()
    # Higher-priority streamers (priority: in sources.yml) get scanned first.
    todo = sorted(todo, key=lambda v: -int((source_by_id(v["source_id"]) or {})
                                           .get("priority", 1)))[:lf["max_vods_per_run"]]
    total, t0 = 0, time.time()
    signal.signal(signal.SIGALRM, _alarm)
    for v in todo:
        left = lf.get("stage_budget_s", 900) - (time.time() - t0)
        room = lf.get("max_clips_per_run", 8) - total
        if left < 120 or room <= 0:
            log.info("long-form: time budget used, leaving the rest for next cycle")
            break
        signal.alarm(int(min(lf.get("vod_budget_s", 600), left)))
        try:
            total += process_vod(conn, v, conf, room=room)
        except VodTimeout:
            conn.rollback()
            if v["lane"] == "publish":
                # skip this chunk rather than retry it forever
                nxt = (v["processed_to"] or 0) + lf["max_chunk_s"]
                log.error("VOD %s timed out — skipping to %.0fs", v["vod_id"], nxt)
                conn.execute("UPDATE vods SET processed_to=?, state='scanning', "
                             "updated_at=CURRENT_TIMESTAMP WHERE vod_id=?",
                             (nxt, v["vod_id"]))
            else:
                log.error("VOD %s timed out — dropped from scouting", v["vod_id"])
                conn.execute("UPDATE vods SET state='error', "
                             "updated_at=CURRENT_TIMESTAMP WHERE vod_id=?",
                             (v["vod_id"],))
            conn.commit()
        except Exception as e:  # noqa: BLE001
            log.error("VOD %s failed: %s", v["vod_id"], e)
            conn.execute("UPDATE vods SET state='error', "
                         "updated_at=CURRENT_TIMESTAMP WHERE vod_id=?",
                         (v["vod_id"],))
            conn.commit()
        finally:
            signal.alarm(0)
    log.info("long-form: %d clips cut this run", total)
    return total


if __name__ == "__main__":
    run()
