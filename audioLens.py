import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from difflib import SequenceMatcher

import requests
import numpy as np
import soundfile as sf
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
from scipy import signal
import warnings

warnings.filterwarnings("ignore")

# =========================================================
# CREDENTIALS
# =========================================================
SPOTIFY_CLIENT_ID = "c9a1fe2098204c7ab54cf9556c2bd470"
SPOTIFY_CLIENT_SECRET = "ef02ec0f4cbc4ca79b00c0bc4bcc6f34"
GETSONGBPM_API_KEY = "3bac91f58179afac580ffde9f7150fe7".strip()

KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES = KEY_NAMES[:]

# =========================================================
# COLORS
# =========================================================
BG = "#0f1117"
PANEL = "#181b26"
CARD = "#1e2130"
ACCENT = "#7c5cfc"
ACCENT2 = "#26c6a0"
ACCENT_G = "#1db954"
ERR = "#e05555"
TXT = "#dde1f0"
TXT2 = "#8a91b4"
TXT3 = "#50576e"
BTN_BG = "#252840"

# =========================================================
# HELPERS
# =========================================================
def _sim(a: str, b: str) -> float:
    def clean(t):
        t = t.lower()
        t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)
        t = re.sub(r"[^a-z0-9\u05d0-\u05ea\s]", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    a, b = clean(a), clean(b)
    if not a or not b:
        return 0.0

    ratio = SequenceMatcher(None, a, b).ratio()
    a_words, b_words = set(a.split()), set(b.split())
    overlap = len(a_words & b_words) / len(a_words | b_words) if a_words and b_words else 0.0
    contains_bonus = 0.1 if a in b or b in a else 0.0
    return min(ratio * 0.65 + overlap * 0.35 + contains_bonus, 1.0)


def _normalize_key_mode(raw_key: str):
    if not raw_key:
        return None, None

    s = raw_key.strip()
    s = s.replace("♯", "#").replace("♭", "b")
    s = s.replace(" major", " Major").replace(" minor", " Minor")

    m = re.search(r"([A-G][#b]?)[\s\-_\/]*(Major|Minor)", s, re.IGNORECASE)
    if m:
        key = m.group(1).upper()
        mode = "maj" if m.group(2).lower() == "major" else "min"
        repl = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}
        key = repl.get(key, key)
        return key, mode

    m2 = re.search(r"^([A-G][#b]?)(m)?$", s, re.IGNORECASE)
    if m2:
        key = m2.group(1).upper()
        mode = "min" if m2.group(2) else "maj"
        repl = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}
        key = repl.get(key, key)
        return key, mode

    return None, None


# =========================================================
# SPOTIFY  (search only)
# =========================================================
_spotify_token = None
_spotify_token_lock = threading.Lock()


def spotify_ready():
    return (
        SPOTIFY_CLIENT_ID
        and SPOTIFY_CLIENT_SECRET
        and SPOTIFY_CLIENT_ID != "YOUR_CLIENT_ID_HERE"
        and SPOTIFY_CLIENT_SECRET != "YOUR_CLIENT_SECRET_HERE"
    )


def _refresh_spotify_token():
    global _spotify_token
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=12,
        verify=False,
    )
    resp.raise_for_status()
    _spotify_token = resp.json().get("access_token")


def _spotify_get(url):
    global _spotify_token

    for _ in range(2):
        with _spotify_token_lock:
            if not _spotify_token:
                _refresh_spotify_token()
            tok = _spotify_token

        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {tok}"},
            timeout=12,
            verify=False,
        )

        if resp.status_code == 401:
            with _spotify_token_lock:
                _spotify_token = None
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError("Spotify auth failed")


def get_audio_features(spotify_id: str):
    try:
        data = _spotify_get(f"https://api.spotify.com/v1/audio-features/{spotify_id}")
    except Exception as e:
        if "403" in str(e) or "404" in str(e):
            return None, None, None, None
        return None, None, None, f"Spotify error: {e}"

    tempo = data.get("tempo")
    key_int = data.get("key", -1)
    mode_int = data.get("mode", -1)

    if tempo is None or key_int == -1 or mode_int == -1:
        return None, None, None, None

    bpm = round(float(tempo), 1)
    key = KEY_NAMES[key_int % 12]
    mode = "maj" if mode_int == 1 else "min"
    return bpm, key, mode, None


def get_bpm_key_from_preview(preview_url: str):
    try:
        resp = requests.get(preview_url, timeout=20, verify=False)
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name
        try:
            res = analyze_file(tmp_path)
        finally:
            os.unlink(tmp_path)
        if res["bpm"] and res["key"]:
            return res["bpm"], res["key"], res["mode"], None
        return None, None, None, None
    except Exception as e:
        return None, None, None, f"Preview analysis failed: {e}"


def spotify_search(query: str):
    if not spotify_ready():
        return [], "Spotify credentials missing — add them at the top of the script."

    q = query.strip()
    if not q:
        return [], "Please enter a song or artist name."

    try:
        data = _spotify_get(
            f"https://api.spotify.com/v1/search?q={requests.utils.quote(q)}&type=track&limit=10&market=IL"
        )
    except Exception as e:
        return [], f"Search error: {e}"

    rows = []
    seen = set()

    for t in data.get("tracks", {}).get("items", []):
        tid = t.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)

        name = t.get("name", "")
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        album = t.get("album", {}).get("name", "")
        score = max(_sim(q, name), _sim(q, artists) * 0.9)

        rows.append(
            {
                "spotify_id": tid,
                "name": name,
                "artist": artists,
                "album": album,
                "score": score,
                "preview_url": t.get("preview_url"),
            }
        )

    if not rows:
        return [], f'No results for "{query}".'

    rows.sort(key=lambda x: x["score"], reverse=True)

    out = []
    seen_pairs = set()
    for r in rows:
        pair = (r["name"].lower(), r["artist"].lower())
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            out.append(r)

    return out[:8], None


# =========================================================
# GETSONGBPM
# =========================================================
def getsongbpm_ready():
    return (
        GETSONGBPM_API_KEY
        and GETSONGBPM_API_KEY != "YOUR_GETSONGBPM_API_KEY_HERE"
    )


def _getsongbpm_search(query: str):
    """
    Try GetSongBPM in 2 ways:
    1) X-API-KEY header
    2) api_key query param
    """
    base_url = "https://api.getsong.co/search/"
    common_params = {
        "type": "song",
        "lookup": query,
        "limit": 10,
    }

    try:
        params = dict(common_params)
        params["api_key"] = GETSONGBPM_API_KEY.strip()

        resp = requests.get(
            base_url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=15,
            verify=False,
        )

        if resp.status_code == 401:
            return None, "GetSongBPM API key is invalid or not activated yet."
        if resp.status_code == 403:
            return None, "GetSongBPM blocked the request."
        if resp.status_code == 429:
            return None, "GetSongBPM rate limit reached."
        if not resp.ok:
            return None, f"GetSongBPM error: HTTP {resp.status_code}"

        return resp.json(), None

    except Exception as e:
        return None, f"GetSongBPM request failed: {e}"


def get_bpm_key_from_getsongbpm(song: str, artist: str):
    print("Searching GetSongBPM for:", song, "|", artist)

    if not getsongbpm_ready():
        err = "GetSongBPM API key is missing."
        print("GetSongBPM error:", err)
        return None, None, None, err

    items, err = [], None
    for query in [song, f"{song} {artist}"]:
        data, err = _getsongbpm_search(query)
        if err:
            break
        raw = data.get("search", []) if isinstance(data, dict) else []
        items = raw if isinstance(raw, list) else []
        if items:
            break

    if err:
        return None, None, None, err

    if not items:
        return None, None, None, None

    best = None
    best_score = -1.0

    for item in items:
        title = item.get("title", "") or ""
        artist_obj = item.get("artist", {}) or {}
        artist_name = artist_obj.get("name", "") if isinstance(artist_obj, dict) else ""
        score = _sim(song, title) * 0.7 + _sim(artist, artist_name) * 0.3

        if score > best_score:
            best_score = score
            best = item

    if not best:
        return None, None, None, None

    print("GetSongBPM best match:", best)

    tempo = best.get("tempo")
    raw_key = str(best.get("key_of", "")).strip()

    bpm = None
    if tempo is not None and str(tempo).strip():
        try:
            bpm = round(float(tempo), 1)
        except Exception:
            bpm = None

    key, mode = _normalize_key_mode(raw_key)

    print("Parsed BPM/Key/Mode:", bpm, key, mode)

    if bpm is None or key is None:
        return None, None, None, None

    return bpm, key, mode, None


# =========================================================
# BEATPORT
# =========================================================
def get_bpm_key_from_beatport(song: str, artist: str):
    query = f"{song} {artist}".strip()
    try:
        resp = requests.get(
            "https://api.beatport.com/v4/catalog/search/",
            params={"q": query, "type": "tracks", "per_page": 10},
            headers={"Accept": "application/json"},
            timeout=15,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return None, None, None, f"Beatport error: {e}"

    tracks = data.get("tracks", [])
    if not tracks:
        return None, None, None, None

    best, best_score = None, -1.0
    for t in tracks:
        title = t.get("name", "") or ""
        artist_name = ", ".join(a.get("name", "") for a in t.get("artists", []))
        score = _sim(song, title) * 0.7 + _sim(artist, artist_name) * 0.3
        if score > best_score:
            best_score, best = score, t

    if not best:
        return None, None, None, None

    bpm = best.get("bpm")
    key_obj = best.get("key") or {}
    key_name = key_obj.get("name", "")

    if not bpm or not key_name:
        return None, None, None, None

    key, mode = _normalize_key_mode(key_name)
    if key is None:
        return None, None, None, None

    return round(float(bpm), 1), key, mode, None


# =========================================================
# DSP — BPM
# =========================================================
def _onset_env(data, sr, hop=256, fsize=2048):
    if len(data) < fsize:
        data = np.pad(data, (0, fsize - len(data)))
    nf = max(1, 1 + (len(data) - fsize) // hop)
    env = np.zeros(nf)
    hann = np.hanning(fsize)
    prev = None
    for i in range(nf):
        f = data[i * hop:i * hop + fsize]
        if len(f) < fsize:
            f = np.pad(f, (0, fsize - len(f)))
        mag = np.abs(np.fft.rfft(f * hann))
        if prev is not None:
            env[i] = np.sum(np.maximum(mag - prev, 0))
        prev = mag
    return env


def _band_env(data, sr, lo, hi, hop=256, fsize=1024):
    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=sr, output="sos")
    flt = signal.sosfilt(sos, data)
    if len(flt) < fsize:
        flt = np.pad(flt, (0, fsize - len(flt)))
    nf = max(1, 1 + (len(flt) - fsize) // hop)
    env = np.zeros(nf)
    for i in range(nf):
        f = flt[i * hop:i * hop + fsize]
        if len(f) < fsize:
            f = np.pad(f, (0, fsize - len(f)))
        env[i] = np.sum(f**2)
    env = np.log1p(env)
    diff = np.diff(env, prepend=env[0])
    return np.maximum(diff, 0)


def _ac_bpm(env, env_sr, lo=60, hi=200):
    env = env - env.mean()
    if np.abs(env).max() < 1e-8:
        return {}
    ac = signal.correlate(env, env, mode="full")
    ac = ac[len(ac) // 2:]
    ac /= ac[0] + 1e-8
    lmin = int(env_sr * 60 / hi)
    lmax = min(int(env_sr * 60 / lo), len(ac) - 1)
    if lmin < 1 or lmin >= lmax:
        return {}
    sc = {}
    for lag in range(lmin, lmax + 1):
        bpm = 60 * env_sr / lag
        s = ac[lag]
        for m, w in [(2, 0.5), (3, 0.25), (4, 0.125)]:
            idx = lag * m
            if idx < len(ac):
                s += w * ac[idx]
        sc[round(float(bpm), 2)] = float(s)
    return sc


def _merge(scs, lo=60, hi=200, res=0.5):
    grid = np.arange(lo, hi + res, res)
    total = np.zeros(len(grid))
    for sc, w in scs:
        for bpm, s in sc.items():
            idx = int(round((bpm - lo) / res))
            if 0 <= idx < len(total):
                total[idx] += w * max(s, 0)
    return grid, total


def _resolve(grid, total, lo=60, hi=200):
    best = float(grid[np.argmax(total)])
    cands = {best: total[np.argmax(total)]}
    for f in [0.5, 2.0]:
        alt = best * f
        if lo <= alt <= hi:
            ai = int(round((alt - lo) / 0.5))
            if 0 <= ai < len(total):
                cands[alt] = total[ai]
    return round(float(max(cands, key=lambda b: cands[b] * (1 - 0.1 * abs(b - 120) / 120))), 1)


# =========================================================
# DSP — KEY
# =========================================================
def _norm(a):
    return (a - a.mean()) / (a.std() + 1e-8)


def _build_profiles():
    KM = _norm(np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]))
    Km = _norm(np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]))
    TM = _norm(np.array([5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0]))
    Tm = _norm(np.array([5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0]))
    MAJ = KM * 0.6 + TM * 0.4
    MIN = Km * 0.6 + Tm * 0.4
    return np.apply_along_axis(
        _norm,
        1,
        np.vstack([
            np.stack([np.roll(MAJ, r) for r in range(12)]),
            np.stack([np.roll(MIN, r) for r in range(12)]),
        ]),
    )


_PROFILES = _build_profiles()


def _chroma(data, sr, hop=512, nfft=8192):
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    hw = [1.0, 0.5, 0.33, 0.25, 0.2]
    pc_w = np.zeros(len(freqs))
    pc_map = np.full(len(freqs), -1, dtype=int)
    for bi, f in enumerate(freqs):
        if f < 20:
            continue
        bp, bw = -1, 0.0
        for h, hv in enumerate(hw, 1):
            fd = f / h
            if not (27.5 <= fd <= 4200):
                continue
            midi = 12 * np.log2(fd / 440) + 69
            dev = abs(midi - round(midi))
            if dev < 0.25:
                w = hv * (1 - dev / 0.25 * 0.3)
                if w > bw:
                    bw, bp = w, int(round(midi)) % 12
        if bp >= 0:
            pc_map[bi], pc_w[bi] = bp, bw
    valid = pc_map >= 0
    hann = np.hanning(nfft)
    nf = max(1, 1 + (max(0, len(data) - nfft)) // hop)
    pad = np.zeros(nfft + (nf - 1) * hop)
    pad[:len(data)] = data[:len(pad)]
    idx = np.arange(nf)[:, None] * hop + np.arange(nfft)
    mag = np.abs(np.fft.rfft(pad[idx] * hann, axis=1))
    mv = mag[:, valid] * pc_w[valid]
    ch = np.zeros((12, nf))
    np.add.at(ch, pc_map[valid], mv.T)
    ch = np.log1p(ch)
    en = ch.sum(0)
    ch[:, en < np.percentile(en, 20)] *= 0.1
    return ch


def detect_key(data, sr):
    try:
        tgt = 22050
        if sr != tgt:
            g = np.gcd(int(sr), tgt)
            data = signal.resample_poly(data, tgt // g, sr // g)
            sr = tgt
        cf = _chroma(data, sr).mean(1)
        sl = max(sr * 4, len(data) // 8)
        votes = np.zeros(24)
        for i in range(max(1, len(data) // sl)):
            seg = data[i * sl:(i + 1) * sl]
            if len(seg) < sr:
                continue
            mc = _chroma(seg, sr).mean(1)
            if mc.std() < 1e-8:
                continue
            votes += _PROFILES @ _norm(mc) / 12
        comb = _PROFILES @ _norm(cf) / 12 * 1.5 + votes
        bi = int(np.argmax(comb))
        mode = "maj" if bi < 12 else "min"
        key = bi % 12
        sc = np.sort(comb)[::-1]
        conf = float(np.clip((sc[0] - sc[1]) / 0.5, 0, 1))
        disp = cf / cf.max() if cf.max() > 0 else cf
        return NOTE_NAMES[key], mode, conf, disp
    except Exception:
        return None, None, 0.0, np.zeros(12)


def analyze_file(path, lo=60, hi=200):
    res = {"bpm": None, "key": None, "mode": None, "confidence": 0.0, "chroma": np.zeros(12)}
    try:
        data, sr = sf.read(path)
        if data.ndim > 1:
            data = data.mean(1)
        data = data.astype(np.float64)
        mx = np.abs(data).max()
        if mx > 0:
            data /= mx
        k, m, c, ch = detect_key(data, sr)
        res.update(key=k, mode=m, confidence=c, chroma=ch)
        tgt = 22050
        if sr != tgt:
            g = np.gcd(int(sr), tgt)
            data = signal.resample_poly(data, tgt // g, sr // g)
            sr = tgt
        hop = 256
        env_sr = sr / hop
        b_sc = _ac_bpm(_band_env(data, sr, 30, 200, hop), env_sr, lo, hi)
        m_sc = _ac_bpm(_band_env(data, sr, 200, 2000, hop), env_sr, lo, hi)
        f_env = _onset_env(data, sr, hop)
        if f_env.max() > 0:
            f_env /= f_env.max()
        f_sc = _ac_bpm(f_env, env_sr, lo, hi)
        grid, tot = _merge([(b_sc, 1.5), (m_sc, 1.0), (f_sc, 1.2)], lo, hi)
        if tot.max() >= 1e-8:
            res["bpm"] = _resolve(grid, tot, lo, hi)
    except Exception:
        pass
    return res


# =========================================================
# CIRCLE OF FIFTHS CANVAS
# =========================================================
class CircleOfFifthsCanvas(tk.Canvas):
    # Clockwise from top (C = 12 o'clock). Traditional circle of fifths notation.
    _MAJOR_LABELS = ["C", "G", "D", "A", "E", "B", "Gb", "Db", "Ab", "Eb", "Bb", "F"]
    _MINOR_LABELS = ["Am", "Em", "Bm", "F#m", "C#m", "G#m", "Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm"]
    _MAJ_POS = {"C":0,"G":1,"D":2,"A":3,"E":4,"B":5,"F#":6,"C#":7,"G#":8,"D#":9,"A#":10,"F":11}
    _MIN_POS = {"A":0,"E":1,"B":2,"F#":3,"C#":4,"G#":5,"D#":6,"A#":7,"F":8,"C":9,"G":10,"D":11}

    _GLOW   = "#3b2899"
    _BORDER = "#b09eff"

    def __init__(self, master, **kw):
        super().__init__(master, bg=PANEL, highlightthickness=0, **kw)
        self._key = None
        self._mode = None
        self.bind("<Configure>", lambda _: self._draw())

    def set(self, key, mode):
        self._key = key
        self._mode = mode
        self._draw()

    def clear(self):
        self._key = None
        self._mode = None
        self._draw()

    def _arc_pts(self, cx, cy, r, a1, a2, steps=36):
        pts = []
        for i in range(steps + 1):
            a = math.radians(a1 + (a2 - a1) * i / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return pts

    def _ring_seg(self, cx, cy, r_in, r_out, a1, a2, fill, outline=None, width=2):
        pts = self._arc_pts(cx, cy, r_out, a1, a2) + self._arc_pts(cx, cy, r_in, a2, a1)
        self.create_polygon(pts, fill=fill, outline=outline if outline is not None else PANEL, width=width)

    def _draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 60 or H < 60:
            return

        cx, cy = W / 2, H / 2
        R = min(W, H) / 2 - 10

        r_out  = R
        r_mid  = R * 0.635
        r_in   = R * 0.375
        r_core = R * 0.225

        fmaj      = max(8,  min(15, int(R * 0.140)))
        fmin      = max(7,  min(12, int(R * 0.103)))
        fcore     = max(12, min(24, int(R * 0.200)))
        fcore_sub = max(7,  min(14, int(R * 0.112)))

        maj_hi = self._MAJ_POS.get(self._key) if self._mode == "maj" and self._key else None
        min_hi = self._MIN_POS.get(self._key) if self._mode == "min" and self._key else None

        for i in range(12):
            a_mid = -90 + i * 30
            a1, a2 = a_mid - 15, a_mid + 15
            is_maj = (i == maj_hi)
            is_min = (i == min_hi)

            # Glow behind highlighted segment (drawn first, larger)
            if is_maj:
                self._ring_seg(cx, cy, r_mid - 4, r_out + 4, a1 - 2, a2 + 2,
                               self._GLOW, outline=PANEL, width=1)
            if is_min:
                self._ring_seg(cx, cy, r_in - 4, r_mid + 4, a1 - 2, a2 + 2,
                               self._GLOW, outline=PANEL, width=1)

            # Outer (major) ring
            self._ring_seg(cx, cy, r_mid, r_out, a1, a2,
                           ACCENT if is_maj else BTN_BG,
                           outline=self._BORDER if is_maj else PANEL,
                           width=3 if is_maj else 2)
            # Inner (minor) ring
            self._ring_seg(cx, cy, r_in, r_mid, a1, a2,
                           ACCENT if is_min else CARD,
                           outline=self._BORDER if is_min else PANEL,
                           width=3 if is_min else 2)

            rad  = math.radians(a_mid)
            rmaj = (r_mid + r_out) / 2
            rmin = (r_in  + r_mid) / 2

            self.create_text(
                cx + rmaj * math.cos(rad), cy + rmaj * math.sin(rad),
                text=self._MAJOR_LABELS[i],
                fill="white" if is_maj else TXT,
                font=("Arial", fmaj + (2 if is_maj else 0), "bold" if is_maj else "normal"),
            )
            self.create_text(
                cx + rmin * math.cos(rad), cy + rmin * math.sin(rad),
                text=self._MINOR_LABELS[i],
                fill="white" if is_min else TXT2,
                font=("Arial", fmin + (1 if is_min else 0), "bold" if is_min else "normal"),
            )

        # Center circle
        has_key = bool(self._key and self._mode)
        self.create_oval(
            cx - r_core, cy - r_core, cx + r_core, cy + r_core,
            fill=ACCENT if has_key else CARD,
            outline=self._BORDER if has_key else TXT3,
            width=2,
        )
        if has_key:
            if self._mode == "maj" and maj_hi is not None:
                display = self._MAJOR_LABELS[maj_hi]
                mode_str = "Major"
            elif self._mode == "min" and min_hi is not None:
                display = self._MINOR_LABELS[min_hi].rstrip("m") or self._MINOR_LABELS[min_hi]
                mode_str = "Minor"
            else:
                display, mode_str = self._key, ""
            self.create_text(
                cx, cy - r_core * 0.18,
                text=display,
                fill="white",
                font=("Arial", fcore, "bold"),
            )
            self.create_text(
                cx, cy + r_core * 0.52,
                text=mode_str,
                fill="#ccbbff",
                font=("Arial", fcore_sub, "normal"),
            )


# =========================================================
# SPLASH SCREEN
# =========================================================
class SplashScreen(tk.Toplevel):
    _W, _H = 420, 300

    def __init__(self, parent):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        W, H = self._W, self._H
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
        self.configure(bg=BG)

        cv = tk.Canvas(self, width=W, height=H, bg=BG, highlightthickness=0)
        cv.pack(fill="both", expand=True)
        self._cv = cv

        self._draw_card(cv, 14, 14, W - 14, H - 14, r=20)

        self._icon = cv.create_text(
            W // 2, 88, text="⊙",
            font=("Helvetica Neue", 58, "bold"), fill=ACCENT,
        )

        cx = W // 2
        cv.create_text(cx - 3, 152, text="Audio",
                       font=("Helvetica Neue", 30, "bold"), fill=TXT, anchor="e")
        cv.create_text(cx + 3, 152, text="Lens",
                       font=("Helvetica Neue", 30, "bold"), fill=ACCENT, anchor="w")
        cv.create_text(cx, 178, text="v 1.0",
                       font=("Helvetica Neue", 10), fill=TXT3)

        cv.create_line(cx - 72, 200, cx + 72, 200, fill=TXT3, width=1)

        cv.create_text(cx, 218, text="by Yael Amitay",
                       font=("Helvetica Neue", 11), fill=TXT2)

        self._dots_item = cv.create_text(
            cx, 260, text="Loading   ",
            font=("Helvetica Neue", 13, "bold"), fill=TXT2,
        )

        self._pulse_n = 0
        self._dot_n = 0
        self._anim_id = None
        self._animate()
        self.update()

    def _draw_card(self, cv, x1, y1, x2, y2, r):
        kw_fill = dict(fill=PANEL, outline=PANEL)
        cv.create_arc(x1, y1, x1 + 2*r, y1 + 2*r, start=90, extent=90, style="pieslice", **kw_fill)
        cv.create_arc(x2 - 2*r, y1, x2, y1 + 2*r, start=0, extent=90, style="pieslice", **kw_fill)
        cv.create_arc(x2 - 2*r, y2 - 2*r, x2, y2, start=270, extent=90, style="pieslice", **kw_fill)
        cv.create_arc(x1, y2 - 2*r, x1 + 2*r, y2, start=180, extent=90, style="pieslice", **kw_fill)
        cv.create_rectangle(x1 + r, y1, x2 - r, y2, **kw_fill)
        cv.create_rectangle(x1, y1 + r, x2, y2 - r, **kw_fill)

        kw_ol = dict(outline=ACCENT, width=1)
        cv.create_arc(x1, y1, x1 + 2*r, y1 + 2*r, start=90, extent=90, style="arc", **kw_ol)
        cv.create_arc(x2 - 2*r, y1, x2, y1 + 2*r, start=0, extent=90, style="arc", **kw_ol)
        cv.create_arc(x2 - 2*r, y2 - 2*r, x2, y2, start=270, extent=90, style="arc", **kw_ol)
        cv.create_arc(x1, y2 - 2*r, x1 + 2*r, y2, start=180, extent=90, style="arc", **kw_ol)
        cv.create_line(x1 + r, y1, x2 - r, y1, fill=ACCENT, width=1)
        cv.create_line(x2, y1 + r, x2, y2 - r, fill=ACCENT, width=1)
        cv.create_line(x2 - r, y2, x1 + r, y2, fill=ACCENT, width=1)
        cv.create_line(x1, y2 - r, x1, y1 + r, fill=ACCENT, width=1)

    _PULSE = [ACCENT, "#9a7dfd", "#b9a8fe", "#c8bcfe", "#b9a8fe", "#9a7dfd"]

    def _animate(self):
        self._cv.itemconfigure(self._icon, fill=self._PULSE[self._pulse_n % len(self._PULSE)])
        self._pulse_n += 1
        dots = ["   ", ".  ", ".. ", "..."]
        self._cv.itemconfigure(self._dots_item, text="Loading" + dots[self._dot_n % 4])
        self._dot_n += 1
        self._anim_id = self.after(370, self._animate)

    def close(self):
        if self._anim_id:
            self.after_cancel(self._anim_id)
            self._anim_id = None
        self.destroy()


# =========================================================
# MAIN APP
# =========================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("AudioLens")
        self.configure(fg_color=BG)
        w, h = 920, 700
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.resizable(False, False)
        self._splash = SplashScreen(self)
        self._build()
        self.after(1500, self._launch)

    def _launch(self):
        self._splash.close()
        self.deiconify()
        self.lift()
        self.focus_force()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color=PANEL, height=46, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        logo = ctk.CTkFrame(top, fg_color="transparent")
        logo.pack(side="left", padx=14)
        ctk.CTkLabel(logo, text="⊙", text_color=ACCENT,
                     font=("Helvetica Neue", 27, "bold")).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(logo, text="Audio", text_color=TXT,
                     font=("Helvetica Neue", 22, "bold")).pack(side="left")
        ctk.CTkLabel(logo, text="Lens", text_color=ACCENT,
                     font=("Helvetica Neue", 22, "bold")).pack(side="left")
        ctk.CTkLabel(logo, text="v1.0", text_color=TXT3,
                     font=("Helvetica Neue", 10)).pack(side="left", padx=(6, 0), anchor="s", pady=(0, 4))
        ctk.CTkLabel(top, text="© Yael Amitay", text_color=TXT3, font=("Arial", 11)).pack(side="right", padx=18)

        body = ctk.CTkFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(12, 0))

        footer = ctk.CTkFrame(self, fg_color=PANEL, height=46, corner_radius=0)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        ctk.CTkButton(
            footer,
            text="↺  Reset",
            command=self._reset,
            width=104,
            height=30,
            corner_radius=15,
            fg_color="#3d4273",
            hover_color="#4e5490",
            text_color="white",
            font=("Arial", 11, "bold"),
        ).place(relx=0.5, rely=0.5, anchor="center")
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))

        ctk.CTkLabel(left, text="Search by Song / Artist", text_color=TXT, font=("Arial", 13, "bold")).pack(pady=(14, 2), padx=14, anchor="w")
        ctk.CTkLabel(left, text="Powered by Spotify  ·  results show name & artist", text_color=TXT3, font=("Arial", 10)).pack(padx=14, anchor="w")

        self.q_entry = ctk.CTkEntry(
            left,
            placeholder_text="e.g.  Blinding Lights  or  The Weeknd",
            height=36,
            corner_radius=18,
            fg_color=BTN_BG,
            border_color="#2e3250",
            text_color=TXT,
            font=("Arial", 12),
        )
        self.q_entry.pack(fill="x", padx=14, pady=(8, 6))
        self.q_entry.bind("<Return>", lambda _: self._search())

        self.search_btn = ctk.CTkButton(
            left,
            text="Search",
            command=self._search,
            height=34,
            corner_radius=17,
            fg_color=ACCENT,
            hover_color="#6347e0",
            text_color="white",
            font=("Arial", 12, "bold"),
        )
        self.search_btn.pack(fill="x", padx=14)

        self.status_lbl = ctk.CTkLabel(left, text="", text_color=TXT3, font=("Arial", 13), wraplength=340)
        self.status_lbl.pack(pady=(5, 2), padx=14)

        self.results_scroll = ctk.CTkScrollableFrame(left, fg_color=BG, corner_radius=10, height=330)
        self.results_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        right = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=14)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        ctk.CTkLabel(right, text="Analyze Audio File", text_color=TXT, font=("Arial", 13, "bold")).pack(pady=(14, 2), padx=14, anchor="w")
        ctk.CTkLabel(right, text="Upload a file to detect BPM & Key  ·  WAV · MP3 · FLAC", text_color=TXT3, font=("Arial", 10)).pack(padx=14, anchor="w")

        self.upload_btn = ctk.CTkButton(
            right,
            text="Choose File…",
            command=self._pick_file,
            height=34,
            corner_radius=17,
            fg_color=ACCENT2,
            hover_color="#1fa882",
            text_color="#04120e",
            font=("Arial", 12, "bold"),
        )
        self.upload_btn.pack(fill="x", padx=14, pady=(10, 5))

        self.file_lbl = ctk.CTkLabel(right, text="No file selected", text_color=TXT3, font=("Arial", 10))
        self.file_lbl.pack(padx=14, anchor="w")

        self.scan_status = ctk.CTkLabel(right, text="", text_color=TXT3, font=("Arial", 13), wraplength=320, justify="left")
        self.scan_status.pack(pady=(2, 0), padx=14, anchor="w")

        self.song_lbl = ctk.CTkLabel(right, text="", text_color=TXT2, font=("Arial", 11), wraplength=320)
        self.song_lbl.pack(pady=(4, 0), padx=14)

        bpm_row = ctk.CTkFrame(right, fg_color="transparent")
        bpm_row.pack(pady=(8, 2))
        self.bpm_lbl = ctk.CTkLabel(bpm_row, text="--.-", text_color=ACCENT, font=("Arial", 52, "bold"))
        self.bpm_lbl.pack(side="left")
        ctk.CTkLabel(bpm_row, text="BPM", text_color=TXT3, font=("Arial", 15)).pack(side="left", padx=(6, 0), anchor="s", pady=(0, 11))

        self.chroma = CircleOfFifthsCanvas(right)
        self.chroma.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    def _clear_results(self):
        self._stop_preview()
        for w in self.results_scroll.winfo_children():
            w.destroy()

    def _set_busy(self, busy, scope="all"):
        s = "disabled" if busy else "normal"
        if scope in ("all", "search"):
            self.search_btn.configure(state=s)
        if scope in ("all", "upload"):
            self.upload_btn.configure(state=s)

    # ------ animated loading dots ------
    def _anim_start(self, key, label, base, color):
        self._anim_cancel(key)
        if not hasattr(self, "_anim"):
            self._anim = {}
        self._anim[key] = {"lbl": label, "base": base, "color": color, "n": 0, "id": None}
        self._anim_tick(key)

    def _anim_cancel(self, key=None):
        if not hasattr(self, "_anim"):
            return
        keys = list(self._anim.keys()) if key is None else [key]
        for k in keys:
            slot = self._anim.get(k)
            if slot and slot.get("id"):
                self.after_cancel(slot["id"])
            self._anim.pop(k, None)

    def _anim_tick(self, key):
        if not hasattr(self, "_anim") or key not in self._anim:
            return
        slot = self._anim[key]
        frames = [".  ", ".. ", "..."]
        slot["lbl"].configure(
            text=slot["base"] + frames[slot["n"] % 3],
            text_color=slot["color"],
        )
        slot["n"] += 1
        slot["id"] = self.after(450, lambda k=key: self._anim_tick(k))

    def _reset(self):
        # Invalidate every pending background callback before touching the UI
        self._reset_gen = getattr(self, "_reset_gen", 0) + 1
        self._sel_id   = getattr(self, "_sel_id",   0) + 1
        self._anim_cancel()
        self._stop_preview()
        # Unblock buttons immediately so the user can act at once
        self._set_busy(False, "all")
        # Reset all visible labels in one pass
        self.q_entry.delete(0, "end")
        self.status_lbl.configure(text="",      text_color=TXT3)
        self.scan_status.configure(text="",     text_color=TXT3)
        self.bpm_lbl.configure(text="--.-",     text_color=ACCENT)
        self.song_lbl.configure(text="")
        self.file_lbl.configure(text="No file selected")
        # Flush all label changes to screen before destroying widgets
        self.update_idletasks()
        self._clear_results()
        self.chroma.clear()

    def _search(self):
        q = self.q_entry.get().strip()
        if not q:
            self.status_lbl.configure(text="Please enter a song or artist name.", text_color=ERR)
            return
        self._clear_results()
        self._set_busy(True, "search")
        self._set_busy(False, "upload")  # safety reset in case upload button got stuck
        self._anim_start("search", self.status_lbl, "Searching", TXT3)
        rg = getattr(self, "_reset_gen", 0)
        threading.Thread(target=self._do_search, args=(q, rg), daemon=True).start()

    def _do_search(self, q, reset_gen):
        try:
            results, err = spotify_search(q)
        except Exception as e:
            results, err = [], f"Search error: {e}"
        self.after(0, lambda: self._show_results(q, results, err, reset_gen))

    def _show_results(self, q, results, err, reset_gen=None):
        if reset_gen is not None and reset_gen != getattr(self, "_reset_gen", 0):
            return
        self._anim_cancel("search")
        self._set_busy(False, "search")
        self._clear_results()
        if err:
            self.status_lbl.configure(text=err, text_color=ERR)
            return
        self.status_lbl.configure(text=f'{len(results)} result(s) for "{q}"', text_color=TXT3)
        for rec in results:
            self._result_card(rec)

    def _result_card(self, rec):
        card = ctk.CTkFrame(self.results_scroll, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=3, padx=2)

        # Subtle play button on the far left of the card
        play_btn = ctk.CTkButton(
            card,
            text="▶",
            width=34,
            height=34,
            corner_radius=17,
            fg_color="transparent",
            hover_color=BTN_BG,
            text_color=TXT3,
            font=("Arial", 13),
        )
        play_btn.configure(command=lambda rx=rec, b=play_btn: self._play_preview(rx, b))
        play_btn.pack(side="left", padx=(8, 2), pady=10)

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(4, 10), pady=8)
        ctk.CTkLabel(info, text=rec["name"], text_color=TXT, font=("Arial", 12, "bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(info, text=rec["artist"], text_color=ACCENT2, font=("Arial", 10), anchor="w").pack(fill="x")
        ctk.CTkLabel(info, text=rec["album"], text_color=TXT3, font=("Arial", 9), anchor="w").pack(fill="x")

        r = ctk.CTkFrame(card, fg_color="transparent")
        r.pack(side="right", padx=10, pady=8)
        ctk.CTkButton(
            r,
            text="Select",
            width=68,
            height=28,
            corner_radius=14,
            fg_color=ACCENT_G,
            hover_color="#179140",
            text_color="#04120a",
            font=("Arial", 10, "bold"),
            command=lambda rx=rec: self._select(rx),
        ).pack(anchor="e")

    # ------ preview playback (iTunes 30s) ------
    def _fetch_itunes_preview(self, song, artist):
        try:
            q = requests.utils.quote(f"{song} {artist}")
            data = requests.get(
                f"https://itunes.apple.com/search?term={q}&entity=song&limit=10",
                timeout=12, verify=False,
            ).json()
            best_url, best_score = None, 0.0
            for r in data.get("results", []):
                url = r.get("previewUrl")
                if not url:
                    continue
                score = (_sim(song, r.get("trackName", "")) * 0.7
                         + _sim(artist, r.get("artistName", "")) * 0.3)
                if score > best_score:
                    best_score, best_url = score, url
            return best_url
        except Exception:
            return None

    def _play_preview(self, rec, btn):
        cur_id = getattr(self, "_preview_playing_id", None)
        if cur_id is not None and cur_id == rec.get("spotify_id"):
            self._stop_preview()
            return
        self._stop_preview()
        self._preview_playing_id = rec["spotify_id"]
        self._preview_playing_btn = btn
        self._preview_session = getattr(self, "_preview_session", 0) + 1
        session = self._preview_session
        self._anim_start("preview", btn, "", TXT3)  # animated dots while loading
        threading.Thread(
            target=self._do_play_preview, args=(rec, session), daemon=True
        ).start()

    def _stop_preview(self):
        self._anim_cancel("preview")
        self._preview_session = getattr(self, "_preview_session", 0) + 1
        proc = getattr(self, "_preview_proc", None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            self._preview_proc = None
        btn = getattr(self, "_preview_playing_btn", None)
        if btn is not None:
            try:
                btn.configure(text="▶", text_color=TXT3, fg_color="transparent",
                               hover_color=BTN_BG)
            except Exception:
                pass
            self._preview_playing_btn = None
        self._preview_playing_id = None

    def _do_play_preview(self, rec, session):
        tmp_path = None
        try:
            url = self._fetch_itunes_preview(rec["name"], rec["artist"])
            if not url or session != getattr(self, "_preview_session", -1):
                self.after(0, lambda: self._on_preview_not_found(session))
                return
            resp = requests.get(url, timeout=20, verify=False)
            resp.raise_for_status()
            if session != getattr(self, "_preview_session", -1):
                return
            suffix = ".m4a" if ".m4a" in url else ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(resp.content)
                tmp_path = f.name

            if sys.platform == "win32":
                uri = tmp_path.replace("\\", "/")
                ps = (
                    "Add-Type -AssemblyName PresentationCore;"
                    "$mp=[System.Windows.Media.MediaPlayer]::new();"
                    f"$mp.Open([uri]'file:///{uri}');"
                    "$mp.Play();"
                    "Start-Sleep -m 800;"
                    "while(-not $mp.NaturalDuration.HasTimeSpan){Start-Sleep -m 100};"
                    "$s=[int]$mp.NaturalDuration.TimeSpan.TotalSeconds+1;"
                    "Start-Sleep -s $s;"
                    "$mp.Stop();$mp.Close()"
                )
                proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-c", ps],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc = subprocess.Popen(
                    ["afplay", tmp_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

            if session != getattr(self, "_preview_session", -1):
                proc.terminate()
            else:
                self._preview_proc = proc
                self.after(0, lambda s=session: self._on_preview_started(s))
                proc.wait()
        except Exception:
            pass
        finally:
            if tmp_path:
                try:
                    import time; time.sleep(0.3)  # Windows needs a moment to release the file
                    os.unlink(tmp_path)
                except Exception:
                    pass
        if session == getattr(self, "_preview_session", -1):
            self.after(0, self._on_preview_done)

    def _on_preview_started(self, session):
        if session != getattr(self, "_preview_session", -1):
            return
        self._anim_cancel("preview")
        btn = getattr(self, "_preview_playing_btn", None)
        if btn:
            try:
                btn.configure(text="■", text_color=ERR, fg_color="transparent",
                               hover_color=BTN_BG)
            except Exception:
                pass

    def _on_preview_not_found(self, session):
        if session != getattr(self, "_preview_session", -1):
            return
        self._anim_cancel("preview")
        self._preview_proc = None
        btn = getattr(self, "_preview_playing_btn", None)
        if btn:
            try:
                btn.configure(text="▶", text_color=TXT3, fg_color="transparent",
                               hover_color=BTN_BG)
            except Exception:
                pass
            self._preview_playing_btn = None
        self._preview_playing_id = None

    def _on_preview_done(self):
        self._anim_cancel("preview")
        self._preview_proc = None
        btn = getattr(self, "_preview_playing_btn", None)
        if btn:
            try:
                btn.configure(text="▶", text_color=TXT3, fg_color="transparent",
                               hover_color=BTN_BG)
            except Exception:
                pass
            self._preview_playing_btn = None
        self._preview_playing_id = None

    def _select(self, rec):
        self._sel_id = getattr(self, "_sel_id", 0) + 1
        sel_id = self._sel_id
        self.song_lbl.configure(text=f"{rec['name']} — {rec['artist']}")
        self.bpm_lbl.configure(text="--.-", text_color=TXT3)
        self._anim_start("scan", self.scan_status, "Looking for BPM & key", TXT3)
        self.chroma.clear()
        rg = getattr(self, "_reset_gen", 0)
        threading.Thread(target=self._fetch_song_data, args=(rec, sel_id, rg), daemon=True).start()

    def _fetch_song_data(self, rec, sel_id, reset_gen):
        def safe_anim(base, color):
            if getattr(self, "_reset_gen", -1) == reset_gen:
                self._anim_start("scan", self.scan_status, base, color)
        try:
            bpm, key, mode, err = get_audio_features(rec["spotify_id"])

            if bpm is None and err is None:
                self.after(0, lambda: safe_anim("Searching", ACCENT2))
                bpm, key, mode, err = get_bpm_key_from_getsongbpm(rec["name"], rec["artist"])

            if bpm is None and err is None:
                bpm, key, mode, err = get_bpm_key_from_beatport(rec["name"], rec["artist"])

            if bpm is None and err is None and rec.get("preview_url"):
                self.after(0, lambda: safe_anim("Analyzing preview", ACCENT2))
                bpm, key, mode, err = get_bpm_key_from_preview(rec["preview_url"])

            if bpm is None and err is None:
                self.after(0, lambda: safe_anim("Analyzing preview", ACCENT2))
                itunes_url = self._fetch_itunes_preview(rec["name"], rec["artist"])
                if itunes_url:
                    bpm, key, mode, err = get_bpm_key_from_preview(itunes_url)

            self.after(0, lambda: self._handle_song_data(rec, bpm, key, mode, err, sel_id))
        except Exception as e:
            self.after(0, lambda: self._handle_song_data(rec, None, None, None, f"Background error: {e}", sel_id))

    def _handle_song_data(self, rec, bpm, key, mode, err, sel_id=None):
        if sel_id is not None and sel_id != getattr(self, "_sel_id", None):
            return
        self._anim_cancel("scan")
        self.song_lbl.configure(text=f"{rec['name']} — {rec['artist']}")

        if err:
            self.bpm_lbl.configure(text="--.-", text_color=ACCENT)
            self.scan_status.configure(text="No data found. Try uploading an audio file.", text_color=ERR)
            return

        if bpm is None or key is None:
            self.bpm_lbl.configure(text="--.-", text_color=ACCENT)
            self.scan_status.configure(text="No data found. Try uploading an audio file.", text_color=ERR)
            return

        self.bpm_lbl.configure(text=f"{bpm:.1f}", text_color=ACCENT)
        self.scan_status.configure(text="")
        self.chroma.set(key, mode)

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Choose audio file",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg *.aif *.aiff"), ("All", "*.*")],
        )
        self.focus_force()
        if not path:
            return

        self.file_lbl.configure(text=os.path.basename(path))
        self.bpm_lbl.configure(text="--.-", text_color=TXT3)
        self._anim_start("scan", self.scan_status, "Analyzing", ACCENT2)
        self.song_lbl.configure(text=os.path.basename(path))
        self.chroma.clear()
        self._set_busy(True, "upload")
        rg = getattr(self, "_reset_gen", 0)
        threading.Thread(target=self._do_analyze, args=(path, rg), daemon=True).start()

    def _do_analyze(self, path, reset_gen):
        try:
            res = analyze_file(path)
        except Exception:
            res = {"bpm": None, "key": None, "mode": None, "confidence": 0.0, "chroma": np.zeros(12)}
        self.after(0, lambda: self._show_analysis(path, res, reset_gen))

    def _show_analysis(self, path, res, reset_gen=None):
        if reset_gen is not None and reset_gen != getattr(self, "_reset_gen", 0):
            return
        self._anim_cancel("scan")
        self._set_busy(False, "upload")
        if res["bpm"] is None:
            self.bpm_lbl.configure(text="ERR", text_color=ERR)
        else:
            self.bpm_lbl.configure(text=f"{res['bpm']:.1f}", text_color=ACCENT)

        if res["key"] is None:
            self.scan_status.configure(text="Could not detect key.", text_color=ERR)
            self.chroma.clear()
        else:
            conf = int(res["confidence"] * 100)
            self.scan_status.configure(text=f"Key confidence: {conf}%", text_color=ACCENT2)
            self.chroma.set(res["key"], res["mode"])

        self.song_lbl.configure(text=os.path.basename(path))


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()