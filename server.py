"""
Muse - Local Music Preference Analysis System
Requires: FFmpeg in PATH
"""
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import uuid

import librosa
import numpy as np
import yt_dlp
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "muse.db")
AUDIO_DIR = os.path.join(BASE_DIR, "audio_cache")
os.makedirs(AUDIO_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit

# ── database ───────────────────────────────────────────────────────────────
def _get_conn():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    conn = _get_conn()

    # ── profiles table ─────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at REAL
        )
    """)
    for pid, pname in [(1,"設定檔 1"),(2,"設定檔 2"),(3,"設定檔 3")]:
        conn.execute("INSERT OR IGNORE INTO profiles VALUES(?,?,?)",
                     (pid, pname, time.time()))

    # ── songs table ────────────────────────────────────────────────────
    existing_cols = {r[1] for r in
                     conn.execute("PRAGMA table_info(songs)").fetchall()}

    if not existing_cols:
        # Fresh install: create table with profile support from the start
        conn.execute("""
            CREATE TABLE songs (
                id          TEXT PRIMARY KEY,
                youtube_id  TEXT,
                title       TEXT,
                thumbnail   TEXT,
                url         TEXT,
                added_at    REAL,
                features    TEXT,
                normalized  TEXT,
                tags        TEXT,
                profile_id  INTEGER NOT NULL DEFAULT 1,
                UNIQUE(youtube_id, profile_id)
            )
        """)
    elif "profile_id" not in existing_cols:
        # Existing install: migrate — drop old UNIQUE on youtube_id,
        # add profile_id, add composite UNIQUE(youtube_id, profile_id)
        conn.execute("""
            CREATE TABLE songs_v2 (
                id          TEXT PRIMARY KEY,
                youtube_id  TEXT,
                title       TEXT,
                thumbnail   TEXT,
                url         TEXT,
                added_at    REAL,
                features    TEXT,
                normalized  TEXT,
                tags        TEXT,
                profile_id  INTEGER NOT NULL DEFAULT 1,
                UNIQUE(youtube_id, profile_id)
            )
        """)
        conn.execute("""
            INSERT INTO songs_v2
            SELECT id,youtube_id,title,thumbnail,url,
                   added_at,features,normalized,tags,1
            FROM songs
        """)
        conn.execute("DROP TABLE songs")
        conn.execute("ALTER TABLE songs_v2 RENAME TO songs")

    conn.commit()
    conn.close()

_init_db()

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def _sse_response(gen):
    return Response(gen, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

# ── in-memory batch queue (Spotify playlists) ──────────────────────────────
_batch_store: dict = {}
_batch_lock = threading.Lock()

# ── URL helpers ────────────────────────────────────────────────────────────
def _is_spotify(url: str) -> bool:
    return "open.spotify.com" in url

def _is_spotify_playlist(url: str) -> bool:
    return "open.spotify.com/playlist" in url

def _is_youtube_playlist(url: str) -> bool:
    return "youtube.com/playlist" in url or ("list=" in url and "youtu" in url)

# ── Spotify helpers ────────────────────────────────────────────────────────
_SPOTIFY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def _spotify_embed_entity(track_id: str) -> dict:
    """Parse __NEXT_DATA__ from Spotify's public embed page.
    Returns entity dict with keys: name, artists (list), coverArt, etc.
    Raises on failure."""
    import urllib.request as _req
    req = _req.Request(
        f"https://open.spotify.com/embed/track/{track_id}",
        headers={"User-Agent": _SPOTIFY_UA},
    )
    with _req.urlopen(req, timeout=10) as r:
        html = r.read().decode("utf-8", errors="replace")
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.DOTALL)
    if not m:
        raise ValueError("__NEXT_DATA__ not found")
    data = json.loads(m.group(1))
    # path: props → pageProps → state → data → entity
    ent = (data.get("props", {})
               .get("pageProps", {})
               .get("state", {})
               .get("data", {})
               .get("entity", {}))
    if not ent:
        raise ValueError("entity not found in __NEXT_DATA__")
    return ent


def _spotify_playlist_embed(playlist_id: str) -> list[dict]:
    """Scrape track list from Spotify's public embed page for a playlist.
    Returns list of {title, artist, search_query, display}."""
    import urllib.request as _req
    req = _req.Request(
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
        headers={"User-Agent": _SPOTIFY_UA},
    )
    with _req.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="replace")
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.DOTALL)
    if not m:
        raise ValueError("__NEXT_DATA__ not found")
    data = json.loads(m.group(1))
    ent = (data.get("props", {})
               .get("pageProps", {})
               .get("state", {})
               .get("data", {})
               .get("entity", {}))
    tracks = []
    items = ent.get("trackList") or ent.get("tracks", {}).get("items", [])
    for item in items:
        # trackList entries: {title, subtitle, uid, ...}
        # tracks.items entries: {track: {name, artists: [{name}]}}
        if "title" in item:
            t = item.get("title", "")
            a = item.get("subtitle", "")
        else:
            tr = item.get("track") or item
            t  = tr.get("name", "")
            artists = tr.get("artists", [])
            a = ", ".join(x.get("name", "") for x in artists if x.get("name"))
        if not t:
            continue
        tracks.append({
            "title":        t,
            "artist":       a,
            "search_query": f"{t} {a}".strip() if a else t,
            "display":      f"{t} — {a}" if a else t,
        })
    return tracks


def spotify_track_info(url: str) -> dict:
    """Get Spotify track metadata.
    1. Try oEmbed for title + thumbnail (fast, no auth).
    2. If artist is missing from oEmbed, fetch embed page and parse __NEXT_DATA__."""
    import urllib.request as _req

    # Extract track ID
    track_id_m = re.search(
        r'spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]+)', url)
    track_id = track_id_m.group(1) if track_id_m else None

    # Step 1: oEmbed
    api = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
    req = _req.Request(api, headers={"User-Agent": "Mozilla/5.0"})
    with _req.urlopen(req, timeout=12) as r:
        oembed = json.loads(r.read().decode())
    raw_title = oembed.get("title", "")
    thumb     = oembed.get("thumbnail_url", "")

    # oEmbed used to return "Song · Artist"; now often just "Song"
    parts  = raw_title.split(" · ", 1)
    title  = parts[0].strip()
    artist = parts[1].strip() if len(parts) > 1 else ""

    # Step 2: if no artist, parse embed page __NEXT_DATA__
    if not artist and track_id:
        try:
            ent    = _spotify_embed_entity(track_id)
            title  = ent.get("name", title) or title
            artists_raw = ent.get("artists", [])
            if isinstance(artists_raw, list):
                artist = ", ".join(
                    a.get("name", "") for a in artists_raw if a.get("name"))
            elif isinstance(artists_raw, dict):
                # some API shapes have {items: [...]}
                items = artists_raw.get("items", [])
                artist = ", ".join(
                    a.get("profile", {}).get("name", "") or a.get("name", "")
                    for a in items if (a.get("profile", {}).get("name") or a.get("name")))
        except Exception:
            pass  # keep title-only; search will still work

    spotify_title = f"{title} · {artist}" if artist else title
    return {
        "spotify_title": spotify_title,
        "title":  title,
        "artist": artist,
        "thumbnail": thumb,
        "search_query": f"{title} {artist}".strip(),
    }


def spotify_playlist_info(url: str) -> dict:
    """Extract Spotify playlist track list.
    Uses embed-page scraping (no yt-dlp, no auth)."""
    playlist_id_m = re.search(
        r'spotify\.com/(?:intl-[a-z-]+/)?playlist/([A-Za-z0-9]+)', url)
    playlist_id = playlist_id_m.group(1) if playlist_id_m else None

    playlist_title = "Spotify 播放清單"
    tracks: list[dict] = []

    if playlist_id:
        try:
            tracks = _spotify_playlist_embed(playlist_id)
            # Try to get playlist name from oEmbed
            try:
                import urllib.request as _req2
                oe_req = _req2.Request(
                    f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}",
                    headers={"User-Agent": "Mozilla/5.0"})
                with _req2.urlopen(oe_req, timeout=8) as r2:
                    playlist_title = json.loads(r2.read().decode()).get(
                        "title", playlist_title)
            except Exception:
                pass
        except Exception as exc:
            raise RuntimeError(f"無法讀取 Spotify 播放清單: {exc}") from exc
    else:
        raise RuntimeError("無法識別 Spotify 播放清單 ID")

    return {
        "type":   "spotify_playlist",
        "title":  playlist_title,
        "tracks": tracks,
    }

# ── YouTube helpers ────────────────────────────────────────────────────────
def get_youtube_info(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    thumbnail = info.get("thumbnail", "")
    for t in sorted(info.get("thumbnails", []),
                    key=lambda x: x.get("width", 0) or 0, reverse=True):
        if t.get("url"):
            thumbnail = t["url"]
            break
    return {"id": info["id"], "title": info["title"],
            "thumbnail": thumbnail, "duration": info.get("duration", 0)}

def _audio_path(song_id: str) -> str:
    return os.path.join(AUDIO_DIR, f"{song_id}.wav")

def download_audio(youtube_id: str):
    url  = f"https://www.youtube.com/watch?v={youtube_id}"
    tmpl = os.path.join(AUDIO_DIR, f"{youtube_id}.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "outtmpl": tmpl, "quiet": True, "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

# ── YouTube search (single songs only) ────────────────────────────────────
_SKIP_WORDS = [
    "playlist", "mix", "compilation", "full album", "greatest hits",
    "全集", "合輯", "精選輯", "歌單", "連續播放",
]

def _is_single(e: dict) -> bool:
    dur   = e.get("duration") or 0
    title = (e.get("title") or "").lower()
    if dur and (dur < 60 or dur > 600):
        return False
    return not any(w in title for w in _SKIP_WORDS)

def search_youtube(query: str, n: int = 3) -> list:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n * 4}:{query}", download=False)
        results = []
        for e in (info.get("entries") or []):
            if not e or not e.get("id") or not _is_single(e):
                continue
            vid = e["id"]
            results.append({
                "id": vid,
                "title": e.get("title", ""),
                "duration": e.get("duration", 0),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "youtube_music_url": f"https://music.youtube.com/watch?v={vid}",
                "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
            })
            if len(results) >= n:
                break
        return results
    except Exception:
        return []

def fetch_playlist_info(url: str) -> dict:
    """Fetch YouTube playlist metadata."""
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "playlistend": 500}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    if not entries:
        entries = [info]
    videos = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        vid = e["id"]
        videos.append({
            "id": vid, "title": e.get("title", ""),
            "duration": e.get("duration", 0),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        })
    return {"type": "youtube_playlist",
            "title": info.get("title", "播放清單"), "videos": videos}

# ── 83-dimensional feature set ────────────────────────────────────────────
_KEY_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]

ALL_DIMS = (
    # 空間 (2)
    ["stereo_width", "stereo_correlation"]
    # 節奏 (4)
  + ["tempo", "beat_regularity", "onset_mean", "onset_std"]
    # 能量結構 (2)
  + ["harmonic_ratio", "percussive_ratio"]
    # 調性 (2)
  + ["key", "key_strength"]
    # Chroma — 12 半音 (12)
  + [f"chroma_{i}" for i in range(12)]
    # Tonnetz — 和聲關係 (6)
  + [f"tonnetz_{i}" for i in range(6)]
    # 頻譜形狀 (4)
  + ["spectral_centroid_mean", "spectral_centroid_std", "spectral_rolloff", "spectral_bandwidth"]
    # 頻譜對比 (7)
  + [f"contrast_{i}" for i in range(7)]
    # MFCC 均值 (20)
  + [f"mfcc_mean_{i}" for i in range(20)]
    # MFCC 標準差 (20)
  + [f"mfcc_std_{i}" for i in range(20)]
    # 動態 (4)
  + ["rms_mean", "rms_std", "dynamic_range", "zcr_mean"]
)  # total = 83

# 11 UI display dimensions (derived from the 83 raw dims)
_DIM_NAMES_ZH = {
    "rhythm_speed":    "節奏速度",
    "brightness":      "音色亮度",
    "melody_ratio":    "旋律比例",
    "dynamic_range":   "動態範圍",
    "groove":          "律動強度",
    "stereo_width":    "立體寬度",
    "timbre_texture":  "音色質感",
    "bass_energy":     "低頻能量",
    "tonality":        "調性感",
    "beat_regularity": "節拍規律性",
    "bandwidth":       "聲音豐富度",
}
DIMS = list(_DIM_NAMES_ZH.keys())


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def analyze_audio(path: str, max_duration: float = 120.0) -> dict:
    """Extract 83 audio features grouped into 11 categories."""
    y_raw, sr = librosa.load(path, mono=False, sr=22050, duration=max_duration)

    # ── 空間 ───────────────────────────────────────────────────────────────
    if y_raw.ndim == 2 and y_raw.shape[0] == 2:
        y_l, y_r = y_raw[0], y_raw[1]
        y = librosa.to_mono(y_raw)
        diff = y_l - y_r
        summ = y_l + y_r
        stereo_width = float(min(1.0, np.std(diff) / (np.std(summ) + 1e-9)))
        corr_mat = np.corrcoef(y_l, y_r)
        stereo_correlation = float(np.clip(corr_mat[0, 1], -1.0, 1.0))
    else:
        y = y_raw if y_raw.ndim == 1 else librosa.to_mono(y_raw)
        stereo_width = 0.0
        stereo_correlation = 1.0

    # ── 節奏 ───────────────────────────────────────────────────────────────
    tempo_raw, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo_raw)[0])

    if len(beat_frames) > 2:
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        ibi = np.diff(beat_times)
        beat_regularity = float(max(0.0, 1.0 - np.std(ibi) / (np.mean(ibi) + 1e-9)))
    else:
        beat_regularity = 0.5

    onset_env  = librosa.onset.onset_strength(y=y, sr=sr)
    onset_mean = float(np.mean(onset_env))
    onset_std  = float(np.std(onset_env))

    # ── 能量結構 (HPSS) ────────────────────────────────────────────────────
    y_h, y_p = librosa.effects.hpss(y)
    h_e = float(np.mean(y_h ** 2))
    p_e = float(np.mean(y_p ** 2))
    tot = h_e + p_e + 1e-9
    harmonic_ratio   = float(h_e / tot)
    percussive_ratio = float(p_e / tot)

    # ── 調性 (Krumhansl-Kessler) ───────────────────────────────────────────
    chroma_cqt  = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = np.mean(chroma_cqt, axis=1)
    chroma_norm = chroma_mean / (chroma_mean.sum() + 1e-9)

    _maj = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
    _min = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
    _maj = _maj / _maj.sum()
    _min = _min / _min.sum()

    best_score, best_key = -np.inf, 0
    best_maj_c, best_min_c = 0.0, 0.0
    for k in range(12):
        mj = _safe_corr(np.roll(_maj, k), chroma_norm)
        mn = _safe_corr(np.roll(_min, k), chroma_norm)
        if max(mj, mn) > best_score:
            best_score, best_key = max(mj, mn), k
            best_maj_c, best_min_c = mj, mn

    key          = int(best_key)
    key_strength = float(max(0.0, min(1.0, (best_score + 1.0) / 2.0)))
    # Extra field (not in ALL_DIMS) for the major/minor display dimension
    _tonality    = float(max(0.0, min(1.0, (best_maj_c - best_min_c + 1.0) / 2.0)))

    # ── Chroma — 12 半音能量 ──────────────────────────────────────────────
    chroma_vals = chroma_norm.tolist()   # 12 values, already in [0,1]

    # ── Tonnetz — 和聲關係 ────────────────────────────────────────────────
    tonnetz      = librosa.feature.tonnetz(y=y, sr=sr)
    tonnetz_vals = np.mean(tonnetz, axis=1).tolist()  # 6 values in [-1,1]

    # ── 頻譜形狀 ──────────────────────────────────────────────────────────
    cent     = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    sc_mean  = float(np.mean(cent))
    sc_std   = float(np.std(cent))
    rolloff  = float(np.mean(librosa.feature.spectral_rolloff(y=y, sr=sr)))
    bw       = float(np.mean(librosa.feature.spectral_bandwidth(y=y, sr=sr)))

    # ── 頻譜對比 (7 bands) ────────────────────────────────────────────────
    contrast      = librosa.feature.spectral_contrast(y=y, sr=sr, n_bands=6)
    contrast_vals = np.mean(contrast, axis=1).tolist()  # 7 values

    # ── MFCC (20 係數) ────────────────────────────────────────────────────
    mfcc       = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=20)
    mfcc_mean  = np.mean(mfcc, axis=1).tolist()
    mfcc_std_v = np.std(mfcc, axis=1).tolist()

    # ── 動態 ──────────────────────────────────────────────────────────────
    rms       = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    rms_mean  = float(np.mean(rms))
    rms_std   = float(np.std(rms))
    rms_db    = librosa.amplitude_to_db(rms + 1e-7)
    dyn_range = float(max(0.0, np.percentile(rms_db, 95) - np.percentile(rms_db, 5)))
    zcr_mean  = float(np.mean(librosa.feature.zero_crossing_rate(y)))

    out: dict = {
        # 空間
        "stereo_width": stereo_width,
        "stereo_correlation": stereo_correlation,
        # 節奏
        "tempo": tempo,
        "beat_regularity": beat_regularity,
        "onset_mean": onset_mean,
        "onset_std": onset_std,
        # 能量結構
        "harmonic_ratio": harmonic_ratio,
        "percussive_ratio": percussive_ratio,
        # 調性
        "key": key,
        "key_strength": key_strength,
        # 頻譜形狀
        "spectral_centroid_mean": sc_mean,
        "spectral_centroid_std": sc_std,
        "spectral_rolloff": rolloff,
        "spectral_bandwidth": bw,
        # 動態
        "rms_mean": rms_mean,
        "rms_std": rms_std,
        "dynamic_range": dyn_range,
        "zcr_mean": zcr_mean,
        # helper for UI (not in ALL_DIMS)
        "_tonality": _tonality,
    }
    for i, v in enumerate(chroma_vals):   out[f"chroma_{i}"]    = float(v)
    for i, v in enumerate(tonnetz_vals):  out[f"tonnetz_{i}"]   = float(v)
    for i, v in enumerate(contrast_vals): out[f"contrast_{i}"]  = float(v)
    for i, v in enumerate(mfcc_mean):     out[f"mfcc_mean_{i}"] = float(v)
    for i, v in enumerate(mfcc_std_v):    out[f"mfcc_std_{i}"]  = float(v)
    return out


def get_feature_vector(f: dict) -> np.ndarray:
    """Convert raw 83-dim feature dict to a normalized [0,1] vector for cosine similarity."""
    def _c(v, lo, hi):
        return float(min(1.0, max(0.0, (v - lo) / (hi - lo + 1e-9))))

    vec: list = [
        # 空間
        _c(f.get("stereo_width", 0),             0,    1.0),
        _c(f.get("stereo_correlation", 0) + 1,   0,    2.0),   # [-1,1]→[0,1]
        # 節奏
        _c(f.get("tempo", 120),                 40,  260.0),
        _c(f.get("beat_regularity", 0.5),        0,    1.0),
        _c(f.get("onset_mean", 1.0),             0,   12.0),
        _c(f.get("onset_std", 0.5),              0,    8.0),
        # 能量結構
        _c(f.get("harmonic_ratio", 0.5),         0,    1.0),
        _c(f.get("percussive_ratio", 0.5),       0,    1.0),
        # 調性
        _c(f.get("key", 0),                      0,   11.0),
        _c(f.get("key_strength", 0.5),           0,    1.0),
    ]
    # Chroma (12) — already [0,1]
    for i in range(12):
        vec.append(_c(f.get(f"chroma_{i}", 1/12), 0, 1.0))
    # Tonnetz (6) — [-1,1]→[0,1]
    for i in range(6):
        vec.append(_c(f.get(f"tonnetz_{i}", 0) + 1.0, 0, 2.0))
    # 頻譜形狀 (4)
    vec += [
        _c(f.get("spectral_centroid_mean", 2000),  200,  8000),
        _c(f.get("spectral_centroid_std",   500),    0,  3000),
        _c(f.get("spectral_rolloff",       4000),  500, 11000),
        _c(f.get("spectral_bandwidth",     1500),  200,  5000),
    ]
    # 頻譜對比 (7) — typically 0-80 dB per band
    for i in range(7):
        vec.append(_c(f.get(f"contrast_{i}", 20), 0, 80))
    # MFCC mean (20) — coeff 0 is much larger in magnitude
    vec.append(_c(f.get("mfcc_mean_0", -400), -800, -50))
    for i in range(1, 20):
        vec.append(_c(f.get(f"mfcc_mean_{i}", 0), -120, 120))
    # MFCC std (20)
    vec.append(_c(f.get("mfcc_std_0", 30), 0, 120))
    for i in range(1, 20):
        vec.append(_c(f.get(f"mfcc_std_{i}", 10), 0, 50))
    # 動態 (4)
    vec += [
        _c(f.get("rms_mean",      0.05),  0,  0.30),
        _c(f.get("rms_std",       0.03),  0,  0.15),
        _c(f.get("dynamic_range", 20),    0,  50.0),
        _c(f.get("zcr_mean",      0.05),  0,  0.25),
    ]
    assert len(vec) == 83, f"Feature vector length mismatch: {len(vec)}"
    return np.array(vec, dtype=np.float32)


def normalize_features(f: dict) -> dict:
    """Derive the 11 UI display dimensions from a raw feature dict.
    Compatible with both the new 83-dim format and the legacy format."""
    # --- tempo ---
    bpm  = f.get("tempo") or f.get("bpm", 120)
    # --- brightness (spectral centroid) ---
    cent = f.get("spectral_centroid_mean") or f.get("brightness", 2000)
    # --- harmonic ratio ---
    harm = f.get("harmonic_ratio", 0.5)
    # --- dynamic range ---
    dyn  = f.get("dynamic_range") or f.get("dynamic_range_db", 20)
    # --- groove (onset strength) ---
    grv  = f.get("onset_mean") or f.get("groove", 2)
    # --- stereo width ---
    sw   = f.get("stereo_width", 0.3)
    # --- timbre texture (ZCR) ---
    zcr  = f.get("zcr_mean") or f.get("timbre_texture", 0.05)
    # --- bass energy: contrast band-0 (sub-bass) or legacy direct ---
    if "bass_energy" in f and "tempo" not in f:       # legacy format
        bass = float(f["bass_energy"]) / 0.5 * 100
    elif "contrast_0" in f:
        bass = float(f["contrast_0"]) / 60.0 * 100   # ~0-80 dB range
    else:
        bass = 40.0
    # --- tonality (major/minor) ---
    if "_tonality" in f:
        ton = f["_tonality"] * 100
    elif "key_strength" in f:
        ton = f["key_strength"] * 100
    elif "tonality" in f:
        ton = f["tonality"] * 100
    else:
        ton = 50.0
    # --- beat regularity ---
    breg = f.get("beat_regularity", 0.5)
    # --- bandwidth ---
    bw   = f.get("spectral_bandwidth") or f.get("bandwidth", 1500)

    return {
        "rhythm_speed":    min(100, max(0, (bpm  - 60)  / 120  * 100)),
        "brightness":      min(100, max(0, (cent - 500) / 4500 * 100)),
        "melody_ratio":    min(100, max(0,  harm               * 100)),
        "dynamic_range":   min(100, max(0,  dyn               / 40   * 100)),
        "groove":          min(100, max(0,  grv               / 5    * 100)),
        "stereo_width":    min(100, max(0,  sw               * 200)),
        "timbre_texture":  min(100, max(0,  zcr              / 0.15  * 100)),
        "bass_energy":     min(100, max(0,  bass)),
        "tonality":        min(100, max(0,  ton)),
        "beat_regularity": min(100, max(0,  breg             * 100)),
        "bandwidth":       min(100, max(0, (bw   - 200)      / 3000  * 100)),
    }


def generate_tags(features: dict, norm: dict) -> list:
    tags = []
    bpm = features.get("tempo") or features.get("bpm", 120)
    if   bpm < 80:  tags.append("慢速")
    elif bpm < 105: tags.append("中慢速")
    elif bpm < 130: tags.append("中速")
    elif bpm < 160: tags.append("快速")
    else:           tags.append("極速")

    # Key name tag (new format)
    if "key" in features and features.get("key_strength", 0) > 0.35:
        key_label = _KEY_NAMES[int(features["key"])]
        mode_label = "大調" if features.get("_tonality", 0.5) > 0.5 else "小調"
        tags.append(f"{key_label} {mode_label}")

    b = norm.get("brightness", 50)
    if   b > 65: tags.append("明亮音色")
    elif b < 35: tags.append("溫暖音色")
    m = norm.get("melody_ratio", 50)
    if   m > 65: tags.append("旋律性強")
    elif m < 35: tags.append("節奏性強")
    t = norm.get("tonality", 50)
    if   t > 68 and "key" not in features: tags.append("大調歡快")  # legacy only
    elif t < 35 and "key" not in features: tags.append("小調憂鬱")
    if norm.get("bass_energy", 50) > 65:     tags.append("重低音")
    if norm.get("groove", 50) > 65:          tags.append("高律動")
    if norm.get("stereo_width", 50) > 60:    tags.append("寬廣音場")
    if norm.get("dynamic_range", 50) > 65:   tags.append("動態豐富")
    if norm.get("dynamic_range", 50) < 30:   tags.append("高度壓縮")
    if norm.get("beat_regularity", 50) > 75: tags.append("節拍穩定")
    return tags[:6]


def cosine_sim(a, b) -> float:
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def song_similarity(fa: dict, fb: dict) -> float:
    """Cosine similarity using the 83-dim normalized vector.
    Falls back to MFCC + norm distance for legacy songs."""
    if "tempo" in fa and "tempo" in fb:
        va = get_feature_vector(fa)
        vb = get_feature_vector(fb)
        return max(0.0, float(
            np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9)))
    # Legacy fallback
    mfcc_s = max(0.0, cosine_sim(
        fa.get("mfcc", [0]*20), fb.get("mfcc", [0]*20)))
    na = normalize_features(fa)
    nb = normalize_features(fb)
    norm_s = 1.0 - float(
        np.mean([abs(na.get(d, 50) - nb.get(d, 50)) for d in DIMS])) / 100.0
    return 0.45 * mfcc_s + 0.55 * max(0.0, norm_s)


# ── higher-level musical style inference ──────────────────────────────────
def infer_musical_style(all_raw: list) -> dict:
    """Derive instrument/arrangement/harmony preferences from the raw 83-dim library.
    Returns {'display': {label_key: zh_string}, 'query_terms': {lang_code: terms_str}}
    where query_terms is used to auto-enrich recommendation queries."""
    if not all_raw:
        return {}

    def _avg(key, default=0.0):
        vals = [f.get(key, default) for f in all_raw if f.get(key) is not None]
        return float(np.mean(vals)) if vals else default

    harm  = _avg("harmonic_ratio", 0.5)
    sw    = _avg("stereo_width", 0.3)
    dr    = _avg("dynamic_range", 20)
    br    = _avg("beat_regularity", 0.5)
    cent  = _avg("spectral_centroid_mean", 2500)
    zcr   = _avg("zcr_mean", 0.06)
    rms   = _avg("rms_mean", 0.06)
    # contrast_0 ≈ sub-bass energy (dB gap)
    bass  = _avg("contrast_0", 20)

    display: dict = {}   # what the UI shows (zh text)
    # per-language auto query terms, collected as lists then joined
    _t: dict = {k: [] for k in ["zh","yue","ja","ko","en","fr","es","de","it","pt",""]}

    def _add(zh_t, yue_t="", ja_t="", ko_t="", en_t="", fr_t="", es_t="",
             de_t="", it_t="", pt_t=""):
        _t["zh"].append(zh_t or en_t); _t["yue"].append(yue_t or zh_t or en_t)
        _t["ja"].append(ja_t or en_t); _t["ko"].append(ko_t or en_t)
        _t["en"].append(en_t); _t["fr"].append(fr_t or en_t)
        _t["es"].append(es_t or en_t); _t["de"].append(de_t or en_t)
        _t["it"].append(it_t or en_t); _t["pt"].append(pt_t or en_t)
        _t[""].append(en_t)

    # ── 編制傾向 ──────────────────────────────────────────────────────────
    if sw > 0.45 and dr > 22:
        display["arrangement"] = "樂團 / 管弦樂編制"
        _add("樂團", ja_t="バンド", ko_t="밴드", en_t="band",
             fr_t="groupe", es_t="banda", de_t="Band", it_t="band", pt_t="banda")
    elif br > 0.75 and dr < 14:
        display["arrangement"] = "電子 / 合成器主導"
        _add("電子合成器", ja_t="エレクトロ", ko_t="일렉트로닉", en_t="electronic synth",
             fr_t="électronique", es_t="electrónica", de_t="Elektronik")
    elif sw < 0.18 and harm > 0.65:
        display["arrangement"] = "人聲 / 清唱 / 原聲"
        _add("清唱 原聲", ja_t="アコースティック ボーカル", ko_t="어쿠스틱",
             en_t="acoustic vocal", fr_t="acoustique", es_t="acústico")
    else:
        display["arrangement"] = "流行製作編制（樂團 + 合成器混合）"
        # No extra terms — already captured by genre/mood

    # ── 和聲複雜度 ────────────────────────────────────────────────────────
    hs = harm * (1.0 + sw) / 2.0
    if hs > 0.56:
        display["harmony"] = "豐富和聲 / 多聲部合唱"
        _add("和聲 合唱", ja_t="コーラス ハーモニー", ko_t="화음 합창",
             en_t="harmony choir", fr_t="chorale", es_t="coro")
    elif hs < 0.33:
        display["harmony"] = "單線旋律 / 清唱（人聲無伴奏或極簡）"
        _add("清唱", ja_t="シンプルボーカル", ko_t="솔로", en_t="solo vocal")
    else:
        display["harmony"] = "適度和聲（主副歌對比結構）"

    # ── 音色特質 ──────────────────────────────────────────────────────────
    if cent > 4200 and zcr > 0.11:
        display["timbre"] = "明亮銳利（電吉他 / 電子合成器）"
        _add("電吉他", ja_t="ギター", ko_t="기타", en_t="guitar synth")
    elif cent > 3200:
        display["timbre"] = "清亮溫暖（鋼琴 / 弦樂 / 木吉他）"
        _add("鋼琴 弦樂", ja_t="ピアノ ストリングス", ko_t="피아노",
             en_t="piano strings", fr_t="piano", es_t="piano")
    elif cent < 1800:
        display["timbre"] = "低沉溫暖（低音樂器主導）"
        _add("低音", ja_t="ベース", ko_t="베이스", en_t="bass warm")
    else:
        display["timbre"] = "均衡（人聲 / 全頻段）"

    # ── 低頻偏好 ─────────────────────────────────────────────────────────
    if bass > 35:
        display["bass_pref"] = "強勁低頻（重低音偏好）"
        _add("重低音", ja_t="低音重視", ko_t="베이스", en_t="heavy bass")
    elif bass < 12:
        display["bass_pref"] = "低頻偏淡（輕盈音色）"

    # ── 製作風格 ──────────────────────────────────────────────────────────
    if dr < 9:
        display["production"] = "高度壓縮（現代商業電子製作）"
    elif dr > 32:
        display["production"] = "動態豐富（現場感 / 管弦樂錄音）"
        _add("現場錄音", ja_t="ライブ", ko_t="라이브",
             en_t="live recording", fr_t="live", es_t="en vivo")
    else:
        display["production"] = "標準錄音室製作"

    # ── 節拍特性 ──────────────────────────────────────────────────────────
    if br > 0.78:
        display["rhythm_feel"] = "精準機械節拍（電子 / 舞曲）"
        _add("舞曲", ja_t="ダンス", ko_t="댄스", en_t="dance electronic")
    elif br < 0.48:
        display["rhythm_feel"] = "自由律動（爵士 / 民謠 / 即興）"
        _add("爵士", ja_t="ジャズ", ko_t="재즈", en_t="jazz folk",
             fr_t="jazz", es_t="jazz")
    else:
        display["rhythm_feel"] = "穩健節拍（流行 / 搖滾）"

    # ── 能量感 ────────────────────────────────────────────────────────────
    rms_pct = min(100.0, rms / 0.15 * 100.0)
    if rms_pct > 62:
        display["energy"] = "高能量（強勁有力）"
    elif rms_pct < 26:
        display["energy"] = "低能量（靜謐 / 安定）"
    else:
        display["energy"] = "中等能量（舒適流暢）"

    def _join_terms(lst):
        seen, out = set(), []
        for t in lst:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t); out.append(t)
        return " ".join(out[:3])   # max 3 terms per language

    return {
        "display": display,
        "query_terms": {lang: _join_terms(terms) for lang, terms in _t.items()},
    }


# ── language-native recommendation queries ─────────────────────────────────
def build_rec_queries(profile: dict, language="", genre="", mood="", era="", voice="") -> list:
    """
    Build YouTube search queries entirely in the target language.
    Never mix Chinese terms with English audio descriptors.
    Always append a vocal/song suffix for non-instrumental genres.
    """
    r   = profile.get("rhythm_speed", 50)
    g   = profile.get("groove",       50)
    ton = profile.get("tonality",     50)

    is_instr = genre in ("instrumental", "pure_instrumental")

    def _q(*parts):
        return re.sub(r"\s+", " ", " ".join(p for p in parts if p).strip())

    # ── era tokens ────────────────────────────────────────────────────────
    _era_zh = {
        "pre70": "70年代前", "70s": "70年代", "80s": "80年代",
        "pre90": "80年代 懷舊", "90s": "90年代",
        "2000s": "2000年代", "2010s": "2010年代", "2020s": "2024年 最新",
    }
    _era_en = {
        "pre70": "60s", "70s": "70s", "80s": "80s",
        "pre90": "80s classic", "90s": "90s",
        "2000s": "2000s", "2010s": "2010s", "2020s": "2024 new release",
    }

    # ── shared mood / genre maps per language ─────────────────────────────
    _mood_zh  = {"happy":"歡快", "calm":"舒緩 輕鬆", "energetic":"激昂 熱血",
                 "sad":"憂鬱 傷感", "romantic":"浪漫 情歌",
                 "nostalgic":"懷念 懷舊", "uplifting":"振奮 勵志",
                 "dreamy":"夢幻 空靈", "lonely":"孤獨 落寞", "night":"夜晚 夜生活"}
    _mood_ja  = {"happy":"明るい 楽しい", "calm":"癒し ゆったり",
                 "energetic":"激しい 熱い", "sad":"悲しい 泣ける",
                 "romantic":"ロマンチック 恋愛", "nostalgic":"懐かしい レトロ",
                 "uplifting":"前向き 元気", "dreamy":"夢幻的 幻想的",
                 "lonely":"孤独 切ない", "night":"夜 深夜"}
    _mood_ko  = {"happy":"신나는 밝은", "calm":"잔잔한 편안한",
                 "energetic":"강렬한 열정적인", "sad":"슬픈 감성적",
                 "romantic":"로맨틱 사랑", "nostalgic":"추억 레트로",
                 "uplifting":"희망적 응원", "dreamy":"몽환적 감성",
                 "lonely":"쓸쓸한 외로운", "night":"밤 야간"}
    _mood_en  = {"happy":"happy uplifting", "calm":"calm peaceful",
                 "energetic":"energetic intense", "sad":"sad melancholy",
                 "romantic":"romantic love", "nostalgic":"nostalgic retro",
                 "uplifting":"motivational inspiring", "dreamy":"dreamy ethereal",
                 "lonely":"lonely solitude", "night":"late night"}
    _mood_fr  = {"happy":"joyeux", "calm":"calme apaisant", "energetic":"énergique",
                 "sad":"triste mélancolique", "romantic":"romantique",
                 "nostalgic":"nostalgique", "uplifting":"inspirant",
                 "dreamy":"rêveur", "lonely":"solitaire", "night":"nocturne"}
    _mood_es  = {"happy":"alegre", "calm":"tranquilo relajante", "energetic":"enérgico",
                 "sad":"triste melancólico", "romantic":"romántico",
                 "nostalgic":"nostálgico", "uplifting":"motivacional",
                 "dreamy":"soñador", "lonely":"solitario", "night":"nocturno"}

    _genre_zh = {"pop":"流行", "rock":"搖滾", "electronic":"電子",
                 "rnb":"R&B", "hiphop":"嘻哈", "folk":"民謠",
                 "jazz":"爵士", "classical":"古典", "indie":"獨立音樂",
                 "metal":"金屬", "punk":"龐克", "country":"鄉村",
                 "blues":"藍調", "soul":"靈魂樂", "reggae":"雷鬼",
                 "funk":"放克", "dance":"舞曲", "city_pop":"城市流行"}
    _genre_ja = {"pop":"J-POP", "rock":"ロック", "electronic":"エレクトロ",
                 "rnb":"R&B", "hiphop":"ヒップホップ", "folk":"フォーク",
                 "jazz":"ジャズ", "classical":"クラシック", "indie":"インディー",
                 "metal":"メタル", "punk":"パンク", "city_pop":"シティポップ",
                 "soul":"ソウル", "dance":"ダンス", "blues":"ブルース",
                 "funk":"ファンク", "country":"カントリー"}
    _genre_ko = {"pop":"K-POP", "rock":"록", "electronic":"일렉트로닉",
                 "rnb":"R&B", "hiphop":"힙합", "folk":"포크",
                 "jazz":"재즈", "classical":"클래식", "indie":"인디",
                 "metal":"메탈", "soul":"소울", "dance":"댄스",
                 "blues":"블루스", "punk":"펑크", "country":"컨트리",
                 "ballad":"발라드"}
    _genre_en = {"pop":"pop", "rock":"rock", "electronic":"electronic EDM",
                 "rnb":"R&B soul", "hiphop":"hip hop rap", "folk":"folk acoustic",
                 "jazz":"jazz", "classical":"classical orchestral",
                 "indie":"indie alternative", "metal":"metal", "punk":"punk",
                 "country":"country", "blues":"blues", "soul":"soul",
                 "reggae":"reggae", "funk":"funk", "dance":"dance",
                 "city_pop":"city pop"}

    # ── voice suffix helpers ──────────────────────────────────────────────
    _vc_zh = {"male":"男聲 男歌手", "female":"女聲 女歌手", "chorus":"合唱 重唱"}
    _vc_ja = {"male":"男性ボーカル 男性歌手", "female":"女性ボーカル 女性歌手",
              "chorus":"合唱 デュエット"}
    _vc_ko = {"male":"남성보컬 남자가수", "female":"여성보컬 여자가수", "chorus":"합창 듀엣"}
    _vc_en = {"male":"male vocalist male singer", "female":"female vocalist female singer",
              "chorus":"choir duet harmony"}
    _vc_fr = {"male":"chanteur masculin", "female":"chanteuse voix féminine", "chorus":"chorale duo"}
    _vc_es = {"male":"cantante masculino", "female":"cantante femenina", "chorus":"coro dúo"}
    _vc_de = {"male":"männlicher Sänger", "female":"weibliche Sängerin", "chorus":"Chor Duett"}
    _vc_it = {"male":"cantante maschile", "female":"cantante femminile", "chorus":"coro duetto"}
    _vc_pt = {"male":"cantor masculino", "female":"cantora feminina", "chorus":"coral dueto"}

    # ══════════════════════════════════════════════════════════════════════
    # CANTONESE (粵語)
    # ══════════════════════════════════════════════════════════════════════
    if language == "yue":
        tempo  = "快歌"     if r > 65 else ("慢歌 抒情" if r < 38 else "")
        energy = "激昂 熱血" if g > 65 else ("輕柔 舒緩" if g < 35 else "")
        key    = "歡快 大調" if ton > 65 else ("憂鬱 小調" if ton < 35 else "")
        md = _mood_zh.get(mood, "")
        gn = _genre_zh.get(genre, "")
        er = _era_zh.get(era, "")
        vc = _vc_zh.get(voice, "")
        sfx = "純音樂 器樂" if is_instr else "歌手 人聲 官方MV"
        return [
            (_q("粵語 廣東話", gn, md, er, vc, sfx),        "粵語歌曲整體推薦"),
            (_q("粵語 廣東話", gn, md, er, tempo, vc, sfx), "節奏情緒匹配"),
            (_q("香港 粵語", gn, key, er, vc, sfx),          "調性情感匹配"),
            (_q("廣東話", gn, energy, er, vc, sfx),           "律動風格匹配"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # CHINESE (中文/國語)
    # ══════════════════════════════════════════════════════════════════════
    if language == "zh":
        tempo  = "快歌"     if r > 65 else ("慢歌 抒情" if r < 38 else "")
        energy = "高能 熱血" if g > 65 else ("輕柔 舒緩" if g < 35 else "")
        key    = "歡快 大調" if ton > 65 else ("憂鬱 小調" if ton < 35 else "")
        md = _mood_zh.get(mood, "")
        gn = _genre_zh.get(genre, "")
        er = _era_zh.get(era, "")
        vc = _vc_zh.get(voice, "")
        sfx = "純音樂 器樂 輕音樂" if is_instr else "歌手 人聲 歌曲 官方MV"
        return [
            (_q("中文 華語", gn, md, er, vc, sfx),          "中文歌曲整體推薦"),
            (_q("中文 華語", gn, md, er, tempo, vc, sfx),   "節奏情緒匹配"),
            (_q("台灣 國語", gn, md, er, key, vc, sfx),     "調性情感匹配"),
            (_q("中文 華語", gn, energy, er, vc, sfx),       "律動風格匹配"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # JAPANESE
    # ══════════════════════════════════════════════════════════════════════
    if language == "ja":
        tempo = "アップテンポ" if r > 65 else ("スロー バラード" if r < 38 else "")
        key   = "明るい 大調"  if ton > 65 else ("悲しい 切ない 小調" if ton < 35 else "")
        md = _mood_ja.get(mood, "")
        gn = _genre_ja.get(genre, "")
        er = _era_zh.get(era, "")
        vc = _vc_ja.get(voice, "")
        sfx = "BGM インスト 純音楽" if is_instr else "歌手 ボーカル 公式MV"
        return [
            (_q("邦楽", gn, md, er, vc, sfx),               "日文歌曲整體推薦"),
            (_q("日本語", gn, md, er, tempo, vc, sfx),       "節奏情緒匹配"),
            (_q("邦楽", gn, key, er, vc, sfx),               "調性情感匹配"),
            (_q("日本音楽", gn, md, er, vc, sfx),            "風格綜合匹配"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # KOREAN
    # ══════════════════════════════════════════════════════════════════════
    if language == "ko":
        tempo = "신나는 빠른" if r > 65 else ("발라드 잔잔한" if r < 38 else "")
        key   = "밝은 밝고"   if ton > 65 else ("슬픈 감성"   if ton < 35 else "")
        md = _mood_ko.get(mood, "")
        gn = _genre_ko.get(genre, "")
        er = _era_zh.get(era, "")
        vc = _vc_ko.get(voice, "")
        sfx = "인스트루멘탈 BGM" if is_instr else "가수 보컬 MV"
        return [
            (_q("한국음악", gn, md, er, vc, sfx),            "韓文歌曲整體推薦"),
            (_q("한국어", gn, md, er, tempo, vc, sfx),        "節奏情緒匹配"),
            (_q("한국음악", gn, key, er, vc, sfx),            "調性情感匹配"),
            (_q("K-POP 한국", gn, md, er, vc, sfx),           "風格綜合匹配"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # FRENCH
    # ══════════════════════════════════════════════════════════════════════
    if language == "fr":
        sfx = "instrumental musique" if is_instr else "chanteur voix chanson officielle"
        gn  = {"pop":"pop", "rock":"rock", "jazz":"jazz", "folk":"chanson folk",
               "classical":"classique", "electronic":"électronique",
               "rnb":"R&B", "hiphop":"rap", "soul":"soul", "blues":"blues",
               "dance":"dance", "funk":"funk"}.get(genre, "")
        md  = _mood_fr.get(mood, "")
        er  = _era_en.get(era, "")
        vc  = _vc_fr.get(voice, "")
        return [
            (_q("musique française", gn, md, er, vc, sfx), "法文歌曲整體推薦"),
            (_q("chanson française", gn, md, vc, sfx),      "法語歌曲推薦"),
            (_q("france musique", gn, md, er, vc, sfx),     "法國音樂推薦"),
            (_q("francophone", gn, md, vc, sfx),            "法語區音樂推薦"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # SPANISH
    # ══════════════════════════════════════════════════════════════════════
    if language == "es":
        sfx = "instrumental música" if is_instr else "cantante voz canción oficial"
        gn  = {"pop":"pop", "rock":"rock", "jazz":"jazz", "folk":"folklórico",
               "classical":"clásico", "electronic":"electrónica",
               "rnb":"R&B", "hiphop":"reggaeton rap", "soul":"soul",
               "blues":"blues", "reggae":"reggae", "dance":"salsa cumbia",
               "funk":"funk", "country":"ranchera"}.get(genre, "")
        md  = _mood_es.get(mood, "")
        er  = _era_en.get(era, "")
        vc  = _vc_es.get(voice, "")
        return [
            (_q("música española", gn, md, er, vc, sfx),   "西班牙文歌曲整體推薦"),
            (_q("música latina", gn, md, er, vc, sfx),      "拉丁音樂推薦"),
            (_q("español canción", gn, md, er, vc, sfx),    "西班牙語音樂推薦"),
            (_q("latin music", gn, md, er, vc, sfx),         "拉丁風格推薦"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # GERMAN
    # ══════════════════════════════════════════════════════════════════════
    if language == "de":
        sfx = "instrumental Musik" if is_instr else "Sänger Gesang Lied offiziell"
        gn  = {"pop":"Pop", "rock":"Rock", "electronic":"Elektronik",
               "folk":"Folk Volksmusik", "classical":"Klassik",
               "jazz":"Jazz", "hiphop":"HipHop Rap", "metal":"Metal",
               "punk":"Punk", "soul":"Soul", "dance":"Dance",
               "blues":"Blues", "funk":"Funk"}.get(genre, "")
        md  = {"happy":"fröhlich", "calm":"ruhig entspannt", "energetic":"energisch",
               "sad":"traurig melancholisch", "romantic":"romantisch",
               "nostalgic":"nostalgisch", "uplifting":"inspirierend",
               "dreamy":"träumerisch", "lonely":"einsam", "night":"Nacht"}.get(mood, "")
        er  = _era_en.get(era, "")
        vc  = _vc_de.get(voice, "")
        return [
            (_q("deutsche Musik", gn, md, er, vc, sfx),     "德文歌曲整體推薦"),
            (_q("deutschsprachig", gn, md, er, vc, sfx),     "德語音樂推薦"),
            (_q("Deutschland Musik", gn, md, er, vc, sfx),   "德國音樂推薦"),
            (_q("deutsch Lied", gn, md, vc, sfx),            "德語歌曲推薦"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # ITALIAN
    # ══════════════════════════════════════════════════════════════════════
    if language == "it":
        sfx = "strumentale musica" if is_instr else "cantante voce canzone ufficiale"
        gn  = {"pop":"pop", "rock":"rock", "electronic":"elettronica",
               "folk":"folk popolare", "classical":"classica opera",
               "jazz":"jazz", "soul":"soul", "dance":"dance",
               "rnb":"R&B", "hiphop":"hip hop trap"}.get(genre, "")
        md  = {"happy":"allegro", "calm":"calmo rilassante", "energetic":"energico",
               "sad":"triste malinconico", "romantic":"romantico",
               "nostalgic":"nostalgico", "uplifting":"ispirazionale",
               "dreamy":"sognante", "lonely":"solitario", "night":"notturno"}.get(mood, "")
        er  = _era_en.get(era, "")
        vc  = _vc_it.get(voice, "")
        return [
            (_q("musica italiana", gn, md, er, vc, sfx),    "義大利文歌曲整體推薦"),
            (_q("italiano canzone", gn, md, er, vc, sfx),    "義大利語歌曲推薦"),
            (_q("Italia musica", gn, md, er, vc, sfx),       "義大利音樂推薦"),
            (_q("sanremo pop italiano", gn, md, vc, sfx),    "義大利流行推薦"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # PORTUGUESE
    # ══════════════════════════════════════════════════════════════════════
    if language == "pt":
        sfx = "instrumental música" if is_instr else "cantor voz música oficial"
        gn  = {"pop":"pop", "rock":"rock", "electronic":"eletrônica",
               "folk":"folk", "classical":"clássica", "jazz":"jazz",
               "hiphop":"hip hop funk carioca", "soul":"soul",
               "reggae":"reggae", "dance":"axé forró"}.get(genre, "")
        md  = {"happy":"alegre", "calm":"calmo tranquilo", "energetic":"animado",
               "sad":"triste melancólico", "romantic":"romântico",
               "nostalgic":"saudade nostálgico", "uplifting":"inspirador",
               "dreamy":"sonhador", "lonely":"solitário", "night":"noturno"}.get(mood, "")
        er  = _era_en.get(era, "")
        vc  = _vc_pt.get(voice, "")
        return [
            (_q("música portuguesa", gn, md, er, vc, sfx),  "葡萄牙文歌曲整體推薦"),
            (_q("música brasileira", gn, md, er, vc, sfx),   "巴西音樂推薦"),
            (_q("português canção", gn, md, er, vc, sfx),    "葡語歌曲推薦"),
            (_q("brazil samba bossa nova", gn, md, vc, sfx), "巴西風格推薦"),
        ]

    # ══════════════════════════════════════════════════════════════════════
    # ENGLISH / unspecified — keep entirely in English
    # ══════════════════════════════════════════════════════════════════════
    tempo_en  = "upbeat fast"     if r > 65 else ("slow"           if r < 38 else "mid-tempo")
    energy_en = "energetic"       if g > 65 else ("calm"           if g < 35 else "")
    key_en    = "happy uplifting" if ton > 65 else ("melancholy sad" if ton < 35 else "")
    md_en = _mood_en.get(mood, "")
    gn_en = _genre_en.get(genre, "")
    er_en = _era_en.get(era, "")
    lp    = "English" if language == "en" else ""
    vc_en = _vc_en.get(voice, "")
    sfx   = "instrumental BGM no vocals" if is_instr else "singer vocals official music video"
    return [
        (_q(lp, gn_en, md_en, er_en, tempo_en, vc_en, sfx),  "整體音訊偏好推薦"),
        (_q(lp, gn_en, md_en, er_en, key_en,   vc_en, sfx),  "情感氛圍推薦"),
        (_q(lp, gn_en, md_en, er_en, energy_en, vc_en, sfx), "律動能量推薦"),
        (_q(lp, gn_en, er_en, tempo_en, key_en, vc_en, sfx), "節奏調性綜合推薦"),
    ]

# ── extended taste insights ───────────────────────────────────────────────
def compute_taste_insights(all_features: list) -> dict:
    """
    Derive richer taste dimensions from raw 83-dim feature dicts.
    Only works on new-format songs (those with 'tempo' and 'chroma_0').
    Returns an empty dict if too few new-format songs exist.
    """
    from collections import Counter
    new_fmt = [f for f in all_features if "tempo" in f and "chroma_0" in f]
    if len(new_fmt) < 1:
        return {}

    # ── Chroma entropy (note variety) ─────────────────────────────────────
    # 0 = very tonal (one root), 1 = all 12 notes equally used (jazz/modal)
    entropies = []
    for f in new_fmt:
        c = np.array([f.get(f"chroma_{i}", 1/12) for i in range(12)], dtype=np.float32)
        c = c / (c.sum() + 1e-9)
        ent = float(-np.sum(c * np.log2(c + 1e-9)) / np.log2(12 + 1e-9))
        entropies.append(max(0.0, min(1.0, ent)))
    chroma_entropy = float(np.mean(entropies))

    # ── Tonnetz complexity (harmonic richness / chord variety) ────────────
    tn_stds = [float(np.std([f.get(f"tonnetz_{i}", 0) for i in range(6)]))
               for f in new_fmt]
    tonnetz_complexity = float(np.mean(tn_stds))

    # ── Major/minor tendency ──────────────────────────────────────────────
    tonalities = [f.get("_tonality", 0.5) for f in new_fmt if "_tonality" in f]
    major_tendency = float(np.mean(tonalities)) if tonalities else 0.5

    # ── Ensemble/production score ─────────────────────────────────────────
    # Combines stereo width + spectral bandwidth + harmonic ratio
    sw  = float(np.mean([f.get("stereo_width", 0)          for f in new_fmt]))
    bw  = float(np.mean([f.get("spectral_bandwidth", 1500) for f in new_fmt]))
    hr  = float(np.mean([f.get("harmonic_ratio", 0.5)      for f in new_fmt]))
    ensemble_score = float(min(1.0, sw * 0.35 + min(1.0, bw / 5000) * 0.35 + hr * 0.30))

    # ── Beat stability ────────────────────────────────────────────────────
    beat_reg = float(np.mean([f.get("beat_regularity", 0.5) for f in new_fmt]))

    # ── Percussive vs Harmonic dominance ──────────────────────────────────
    perc_avg = float(np.mean([f.get("percussive_ratio", 0.5) for f in new_fmt]))

    # ── Preferred key & consistency ───────────────────────────────────────
    keys = [round(f.get("key", 0)) % 12 for f in new_fmt if "key" in f]
    key_counter = Counter(keys)
    preferred_key   = key_counter.most_common(1)[0][0] if key_counter else 0
    key_consistency = key_counter.most_common(1)[0][1] / len(keys) if keys else 0.0

    # ── Tempo stats ───────────────────────────────────────────────────────
    tempos    = [f.get("tempo", 120) for f in new_fmt]
    tempo_avg = float(np.mean(tempos))
    tempo_std = float(np.std(tempos))

    # ── Texture indicators ────────────────────────────────────────────────
    zcr_avg  = float(np.mean([f.get("zcr_mean", 0.05)              for f in new_fmt]))
    cent_avg = float(np.mean([f.get("spectral_centroid_mean", 2000) for f in new_fmt]))

    return {
        "chroma_entropy":     round(chroma_entropy,     3),
        "tonnetz_complexity": round(tonnetz_complexity, 3),
        "major_tendency":     round(major_tendency,     3),
        "ensemble_score":     round(ensemble_score,     3),
        "beat_regularity":    round(beat_reg,           3),
        "percussive_ratio":   round(perc_avg,           3),
        "preferred_key":      preferred_key,
        "key_consistency":    round(key_consistency,    3),
        "tempo_avg":          round(tempo_avg,          1),
        "tempo_std":          round(tempo_std,          1),
        "zcr_mean":           round(zcr_avg,            4),
        "spectral_centroid":  round(cent_avg,           0),
        "sample_count":       len(new_fmt),
    }


# ── post-search title validators ───────────────────────────────────────────
def _title_matches_language(title: str, language: str) -> bool:
    """Reject titles that clearly belong to the WRONG language.
    Lenient: only rejects when we can positively detect a different script."""
    if not language or not title:
        return True
    has_hiragana = bool(re.search(r'[぀-ゟ]', title))
    has_katakana = bool(re.search(r'[゠-ヿ]', title))
    has_jp_kana  = has_hiragana or has_katakana
    has_hangul   = bool(re.search(r'[가-힯ᄀ-ᇿ]', title))
    has_cjk      = bool(re.search(r'[一-鿿]', title))
    # "Chinese" = CJK without Japanese kana
    has_chinese  = has_cjk and not has_jp_kana

    if language == "ja":
        return not has_chinese and not has_hangul
    if language == "ko":
        return not has_chinese and not has_jp_kana
    if language in ("zh", "yue"):
        return not has_jp_kana and not has_hangul
    if language in ("en", "fr", "es", "de", "it", "pt"):
        return not has_cjk and not has_jp_kana and not has_hangul
    return True


_BGM_TITLE_KW = frozenset([
    "bgm", "instrumental", "inst.", "純音樂", "インスト", "インスツルメンタル",
    "인스트루멘탈", "no vocal", "without vocal", "背景音樂", "カラオケ版",
    "off vocal", "off-vocal", "music box", "オルゴール", "orchestral ver",
])

def _is_bgm_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _BGM_TITLE_KW)


_FEMALE_TITLE_KW = frozenset([
    "女声", "女歌手", "여성보컬", "여자가수", "女性ボーカル", "女声版",
    "female vocal", "female singer",
])
_MALE_TITLE_KW = frozenset([
    "男声", "男歌手", "남성보컬", "남자가수", "男性ボーカル", "男声版",
    "male vocal", "male singer",
])

def _title_matches_voice(title: str, voice: str) -> bool:
    """Reject titles that explicitly declare the opposite gender."""
    if not voice or not title:
        return True
    t = title.lower()
    if voice == "female":
        return not any(kw in t for kw in _MALE_TITLE_KW)
    if voice == "male":
        return not any(kw in t for kw in _FEMALE_TITLE_KW)
    return True


# ── audio-based recommendation helpers ────────────────────────────────────
REC_TEMP_DIR = os.path.join(BASE_DIR, "rec_temp")
os.makedirs(REC_TEMP_DIR, exist_ok=True)


def download_audio_partial(vid: str, duration: int = 120) -> str:
    """Download first `duration` seconds of a YouTube video as WAV.
    Returns the temp WAV path; caller is responsible for deletion."""
    url  = f"https://www.youtube.com/watch?v={vid}"
    stem = os.path.join(REC_TEMP_DIR, f"tmp_{vid}_{uuid.uuid4().hex[:6]}")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": stem + ".%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "quiet": True, "no_warnings": True,
        "external_downloader": "ffmpeg",
        "external_downloader_args": {"ffmpeg_i": ["-t", str(duration)]},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return stem + ".wav"


def estimate_f0_gender(wav_path: str) -> str:
    """Estimate vocal gender from F0 via librosa.pyin.
    Returns 'female', 'male', or 'unknown'."""
    try:
        y, sr = librosa.load(wav_path, sr=22050, mono=True, duration=90.0)
        f0, voiced_flag, _ = librosa.pyin(
            y,
            fmin=librosa.note_to_hz("C2"),
            fmax=librosa.note_to_hz("C7"),
            sr=sr, frame_length=2048, hop_length=512,
        )
        voiced_f0 = f0[voiced_flag & (f0 > 0)]
        if len(voiced_f0) < 20:
            return "unknown"
        median_f0 = float(np.median(voiced_f0))
        if median_f0 > 165:
            return "female"
        if median_f0 < 155:
            return "male"
        return "unknown"
    except Exception:
        return "unknown"


def search_artist_gender_web(artist_name: str) -> str:
    """Web-search for artist gender. Returns 'male', 'female', or 'unknown'."""
    try:
        import urllib.request as _req
        query = urllib.parse.quote(f"{artist_name} singer gender")
        req = _req.Request(
            f"https://www.google.com/search?q={query}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with _req.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore").lower()
        female_hits = html.count(" she ") + html.count(" her ") + html.count("female")
        male_hits   = html.count(" he ")  + html.count(" his ") + html.count(" male")
        if female_hits > male_hits and female_hits > 3:
            return "female"
        if male_hits > female_hits and male_hits > 3:
            return "male"
        return "unknown"
    except Exception:
        return "unknown"


def fetch_youtube_radio(seed_vid: str, n: int = 40) -> list:
    """Fetch YouTube Radio/Mix candidates seeded from `seed_vid`."""
    url  = f"https://www.youtube.com/watch?v={seed_vid}&list=RD{seed_vid}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": n + 10}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        results = []
        for e in (info.get("entries") or []):
            if not e or not e.get("id") or e["id"] == seed_vid:
                continue
            if not _is_single(e):
                continue
            year = str(e.get("release_year") or "")
            if not year:
                ud = str(e.get("upload_date") or "")
                year = ud[:4] if len(ud) >= 4 else ""
            results.append({
                "id": e["id"],
                "title": e.get("title", ""),
                "duration": e.get("duration", 0),
                "uploader": e.get("uploader", ""),
                "release_year": year,
            })
            if len(results) >= n:
                break
        return results
    except Exception:
        return []


def _era_matches_year(year_str: str, era: str) -> bool:
    """Return True if the release year matches the era filter (or if unknown)."""
    if not era:
        return True
    try:
        y = int(year_str)
    except (ValueError, TypeError):
        return True  # unknown year: don't reject
    ranges = {
        "pre70": (0, 1969), "70s": (1970, 1979), "80s": (1980, 1989),
        "pre90": (0, 1989), "90s": (1990, 1999), "2000s": (2000, 2009),
        "2010s": (2010, 2019), "2020s": (2020, 9999),
    }
    lo, hi = ranges.get(era, (0, 9999))
    return lo <= y <= hi


def get_video_full_meta(vid: str) -> dict:
    """Fetch full yt-dlp metadata (tags, description, release_year) without downloading."""
    url = f"https://www.youtube.com/watch?v={vid}"
    opts = {"quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        year = str(info.get("release_year") or "")
        if not year:
            ud = str(info.get("upload_date") or "")
            year = ud[:4] if len(ud) >= 4 else ""
        return {
            "title":        info.get("title", ""),
            "uploader":     info.get("uploader", ""),
            "description":  (info.get("description") or "")[:1000].lower(),
            "tags":         [t.lower() for t in (info.get("tags") or [])],
            "categories":   [c.lower() for c in (info.get("categories") or [])],
            "release_year": year,
        }
    except Exception:
        return {}


def _year_from_title(title: str) -> str:
    """Extract 4-digit year from video title, e.g. 'Song (1985)' → '1985'."""
    m = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', title)
    return m.group(1) if m else ""


def _search_release_year(title: str, uploader: str) -> str:
    """Web-search for a song's release year. Returns 4-digit string or ''."""
    try:
        import urllib.request as _req
        q = urllib.parse.quote(f"{title} {uploader} release year")
        req = _req.Request(
            f"https://www.google.com/search?q={q}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with _req.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore")
        m = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', html)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _meta_text(meta: dict) -> str:
    """Concatenate all searchable metadata fields into a single lowercase string."""
    return " ".join([
        meta.get("title", ""),
        meta.get("uploader", ""),
        meta.get("description", ""),
        " ".join(meta.get("tags", [])),
        " ".join(meta.get("categories", [])),
    ]).lower()


_GENRE_META_KW: dict = {
    "electronic":  ["electronic","edm","synth","techno","house","trance","ambient",
                    "electronica","電子","シンセ","electro","synthwave","vaporwave"],
    "rock":        ["rock","搖滾","ロック","록","alternative","post-rock","indie rock",
                    "guitar","grunge"],
    "metal":       ["metal","heavy","thrash","doom","death metal","硬搖滾","メタル",
                    "메탈","metalcore","metalhead"],
    "jazz":        ["jazz","爵士","ジャズ","재즈","bebop","swing","bossa nova","jazz fusion"],
    "classical":   ["classical","orchestra","symphony","concerto","sonata","古典",
                    "クラシック","클래식","chamber","philharmonic","baroque","opera"],
    "folk":        ["folk","acoustic","民謠","フォーク","포크","singer-songwriter",
                    "fingerpicking","unplugged","bluegrass"],
    "hiphop":      ["hip hop","hiphop","hip-hop","rap","嘻哈","ヒップホップ","힙합",
                    "trap","drill","freestyle","rhyme"],
    "rnb":         ["r&b","rnb","rhythm and blues","soul","neo soul"],
    "indie":       ["indie","獨立","インディー","인디","lo-fi","alternative","bedroom pop"],
    "city_pop":    ["city pop","城市流行","シティポップ","citypop"],
    "dance":       ["dance","舞曲","ダンス","댄스","club","disco","house","dancefloor"],
    "country":     ["country","鄉村","カントリー","컨트리","bluegrass","americana"],
    "blues":       ["blues","藍調","ブルース","블루스"],
    "punk":        ["punk","龐克","パンク","펑크","hardcore","post-punk"],
    "reggae":      ["reggae","雷鬼","レゲエ","레게","ska","dancehall"],
    "funk":        ["funk","放克","ファンク","funky","groove"],
    "soul":        ["soul","靈魂","ソウル","소울","gospel","motown"],
    "pop":         ["pop","流行","ポップ","팝","j-pop","k-pop","c-pop"],
    "instrumental":["instrumental","pure music","纯音乐","純音樂","インスト","인스트","bgm"],
    "pure_instrumental": ["instrumental","純音樂","インスト","bgm","no vocal"],
}


def _genre_meta_matches(meta: dict, genre: str) -> bool | None:
    """Check metadata for genre keywords.
    Returns True = likely match, False = likely mismatch, None = metadata too sparse."""
    kws = _GENRE_META_KW.get(genre, [])
    if not kws:
        return None
    text = _meta_text(meta)
    if len(text.strip()) < 30:
        return None  # not enough metadata to judge
    return any(kw in text for kw in kws)


_MOOD_META_KW: dict = {
    "happy":      ["happy","happiness","joyful","cheerful","upbeat","fun","playful",
                   "快樂","開心","歡快","明朗","楽しい","嬉しい","행복","즐거운"],
    "calm":       ["calm","peaceful","relaxing","relaxation","chill","mellow","serene",
                   "tranquil","soothing","lofi","lo-fi","ambient","冷靜","平靜","放鬆",
                   "穩定","落ち着き","リラックス","잔잔한","힐링"],
    "energetic":  ["energetic","energy","pump","hype","workout","gym","power","intense",
                   "激烈","動感","活力","元気","エネルギッシュ","에너지","열정"],
    "sad":        ["sad","sadness","melancholy","melancholic","heartbreak","grief",
                   "sorrow","sorrowful","悲傷","憂愁","傷感","傷心","悲しい","切ない",
                   "슬픔","이별"],
    "romantic":   ["romantic","romance","love song","love","tender","intimate",
                   "浪漫","情歌","愛情","恋愛","ロマンチック","로맨틱","사랑"],
    "nostalgic":  ["nostalgic","nostalgia","memory","memories","throwback","retro",
                   "懷舊","回憶","思念","懐かしい","ノスタルジア","추억","그리움"],
    "uplifting":  ["uplifting","inspiring","motivating","motivation","empowering",
                   "positive","vibrant","鼓舞","激勵","振奮","勇気","힘나는"],
    "dreamy":     ["dreamy","dream","ethereal","hazy","atmospheric","whimsical",
                   "夢幻","夢想","飄渺","夢","드리미"],
    "lonely":     ["lonely","loneliness","alone","solitude","isolation","孤獨",
                   "寂寞","孤單","孤寂","孤独","寂しい","외로움","고독"],
    "night":      ["night","midnight","late night","nocturnal","after midnight",
                   "夜晚","深夜","夜","夜曲","真夜中","야간","밤"],
}


def _mood_meta_matches(meta: dict, mood: str) -> bool | None:
    """Check metadata for mood keywords.
    Returns True = likely match, False = clear mismatch, None = insufficient data.
    Unlike genre, mood metadata is softer — we only return False when we see
    strong contradicting mood signals, not just absence of target mood."""
    kws = _MOOD_META_KW.get(mood, [])
    if not kws:
        return None
    text = _meta_text(meta)
    if len(text.strip()) < 20:
        return None
    if any(kw in text for kw in kws):
        return True
    # Check for strongly contradicting moods
    _MOOD_ANTONYMS: dict[str, list[str]] = {
        "happy":     ["sad","sadness","grief","melancholy","heartbreak","sorrow","悲傷","悲しい","슬픔"],
        "sad":       ["happy","upbeat","fun","cheerful","energetic","party","dance","快樂","楽しい"],
        "calm":      ["energetic","pump","hype","intense","workout","gym","激烈","エネルギッシュ","에너지"],
        "energetic": ["calm","peaceful","chill","soothing","relaxing","ambient","冷靜","평온"],
        "romantic":  ["energetic","pump","hype","intense","workout","激烈"],
    }
    antonyms = _MOOD_ANTONYMS.get(mood, [])
    if antonyms and any(kw in text for kw in antonyms):
        return False
    return None  # no strong signal either way


def infer_genre_conf(f: dict, genre: str) -> float:
    """Audio-feature confidence [0,1] that `f` matches `genre`."""
    if not genre:
        return 1.0

    def _c(v, lo, hi):
        return min(1.0, max(0.0, (v - lo) / (hi - lo + 1e-9)))

    tempo  = f.get("tempo", 120)
    harm   = f.get("harmonic_ratio", 0.5)
    perc   = f.get("percussive_ratio", 0.5)
    sc     = f.get("spectral_centroid_mean", 2000)
    sc_std = f.get("spectral_centroid_std", 500)
    zcr    = f.get("zcr_mean", 0.05)
    rms    = f.get("rms_mean", 0.05)
    dyn    = f.get("dynamic_range", 20)
    beat_r = f.get("beat_regularity", 0.5)
    bw     = f.get("spectral_bandwidth", 1500)
    c0     = f.get("contrast_0", 20)

    if genre == "electronic":
        return beat_r * 0.4 + _c(sc, 2000, 7000) * 0.3 + perc * 0.3

    if genre == "rock":
        return _c(zcr, 0.05, 0.20) * 0.4 + _c(rms, 0.05, 0.25) * 0.3 + (1 - harm) * 0.3

    if genre == "metal":
        return _c(zcr, 0.10, 0.30) * 0.5 + _c(rms, 0.10, 0.30) * 0.3 + (1 - harm) * 0.2

    if genre == "classical":
        return harm * 0.5 + max(0.0, 1 - zcr / 0.06) * 0.3 + _c(dyn, 20, 50) * 0.2

    if genre == "jazz":
        tn_std = float(np.std([f.get(f"tonnetz_{i}", 0) for i in range(6)]))
        ch_std = float(np.std([f.get(f"chroma_{i}", 1/12) for i in range(12)]))
        return harm * 0.3 + min(tn_std * 5, 1.0) * 0.4 + min(ch_std * 10, 1.0) * 0.3

    if genre == "folk":
        return harm * 0.4 + max(0.0, 1 - sc / 4000) * 0.3 + max(0.0, 1 - zcr / 0.07) * 0.3

    if genre == "hiphop":
        return beat_r * 0.35 + _c(c0, 20, 60) * 0.35 + (1 - harm) * 0.3

    if genre == "dance":
        return beat_r * 0.5 + _c(sc, 1500, 5000) * 0.3 + perc * 0.2

    if genre in ("instrumental", "pure_instrumental"):
        return 1.0

    # pop/rnb/soul/indie/city_pop/etc. are acoustically ambiguous — don't over-filter
    return 0.65


def infer_mood_conf(f: dict, mood: str) -> float:
    """Audio-feature confidence [0,1] that `f` matches `mood`."""
    if not mood:
        return 1.0

    def _c(v, lo, hi):
        return min(1.0, max(0.0, (v - lo) / (hi - lo + 1e-9)))

    tempo  = f.get("tempo", 120)
    harm   = f.get("harmonic_ratio", 0.5)
    rms    = f.get("rms_mean", 0.05)
    onset  = f.get("onset_mean", 2.0)
    dyn    = f.get("dynamic_range", 20)
    zcr    = f.get("zcr_mean", 0.05)

    # Major/minor tendency: major triad (C=0, E=4, G=7) vs minor (C=0, Eb=3, G=7)
    major_sum = sum(f.get(f"chroma_{i}", 0) for i in [0, 4, 7])
    minor_sum = sum(f.get(f"chroma_{i}", 0) for i in [0, 3, 7])
    is_major  = major_sum >= minor_sum

    if mood == "happy":
        return (_c(tempo, 100, 180) * 0.3 +
                (1.0 if is_major else 0.15) * 0.4 +
                _c(rms, 0.04, 0.20) * 0.3)

    if mood == "calm":
        return (max(0.0, 1 - _c(tempo, 60, 120)) * 0.35 +
                harm * 0.35 +
                max(0.0, 1 - _c(rms, 0.0, 0.09)) * 0.3)

    if mood == "energetic":
        return (_c(tempo, 120, 200) * 0.3 +
                _c(rms, 0.07, 0.25) * 0.4 +
                _c(onset, 2.0, 8.0) * 0.3)

    if mood == "sad":
        return (max(0.0, 1 - _c(tempo, 60, 115)) * 0.3 +
                (0.85 if not is_major else 0.1) * 0.4 +
                max(0.0, 1 - _c(rms, 0.0, 0.09)) * 0.3)

    if mood == "romantic":
        tempo_fit = max(0.0, 1 - abs(tempo - 90) / 90)
        return tempo_fit * 0.3 + harm * 0.4 + _c(rms, 0.02, 0.12) * 0.3

    if mood == "nostalgic":
        return (max(0.0, 1 - abs(tempo - 100) / 80) * 0.3 +
                _c(dyn, 15, 35) * 0.35 + harm * 0.35)

    if mood == "uplifting":
        return (_c(tempo, 110, 180) * 0.35 +
                (1.0 if is_major else 0.15) * 0.35 +
                _c(rms, 0.05, 0.20) * 0.3)

    if mood == "dreamy":
        return (max(0.0, 1 - _c(tempo, 60, 105)) * 0.35 +
                harm * 0.35 +
                max(0.0, 1 - zcr / 0.07) * 0.3)

    if mood == "lonely":
        return (max(0.0, 1 - _c(tempo, 60, 110)) * 0.3 +
                (0.85 if not is_major else 0.1) * 0.4 +
                max(0.0, 1 - _c(onset, 0.0, 3.0)) * 0.3)

    if mood == "night":
        return (max(0.0, 1 - _c(rms, 0.0, 0.10)) * 0.35 +
                harm * 0.35 +
                max(0.0, 1 - _c(tempo, 60, 115)) * 0.3)

    return 0.65  # unknown mood: pass with moderate confidence


def _meta_language_matches(meta: dict, language: str) -> bool | None:
    """Detect language from metadata (description, tags, uploader).
    Returns False = clearly wrong language, True = confirmed correct, None = can't tell."""
    if not language:
        return None
    text = " ".join([
        meta.get("description", ""),
        " ".join(meta.get("tags", [])),
        meta.get("uploader", ""),
    ])
    if not text.strip():
        return None

    has_hiragana = bool(re.search(r'[぀-ゟ]', text))
    has_katakana = bool(re.search(r'[゠-ヿ]', text))
    has_jp_kana  = has_hiragana or has_katakana
    has_hangul   = bool(re.search(r'[가-힯ᄀ-ᇿ]', text))
    has_cjk      = bool(re.search(r'[一-鿿]', text))
    has_chinese  = has_cjk and not has_jp_kana

    if language in ("zh", "yue"):
        if has_jp_kana:   return False   # Japanese kana in metadata → Japanese song
        if has_hangul:    return False   # Korean in metadata → Korean song
        if has_chinese:   return True    # CJK without kana → likely Chinese
        return None

    if language == "ja":
        if has_hangul or has_chinese: return False
        if has_jp_kana:               return True
        return None

    if language == "ko":
        if has_jp_kana or has_chinese: return False
        if has_hangul:                 return True
        return None

    if language in ("en", "fr", "es", "de", "it", "pt"):
        # Reject if metadata is dominated by CJK / kana (more than 15 chars)
        cjk_count = len(re.findall(r'[一-鿿぀-ヿ가-힯]', text))
        if cjk_count > 15:
            return False
        return None

    return None


def _search_keyword_confirm(title: str, uploader: str, keywords: list) -> bool:
    """Web-search to confirm any positive keyword applies to this song.
    Fail-open: returns True if the search itself fails."""
    try:
        import urllib.request as _req
        q = urllib.parse.quote(f"{title} {uploader} {' '.join(keywords[:3])}")
        req = _req.Request(
            f"https://www.google.com/search?q={q}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with _req.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore").lower()
        return any(kw in html for kw in keywords)
    except Exception:
        return True  # network failure → don't reject


# ── shared "add one song" logic ────────────────────────────────────────────
def _add_song_gen(url: str, override_title: str = "", profile_id: int = 1):
    """Generator shared by /api/add-song and batch processors."""
    is_spotify = _is_spotify(url)
    try:
        if is_spotify:
            yield _sse({"step":"info","message":"解析 Spotify 資訊…","progress":10})
            sp = spotify_track_info(url)
            query = sp["search_query"]
            yield _sse({"step":"info_done","message":f"Spotify：{sp['spotify_title']}",
                        "progress":18,"title":sp["spotify_title"],"thumbnail":sp["thumbnail"]})
            yield _sse({"step":"search","message":f"搜尋 YouTube：{query}","progress":25})
            results = (search_youtube(f"{query} official audio", 1) or
                       search_youtube(query, 1))
            if not results:
                yield _sse({"step":"error","message":"找不到對應的 YouTube 版本"}); return
            video = results[0]
            vid   = video["id"]
            info  = {
                "id":        vid,
                "title":     override_title or sp["spotify_title"] or video["title"],
                "thumbnail": video["thumbnail"],
                "duration":  video.get("duration", 0),
            }
        else:
            yield _sse({"step":"info","message":"正在取得影片資訊…","progress":10})
            info = get_youtube_info(url)
            vid  = info["id"]
            if override_title:
                info["title"] = override_title
            yield _sse({"step":"info_done","message":f"已取得：{info['title']}",
                        "progress":20,"title":info["title"],"thumbnail":info["thumbnail"]})

        conn = _get_conn()
        if conn.execute("SELECT id FROM songs WHERE youtube_id=? AND profile_id=?",
                        (vid, profile_id)).fetchone():
            conn.close()
            yield _sse({"step":"error","message":"此歌曲已在歌單中"}); return
        conn.close()

        path = _audio_path(vid)
        if not os.path.exists(path):
            yield _sse({"step":"download","message":"正在下載音訊…","progress":35})
            download_audio(vid)
            yield _sse({"step":"download_done","message":"下載完成","progress":60})
        else:
            yield _sse({"step":"download_done","message":"使用快取音訊","progress":60})

        yield _sse({"step":"analyze","message":"正在分析音訊特徵…","progress":75})
        features = analyze_audio(path)
        norm     = normalize_features(features)
        tags     = generate_tags(features, norm)
        sid      = str(uuid.uuid4())

        conn = _get_conn()
        conn.execute(
            "INSERT INTO songs(id,youtube_id,title,thumbnail,url,added_at,"
            "features,normalized,tags,profile_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, vid, info["title"], info["thumbnail"],
             f"https://www.youtube.com/watch?v={vid}",
             time.time(),
             json.dumps({**features, "normalized": norm}),
             json.dumps(norm), json.dumps(tags), profile_id))
        conn.commit(); conn.close()

        yield _sse({"step":"done","message":"分析完成！","progress":100,
                    "song":{"id":sid,"youtube_id":vid,"title":info["title"],
                            "thumbnail":info["thumbnail"],
                            "url":f"https://www.youtube.com/watch?v={vid}",
                            "bpm":round(features.get("tempo", features.get("bpm", 0)),1),
                            "normalized":norm,"tags":tags,"added_at":time.time()}})
    except Exception as e:
        traceback.print_exc()
        yield _sse({"step":"error","message":f"發生錯誤：{e}"})


# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "muse.html")

# ── video / track info ─────────────────────────────────────────────────────
@app.route("/api/youtube-info")
def api_youtube_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "請提供網址"}), 400
    try:
        if _is_spotify(url):
            sp = spotify_track_info(url)
            return jsonify({"id":None,"title":sp["spotify_title"],
                            "thumbnail":sp["thumbnail"],"duration":0,
                            "is_spotify":True,"search_query":sp["search_query"]})
        return jsonify(get_youtube_info(url))
    except Exception as e:
        return jsonify({"error": f"無法取得資訊：{e}"}), 500

# ── add single song (YouTube or Spotify) ──────────────────────────────────
@app.route("/api/add-song")
def api_add_song():
    url        = request.args.get("url", "").strip()
    profile_id = int(request.args.get("profile", 1))
    def generate():
        if not url:
            yield _sse({"step":"error","message":"請提供網址"}); return
        yield from _add_song_gen(url, profile_id=profile_id)
    return _sse_response(generate())

# ── songs list ─────────────────────────────────────────────────────────────
@app.route("/api/profiles")
def api_profiles():
    conn = _get_conn()
    rows = conn.execute("SELECT id,name FROM profiles ORDER BY id").fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])

@app.route("/api/profiles/<int:pid>", methods=["PUT"])
def api_profile_rename(pid):
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "名稱不可為空"}), 400
    if pid not in (1, 2, 3):
        return jsonify({"error": "無效的設定檔"}), 400
    conn = _get_conn()
    conn.execute("UPDATE profiles SET name=? WHERE id=?", (name, pid))
    conn.commit(); conn.close()
    return jsonify({"success": True, "id": pid, "name": name})

@app.route("/api/songs")
def api_songs():
    profile_id = int(request.args.get("profile", 1))
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id,youtube_id,title,thumbnail,url,added_at,features,normalized,tags "
        "FROM songs WHERE profile_id=? ORDER BY added_at DESC",
        (profile_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        f = json.loads(r["features"])
        n = json.loads(r["normalized"]) if r["normalized"] else \
            f.get("normalized", normalize_features(f))
        out.append({"id":r["id"],"youtube_id":r["youtube_id"],
                    "title":r["title"],"thumbnail":r["thumbnail"],
                    "url":r["url"],"added_at":r["added_at"],
                    "bpm":round(f.get("tempo", f.get("bpm", 0)),1),
                    "normalized":n,"tags":json.loads(r["tags"])})
    return jsonify(out)

@app.route("/api/songs-raw")
def api_songs_raw():
    """Return raw 83-dim feature dicts for all songs (for the taste detail panel)."""
    profile_id = int(request.args.get("profile", 1))
    conn = _get_conn()
    rows = conn.execute(
        "SELECT features FROM songs WHERE profile_id=?",
        (profile_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        f = json.loads(r["features"])
        f.pop("normalized", None)
        out.append(f)
    return jsonify(out)

@app.route("/api/delete-song/<sid>", methods=["DELETE"])
def api_delete_song(sid):
    conn = _get_conn()
    conn.execute("DELETE FROM songs WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({"success": True})

# ── playlist info (YouTube or Spotify) ────────────────────────────────────
@app.route("/api/playlist-info")
def api_playlist_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "請提供播放清單網址"}), 400
    try:
        if _is_spotify_playlist(url):
            return jsonify(spotify_playlist_info(url))
        return jsonify(fetch_playlist_info(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── YouTube batch-add (IDs) ────────────────────────────────────────────────
@app.route("/api/batch-add")
def api_batch_add():
    ids_str     = request.args.get("ids", "")
    youtube_ids = [i.strip() for i in ids_str.split(",") if i.strip()]
    profile_id  = int(request.args.get("profile", 1))

    def generate():
        if not youtube_ids:
            yield _sse({"type":"error","message":"沒有提供影片 ID"}); return
        yield _sse({"type":"start","total":len(youtube_ids)})
        for i, vid in enumerate(youtube_ids):
            yield _sse({"type":"song_start","index":i,"total":len(youtube_ids),"youtube_id":vid})
            try:
                conn = _get_conn()
                if conn.execute("SELECT id FROM songs WHERE youtube_id=? AND profile_id=?",
                                (vid, profile_id)).fetchone():
                    conn.close()
                    yield _sse({"type":"song_skip","index":i,"youtube_id":vid,"message":"已在歌單中"})
                    continue
                conn.close()
                url  = f"https://www.youtube.com/watch?v={vid}"
                info = get_youtube_info(url)
                yield _sse({"type":"song_info","index":i,
                            "title":info["title"],"thumbnail":info["thumbnail"]})
                path = _audio_path(vid)
                if not os.path.exists(path):
                    download_audio(vid)
                features = analyze_audio(path)
                norm     = normalize_features(features)
                tags     = generate_tags(features, norm)
                sid      = str(uuid.uuid4())
                conn = _get_conn()
                conn.execute(
                    "INSERT OR IGNORE INTO songs(id,youtube_id,title,thumbnail,url,added_at,"
                    "features,normalized,tags,profile_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, vid, info["title"], info["thumbnail"], url, time.time(),
                     json.dumps({**features,"normalized":norm}),
                     json.dumps(norm), json.dumps(tags), profile_id))
                conn.commit(); conn.close()
                yield _sse({"type":"song_done","index":i,
                            "song":{"id":sid,"youtube_id":vid,"title":info["title"],
                                    "thumbnail":info["thumbnail"],"url":url,
                                    "bpm":round(features.get("tempo", features.get("bpm", 0)),1),
                                    "normalized":norm,"tags":tags}})
            except Exception as e:
                traceback.print_exc()
                yield _sse({"type":"song_error","index":i,"youtube_id":vid,"message":str(e)})
        yield _sse({"type":"batch_done"})

    return _sse_response(generate())

# ── Spotify batch-add (search queries) ────────────────────────────────────
@app.route("/api/create-batch", methods=["POST"])
def api_create_batch():
    data    = request.get_json(force=True) or {}
    tracks  = data.get("tracks", [])   # [{query, title}]
    bid     = str(uuid.uuid4())[:10]
    with _batch_lock:
        _batch_store[bid] = tracks
    return jsonify({"batch_id": bid})

@app.route("/api/run-batch")
def api_run_batch():
    bid        = request.args.get("id", "")
    profile_id = int(request.args.get("profile", 1))
    with _batch_lock:
        tracks = _batch_store.pop(bid, [])

    def generate():
        if not tracks:
            yield _sse({"type":"error","message":"批次不存在或已過期"}); return
        yield _sse({"type":"start","total":len(tracks)})
        for i, track in enumerate(tracks):
            query = track.get("query", "")
            title = track.get("title", query)
            yield _sse({"type":"song_start","index":i,"total":len(tracks)})
            try:
                yield _sse({"type":"song_search","index":i,"query":query})
                results = (search_youtube(f"{query} official audio", 1) or
                           search_youtube(query, 1))
                if not results:
                    yield _sse({"type":"song_error","index":i,
                                "message":f"找不到：{title}"}); continue
                video = results[0]
                vid   = video["id"]

                conn = _get_conn()
                if conn.execute("SELECT id FROM songs WHERE youtube_id=? AND profile_id=?",
                                (vid, profile_id)).fetchone():
                    conn.close()
                    yield _sse({"type":"song_skip","index":i,"message":"已在歌單中"})
                    continue
                conn.close()

                yield _sse({"type":"song_info","index":i,
                            "title":title,"thumbnail":video["thumbnail"]})
                path = _audio_path(vid)
                if not os.path.exists(path):
                    download_audio(vid)
                features = analyze_audio(path)
                norm     = normalize_features(features)
                tags     = generate_tags(features, norm)
                sid      = str(uuid.uuid4())
                url      = f"https://www.youtube.com/watch?v={vid}"

                conn = _get_conn()
                conn.execute(
                    "INSERT OR IGNORE INTO songs(id,youtube_id,title,thumbnail,url,added_at,"
                    "features,normalized,tags,profile_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, vid, title, video["thumbnail"], url, time.time(),
                     json.dumps({**features,"normalized":norm}),
                     json.dumps(norm), json.dumps(tags), profile_id))
                conn.commit(); conn.close()

                yield _sse({"type":"song_done","index":i,
                            "song":{"id":sid,"youtube_id":vid,"title":title,
                                    "thumbnail":video["thumbnail"],"url":url,
                                    "bpm":round(features.get("tempo", features.get("bpm", 0)),1),
                                    "normalized":norm,"tags":tags}})
            except Exception as e:
                traceback.print_exc()
                yield _sse({"type":"song_error","index":i,"message":str(e)})
        yield _sse({"type":"batch_done"})

    return _sse_response(generate())

# ── local file upload ──────────────────────────────────────────────────────
_ALLOWED_EXTS = {".mp3",".wav",".flac",".m4a",".aac",".ogg",".opus",".wma"}

@app.route("/api/upload-file", methods=["POST"])
def api_upload_file():
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "沒有收到檔案"}), 400
    filename = secure_filename(f.filename or "upload")
    ext      = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_EXTS:
        return jsonify({"error": f"不支援的格式，請使用 MP3/WAV/FLAC/M4A/AAC/OGG"}), 400

    file_id   = str(uuid.uuid4())[:12]
    orig_path = os.path.join(AUDIO_DIR, f"local_{file_id}{ext}")
    wav_path  = os.path.join(AUDIO_DIR, f"local_{file_id}.wav")
    f.save(orig_path)

    if ext == ".wav":
        os.rename(orig_path, wav_path)
    else:
        try:
            subprocess.run(
                ["ffmpeg", "-i", orig_path, wav_path, "-y", "-loglevel", "quiet"],
                check=True, timeout=120)
            os.remove(orig_path)
        except Exception as e:
            if os.path.exists(orig_path):
                os.remove(orig_path)
            return jsonify({"error": f"轉換失敗：{e}"}), 500

    title = os.path.splitext(filename)[0]
    return jsonify({"file_id": file_id, "title": title,
                    "size_mb": round(os.path.getsize(wav_path)/1024/1024, 1)})

@app.route("/api/analyze-upload")
def api_analyze_upload():
    file_id    = request.args.get("file_id", "")
    title      = request.args.get("title", "本機音訊").strip() or "本機音訊"
    profile_id = int(request.args.get("profile", 1))

    def generate():
        song_id  = f"local_{file_id}"
        wav_path = os.path.join(AUDIO_DIR, f"{song_id}.wav")
        if not os.path.exists(wav_path):
            yield _sse({"step":"error","message":"找不到上傳的檔案"}); return
        try:
            conn = _get_conn()
            if conn.execute("SELECT id FROM songs WHERE youtube_id=? AND profile_id=?",
                            (song_id, profile_id)).fetchone():
                conn.close()
                yield _sse({"step":"error","message":"此檔案已在歌單中"}); return
            conn.close()

            yield _sse({"step":"analyze","message":"正在分析音訊特徵…","progress":30})
            features = analyze_audio(wav_path)
            norm     = normalize_features(features)
            tags     = generate_tags(features, norm)
            sid      = str(uuid.uuid4())

            conn = _get_conn()
            conn.execute(
                "INSERT INTO songs(id,youtube_id,title,thumbnail,url,added_at,"
                "features,normalized,tags,profile_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, song_id, title, "", f"local://{wav_path}",
                 time.time(),
                 json.dumps({**features,"normalized":norm}),
                 json.dumps(norm), json.dumps(tags), profile_id))
            conn.commit(); conn.close()

            yield _sse({"step":"done","message":"分析完成！","progress":100,
                        "song":{"id":sid,"youtube_id":song_id,"title":title,
                                "thumbnail":"","url":"",
                                "bpm":round(features.get("tempo", features.get("bpm", 0)),1),
                                "normalized":norm,"tags":tags,
                                "added_at":time.time()}})
        except Exception as e:
            traceback.print_exc()
            yield _sse({"step":"error","message":f"發生錯誤：{e}"})

    return _sse_response(generate())

# ── taste analysis ─────────────────────────────────────────────────────────
@app.route("/api/analyze-taste")
def api_analyze_taste():
    profile_id = int(request.args.get("profile", 1))
    conn = _get_conn()
    rows = conn.execute(
        "SELECT features,normalized FROM songs WHERE profile_id=?",
        (profile_id,)).fetchall()
    conn.close()
    if len(rows) < 3:
        return jsonify({"error":"需要至少 3 首歌曲"}), 400
    all_norm, all_raw = [], []
    for r in rows:
        f = json.loads(r["features"])
        f_clean = dict(f); f_clean.pop("normalized", None)
        if "tempo" in f_clean:          # new 83-dim format only
            all_raw.append(f_clean)
        n = json.loads(r["normalized"]) if r["normalized"] else \
            f.get("normalized", normalize_features(f))
        all_norm.append(n)
    profile, std_p = {}, {}
    for dim in DIMS:
        vals = [n.get(dim, 50) for n in all_norm]
        profile[dim] = float(np.mean(vals))
        std_p[dim]   = float(np.std(vals))
    avg_std     = float(np.mean(list(std_p.values())))
    consistency = round(max(0.0, min(100.0, 100.0 - avg_std * 2.0)), 1)
    style       = infer_musical_style(all_raw)
    insights    = compute_taste_insights(all_raw)
    return jsonify({"profile":profile,"std":std_p,
                    "consistency":consistency,"song_count":len(rows),
                    "style":style,"insights":insights})

# ── recommendations (SSE) ─────────────────────────────────────────────────
@app.route("/api/recommend-audio")
def api_recommend_audio():
    """Audio-fingerprint-based recommendation: compares 83-dim candidate vectors
    against the library centroid instead of keyword search."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import random

    language   = request.args.get("language", "")
    genre      = request.args.get("genre",    "")
    mood       = request.args.get("mood",     "")
    era        = request.args.get("era",      "")
    voice      = request.args.get("voice",    "")
    exclude    = request.args.get("exclude",  "").strip()
    include    = request.args.get("include",  "").strip()
    profile_id = int(request.args.get("profile", 1))

    excl_kw  = [k.strip().lower() for k in re.split(r"[,，\s]+", exclude) if k.strip()]
    incl_kw  = [k.strip().lower() for k in re.split(r"[,，\s]+", include) if k.strip()]
    is_instr = genre in ("instrumental", "pure_instrumental")

    def generate():
        # ── 1. Load library + build 83-dim centroid ───────────────────────
        conn = _get_conn()
        rows = conn.execute(
            "SELECT youtube_id,title,features,normalized FROM songs WHERE profile_id=?",
            (profile_id,)).fetchall()
        conn.close()
        if len(rows) < 3:
            yield _sse({"type": "error", "message": "需要至少 3 首歌曲"}); return

        # Collect ALL library youtube IDs for exclusion (regardless of feature format)
        lib_id_set = {r["youtube_id"] for r in rows if r["youtube_id"]}

        all_raw, all_norm, lib_vecs, lib_vids = [], [], [], []
        for r in rows:
            f      = json.loads(r["features"])
            f_clean = {k: v for k, v in f.items() if k != "normalized"}
            if "tempo" in f_clean:
                all_raw.append(f_clean)
                lib_vecs.append(get_feature_vector(f_clean))
                lib_vids.append(r["youtube_id"])
            n_raw = r["normalized"]
            n = json.loads(n_raw) if n_raw else f.get("normalized", normalize_features(f))
            if n:
                all_norm.append(n)

        if not lib_vecs:
            yield _sse({"type": "error",
                        "message": "歌單中沒有 83 維度特徵，請先重新加入歌曲"}); return

        profile_vec = np.mean(lib_vecs, axis=0).astype(np.float32)

        # ── 2. Pick seed videos (top 5 nearest to centroid) ───────────────
        sims_lib = sorted(
            [(float(np.dot(v, profile_vec) /
                    (np.linalg.norm(v) * np.linalg.norm(profile_vec) + 1e-9)), vid)
             for v, vid in zip(lib_vecs, lib_vids)],
            reverse=True)
        seed_vids = [vid for _, vid in sims_lib[:5] if vid]
        random.shuffle(seed_vids)

        yield _sse({"type": "status", "message": "蒐集候選歌曲…", "progress": 3})

        # ── 3. Gather candidates from Radio playlists + search supplement ──
        candidate_ids: set = set()
        candidates:   list = []

        for seed_vid in seed_vids:
            for c in fetch_youtube_radio(seed_vid, n=40):
                if c["id"] not in candidate_ids and c["id"] not in lib_id_set:
                    candidate_ids.add(c["id"])
                    candidates.append(c)

        # Keyword supplement for variety
        norm_profile = {d: float(np.mean([n.get(d, 50) for n in all_norm])) for d in DIMS}
        style        = infer_musical_style(all_raw)
        extra_terms  = style.get("query_terms", {}).get(language, "") or ""
        if include:
            extra_terms = (extra_terms + " " + include).strip()

        supp_queries = build_rec_queries(norm_profile, language, genre, mood, era, voice)
        for q, _ in supp_queries[:4]:
            if extra_terms:
                q = re.sub(r"\s+", " ", q + " " + extra_terms).strip()
            for v in search_youtube(q, 10):
                if v["id"] not in candidate_ids and v["id"] not in lib_id_set:
                    candidate_ids.add(v["id"])
                    candidates.append({
                        "id": v["id"], "title": v["title"],
                        "duration": v.get("duration", 0),
                        "uploader": "", "release_year": "",
                    })

        random.shuffle(candidates)
        total_cands = len(candidates)

        if total_cands == 0:
            yield _sse({"type": "error",
                        "message": "找不到候選歌曲，請嘗試調整篩選條件"}); return

        yield _sse({"type": "status",
                    "message": f"找到 {total_cands} 首候選，開始音訊分析…",
                    "progress": 6, "total": total_cands})

        # ── 4. Parallel audio analysis ─────────────────────────────────────
        analyzed_count = [0]
        results_lock   = threading.Lock()
        passed_results: list = []

        # capture for closure
        _profile_vec  = profile_vec
        _norm_profile = norm_profile   # 11-dim human-readable profile average
        _excl_kw      = excl_kw
        _incl_kw      = incl_kw
        _language     = language
        _genre        = genre
        _mood         = mood
        _voice        = voice
        _era          = era
        _is_instr     = is_instr

        def analyze_candidate(c: dict):
            vid      = c["id"]
            title    = c.get("title", "")
            title_lc = title.lower()

            # ── Layer 1: Fast title pre-filters (no network) ──────────────
            if not _title_matches_language(title, _language):
                return None
            if not _is_instr and _is_bgm_title(title):
                return None
            if _excl_kw and any(kw in title_lc for kw in _excl_kw):
                return None

            # ── Layer 2: Full metadata fetch (tags, description, year) ────
            meta = get_video_full_meta(vid)
            if not meta:
                meta = {
                    "title": title, "uploader": c.get("uploader", ""),
                    "description": "", "tags": [], "categories": [],
                    "release_year": c.get("release_year", ""),
                }
            uploader = meta.get("uploader", "") or c.get("uploader", "")

            # ── Layer 2b: Language — metadata script check ────────────────
            if _language:
                lang_meta = _meta_language_matches(meta, _language)
                if lang_meta is False:
                    return None

            # ── Layer 3: Era — metadata year + title year + web search ────
            if _era:
                year = meta.get("release_year") or c.get("release_year", "")
                if not year:
                    year = _year_from_title(title)
                if not year:
                    year = _search_release_year(title, uploader)
                if year and not _era_matches_year(year, _era):
                    return None

            # ── Layer 4: Genre — metadata keyword check ───────────────────
            genre_meta = None  # None = insufficient data, True/False = decision
            if _genre and not _is_instr:
                genre_meta = _genre_meta_matches(meta, _genre)

            # ── Layer 4b: Mood — metadata keyword check (pre-download) ────
            mood_meta = None  # None = insufficient data, True = match, False = mismatch
            if _mood:
                mood_meta = _mood_meta_matches(meta, _mood)
                # If metadata clearly contradicts the mood, skip without downloading
                if mood_meta is False:
                    return None

            # ── Layer 5: Exclude keywords in metadata ─────────────────────
            if _excl_kw:
                mt = _meta_text(meta)
                if any(kw in mt for kw in _excl_kw):
                    return None

            # ── Layer 6: Download + analyze audio ─────────────────────────
            tmp_path = None
            try:
                tmp_path = download_audio_partial(vid, duration=120)
                if not os.path.exists(tmp_path):
                    return None

                feats = analyze_audio(tmp_path)
                if "tempo" not in feats:
                    return None

                cand_vec   = get_feature_vector(feats)
                norm_c     = np.linalg.norm(cand_vec)
                norm_p     = np.linalg.norm(_profile_vec)
                similarity = float(np.dot(cand_vec, _profile_vec) /
                                   (norm_c * norm_p + 1e-9))

                # ── Layer 7: Genre — audio confidence + metadata combined ──
                if _genre and not _is_instr:
                    audio_conf = infer_genre_conf(feats, _genre)
                    # Reject only when metadata says mismatch AND audio says mismatch
                    if genre_meta is False and audio_conf < 0.35:
                        return None
                    # Reject if audio strongly disagrees and metadata is absent
                    if genre_meta is None and audio_conf < 0.25:
                        return None

                # ── Layer 8: Mood — metadata primary, audio fallback ──────
                if _mood:
                    if mood_meta is True:
                        pass  # metadata confirmed match → accept
                    else:
                        mood_conf = infer_mood_conf(feats, _mood)
                        # When metadata had no signal, use a lenient threshold;
                        # when metadata was absent entirely, keep normal threshold.
                        threshold = 0.22 if mood_meta is None else 0.30
                        if mood_conf < threshold:
                            return None

                # ── Layer 9: Voice — F0 + web search ──────────────────────
                gender = "unknown"
                if _voice in ("male", "female"):
                    gender = estimate_f0_gender(tmp_path)
                    if gender == "unknown" and uploader:
                        gender = search_artist_gender_web(uploader)
                    if gender != "unknown" and gender != _voice:
                        return None

                # ── Layer 10: Positive keywords — title + metadata + web ───
                if _incl_kw:
                    mt = _meta_text(meta)
                    full_text = title_lc + " " + mt
                    if not any(kw in full_text for kw in _incl_kw):
                        if not _search_keyword_confirm(title, uploader, _incl_kw):
                            return None

                # ── Dimension comparison vs profile average ───────────────
                cand_norm = normalize_features(feats)
                deviations = []
                for dim in DIMS:
                    sv = cand_norm.get(dim, 50)
                    pv = _norm_profile.get(dim, 50)
                    deviations.append({
                        "key":           dim,
                        "label":         _DIM_NAMES_ZH[dim],
                        "song_value":    round(sv, 1),
                        "profile_value": round(pv, 1),
                        "deviation":     round(abs(sv - pv), 1),
                        "direction":     "偏高" if sv > pv else "偏低",
                    })

                return {
                    "id":               vid,
                    "title":            title,
                    "duration":         c.get("duration", 0),
                    "url":              f"https://www.youtube.com/watch?v={vid}",
                    "youtube_music_url":f"https://music.youtube.com/watch?v={vid}",
                    "thumbnail":        f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
                    "similarity":       round(similarity * 100, 1),
                    "gender":           gender,
                    "normalized":       cand_norm,
                    "deviations":       deviations,
                }
            except Exception:
                return None
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(analyze_candidate, c): c for c in candidates}
            for future in as_completed(futures):
                analyzed_count[0] += 1
                pct    = 6 + int((analyzed_count[0] / total_cands) * 90)
                result = future.result()

                with results_lock:
                    if result is not None:
                        passed_results.append(result)
                    best_sim = max((r["similarity"] for r in passed_results), default=0.0)

                yield _sse({
                    "type":     "progress",
                    "analyzed": analyzed_count[0],
                    "total":    total_cands,
                    "progress": min(pct, 96),
                    "best_sim": round(best_sim, 1),
                    "passed":   len(passed_results),
                })
                if result is not None:
                    yield _sse({"type": "result", "video": result})

        yield _sse({
            "type":          "done",
            "total_analyzed": total_cands,
            "total_passed":   len(passed_results),
        })

    return _sse_response(generate())

# ── rate song (SSE) ────────────────────────────────────────────────────────
@app.route("/api/rate-song")
def api_rate_song():
    url        = request.args.get("url","").strip()
    profile_id = int(request.args.get("profile", 1))
    def generate():
        if not url:
            yield _sse({"step":"error","message":"請提供網址"}); return
        try:
            yield _sse({"step":"info","message":"正在取得資訊…","progress":10})
            if _is_spotify(url):
                sp   = spotify_track_info(url)
                yield _sse({"step":"search","message":f"搜尋 YouTube：{sp['search_query']}","progress":20})
                res  = (search_youtube(f"{sp['search_query']} official audio", 1) or
                        search_youtube(sp["search_query"], 1))
                if not res:
                    yield _sse({"step":"error","message":"找不到對應 YouTube 版本"}); return
                vid  = res[0]["id"]
                info = {"title":sp["spotify_title"],"thumbnail":res[0]["thumbnail"]}
            else:
                raw  = get_youtube_info(url)
                vid  = raw["id"]
                info = {"title":raw["title"],"thumbnail":raw["thumbnail"]}

            conn = _get_conn()
            row  = conn.execute(
                "SELECT features,normalized FROM songs WHERE youtube_id=? AND profile_id=?",
                (vid, profile_id)).fetchone()
            conn.close()
            if row:
                features = json.loads(row["features"])
                norm = json.loads(row["normalized"]) if row["normalized"] else \
                       features.get("normalized", normalize_features(features))
                yield _sse({"step":"download_done","message":"使用歌單中的分析資料","progress":60})
            else:
                path = _audio_path(vid)
                if not os.path.exists(path):
                    yield _sse({"step":"download","message":"正在下載音訊…","progress":30})
                    download_audio(vid)
                    yield _sse({"step":"download_done","message":"下載完成","progress":60})
                else:
                    yield _sse({"step":"download_done","message":"使用快取音訊","progress":60})
                yield _sse({"step":"analyze","message":"正在分析音訊…","progress":75})
                features = analyze_audio(path)
                norm     = normalize_features(features)

            yield _sse({"step":"compare","message":"對比品味偏好…","progress":88})
            conn = _get_conn()
            lib_rows = conn.execute(
                "SELECT id,title,thumbnail,features,normalized FROM songs WHERE profile_id=?",
                (profile_id,)).fetchall()
            conn.close()
            if len(lib_rows) < 3:
                yield _sse({"step":"error","message":"需要至少 3 首歌曲"}); return
            lib = []
            for r in lib_rows:
                lf = json.loads(r["features"])
                ln = json.loads(r["normalized"]) if r["normalized"] else \
                     lf.get("normalized", normalize_features(lf))
                lib.append({"id":r["id"],"title":r["title"],
                            "thumbnail":r["thumbnail"],"features":lf,"norm":ln})
            profile = {d:float(np.mean([s["norm"].get(d,50) for s in lib])) for d in DIMS}
            deviations = []
            for dim in DIMS:
                sv, pv = norm.get(dim,50), profile[dim]
                deviations.append({"key":dim,"label":_DIM_NAMES_ZH[dim],
                    "song_value":round(sv,1),"profile_value":round(pv,1),
                    "deviation":round(abs(sv-pv),1),
                    "direction":"偏高" if sv>pv else "偏低"})
            deviations.sort(key=lambda x: x["deviation"], reverse=True)
            match_score = round(max(0.0,min(100.0,
                100.0 - float(np.mean([d["deviation"] for d in deviations])))), 1)
            sims = sorted([
                {"id":s["id"],"title":s["title"],"thumbnail":s["thumbnail"],
                 "similarity":round(song_similarity(features, s["features"])*100, 1)}
                for s in lib], key=lambda x: x["similarity"], reverse=True)
            yield _sse({"step":"done","progress":100,
                        "title":info["title"],"thumbnail":info["thumbnail"],
                        "youtube_id":vid,"match_score":match_score,
                        "normalized":norm,"profile":profile,
                        "deviations":deviations,"similar_songs":sims[:3]})
        except Exception as e:
            traceback.print_exc()
            yield _sse({"step":"error","message":f"發生錯誤：{e}"})
    return _sse_response(generate())

if __name__ == "__main__":
    print(f"DB: {DB_PATH}")
    print("Muse server → http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
