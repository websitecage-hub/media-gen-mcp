#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vibes.ai - full-featured CLI client for everything the web app can do.

SINGLE-FILE build with FULLY AUTOMATIC session self-healing: sessions are
carried inside the file (priority: env VIBES_SESSION -> ./session.json ->
embedded copy, which self-updates on every save). If a session dies or is
missing, the script regenerates it by itself — no manual /login needed.
/mode /projects /use /img /v /ref /audio /lipsync /voices …
"""
import io, json, os, sys, time, uuid, random, threading, re, base64
from datetime import datetime
from urllib.parse import parse_qs
try:
    from curl_cffi import requests
    from nacl.public import SealedBox, PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as _e:
    print(f"[!] Missing dependency: {_e}")
    print(f"    Install with: pip install curl_cffi PyNaCl cryptography")
    try:
        import subprocess
        print("[*] Attempting auto-install...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "curl_cffi", "PyNaCl", "cryptography", "--quiet"])
        print("[+] Auto-install done, please rerun")
    except Exception as _ae:
        print(f"[!] Auto-install failed: {_ae}")
    sys.exit(1)

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── reliability: logging, safe I/O, validation ──────────────────────────
import logging, traceback, pathlib
LOG_FILE = os.path.join(ROOT_DIR, "vibes.log")
try:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
except Exception:
    logging.basicConfig(level=logging.INFO)
VLOOG = logging.getLogger("vibes")
def _v_safe_write(path, data_bytes):
    try:
        tmp = path + ".tmp"
        d = os.path.dirname(path)
        if d: os.makedirs(d, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data_bytes); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        VLOOG.warning(f"safe_write {path}: {e}")
        try:
            with open(path, "wb") as f: f.write(data_bytes)
            return True
        except Exception as e2:
            VLOOG.error(f"fallback write {path}: {e2}")
            return False
def _v_validate_file(path, max_mb=200):
    if not path or not isinstance(path, str): raise ValueError("file path required")
    if not os.path.exists(path): raise FileNotFoundError(f"file not found: {path}")
    if not os.path.isfile(path): raise ValueError(f"not a file: {path}")
    sz = os.path.getsize(path)
    if sz == 0: raise ValueError("file is empty")
    if sz > max_mb*1024*1024: raise ValueError(f"file {sz} exceeds {max_mb} MB")
    return path
def _v_retry(fn, retries=3, base=0.8):
    last=None
    for i in range(retries+1):
        try: return fn()
        except Exception as e:
            last=e
            if i>=retries: break
            d=min(base*(2**i)+random.random(), 8)
            VLOOG.warning(f"retry {i+1}/{retries} {type(e).__name__}: {e} sleep {d:.1f}s")
            time.sleep(d)
    raise last

# ── embedded session (materializes once into ./session.json) ────────────
# auto-updated by save_session(): the newest WORKING session travels
# inside this file, so copying it anywhere = instant login.
_SESSION_PAYLOAD = r"""#_SP_BEGIN
{
  "impersonate": "chrome_android",
  "saved_at": 1787764845.5226,
  "cookies": [
    {
      "name": "meta_session",
      "value": "7f3799aa-cdc0-47d8-8b37-d19d56883ce1.8oyWwsnCR2cw-ZRvdYM4Lo51tDYIEVpn2iZgn_s8IuA",
      "domain": ".vibes.ai",
      "path": "/"
    },
    {
      "name": "cookie_ack",
      "value": "true",
      "domain": ".vibes.ai",
      "path": "/"
    },
    {
      "name": "meta_session",
      "value": "7f3799aa-cdc0-47d8-8b37-d19d56883ce1.8oyWwsnCR2cw-ZRvdYM4Lo51tDYIEVpn2iZgn_s8IuA",
      "domain": "vibes.ai",
      "path": "/"
    },
    {
      "name": "datr",
      "value": "8LmOaq99KB3vzympUPqU0cQy",
      "domain": ".auth.meta.com",
      "path": "/"
    },
    {
      "name": "ps_l",
      "value": "1",
      "domain": ".auth.meta.com",
      "path": "/"
    },
    {
      "name": "ps_n",
      "value": "1",
      "domain": ".auth.meta.com",
      "path": "/"
    },
    {
      "name": "fs",
      "value": "Ftr0pqueyNkDFkwYDjF0ZGkzSWgwblhjNk9nFqzo9agNGAAA",
      "domain": ".auth.meta.com",
      "path": "/"
    },
    {
      "name": "locale",
      "value": "hi_IN",
      "domain": ".auth.meta.com",
      "path": "/"
    },
    {
      "name": "dbln",
      "value": "{\"1041379025730050\":\"6E0VyY24\"}",
      "domain": ".auth.meta.com",
      "path": "/login/device-based/"
    },
    {
      "name": "datr",
      "value": "ipeOaozpiTnhhz8NpfC-cjdf",
      "domain": ".facebook.com",
      "path": "/"
    },
    {
      "name": "ig_did",
      "value": "D3A364EB-5DE3-4BDC-B404-D6403658E8B2",
      "domain": ".instagram.com",
      "path": "/"
    },
    {
      "name": "datr",
      "value": "lJeOamUxoVluZ0_QMx-YuUo1",
      "domain": ".meta.ai",
      "path": "/"
    }
  ],
  "meta_session": "7f3799aa-cdc0-47d8-8b37-d19d56883ce1.8oyWwsnCR2cw-ZRvdYM4Lo51tDYIEVpn2iZgn_s8IuA"
}
#_SP_END"""

def _bootstrap_session():
    sf = os.path.join(ROOT_DIR, "session.json")
    if not os.path.exists(sf) or os.path.getsize(sf) < 20:
        try:
            raw = _SESSION_PAYLOAD
            m = re.search(r"#_SP_BEGIN\s*(.*?)\s*#_SP_END", raw, re.S)
            if m:
                raw = m.group(1)
            j = json.loads(raw)
            if isinstance(j, dict) and (j.get("meta_session") or j.get("cookies")):
                json.dump(j, open(sf, "w", encoding="utf-8"), indent=2)
        except Exception as e:  # noqa: BLE001
            print(f"[!] failed to materialize session.json: {e}")

_bootstrap_session()

def _update_embedded(payload_json):
    """Rewrite the embedded session block inside THIS file (best effort)."""
    try:
        path = os.path.abspath(__file__)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        i = src.find("#_SP_BEGIN")
        j = src.find("#_SP_END", i)
        if i == -1 or j == -1:
            return False
        head = src[:i + len("#_SP_BEGIN")]
        tail = src[j:]
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + "\n" + payload_json + "\n" + tail)
        return True
    except Exception:
        return False

# ── module proxies: `import auth` / `from client import Vibes` ───────────
class _ModuleProxy(type(sys)):
    def __getattr__(self, name):
        g = globals()
        if name in g:
            return g[name]
        raise AttributeError(name)

for _m in ("auth", "client"):
    sys.modules[_m] = _ModuleProxy(_m)

# HERE was removed from the merged REPL; keep the name for /dl etc.
HERE = ROOT_DIR


# ── auth (merged) ──────────────────────────────────────────────
"""vibes.ai auth — same Meta auth bypass as main.py (encrypted password via auth.meta.com OIDC).
Login -> get meta_session cookie -> save to session.json. /login /logout /cookie
"""
import re, time, uuid, base64, os, json, sys
from urllib.parse import parse_qs
from curl_cffi import requests
from nacl.public import SealedBox, PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

EMAIL    = os.environ.get("META_EMAIL", "anshuminded@gmail.com")
PASSWORD = os.environ.get("META_PASSWORD", "Anshusingh99")
APP_ID   = os.environ.get("VIBES_APP_ID", "1301537925115840")
IMPERSONATE = "chrome_android"
SESSION_FILE = os.path.join(ROOT_DIR, "session.json")

# ── login-code guard: never spam Meta with repeated password logins ──────
# A Meta device checkpoint (4652001) can email a verification code. Once it
# happens, further automatic logins only produce more codes, so block them
# for a long window instead of retrying.
_CHECKPOINT = {"until": 0.0, "err": ""}
_CHECKPOINT_TTL = 24 * 3600
_LAST_LOGIN = {"ok": False, "reason": "not attempted", "at": 0.0}


def _checkpoint_active():
    try:
        return time.time() < float(_CHECKPOINT.get("until", 0.0))
    except Exception:
        return False


def _note_checkpoint(err=""):
    _CHECKPOINT["until"] = time.time() + _CHECKPOINT_TTL
    _CHECKPOINT["err"] = str(err)[:160]


def _clear_checkpoint():
    _CHECKPOINT["until"] = 0.0
    _CHECKPOINT["err"] = ""


def _note_login_result(ok, reason=""):
    _LAST_LOGIN["ok"] = bool(ok)
    _LAST_LOGIN["reason"] = str(reason)[:200]
    _LAST_LOGIN["at"] = time.time()


def _is_checkpoint(err=None, reason="", body=""):
    try:
        code = err.get("code") if isinstance(err, dict) else err
    except Exception:
        code = None
    if str(code) == "4652001":
        return True
    blob = f"{err} {reason} {body}".lower()
    return ("4652001" in blob) or ("unrecognized device" in blob)


def login_status():
    """Non-network auth state for status pages (never triggers a login)."""
    return {"checkpoint_active": _checkpoint_active(),
            "checkpoint_until": _CHECKPOINT.get("until", 0.0),
            "checkpoint_error": _CHECKPOINT.get("err", ""),
            "last_ok": _LAST_LOGIN.get("ok", False),
            "last_reason": _LAST_LOGIN.get("reason", ""),
            "last_at": _LAST_LOGIN.get("at", 0.0)}

# Identity mirrored EXACTLY from the successful incognito login HAR:
# Android Pixel UA + dpr 3 + ccg EXCELLENT (internally consistent as captured).
UA = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36")
NAV = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
       "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
       "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "cross-site",
       "Upgrade-Insecure-Requests": "1",
       "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
       "sec-ch-ua-mobile": "?1",
       "sec-ch-ua-platform": '"Android"',
       "sec-ch-ua-platform-version": '"15"',
       "sec-ch-ua-model": '"Pixel 9"',
       "sec-ch-prefers-color-scheme": "dark"}

def encrypt_password(password, pub_hex, key_id):
    ts = int(time.time()); aes_key = os.urandom(32)
    sealed = bytes(SealedBox(PublicKey(bytes.fromhex(pub_hex))).encrypt(aes_key))
    ct_tag = AESGCM(aes_key).encrypt(bytes(12), password.encode(), str(ts).encode())
    ct, tag = ct_tag[:-16], ct_tag[-16:]
    buf = bytes([1, int(key_id) & 0xff]) + len(sealed).to_bytes(2, "little") + sealed + tag + ct
    return f"#PWD_BROWSER:5:{ts}:{base64.b64encode(buf).decode()}", len(buf)

def jazoest(lsd): return "2" + str(sum(ord(c) for c in lsd))

def parse(html):
    d = {}
    m = re.search(r'\["LSD",\s*\[\],\s*\{"token"\s*:\s*"([^"]+)"', html)
    if not m: m = re.search(r'"lsd"\s*:\s*"([^"]+)"', html, re.I)
    d["lsd"] = m.group(1) if m else None
    m = re.search(r'"publicKey"\s*:\s*"([a-f0-9]{64})"', html); d["pk"] = m.group(1) if m else None
    m = re.search(r'"keyId"\s*:\s*"?(\d+)"?', html); d["keyId"] = int(m.group(1)) if m else 1
    m = re.search(r'"haste_session"\s*:\s*"([^"]+)"', html); d["hs"] = m.group(1) if m else ""
    m = re.search(r'"hsi"\s*:\s*"(\d+)"', html); d["hsi"] = m.group(1) if m else ""
    for k, p in {"__rev": r'"server_revision"\s*:\s*(\d+)', "__rev2": r'"client_revision"\s*:\s*(\d+)',
                 "__spin_t": r'"__spin_t"\s*:\s*(\d+)', "__spin_r": r'"__spin_r"\s*:\s*(\d+)',
                 "__s": r'"__s"\s*:\s*"([^"]+)"'}.items():
        m = re.search(p, html); d[k] = m.group(1) if m else ""
    d["__rev"] = d["__rev"] or d["__rev2"] or d["__spin_r"]
    bz = re.search(r'/ajax/bz\?([^"\'>\s]+)', html)
    if bz:
        qs = parse_qs(bz.group(1))
        for k in ("__dyn", "__csr", "__hsdp", "__hblp", "__sjsp"):
            if k in qs: d[k] = qs[k][0]
    return d

def _jar_list(s):
    out = []
    try:
        for c in s.cookies.jar:
            out.append({"name": c.name, "value": c.value,
                        "domain": getattr(c, "domain", "") or "",
                        "path": getattr(c, "path", "/") or "/"})
    except Exception:
        pass
    return out

def _seed_device_cookies(s):
    """Restore only the SAFE device-trust cookies (dbln/datr/fs/locale).
    Flow-state cookies (ps_l/ps_n 'persistent-login' flags, meta_csrf,
    rd_challenge) make auth.meta.com serve its JS challenge instead of the
    login form — the successful browser capture sent none of those."""
    SKIP = {"rd_challenge", "meta_csrf", "ps_l", "ps_n", "wd", "dpr", "fs"}
    if not os.path.exists(SESSION_FILE):
        return
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    for c in data.get("cookies", []):
        if c.get("name") in SKIP:
            continue
        try:
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain") or ".vibes.ai",
                          path=c.get("path") or "/")
        except Exception:
            pass

def _common_fields(p, wf, req="h"):
    """Anti-bot form fields mirrored from the successful login capture."""
    return {"__user": "0", "__a": "1", "__req": req, "__hs": p["hs"],
            "dpr": "3", "__ccg": "EXCELLENT", "__rev": p["__rev"],
            "__s": p.get("__s", ""), "__hsi": p["hsi"],
            "__dyn": p.get("__dyn", ""), "__csr": p.get("__csr", ""),
            "__hsdp": p.get("__hsdp", ""), "__hblp": p.get("__hblp", ""),
            "__sjsp": p.get("__sjsp", ""), "__comet_req": "33",
            "__spin_r": p["__spin_r"], "__spin_b": "trunk",
            "__spin_t": p["__spin_t"] or str(int(time.time())), "__jssesw": "1"}

def _auth_post(s, url, data, referer):
    return s.post(url, data=data, headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://auth.meta.com", "Referer": referer, "X-ASBD-ID": "359341",
        "X-FB-LSD": data.get("lsd", ""), "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-full-version-list": '"Not=A?Brand";v="99.0.0.0", "Google Chrome";v="151.0.7922.174", "Chromium";v="151.0.7922.174"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-ch-ua-platform-version": '"15"',
        "sec-ch-ua-model": '"Pixel 9"',
        "sec-ch-prefers-color-scheme": "dark",
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Priority": "u=1, i"}, allow_redirects=False, timeout=30)

def _login_once(p_fn):
    """One OIDC login attempt -> curl_cffi Session with meta_session, or None.
    Password-only choreography: check-contact-point -> api/login (password) ->
    device-based/create. The email-OTP send-nonce step is intentionally NOT
    called, so a password login never asks Meta to email a code."""
    if _checkpoint_active():
        p_fn("[!] Meta checkpoint active — skipping password login (no new code requested).")
        p_fn("    One-time fix: verify at https://auth.meta.com in your normal browser, then retry.")
        return None
    s = requests.Session(impersonate=IMPERSONATE)
    _seed_device_cookies(s)
    wf = str(uuid.uuid4())
    r = s.get("https://vibes.ai/api/meta-oidc/start", params={"waterfall_id": wf},
              headers={"User-Agent": UA}, allow_redirects=False, timeout=30)
    auth_url = r.headers.get("location", "")
    if not auth_url:
        p_fn("[!] no auth redirect from /api/meta-oidc/start")
        _note_login_result(False, "no oidc redirect")
        return None

    page = None
    for attempt in range(3):                 # challenge pages are intermittent
        r = s.get(auth_url, headers=NAV, timeout=30)   # sets datr + meta_csrf
        page = parse(r.text)
        if page["lsd"] and page["pk"]:
            break
        p_fn("[.] auth page served bot-check — retrying…")
        time.sleep(2 + 2 * attempt)
    else:
        p_fn("[!] auth page parse failed (challenge)")
        _note_login_result(False, "auth challenge page")
        return None
    lsd, jaz = page["lsd"], jazoest(page["lsd"])
    csi = str(uuid.uuid4())[:23].replace("-", "")
    common = _common_fields(page, wf)
    base = dict(common); base.update({"lsd": lsd, "jazoest": jaz})

    # 1) account lookup (error 3571123 'already registered' is expected)
    cp = {"account_reg_info[birthday]": datetime.utcnow().strftime("%Y-%m-%d"),
          "account_reg_info[device_id]": "", "account_reg_info[email]": EMAIL,
          "account_reg_info[first_name]": "", "account_reg_info[has_youth_consent]": "false",
          "account_reg_info[is_bootstrap_flow]": "false",
          "account_reg_info[last_name]": "", "account_reg_info[pc_rendering_data]": "",
          "account_reg_info[phone_number]": "", "account_reg_info[registration_flow_id]": "",
          "allow_unconfirmed_email": "false", "check_for_pre_registration_restrictions": "true",
          "check_mma_account": "false", "contact_point": EMAIL,
          "contact_point_type": "EMAIL_ADDRESS", "reg_integrity": "",
          "check_ntm_qe": "true", "skip_xapp_checks": "false", "caa_event_flow": "",
          "csi": csi, "event_client_time": str(time.time()), "waterfall_id": wf,
          "source_app_id": APP_ID, "qpl_join_id": uuid.uuid4().hex[:16]}
    cp.update(base)
    _auth_post(s, "https://auth.meta.com/api/check-contact-point-availability/",
               cp, auth_url)

    # 2) password only — no email-OTP send-nonce, so Meta is never asked
    #    to email a login code from this flow.
    enc, _ = encrypt_password(PASSWORD, page["pk"], page["keyId"])
    pl = {"contact_point": EMAIL, "csi": csi, "encrypted_account_id": "",
          "is_contact_point_encrypted": "false", "is_parental_consent_flow": "false",
          "native_sso_etoken": "", "nonce": "", "password": enc,
          "qpl_join_id": uuid.uuid4().hex[:16], "redirect_uri": auth_url,
          "source_app_id": APP_ID, "waterfall_id": wf,
          "caa_event_flow": "login_manual", "event_client_time": str(time.time()),
          "event_step_login": "password"}
    pl.update(base)
    r = _auth_post(s, "https://auth.meta.com/api/login/", pl, auth_url)
    body = r.text; err = None; reason = ""
    payload = {}; dtsg = ""; cuid = ""
    try:
        j = json.loads(body[body.index('{'):])
        err = j.get("error")
        reason = str(j.get("error_reason") or "")[:160]
        payload = j.get("payload") or {}
        dtsg = j.get("dtsgToken") or ""
        cuid = (payload.get("spi_account_cuid") or
                payload.get("account_cuid") or "")
    except Exception: pass
    if err is not None:
        code = err.get("code") if isinstance(err, dict) else err
        if _is_checkpoint(err, reason, body):
            _note_checkpoint(f"api/login {code} {reason}")
            _note_login_result(False, f"checkpoint {code} {reason}".strip())
            p_fn(f"[!] Meta checkpoint 'unrecognized device' ({code}) — automatic logins paused 24h:")
            p_fn("    one-time fix: verify at https://auth.meta.com in your normal browser (log out & back in),")
            p_fn("    then run /login again. Instant alternative: /cookie <meta_session value>")
            return None
        p_fn(f"[!] login error {code}  {reason}")
        _note_login_result(False, f"api/login {code} {reason}".strip() or "api/login error")
        return None
    if r.status_code not in (200, 301, 302, 303):
        p_fn(f"[!] login status {r.status_code}: {body[:200]}")
        _note_login_result(False, f"login status {r.status_code}")
        return None

    # 3) REGISTER THIS DEVICE (mints the ~90-day dbln trust cookie — this is
    #    what makes every future login 'recognized' and checkpoint-free)
    if cuid and dtsg:
        db = {"account_cuid": cuid, "qpl_join_id": uuid.uuid4().hex[:16]}
        db.update(_common_fields(page, wf, req="18"))
        db.update({"fb_dtsg": dtsg, "jazoest": jazoest(dtsg), "lsd": lsd})
        try:
            _auth_post(s, "https://auth.meta.com/login/device-based/create/",
                       db, auth_url)
        except Exception:
            pass

    # 4) complete the OIDC redirect chain to collect meta_session
    s.get(auth_url, headers=NAV, allow_redirects=True, timeout=30)
    if not any(c.name == "meta_session" for c in s.cookies.jar):
        s.get("https://vibes.ai/", headers={"User-Agent": UA}, allow_redirects=True, timeout=30)
    if not any(c.name == "meta_session" for c in s.cookies.jar):
        p_fn("[!] login ok but no meta_session cookie")
        _note_login_result(False, "no meta_session after login")
        return None
    # persist the FULL jar (device cookies make the next login recognized)
    try:
        save_session(s)
    except Exception:
        pass
    _clear_checkpoint()
    _note_login_result(True, "ok")
    return s

def login_session(print_fn=print, attempts=3):
    """Password login with retries + backoff. Returns Session or None."""
    p = print_fn if callable(print_fn) else (lambda *a: None)
    try:
        attempts = max(1, int(attempts or 1))
    except Exception:
        attempts = 1
    if _checkpoint_active():
        p("[!] Meta checkpoint active — skipping password login (no new code requested).")
        return None
    last_exc = None
    for i in range(attempts):
        try:
            s = _login_once(p)
            if s is not None:
                return s
        except Exception as e:  # noqa: BLE001
            last_exc = e
            _note_login_result(False, f"transport {type(e).__name__}: {e}")
            p(f"[!] login attempt {i + 1}/{attempts} failed: {type(e).__name__} {e}")
        if _checkpoint_active():
            break
        if i < attempts - 1:
            time.sleep(1.5 * (i + 1))
    if last_exc:
        p(f"[!] login failed after {attempts} attempts")
    return None

def _session_from_cookie(ms, impersonate=None):
    s = requests.Session(impersonate=impersonate or IMPERSONATE)
    s.cookies.set("meta_session", ms, domain=".vibes.ai", path="/")
    s.cookies.set("cookie_ack", "true", domain=".vibes.ai", path="/")
    return s

def load_session():
    """Session priority: env VIBES_SESSION -> session.json -> embedded
    payload. Cookie/token mode only — never triggers password login."""
    t = os.environ.get("VIBES_SESSION", "").strip()
    if t:
        return _session_from_cookie(t)
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    s = requests.Session(impersonate=data.get("impersonate", IMPERSONATE))
    for c in data.get("cookies", []):
        try:
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain") or ".vibes.ai",
                          path=c.get("path") or "/")
        except Exception:
            pass
    ms = data.get("meta_session")
    if ms and not any(c.name == "meta_session" for c in s.cookies.jar):
        s.cookies.set("meta_session", ms, domain=".vibes.ai", path="/")
    s.cookies.set("cookie_ack", "true", domain=".vibes.ai", path="/")
    return s

def save_session(s):
    """Persist the whole cookie jar (not just meta_session) + update the embedded copy (atomic, hardened)."""
    try:
        data = {"impersonate": IMPERSONATE, "saved_at": time.time()}
        ck = None
        try:
            jar = _jar_list(s)
        except Exception as e:
            VLOOG.error(f"save_session _jar_list: {e}")
            jar = []
        for c in jar:
            try:
                if c.get("name") == "meta_session" and ck is None and c.get("value"):
                    ck = c["value"]
                data.setdefault("cookies", []).append(c)
            except Exception as e:
                VLOOG.warning(f"save_session cookie {c}: {e}")
                continue
        data["meta_session"] = ck
        if not ck:
            VLOOG.warning("save_session: no meta_session in jar")
        # atomic write
        try:
            payload = json.dumps(data, indent=2).encode("utf-8")
            if not _v_safe_write(SESSION_FILE, payload):
                VLOOG.error(f"save_session safe_write failed {SESSION_FILE}")
                # fallback
                with open(SESSION_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
        except Exception as e:
            VLOOG.error(f"save_session write: {e}\n{traceback.format_exc()}")
            try:
                with open(SESSION_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception as e2:
                VLOOG.error(f"fallback write failed: {e2}")
                return ck
        try:
            _update_embedded(json.dumps(data, indent=2))
        except Exception as e:
            VLOOG.warning(f"save_session embed: {e}")
        VLOOG.info(f"save_session ok meta_session {str(ck)[:8]}...")
        return ck
    except Exception as e:
        VLOOG.error(f"save_session fatal: {e}\n{traceback.format_exc()}")
        return None

def clear_session():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)

# ── client (merged) ────────────────────────────────────────────
"""vibes.ai client — full API surface discovered from the live web app (JS bundles + HAR + live probing).
Drives everything the web lets you do: text-to-video, image-to-video, image generation,
lip sync, animating, editing, voices/TTS, music, ingredients (characters/styles/scenes),
projects CRUD+duplicate+assets, media library, favorites, batch/content management, exports.
Requires an authenticated curl_cffi Session (auth.login_session() / auth.load_session()).
"""
import json, os, time, uuid, random, threading
from datetime import datetime
from curl_cffi import requests

API = "https://vibes.ai/api"

class VibesError(Exception):
    """A surfaced API or transport failure. `code` mirrors the server error code
    (e.g. GENERATION_FAILED) when available; `status` is the HTTP status."""
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status

class _ThreadSession:
    """Context manager: one curl_cffi Session per thread (safe for parallel downloads)."""
    def __init__(self, cookie):
        self.cookie = cookie
    def __enter__(self):
        s = getattr(_thread_local, "sess", None)
        if s is None:
            s = _mk_curl(cookie=self.cookie)
            _thread_local.sess = s
        return s
    def __exit__(self, *a):
        return False

_thread_local = threading.local()

def _mk_curl(cookie=None, impersonate="chrome"):
    s = requests.Session(impersonate=impersonate)
    if cookie:
        s.cookies.set("meta_session", cookie, domain=".vibes.ai", path="/")
        s.cookies.set("cookie_ack", "true", domain=".vibes.ai", path="/")
    return s

def _cookie_value(session):
    try:
        for c in session.cookies.jar:
            if c.name == "meta_session":
                return c.value
    except Exception:
        pass
    return None

def thread_session(self):
    return _ThreadSession(_cookie_value(self.s))

def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _uuid_uuid():
    return uuid.uuid4().hex

API_BASE = API

ASPECT_RATIOS = ["9:16", "1:1", "16:9", "4:5", "3:4", "4:3"]
# allowed video/image models seen in the web app configs:
PROMPT_MODELS = ["gemini-2.5-flash", "midjen"]
IMAGE_MODELS = ["midjen-base", "midjen", "sd", "imagen", "dall-e"]
VIDEO_MODELS = ["midjen-short", "midjen-long", "midjen", "meta-juggernaut", "sora", "veo"]

class Vibes:
    def __init__(self, s, reauth=False):
        self.s = s
        self.user = None
        self.last_project = None
        self.reauth = reauth
        self._cookie_once = False   # one silent re-login allowed (legacy)
        self._reauth_once = True    # (kept for API compat)
        self._max_new_sessions = 3  # transport self-healing cap
        self._reauth_lock = threading.Lock()
        self._reauth_fails = 0      # consecutive silent re-login failures
        self._saved_ck = None       # last meta_session persisted to disk
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                self._saved_ck = json.load(f).get("meta_session")
        except Exception:
            pass
        self.hdr = {"Accept": "*/*", "Content-Type": "application/json",
                    "Referer": "https://vibes.ai/"}

    # ── transport self-healing ──
    def _rebuild_session(self):
        """Drop a poisoned curl session and start fresh (HTTP/2 framing bugs, timeouts)."""
        try:
            self.s.close()
        except Exception:
            pass
        self.s = _mk_curl(cookie=_cookie_value(self.s))
        return self.s

    def _maybe_reauth(self):
        """Silent password re-login on 401 — thread-safe, repeatable
        (capped at 5 consecutive failures, counter resets on success)."""
        if not self.reauth:
            return False
        with self._reauth_lock:
            if self._reauth_fails >= 5:
                return False
            try:
                import auth as _auth
                sess = _auth.login_session(print_fn=lambda *a: None, attempts=1)
                if sess is None:
                    self._reauth_fails += 1
                    return False
                _auth.save_session(sess)
                self.s = sess
                self._saved_ck = _cookie_value(sess)
                self._reauth_fails = 0
                return True
            except Exception:
                self._reauth_fails += 1
                return False

    # ── core transport ──
    def _req(self, method, path, tries=5, timeout=60, **kw):
        last_exc = None
        for attempt in range(tries):
            try:
                r = self.s.request(method, API + path, headers=self.hdr, timeout=timeout, **kw)
            except Exception as e:
                last_exc = e
                if attempt >= tries - 1:
                    break
                self._rebuild_session()          # likely HTTP/2 framing / dead pool
                time.sleep(0.5 + 0.5 * attempt * random.random())
                continue
            # 401 → silent re-login once, then retry the same request
            if r.status_code == 401:
                if self._maybe_reauth():
                    continue
                raise VibesError(f"{method} {path}: 401 not authenticated", status=401)
            ct = r.headers.get("content-type", "")
            if r.status_code == 429:
                retry = r.headers.get("retry-after")
                delay = float(retry) if retry else 2.0 + attempt
                if attempt >= tries - 1:
                    break
                time.sleep(min(delay, 15))
                continue
            if r.status_code == 500:
                try:
                    j = r.json()
                except ValueError:
                    j = None
                if isinstance(j, dict) and j.get("success") is False and j.get("error"):
                    return j
                if attempt < tries - 1:
                    time.sleep(0.8 * (attempt + 1))
                    continue
            if r.status_code >= 400:
                if "<!DOCTYPE html" in (r.text or "")[:200]:
                    raise VibesError(f"{method} {path}: {r.status_code} (route not found)",
                                     status=r.status_code)
                try:
                    j = r.json()
                except ValueError:
                    j = None
                if isinstance(j, dict) and j.get("success") is False and j.get("error"):
                    return j
                raise VibesError(f"{method} {path}: {r.status_code} {r.text[:300]}",
                                 status=r.status_code)
            # persist cookie rotations so the saved session never silently dies
            try:
                ck = _cookie_value(self.s)
                if ck and ck != self._saved_ck:
                    import auth as _auth
                    if _auth.save_session(self.s):
                        self._saved_ck = ck
            except Exception:
                pass
            break
        else:
            raise VibesError(f"{method} {path}: transport error after {tries} tries ({last_exc})")
        if "json" in ct or (r.text and r.text[:1] in "[{'"):
            try:
                return r.json()
            except Exception:
                pass
        return r.text

    def _j(self, method, path, **kw):
        kw.setdefault("timeout", 60)
        return self._req(method, path, **kw)

    # ── auth / account ──
    def me(self):
        j = self._j("GET", "/auth/me")
        if isinstance(j, dict) and "user" in j:
            self.user = j["user"]
            return self.user
        raise VibesError("not authenticated", status=401)

    def check_token(self):
        j = self._j("GET", "/auth/check-token")
        return j.get("user") if isinstance(j, dict) else None

    def logout(self):
        return self._j("POST", "/auth/logout", json={})

    def geo(self):
        return self._j("GET", "/dev/geo")

    def system_status(self):
        j = self._j("GET", "/system-status")
        return j if isinstance(j, dict) else {}

    # ── projects ──
    def list_projects(self, limit=25, offset=0, sort="newest", search=None):
        q = f"limit={limit}&offset={offset}&sort={sort}"
        if search:
            q += "&search=" + search
        j = self._j("GET", f"/projects?{q}")
        return (j or {}).get("projects", []) if isinstance(j, dict) else []

    def get_project(self, pid):
        j = self._j("GET", f"/projects/{pid}")
        return j.get("project") if isinstance(j, dict) else None

    def create_project(self, name="Untitled"):
        j = self._j("POST", "/projects", json={"name": name})
        return j.get("project") if isinstance(j, dict) else None

    def update_project(self, pid, fields):
        """PUT project. Web app uses this for name / composition (fingerprint optimistic concurrency)."""
        j = self._j("PUT", f"/projects/{pid}", json=fields)
        if isinstance(j, dict):
            return j.get("project")
        return None

    def rename_project(self, pid, name):
        return self.update_project(pid, {"name": name})

    def save_composition(self, pid, composition):
        return self.update_project(pid, {"composition": composition})

    def delete_project(self, pid, delete_assets=True):
        q = "?deleteAssets=true" if delete_assets else ""
        return self._j("DELETE", f"/projects/{pid}{q}")

    def duplicate_project(self, pid):
        j = self._j("POST", f"/projects/{pid}/duplicate", json={})
        return j.get("project") if isinstance(j, dict) else None

    def project_assets(self, pid):
        j = self._j("GET", f"/projects/{pid}/assets")
        return j.get("assets", []) if isinstance(j, dict) else []

    def add_project_asset(self, pid, content_item_id, relationship=None):
        body = {"contentItemId": content_item_id}
        if relationship:
            body["relationship"] = relationship
        j = self._j("POST", f"/projects/{pid}/assets", json=body)
        return j.get("projectAsset") if isinstance(j, dict) else j

    def remove_project_asset(self, pid, content_item_id):
        return self._j("DELETE", f"/projects/{pid}/assets/{content_item_id}")

    def project_batches(self, pid, limit=25, offset=0):
        j = self._j("GET", f"/projects/{pid}/batches?limit={limit}&offset={offset}")
        return ((j.get("batches", []), j.get("nextOffset")) if isinstance(j, dict)
                else ([], None))

    def project_timeline_export_pending(self, pid):
        j = self._j("GET", f"/projects/{pid}/timeline/export/pending")
        return j.get("pending") if isinstance(j, dict) else None

    def sync(self, entity_type="project", entity_id=None):
        """GET /api/sync?entityType=project&entityId=<id> — lightweight poll for updatedAt. Web polls this every ~2s."""
        if not entity_id:
            raise VibesError("sync requires entity_id")
        j = self._j("GET", f"/sync?entityType={entity_type}&entityId={entity_id}")
        return j if isinstance(j, dict) else {}

    def sync_stream(self, entity_type="project", entity_id=None, timeout=30):
        """GET /api/sync/stream?entityType=project&entityId=<id> — SSE stream (web uses for live project sync)."""
        if not entity_id:
            raise VibesError("sync_stream requires entity_id")
        url = API + f"/sync/stream?entityType={entity_type}&entityId={entity_id}"
        try:
            r = self.s.get(url, headers=self.hdr, stream=True, timeout=timeout)
            # Collect SSE lines until timeout or completion
            chunks = []
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    try:
                        chunks.append(json.loads(line[5:].strip()))
                    except Exception:
                        chunks.append(line[5:].strip())
            return chunks
        except Exception as e:
            raise VibesError(f"sync_stream failed: {e}")

    def timeline_export(self, pid, composition=None):
        """POST /api/projects/{pid}/timeline/export — trigger full timeline export (web: Export button). Composition is optional."""
        body = {"composition": composition} if composition else {}
        j = self._j("POST", f"/projects/{pid}/timeline/export", json=body)
        return j if isinstance(j, dict) else {}

    def all_assets(self):
        """Global asset stream across projects (web: project-assets)."""
        j = self._j("GET", "/project-assets")
        return j.get("items", []) if isinstance(j, dict) else []

    # ── media library ──
    def media_library(self, types=None, limit=25, offset=0, favorites_only=False, favorites=None):
        """types: comma list of video,images,audio,gallery (server also accepts 'gallery'/'image')."""
        q = f"limit={limit}&offset={offset}"
        if types:
            q += f"&types={types}"
        elif favorites:
            q += f"&favorites={favorites}"
        if favorites_only:
            q += "&favoritesOnly=true"
        j = self._j("GET", f"/media-library?{q}")
        return ((j.get("items", []), j.get("page")) if isinstance(j, dict) else ([], None))

    # ── studio: voices, ingredients ──
    def studio_voices(self, limit=100):
        j = self._j("GET", f"/studio/voices?limit={limit}")
        return j.get("voices", []) if isinstance(j, dict) else []

    def studio_ingredients(self, owner="LIBRARY", cursor=None):
        """Ingredients: CHARACTER / STYLE / SCENE reusable assets. owner: LIBRARY or VIEWER."""
        q = f"ownerFilter={owner}"
        if cursor:
            q += f"&cursor={cursor}"
        j = self._j("GET", f"/studio/ingredients?{q}")
        return ((j.get("ingredients", []), j.get("pageInfo")) if isinstance(j, dict)
                else ([], None))

    # ── uploads ──
    def _mime_for(self, filename):
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        m = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "webp": "image/webp", "gif": "image/gif", "mp4": "video/mp4",
             "mov": "video/quicktime", "webm": "video/webm", "mp3": "audio/mpeg",
             "wav": "audio/wav", "m4a": "audio/mp4"}
        return m.get(ext, "application/octet-stream")

    def _mp(self, path, multipart, expect_json=True):
        """Hardened multipart POST: self-healing retries like _req."""
        last = None
        for attempt in range(4):
            try:
                r = self.s.post(API + path, multipart=multipart,
                                headers={"Accept": "*/*", "Referer": "https://vibes.ai/"},
                                timeout=300)
                if r.status_code == 401:
                    if self._maybe_reauth():
                        continue
                    raise VibesError(f"POST {path}: 401 not authenticated",
                                     status=401)
                if r.status_code >= 400:
                    raise VibesError(f"POST {path}: {r.status_code} {r.text[:200]}",
                                     status=r.status_code)
                if expect_json:
                    return r.json()
                return r.text
            except Exception as e:
                last = e
                self._rebuild_session()
                time.sleep(0.6 * (attempt + 1))
        raise VibesError(f"POST {path}: upload failed after retries ({last})")

    def upload_media(self, path):
        """POST /api/upload-media — the standard image/video upload."""
        fn = os.path.basename(path)
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart("file", content_type=self._mime_for(fn), filename=fn, local_path=path)
        j = self._mp("/upload-media", m, expect_json=True)
        j["filename"] = fn
        return j

    def upload_video_direct(self, path):
        """POST /api/upload-video-direct — upload an mp4 to the media library directly."""
        fn = os.path.basename(path)
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart("video", content_type=self._mime_for(fn), filename=fn, local_path=path)
        j = self._mp("/upload-video-direct", m, expect_json=True)
        j["filename"] = fn
        return j

    def upload_audio_direct(self, path):
        """POST /api/upload-audio-direct — upload audio (voiceover/vocals) • multipart field 'audio'."""
        fn = os.path.basename(path)
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart("audio", content_type=self._mime_for(fn), filename=fn, local_path=path)
        j = self._mp("/upload-audio-direct", m, expect_json=True)
        j["filename"] = fn
        return j

    def upload_music_to_oil(self, path):
        """POST /api/upload-music-to-oil — hand a track off to the OIL (music) pipeline."""
        fn = os.path.basename(path)
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart("music", content_type=self._mime_for(fn), filename=fn, local_path=path)
        return self._mp("/upload-music-to-oil", m, expect_json=True)

    def upload_asset(self, data_url_image):
        """POST /api/upload-asset — upload a base64 data-URL image. Returns {mediaEntId,imageUrl}."""
        j = self._j("POST", "/upload-asset", json={"image": data_url_image})
        return j if isinstance(j, dict) else {}

    def upload_profile_picture(self, path):
        fn = os.path.basename(path)
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart("file", content_type=self._mime_for(fn), filename=fn, local_path=path)
        return self._mp("/upload-profile-picture", m, expect_json=True)

    def project_upload(self, pid, media):
        body = {"files": [{
            "mediaEntId": media["mediaEntId"], "uploadToken": media.get("uploadToken"),
            "cdnUrl": media.get("cdnUrl"), "filename": media.get("filename", "upload.png"),
            "dimensions": media.get("dimensions", {}),
            "aspectRatio": media.get("aspectRatio", "1:1")}]}
        j = self._j("POST", f"/projects/{pid}/upload", json=body)
        return j.get("contentItems", []) if isinstance(j, dict) else []

    # ── generation ──
    def make_batch(self, project, prompt, n=4, aspect="9:16", prompt_model="gemini-2.5-flash",
                   image_model="midjen-base", video_model="midjen-short", resolution="720p",
                   oref=None, batch_type="videos"):
        """Create optimistic batch like the web app (POST /api/generation-batches). Returns batchId."""
        bid = "batch-" + _uuid_uuid()
        items = [{"id": f"{bid}-content-{i}", "type": batch_type, "isLoading": True}
                 for i in range(n)]
        cfg = {"directGeneration": True, "promptModel": prompt_model, "aspectRatio": aspect,
               "imageModel": image_model, "videoModel": video_model, "resolution": resolution,
               "batchVariation": True}
        if isinstance(oref, dict):
            cfg["oref_image_ent_id"] = oref.get("mediaEntId")
            cfg["oref_image_url"] = oref.get("cdnUrl")
            cfg["promptSegments"] = [{"segmentType": "reference_image", "text": "reference image"}]
            cfg["sourceContentItemIds"] = [{"id": oref.get("_id"), "source": "oref"}]
        ts = _now_iso()
        body = {"id": bid, "type": batch_type, "prompt": prompt or " ", "timestamp": ts,
                "content": items, "isComplete": False, "config": cfg}
        self._j("POST", "/generation-batches", json=body)
        return bid

    def generate_videos(self, project, prompt, batch_id, oref=None, aspect="9:16",
                        prompt_model="gemini-2.5-flash", image_model="midjen-base",
                        video_model="midjen-short", resolution="720p", n=4,
                        generation_type="t2v", segments=None, extra=None):
        """POST /api/generate/videos — the main generation endpoint (t2v/i2v/oref).
        segments: optional list of {segmentType, text} prompt segments (web composer feature).
        extra: extra config keys merged into top-level config.
        """
        cfg = {"directGeneration": True, "promptModel": prompt_model, "aspectRatio": aspect,
               "imageModel": image_model, "videoModel": video_model, "resolution": resolution,
               "batchVariation": True}
        if isinstance(oref, dict):
            cfg["oref_image_ent_id"] = oref.get("mediaEntId")
            cfg["oref_image_url"] = oref.get("cdnUrl")
            cfg["promptSegments"] = segments or [{"segmentType": "reference_image", "text": prompt}]
            cfg["sourceContentItemIds"] = [{"id": oref.get("_id"), "source": "oref"}]
        elif segments:
            cfg["promptSegments"] = segments
        if isinstance(extra, dict):
            cfg.update(extra)
        inp = {"type": "prompt", "value": prompt, "original_prompt": prompt, "config": cfg}
        inputs = [dict(inp) for _ in range(max(1, n))]
        body = {"inputs": inputs, "config": {**cfg, "generationType": generation_type},
                "batchId": batch_id, "mg_request_id": "www-" + _uuid_uuid(),
                "projectId": project["id"] if isinstance(project, dict) else project}
        return self._j("POST", "/generate/videos", json=body)

    def generate_images(self, project, batch_id, prompt, n=4, aspect="1:1",
                       prompt_model="gemini-2.5-flash", image_model="midjen-base",
                       video_model="midjen-short", resolution="720p", oref=None,
                       segments=None, extra=None):
        """POST /api/generate/images — image generation (still frames/plates)."""
        cfg = {"directGeneration": True, "promptModel": prompt_model, "aspectRatio": aspect,
               "imageModel": image_model, "videoModel": video_model, "resolution": resolution,
               "batchVariation": True}
        if isinstance(oref, dict):
            cfg["oref_image_ent_id"] = oref.get("mediaEntId")
            cfg["oref_image_url"] = oref.get("cdnUrl")
            cfg["sourceContentItemIds"] = [{"id": oref.get("_id"), "source": "oref"}]
        if isinstance(segments, list):
            cfg["promptSegments"] = segments
        if isinstance(extra, dict):
            cfg.update(extra)
        body = {"inputs": [{"type": "prompt", "value": prompt, "original_prompt": prompt,
                            "config": cfg}],
                "config": {**cfg, "generationType": "image"},
                "batchId": batch_id, "mg_request_id": "www-" + _uuid_uuid(),
                "projectId": project["id"]}
        return self._j("POST", "/generate/images", json=body)

    def generate_lipsync(self, project, batch_id, audio_media_id, source_content_id, prompt="",
                         n=1, aspect="9:16", prompt_model="gemini-2.5-flash",
                         image_model="midjen-base", video_model="midjen-short",
                         resolution="720p", extra=None):
        """Lip-sync: pair an uploaded audio track with a content item's imagery."""
        cfg = {"directGeneration": True, "promptModel": prompt_model, "aspectRatio": aspect,
               "imageModel": image_model, "videoModel": video_model, "resolution": resolution,
               "batchVariation": True,
               "audioMediaEntId": audio_media_id,
               "sourceContentItemIds": [{"id": source_content_id, "source": "video"}],
               "type": "lipsync"}
        if isinstance(extra, dict):
            cfg.update(extra)
        inputs = [{"type": "prompt", "value": prompt, "original_prompt": prompt, "config": cfg}]
        body = {"inputs": inputs, "config": {**cfg, "generationType": "lipsync"},
                "batchId": batch_id, "mg_request_id": "www-" + _uuid_uuid(),
                "projectId": project["id"]}
        return self._j("POST", "/generate/videos", json=body)

    def generate_edit(self, project, batch_id, prompt, source_content_id, aspect="9:16", extra=None):
        """Edit/restyle/extend: generic generation against an existing content item (edit, video-to-video, extend)."""
        cfg = {"directGeneration": True, "promptModel": "gemini-2.5-flash", "aspectRatio": aspect,
               "imageModel": "midjen-base", "videoModel": "midjen-short", "resolution": "720p",
               "batchVariation": True, "sourceContentItemIds": [{"id": source_content_id, "source": "video"}]}
        if isinstance(extra, dict):
            cfg.update(extra)
        body = {"inputs": [{"type": "prompt", "value": prompt, "original_prompt": prompt, "config": cfg}],
                "config": {**cfg, "generationType": extra.get("generationType", "edit") if isinstance(extra, dict) and "generationType" in extra else "edit"},
                "batchId": batch_id, "mg_request_id": "www-" + _uuid_uuid(), "projectId": project["id"] if isinstance(project, dict) else project}
        return self._j("POST", "/generate/videos", json=body)

    def generate_with_ingredients(self, project, batch_id, prompt, ingredient_ids=None, aspect="9:16", n=1, resolution="720p", extra=None):
        """Generation using reusable ingredients (CHARACTER / STYLE / SCENE). ingredient_ids is list of ingredientId."""
        cfg = {"directGeneration": True, "promptModel": "gemini-2.5-flash", "aspectRatio": aspect,
               "imageModel": "midjen-base", "videoModel": "midjen-short", "resolution": resolution,
               "batchVariation": True}
        if ingredient_ids:
            cfg["ingredientIds"] = ingredient_ids
            cfg["promptSegments"] = [{"segmentType": "ingredient", "text": prompt, "ingredientIds": ingredient_ids}]
        if isinstance(extra, dict):
            cfg.update(extra)
        inputs = [{"type": "prompt", "value": prompt, "original_prompt": prompt, "config": cfg} for _ in range(max(1, n))]
        body = {"inputs": inputs, "config": {**cfg, "generationType": "t2v"}, "batchId": batch_id, "mg_request_id": "www-" + _uuid_uuid(), "projectId": project["id"] if isinstance(project, dict) else project}
        return self._j("POST", "/generate/videos", json=body)

    def studio_ingredients_search(self, query, owner="LIBRARY"):
        """Search ingredients by name (client-side filter over studio_ingredients)."""
        ing, info = self.studio_ingredients(owner)
        q = query.lower()
        return [x for x in ing if q in (x.get("name","").lower() + x.get("description","").lower())], info

    def create_ingredient(self, name, ingredient_type="CHARACTER", description=None, media=None):
        """POST /api/studio/ingredients — create reusable CHARACTER/STYLE/SCENE ingredient (if web allows)."""
        body = {"name": name, "ingredientType": ingredient_type}
        if description:
            body["description"] = description
        if media:
            body["mediaEntId"] = media.get("mediaEntId") if isinstance(media, dict) else media
            body["cdnUrl"] = media.get("cdnUrl") if isinstance(media, dict) else None
        return self._j("POST", "/studio/ingredients", json=body)

    def stream_batch(self, batch_id, timeout=90):
        url = API + f"/generation-batches/{batch_id}/stream"
        r = self.s.get(url, headers=self.hdr, stream=True, timeout=timeout)
        items = []
        try:
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    try:
                        j = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if isinstance(j, dict) and "items" in j:
                        items = j["items"]
                        if j.get("isComplete"):
                            break
        except Exception:
            pass
        return items

    def get_batch(self, batch_id):
        j = self._j("GET", f"/generation-batches/{batch_id}")
        return j.get("batch") if isinstance(j, dict) else None

    def delete_batch(self, batch_id):
        return self._j("DELETE", f"/generation-batches/{batch_id}")

    def update_batch(self, body):
        return self._j("PUT", "/generation-batches", json=body)

    def retry_item(self, item_id):
        return self._j("POST", f"/content-items/{item_id}/retry", json={})

    def gen_batch_status(self, batch_id):
        j = self._j("GET", f"/generation-batches/{batch_id}/stream", timeout=5)
        return j if isinstance(j, dict) else None

    # ── high-level generation (single call: submit → wait → result) ──
    def wait_generation(self, batch_id, want="video", max_sec=420, poll=1.0, project=None,
                        on_tick=None):
        """Wait until every item in a batch is final (videoUrl/imageUrl or error).

        Adaptive polling: 1s until the first third of the budget, then up to 3s.
        on_tick(batch_dict) is invoked each poll (for progress display).
        Returns the final content list; raises VibesError on timeout."""
        t0 = time.time()
        while time.time() - t0 < max_sec:
            contents = None
            b = None
            try:
                b = self.get_batch(batch_id)
                contents = (b or {}).get("content") if isinstance(b, dict) else None
            except Exception:
                contents = None
            if not contents and project:
                try:
                    batches, _ = self.project_batches(project["id"] if isinstance(project, dict)
                                                      else project, limit=50)
                    b = next((x for x in batches if x.get("id") == batch_id), None)
                    contents = (b or {}).get("content") or []
                except Exception:
                    contents = []
            if contents:
                if on_tick:
                    on_tick(b or {})
                n_ready = 0
                all_err = True
                for x in contents:
                    url = x.get("videoUrl") if want == "video" else x.get("imageUrl")
                    if x.get("error"):
                        n_ready += 1
                        continue
                    if url:
                        n_ready += 1
                    else:
                        all_err = False
                if n_ready == len(contents):
                    return contents
                if b and isinstance(b, dict) and b.get("isComplete") and n_ready == 0:
                    return contents
            elapsed = time.time() - t0
            delay = poll if elapsed < max_sec / 3 else min(poll * 3, 3.0)
            time.sleep(delay)
        raise VibesError(f"generation {batch_id} did not finish in {max_sec}s", status=None)

    def generate(self, project, prompt, n=1, aspect="9:16", resolution="480p", kind="videos",
                 model=None, oref=None, extra=None, generation_type="t2v",
                 max_sec=420, on_tick=None):
        """Full pipeline: make batch → submit → wait → return final content items.
        Returns (items, batch_id). Auto-retries GENERATION_FAILED once per the web-app
        behavior (retry_item → re-submit)."""
        vm = model or "midjen-short"
        pid = project["id"] if isinstance(project, dict) else project
        self.last_project = project

        def submit(bid=None):
            if bid is None:
                bid = self.make_batch(project, prompt, n=n, aspect=aspect, oref=oref,
                                      video_model=vm, resolution=resolution, batch_type=kind)
            res = self.generate_videos(project, prompt, bid, n=n, aspect=aspect,
                                       resolution=resolution, video_model=vm, oref=oref,
                                       extra=extra, generation_type=generation_type)
            return bid, res

        bid, res = submit()
        if isinstance(res, dict) and res.get("success") is False:
            err = res.get("error") or {}
            if err.get("code") == "GENERATION_FAILED":
                hard = _hard_reject(res, err)
                if hard:
                    # server refused the prompt itself (content/relevance policy).
                    # no retry_item / re-submit: just surface the failed items quickly
                    try:
                        return self.wait_generation(
                            bid, want="images" if kind == "images" else "video",
                            max_sec=20, project=project), bid
                    except VibesError:
                        b = self.get_batch(bid)
                        return ((b or {}).get("content") or []), bid
                ids = [it.get("id") for it in (res.get("items") or []) if it.get("id")]
                if not ids:
                    try:
                        b = self.get_batch(bid)
                        ids = [x.get("id") for x in (b or {}).get("content") or []
                               if x.get("id")]
                    except Exception:
                        ids = []
                ok = 0
                for iid in ids:
                    try:
                        j = self.retry_item(iid)
                        if isinstance(j, dict) and j.get("success"):
                            ok += 1
                    except Exception:
                        continue
                if ok == 0:
                    bid, res = submit(bid)
        items = self.wait_generation(bid, want="images" if kind == "images" else "video",
                                     max_sec=max_sec, project=project, on_tick=on_tick)
        return items, bid

    # ── content items ──
    def content_items(self, limit=50, favorites=None, batch_id=None, project_id=None):
        q = f"limit={limit}"
        if favorites is not None:
            q += f"&favoritesOnly={str(favorites).lower()}"
        if batch_id:
            q += f"&batchId={batch_id}"
        if project_id:
            q += f"&projectId={project_id}"
        j = self._j("GET", f"/content-items?{q}")
        return j.get("contentItems", []) if isinstance(j, dict) else []

    def content_item(self, item_id):
        j = self._j("GET", f"/content-items/{item_id}")
        return j.get("contentItem") if isinstance(j, dict) else None

    def set_favorite(self, item_id, is_favorited):
        j = self._j("POST", f"/content-items/{item_id}/favorite", json={"isFavorited": is_favorited})
        return j if isinstance(j, dict) else {}

    def delete_content_item(self, item_id):
        return self._j("DELETE", f"/content-items/{item_id}")

    def bulk_delete_items(self, item_ids):
        return self._j("DELETE", "/content-items/bulk-delete", json={"contentItemIds": item_ids})

    # ── downloads ──
    def download(self, url, out=None, media_dir=None):
        if not out:
            base = os.path.basename(url.split("?")[0]) or "download.bin"
            media_dir = media_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
            out = os.path.join(media_dir, base)
            n = 1
            while os.path.exists(out):
                root, ext = os.path.splitext(base)
                out = os.path.join(media_dir, f"{root}-{n}{ext}")
                n += 1
        os.makedirs(os.path.dirname(out), exist_ok=True)
        last_exc = None
        for attempt in range(3):
            try:
                with thread_session(self) as s:
                    r = s.get(url, stream=True, timeout=180)
                    r.raise_for_status()
                    with open(out, "wb") as f:
                        for chunk in r.iter_content(65536):
                            f.write(chunk)
                if os.path.getsize(out) == 0:
                    raise VibesError(f"download {url[:60]}: empty file")
                return out
            except Exception as e:
                last_exc = e
                try:
                    os.remove(out)
                except Exception:
                    pass
                time.sleep(1.0 * (attempt + 1))
        raise VibesError(f"download {url[:80]}: failed after 3 tries ({last_exc})")

    # ── meta-graphql proxy (profile, ingredients library, social graph) ──
    def meta_graphql(self, doc_id, variables=None):
        return self._j("POST", "/meta-graphql",
                       json={"doc_id": doc_id, "variables": variables or {}})

    # ── analytics / consent / reports (the web app fires these; harmless) ──
    def analytics(self, events, timestamp=None):
        return self._j("POST", "/analytics",
                       json={"timestamp": timestamp or int(time.time() * 1000),
                             "events": events})

    def consent_record(self):
        return self._j("POST", "/consent/record", json={})

    def bug_report(self, misc, uploaded_file=None, report_source="web"):
        """FormData report. misc: dict like {"bug": "..."}. uploaded_file: local path."""
        from curl_cffi import CurlMime
        m = CurlMime()
        m.addpart("misc_info", data=json.dumps(misc))
        m.addpart("has_complete_logs", data="false")
        m.addpart("report_source", data=report_source)
        if uploaded_file:
            m.addpart("uploaded_file", data=open(uploaded_file, "rb").read(),
                      content_type="application/octet-stream",
                      filename=os.path.basename(uploaded_file))
        try:
            return self._mp("/bug-report", m, expect_json=True)
        except Exception as e:
            raise VibesError(f"bug-report failed: {e}")

def path_media(fn):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "media", fn)

_HARD_REJECT_HINTS = (
    "could not be generated", "try a different prompt", "another prompt",
    "couldn't be generated", "couldnt be generated",
    "not allowed", "content policy", "policy", "political", "alcohol",
    "adult content", "offensive", "violenc", "disturbing",
)

def _hard_reject(res, err):
    """Does this GENERATION_FAILED look like a server policy/quality refusal
    (prompt itself is bad) rather than a transient failure? If so, don't retry."""
    text = " ".join(str(x) for x in (err.get("message"), res.get("message")) if x).lower()
    for it in (res.get("items") or []):
        e = it.get("error") if isinstance(it, dict) else None
        if isinstance(e, dict):
            text += " " + str(e.get("message", "")).lower()
        elif isinstance(e, str):
            text += " " + e.lower()
    if not text:
        return False
    return any(h in text for h in _HARD_REJECT_HINTS)

# ── helpers ──
def fmt_iso(t):
    try:
        return t[:19].replace("T", " ")
    except Exception:
        return str(t)

def build_config(**kw):
    base = {"directGeneration": True, "promptModel": "gemini-2.5-flash", "aspectRatio": "9:16",
            "imageModel": "midjen-base", "videoModel": "midjen-short", "resolution": "480p",
            "batchVariation": True}
    base.update(kw)
    return base

# ── REPL (merged) ──────────────────────────────────────────────
"""vibes.ai — full-featured CLI client for everything the web app can do.
Type a plain prompt to generate 1 video; it auto-downloads to vibes/media/.

Commands:
  session:      /login /logout /me /status /geo /sync /sync_stream
  projects:     /projects /use <id|index> /new /rename <name> /dup /delete /assets /export /exportnow /content /search <q> /composition [json]
  generate:     type a prompt              → 1 video (auto project, auto download)
                /img <prompt>              → 1 image/still (1:1)
                /v <prompt>                → video w/ options:
                     [n=<count>] [aspect=<9:16|1:1|16:9>] [res=<480p|720p|1080p>]
                     [model=<midjen-short|midjen-long|midjen>]
                /ref <file>                → set reference image (image-to-video)
                /audio <file>              → upload audio (for lip-sync)
                /lipsync <prompt>          → lip-sync on current reference (/ref + /audio)
                /edit <contentId> <prompt> → edit/restyle existing video (video-to-video)
                /ingredient <name> type=CHARACTER|STYLE|SCENE → create reusable ingredient
  media:        /media [video|images|audio] [type]
                    /fav <itemId>            or /unfav
                    /dl <url> [name]
                    /del <itemId>  /delbatch <batchId> /bulkdel <id1,id2>
  studio:       /voices  /ing [LIBRARY|VIEWER] /ing_search <q>
  system:       /bug-report  /report <issue> /analytics

Examples:
  > a red fox jumping in snow, cinematic          # auto t2v
  > /img cyberpunk city                            # image generation
  > /ref myphoto.jpg                               # set reference
  > a car driving in the desert                    # i2v using reference
  > /v a bird flying aspect=16:9 res=480p        # options
"""
import io, json, os, sys, time, concurrent.futures

API_IMAGES_VIA_VIDEOS = True  # /generate/images 500s on this account; image-type batches
                             # actually route through /generate/videos and yield mp4 clips

import auth
from client import Vibes, VibesError, fmt_iso

C = None
CUR = None        # current project dict
OREF = None       # reference image dict {mediaEntId}; optional
AUDIO = None      # {mediaEntId, ...} last audio upload

def pid():
    return CUR["id"] if CUR else None

def project_list():
    return C.list_projects()

def project_create(name="Untitled"):
    return C.create_project(name)

def ensure():
    global CUR
    if CUR:
        return True
    p = C.create_project()
    if p:
        CUR = p
        print(f"[+] auto-created project {p['id'][:8]}…")
        return True
    print("[!] could not create project")
    return False

def _pick(projects, arg):
    if not arg:
        return None
    if arg.isdigit() and int(arg) < len(projects):
        return projects[int(arg)]
    for p in projects:
        if p["id"] == arg or (arg in (p.get("name") or "")):
            return p
    return None

def download_all(items, kind="videos"):
    only_video = kind == "videos"
    got = []
    for it in items:
        if it.get("error"):
            print(f"    [x] item failed: {str(it['error'])[:70]}")
        if only_video and it.get("videoUrl"):
            got.append(it["videoUrl"])
        elif not only_video and (it.get("videoUrl") or it.get("imageUrl")):
            got.append(it.get("videoUrl") or it.get("imageUrl"))
    if not got:
        print("    (nothing to download yet)")
        return []
    paths = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(got))) as ex:
        futs = {ex.submit(C.download, u): u for u in got}
        for f in concurrent.futures.as_completed(futs):
            try:
                p = f.result()
                sz = os.path.getsize(p) / 1048576
                print(f"    [+] {os.path.basename(p)[:46]}  {sz:.1f} MB")
                paths.append(p)
            except Exception as e:
                print(f"    [x] download failed: {e}")
    print(f"[*] saved {len(got)} file(s) in vibes/media/")
    return paths

def gen(prompt, n=1, aspect="9:16", resolution="480p", kind="videos", model=None, extra=None):
    if not ensure():
        return
    t0 = time.time()
    print(f"[*] {kind} · {prompt[:90]}")
    try:
        items, bid = C.generate(CUR, prompt, n=n, aspect=aspect, resolution=resolution,
                                kind=kind, model=model, oref=OREF, extra=extra,
                                generation_type="t2v")
    except VibesError as e:
        print(f"[!] {e}")
        return
    if not items:
        print("[!] no result — try another prompt")
        return
    n_ok = sum(1 for it in items if not it.get("error"))
    print(f"[*] {n_ok}/{len(items)} finished in {time.time()-t0:.0f}s")
    download_all(items, kind=kind)

def set_ref(path):
    global OREF
    if not os.path.exists(path):
        print("[!] file not found:", path)
        return False
    if not ensure():
        return False
    j = C.upload_media(path)
    items = C.project_upload(pid(), j)
    if items:
        OREF = {"mediaEntId": j["mediaEntId"], "cdnUrl": j.get("cdnUrl"),
                "_id": items[0]["id"]}
        print(f"[+] reference set ({os.path.basename(path)}) → type videos now")
        return True
    print("[!] upload failed")
    return False

def _fresh_client(quiet=False, attempts=3):
    """Password-login and return a ready Vibes client (or None)."""
    s = auth.login_session(print_fn=None if quiet else print, attempts=attempts)
    if s is None:
        return None
    auth.save_session(s)
    v = Vibes(s)
    try:
        u = v.me()
        if not quiet:
            print(f"[+] logged in as {u['username']} ({u['id'][:8]}…)")
    except Exception:
        pass
    return v

# ── automatic self-healing session regeneration ──────────────────────────
_auto = {"fails": 0, "last": 0.0}
_AUTO_MAX_FAILS = 5          # stop after N consecutive failures per run
_AUTO_BACKOFF = 30           # seconds between attempts, grows each fail

def _auto_login():
    """Regenerate a dead/missing session automatically: password flow
    (seeded device cookies make it look like the same browser), then save
    + re-embed the new cookie. Backs off between failed attempts."""
    global C
    now = time.time()
    if _auto["fails"] >= _AUTO_MAX_FAILS:
        return False
    wait = _auto["fails"] * _AUTO_BACKOFF
    if now - _auto["last"] < wait:
        return False
    print("[*] session dead/missing → regenerating automatically…")
    c = None
    try:
        c = _fresh_client(quiet=True, attempts=1)
    except Exception:
        c = None
    if c is not None:
        _auto["fails"], _auto["last"] = 0, 0.0
        C = c
        try:
            u = c.me()
            print(f"[+] auto-relogin OK as {u['username']} — new session saved & embedded")
        except Exception:
            print("[+] auto-relogin OK — new session saved & embedded")
        return True
    _auto["fails"] += 1
    _auto["last"] = time.time()
    if _auto["fails"] >= _AUTO_MAX_FAILS:
        print("[!] auto-login blocked repeatedly by Meta (device checkpoint).")
        print("    ONE-TIME fix: log out & back in at https://auth.meta.com in your normal")
        print("    browser, then rerun — from then on this script self-heals on its own.")
        print("    Instant alternative: /cookie <meta_session value> from any logged-in browser.")
    else:
        print(f"[.] auto-relogin failed ({_auto['fails']}/{_AUTO_MAX_FAILS}) — will retry in "
              f"{_auto['fails'] * _AUTO_BACKOFF}s or on your next command")
    return False

def dispatch(op, arg):
    global CUR, OREF, AUDIO, C
    v = C
    if op == "/login":
        c = _fresh_client()
        if c is not None:
            C = c
    elif op == "/cookie":
        val = arg.strip().strip('"').strip("'")
        if not val:
            print("usage: /cookie <meta_session value>")
            print("  copy the meta_session cookie from any logged-in vibes.ai browser")
            print("  (DevTools → Application → Cookies → vibes.ai)")
            return
        s = requests.Session(impersonate=IMPERSONATE)
        s.cookies.set("meta_session", val, domain=".vibes.ai", path="/")
        s.cookies.set("cookie_ack", "true", domain=".vibes.ai", path="/")
        probe = Vibes(s)
        try:
            u = probe.me()
        except Exception as e:
            print("[!] cookie rejected:", str(e)[:140])
            return
        auth.save_session(s)
        C = probe
        print(f"[+] logged in as {u['username']} ({u['id'][:8]}…) — session saved")
    elif op == "/logout":
        auth.clear_session()
        print("[.] session cleared")
    elif op == "/me":
        u = v.me()
        print(json.dumps({k: u.get(k) for k in ("id", "username", "abraUserId", "accountStatus")}, indent=2))
        kp = u.get("kadabraProfile") or {}
        print(f"    kadabra: {kp.get('kadabraProfileUsername')} id={kp.get('kadabraProfileId')}")
    elif op == "/status":
        print(json.dumps(v.system_status())[:200])
    elif op == "/geo":
        print(json.dumps(v.geo())[:200])
    elif op == "/new":
        if arg:
            p = v.create_project(name=arg)
            if p:
                CUR = p
                print(f"[+] current project: {p.get('id')}  {p.get('name')}")
        else:
            p = v.create_project()
            if p:
                CUR = p
                print(f"[+] current project: {p.get('id')}  {p.get('name')}")
    elif op in ("/projects", "/list"):
        for i, p in enumerate(v.list_projects()):
            print(f"  [{i}] {p['id']}  {p.get('name')}  thumb={bool(p.get('thumbnailUrl'))}")
    elif op == "/use":
        projects = v.list_projects()
        if not arg:
            for i, p in enumerate(projects):
                print(f"  [{i}] {p['id']}  {p.get('name')}  {fmt_iso(p.get('createdAt'))}")
            return
        p = _pick(projects, arg)
        if p:
            CUR = p
            print(f"[+] current project: {p.get('id')}  {p.get('name')}")
        else:
            print("[!] project not found")
    elif op == "/rename":
        parts = arg.split(None, 1)
        if not parts:
            print("[!] usage: /rename <id|idx> <newname>")
            return
        p = _pick(v.list_projects(), parts[0])
        if not p:
            print("[!] project not found:", parts[0])
            return
        name = parts[1] if len(parts) > 1 else p.get("name")
        np = v.rename_project(p["id"], name)
        if np:
            CUR = np
            print(f"[+] renamed → {np.get('name')}")
        else:
            print("[!] rename rejected by server")
    elif op == "/dup":
        if not CUR:
            print("[!] no current project")
            return
        p = v.duplicate_project(CUR["id"])
        if p:
            print(f"[+] duplicated → {p.get('id')}  {p.get('name')}")
    elif op == "/delete":
        if not CUR:
            print("[!] no current project; select with /use")
            return
        v.delete_project(CUR["id"])
        print(f"[+] deleted {CUR['id']}")
        CUR = None
    elif op == "/assets":
        if not CUR:
            print("[!] no current project")
            return
        for a in v.project_assets(pid()):
            print(f"  {a.get('contentItemId','')[:26]:26s} [{a.get('relationship')}] "
                  f"{a.get('type')}  vid={bool(a.get('videoUrl'))}")
    elif op == "/export":
        if not CUR:
            print("[!] no current project")
            return
        print("pending export:", v.project_timeline_export_pending(pid()))
    elif op == "/content":
        items = v.content_items(limit=40)
        print(f"{len(items)} content items")
        for it in items:
            fl = "♥" if it.get("isFavorited") else "·"
            lip = " lipsync" if it.get("isLipsync") else ""
            print(f"  {fl} {it.get('type',''):6s} {str(it.get('id',''))[:26]:26} {lip}")
    elif op == "/media":
        args = arg.split()
        types_ = None
        for a in args:
            if a.lower() in ("video", "images", "audio", "gallery", "media"):
                types_ = (types_ + "," if types_ else "") + a.lower()
        types_ = types_ or "video,images,audio"
        items, _ = v.media_library(types=types_, limit=50)
        print(f"media library ({types_}) · {len(items)}")
        for it in items:
            url = it.get("fullUrl") or it.get("thumbnailUrl") or ""
            fav = "♥" if it.get("isFavorited") else "·"
            print(f"  {fav} {str(it.get('id',''))[:26]:24} "
                  f"{str(it.get('type',''))[:8]:8} {url.split('?')[0][:50]}")
    elif op == "/fav":
        if not arg:
            print("[!] usage: /fav <itemId>")
            return
        print(v.set_favorite(arg, True))
    elif op == "/unfav":
        if not arg:
            print("[!] usage: /unfav <itemId>")
            return
        print(v.set_favorite(arg, False))
    elif op == "/del":
        if not arg:
            print("[!] usage: /del <itemId>")
            return
        print(v.delete_content_item(arg))
    elif op == "/delbatch":
        if not arg:
            print("[!] usage: /delbatch <batchId>")
            return
        print(v.delete_batch(arg))
    elif op == "/voices":
        for vo in v.studio_voices():
            print(f"  {vo['id'].ljust(30)} {vo.get('name','')[:18]:16} {vo.get('description','')[:42]}")
    elif op == "/ing":
        owner = "LIBRARY"
        if arg:
            owner = arg.upper()
        ing, info = v.studio_ingredients(owner)
        print(f"ingredients [{owner}]")
        for it in ing[:40]:
            print(f"  {it.get('ingredientType','?')[:9]:9s} {it.get('name','')[:40]:40}  {it.get('ingredientId')}")
        if info and info.get("hasNextPage"):
            print("  …more (endCursor)", info.get("endCursor"))
    elif op == "/dl":
        parts = arg.split(None, 1)
        if not parts or not parts[0]:
            print("[!] usage: /dl <url> [filename]")
            return
        out = None
        if len(parts) > 1:
            out = os.path.join(HERE, "media", parts[1])
        p = v.download(parts[0], out=out)
        print("[+] saved", p)
    elif op == "/ref":
        if not arg:
            print("[!] usage: /ref <imagefile>")
            return
        if not ensure():
            return
        set_ref(arg)
    elif op == "/aud":
        if not arg:
            print("[!] usage: /au <audiofile>")
            return
        global AUDIO
        j = C.upload_audio_direct(arg)
        AUDIO = {"mediaEntId": j["mediaEntId"], "cdnUrl": j.get("cdnUrl")}
        print(f"[+] audio ready {AUDIO['mediaEntId']}")
    elif op == "/lipsync":
        if not arg:
            print("[!] usage: /lipsync <prompt> (needs /ref or /au set)")
            return
        if not ensure():
            return
        bid = C.make_batch(CUR, arg, n=1, aspect="9:16", oref=OREF)
        extra = {}
        if OREF:
            extra["sourceContentItemIds"] = [{"id": OREF["_id"], "source": "image"}]
        if AUDIO:
            extra["audioMediaEntId"] = AUDIO["mediaEntId"]
        res = C.generate_videos(CUR, arg, bid, n=1, oref=OREF, extra=extra)
        if isinstance(res, dict) and res.get("success"):
            print("[*] queued (lipsync)")
        else:
            print("[!] lipsync failed", res)
    elif op == "/img":
        if not arg:
            print("[!] usage: /img <prompt>")
            return
        gen(arg, kind="images", n=1, aspect="1:1")
    elif op == "/v":
        if not arg:
            print("[!] usage: /v <prompt> [n <n>] [aspect <ratio>] [res <x>] [model <x>]")
            return
        tokens = arg.split()
        prompt = []
        n = 1
        aspect = "9:16"
        res = "480p"
        model = None
        i = 0
        while i < len(tokens):
            t = tokens[i]
            m = None
            for key, name in (("n", "n"), ("count", "n"), ("aspect", "aspect"),
                              ("res", "res"), ("resolution", "res"), ("model", "model")):
                if t.startswith(key + "="):
                    m = (name, t[len(key) + 1:], 1)
                    break
                if t == key:
                    m = (name, None, 2)
                    break
            if m:
                name, val, step = m
                if val is None:
                    if i + 1 >= len(tokens):
                        print(f"[!] missing value for {key}")
                        return
                    val = tokens[i + 1]
                if name == "n":
                    n = max(1, min(40, int(val)))
                elif name == "aspect":
                    aspect = val
                elif name == "res":
                    res = val
                else:
                    model = val
                i += step
                continue
            prompt.append(t)
            i += 1
        gen(" ".join(prompt), n=n, aspect=aspect, resolution=res, model=model)
    elif op == "/sync":
        if not CUR:
            print("[!] no current project")
            return
        print(v.sync(entity_id=pid()))
    elif op == "/sync_stream":
        if not CUR:
            print("[!] no current project")
            return
        print(v.sync_stream(entity_id=pid()))
    elif op == "/search":
        if not arg:
            print("[!] usage: /search <query>  (searches projects + ingredients + media)")
            return
        print(f"projects matching '{arg}':")
        for p in v.list_projects(search=arg):
            print(f"  {p['id']} {p.get('name')}")
        print(f"ingredients matching '{arg}':")
        ing, _ = v.studio_ingredients_search(arg)
        for it in ing[:20]:
            print(f"  {it.get('ingredientId')} {it.get('name')}")
        print(f"media search via library favorites? use /media")
    elif op == "/bulkdel":
        if not arg:
            print("[!] usage: /bulkdel <id1,id2,...>")
            return
        ids = [x.strip() for x in arg.split(",") if x.strip()]
        print(v.bulk_delete_items(ids))
    elif op == "/composition":
        if not CUR:
            print("[!] no current project")
            return
        if arg:
            try:
                comp = json.loads(arg)
            except Exception:
                comp = {"custom": arg}
            print(v.save_composition(pid(), comp))
        else:
            proj = v.get_project(pid())
            print(json.dumps(proj.get("composition", {}) if proj else {}, indent=2)[:2000])
    elif op == "/exportnow":
        if not CUR:
            print("[!] no current project")
            return
        print(v.timeline_export(pid()))
    elif op == "/ingredient":
        if not arg:
            print("[!] usage: /ingredient <name> [type=CHARACTER|STYLE|SCENE] [media=<file>]")
            return
        # parse name and type
        parts = arg.split()
        name = parts[0]
        itype = "CHARACTER"
        for p in parts[1:]:
            if p.startswith("type="):
                itype = p.split("=",1)[1].upper()
        print(v.create_ingredient(name, ingredient_type=itype))
    elif op == "/edit":
        if not arg:
            print("[!] usage: /edit <contentId> <prompt>  (edit/restyle video)")
            return
        cid, _, prompt = arg.partition(" ")
        if not cid or not prompt.strip():
            print("[!] need both contentId and prompt")
            return
        if not ensure():
            return
        bid = C.make_batch(CUR, prompt, n=1, aspect="9:16", batch_type="videos")
        res = C.generate_edit(CUR, bid, prompt.strip(), source_content_id=cid)
        print(res)
        try:
            items = C.wait_generation(bid, project=CUR)
            download_all(items)
        except Exception as e:
            print(f"[!] wait failed: {e}")
    elif op == "/exportall":
        pass
    elif op == "/help":
        print(__doc__.split("Commands:")[0].strip() if False else __doc__)
    elif op == "/quit":
        print("bye")
        sys.exit(0)
    else:
        print(f"[?] unknown command {op}; /help for the list")

def repl():
    global C
    C = None
    s = auth.load_session()
    if s:
        C = Vibes(s)
        try:
            u = C.me()
            print(f"[+] session: {u['username']}")
            try:
                auth.save_session(C.s)   # persist any rotation, keep embedded copy fresh
            except Exception:
                pass
        except Exception:
            C = None
            print("[.] saved session rejected")
            _auto_login()            # self-heal right away
    else:
        print("[.] no session found yet")
        _auto_login()                # self-heal right away
    print("> type a prompt → generate videos (auto-save to vibes/media/)")
    print("> /help  /projects  /media  /voices  /img  /v  /ref /  /upload  /dl  /dup…")
    consecutive_errors = 0
    while True:
        try:
            raw = input("vibes> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        except Exception as e:
            VLOOG.error(f"input error: {e}")
            consecutive_errors += 1
            if consecutive_errors > 10:
                print("[!] too many input errors, exiting safely")
                break
            time.sleep(0.5)
            continue
        if not raw:
            continue
        if len(raw) > 60000:
            print(f"[!] input too long {len(raw)} chars, truncated")
            raw = raw[:60000]
            VLOOG.warning(f"input truncated {len(raw)}")
        consecutive_errors = 0
        if raw.startswith("/"):
            parts = raw.split(None, 1)
            op, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        else:
            op, arg = raw, ""          # plain text = prompt → video
        if op not in ("/login", "/cookie", "/quit", "/exit", "/help") and C is None:
            if not _auto_login():      # try regenerating before giving up
                print("[!] still no session — /cookie <value> or /login")
                continue
        done = False
        for attempt in range(2):       # one automatic recovery retry on 401
            try:
                if op.startswith("/"):
                    dispatch(op, arg)
                else:
                    gen(raw)
                done = True
                break
            except VibesError as e:
                VLOOG.warning(f"VibesError {op}: {e} status={getattr(e,'status',None)}")
                print("[!]", e)
                if getattr(e, "status", None) == 401 and attempt == 0:
                    C = None
                    try:
                        if _auto_login():
                            continue       # session regenerated → retry the command
                    except Exception as ae:
                        VLOOG.error(f"_auto_login: {ae}")
                done = True
                break
            except Exception as e:
                VLOOG.error(f"dispatch {op} {type(e).__name__}: {e}\n{traceback.format_exc()}")
                print("[!]", type(e).__name__, e)
                consecutive_errors += 1
                if consecutive_errors > 20:
                    print("[!] too many errors, pausing 5s")
                    time.sleep(5)
                    consecutive_errors = 0
                done = True
                break


if __name__ == "__main__":
    try:
        # fix Windows stdout encoding (only when actually running the REPL)
        if not (getattr(sys.stdout, "encoding", None) or "").lower().startswith("utf"):
            try:
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            except Exception:
                pass
        try:
            repl()
        except KeyboardInterrupt:
            print("\n[*] bye!")
        except Exception as e:
            VLOOG.error(f"repl fatal: {e}\n{traceback.format_exc()}")
            print(f"[!] repl crashed: {e} — restarting...")
            try:
                time.sleep(1)
                repl()
            except Exception as e2:
                VLOOG.error(f"second repl crash: {e2}\n{traceback.format_exc()}")
                print(f"[!] second crash: {e2} — exiting safely")
                sys.exit(0)
    except Exception as e:
        try:
            VLOOG.error(f"__main__ fatal: {e}\n{traceback.format_exc()}")
        except: pass
        print(f"[!] fatal: {e}")
        try:
            # try tokenless repl still? just exit gracefully
            sys.exit(0)
        except:
            sys.exit(1)
