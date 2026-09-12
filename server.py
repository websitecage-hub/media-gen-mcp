#!/usr/bin/env python3
"""Media generation MCP server (images via meta.ai, videos via vibes.ai).

Exposes MCP tools over Streamable HTTP at /mcp so Claude/ChatGPT can call:
  images/videos/lipsync/library  -> vibes.ai & meta.ai backends
  meta.ai full suite             -> chat sandbox files, image editing,
                                    transparent assets, GIFs, web/social/
                                    places search, deep research

Media delivery: generated media is embedded directly in the MCP response as
image content blocks (renders inline in Claude) AND returned as public CDN
URLs (download links). Nothing is stored on this server.

HTTP endpoints:
  GET /         -> info page
  GET /health   -> keep-alive probe for UptimeRobot (GET and HEAD)
  POST /mcp     -> MCP Streamable HTTP transport

Storage hygiene: a background janitor deletes stray temp/media files hourly,
so Render's free disk can never fill up.
"""
import asyncio
import base64
import glob
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid

import meta as meta_client
import vibes as vibes_mod

from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, ImageContent

START_TIME = time.time()

MAX_EMBED_BYTES = 4_500_000   # stay under MCP message limits

# ── shared helpers ────────────────────────────────────────────────────────


def _download_temp(url, suffix=".bin"):
    """Fetch a remote media file to a temp path."""
    from curl_cffi import requests as _rq
    r = _rq.get(url, timeout=120, impersonate="chrome")
    if r.status_code != 200 or len(r.content) < 256:
        raise RuntimeError(f"cannot fetch {url[:80]} ({r.status_code})")
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.write(fd, r.content)
    os.close(fd)
    return tmp


_EXT_MIME = {"jpg": ".jpg", "jpeg": ".jpg", "png": ".png", "webp": ".webp",
             "gif": ".gif", "mp4": ".mp4", "mov": ".mov", "webm": ".webm",
             "mp3": ".mp3", "wav": ".wav", "m4a": ".m4a", "ogg": ".ogg"}


def _suffix_for_url(url):
    base = url.split("?")[0]
    ext = os.path.splitext(base)[1].lower().lstrip(".")
    return _EXT_MIME.get(ext, ".bin")


def _embed_media_block(url):
    """Fetch a public CDN URL and build an ImageContent block (inline render).
    Returns None when the file is too big or not an embeddable type."""
    try:
        from curl_cffi import requests as _rq
        r = _rq.get(url, timeout=90, impersonate="chrome")
        if r.status_code != 200 or len(r.content) > MAX_EMBED_BYTES:
            return None
        mime = (r.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/") or "svg" in mime:
            return None
        return ImageContent(type="image",
                            data=base64.b64encode(r.content).decode(),
                            mimeType=mime)
    except Exception:  # noqa: BLE001
        return None


def _media_response(header, urls, note=""):
    """Text with clean download links + inline-embedded image blocks."""
    lines = [header]
    for u in urls:
        name = os.path.basename(u.split("?")[0]) or "media"
        lines.append(f"- [{name}]({u})")
    if note:
        lines.append(f"\n{note}")
    blocks = [TextContent(type="text", text="\n".join(lines))]
    for u in urls[:3]:
        blk = _embed_media_block(u)
        if blk is not None:
            blocks.append(blk)
    return blocks

# ── meta.ai helpers ───────────────────────────────────────────────────────


async def meta_ask(message, mode="instant", timeout=240, attachments=None,
                   attempts=2, conversation_id=None, account=1):
    """Full-capability meta.ai passthrough. Reuses conversation_id for context.
    account=1 uses META_TOKEN, account=2 uses META_TOKEN_2 (second account)."""
    if int(account or 1) == 2:
        token = meta_client.load_token_2()
        if not token:
            raise RuntimeError("no META_TOKEN_2 configured for image account 2")
    else:
        token = meta_client.load_token()
        if not token:
            raise RuntimeError("no META_TOKEN available")
    conv = conversation_id or str(uuid.uuid4())
    last_err = None
    for attempt in range(attempts):
        sess = meta_client.DGWSession(token, conv, download_media=False)
        try:
            await sess.connect(timeout=20)
            text, media = await sess.ask(message, mode=mode, timeout=timeout,
                                         attachments=attachments)
            urls = [{"url": m.get("url"), "mime_type": m.get("mime_type", ""),
                     "filename": m.get("filename", "")}
                    for m in media if m.get("url")]
            if text.strip() or urls:
                return {"text": meta_client.clean_chunk(text.strip()),
                        "media": urls}
            last_err = RuntimeError("empty response from meta.ai")
        except Exception as e:  # noqa: BLE001
            last_err = e
        finally:
            await sess.close()
        await asyncio.sleep(1.5)
    raise RuntimeError(f"meta.ai request failed: {last_err}")


def _meta_attachment_from_url(url):
    """Download a remote file and upload it to meta.ai rupload -> attachment."""
    import mimetypes
    tmp = _download_temp(url, _suffix_for_url(url))
    try:
        token = meta_client.load_token()
        media_id = meta_client.upload_file(tmp, token)
        fname = os.path.basename(url.split("?")[0]) or "upload"
        return {"media_id": media_id,
                "mime_type": mimetypes.guess_type(tmp)[0]
                or "application/octet-stream",
                "filename": fname}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

# ── vibes.ai helpers ──────────────────────────────────────────────────────

_vibes_lock = threading.Lock()
_vibes_client = None


def _session_cookie_candidates():
    """All distinct meta_session values available (env, file, embedded)."""
    vals = []
    env = os.environ.get("VIBES_SESSION", "").strip()
    if env:
        vals.append(env)
    sf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "session.json")
    try:
        data = json.load(open(sf, encoding="utf-8"))
        for c in data.get("cookies", []):
            if c.get("name") == "meta_session":
                v = c.get("value")
                if v:
                    vals.append(v)
        if data.get("meta_session"):
            vals.append(data["meta_session"])
    except Exception:  # noqa: BLE001
        pass
    raw = getattr(vibes_mod, "_SESSION_PAYLOAD", "")
    import re as _re
    m = _re.search(r"#_SP_BEGIN\s*(.*?)\s*#_SP_END", raw, _re.S) or \
        (_re.search(r"\{.*\}", raw, _re.S))
    if m:
        try:
            j = json.loads(m.group(1) if "#_SP_BEGIN" in raw else m.group(0))
            for c in j.get("cookies", []):
                if c.get("name") == "meta_session":
                    v = c.get("value")
                    if v:
                        vals.append(v)
            if j.get("meta_session"):
                vals.append(j["meta_session"])
        except Exception:  # noqa: BLE001
            pass
    seen = set()
    out = []
    for v in vals:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _try_vibes_cookie(value):
    from curl_cffi import requests as _rq
    s = _rq.Session(impersonate="chrome")
    s.cookies.set("meta_session", value, domain=".vibes.ai", path="/")
    s.cookies.set("cookie_ack", "true", domain=".vibes.ai", path="/")
    return s


def _probe_vibes(v):
    """Classify a vibes session without starting a new password login here.

    Returns 'ok', 'auth' (401/403: cookie rejected), or 'transport' (network/
    timeout/5xx: keep the saved session, do NOT password-login for this).
    """
    try:
        v.me()
        return "ok"
    except vibes_mod.VibesError as e:
        return "auth" if getattr(e, "status", None) in (401, 403) else "transport"
    except Exception:  # noqa: BLE001
        return "transport"


def _get_vibes():
    global _vibes_client
    with _vibes_lock:
        saw_auth = False
        # reuse verified client; a transport blip must not look like a logout
        if _vibes_client is not None:
            st = _probe_vibes(_vibes_client)
            if st == "ok":
                return _vibes_client
            if st == "auth":
                saw_auth = True
            else:
                try:
                    _vibes_client._rebuild_session()
                    st = _probe_vibes(_vibes_client)
                    if st == "ok":
                        return _vibes_client
                    if st == "auth":
                        saw_auth = True
                except Exception:  # noqa: BLE001
                    pass
        # 1) full materialized session (all device cookies) — most reliable.
        #    Probe with reauth=False so verification itself never starts a
        #    password login; the single quiet login below is the only one.
        s = vibes_mod.auth.load_session()
        if s is not None:
            st = _probe_vibes(vibes_mod.Vibes(s, reauth=False))
            if st == "ok":
                _vibes_client = vibes_mod.Vibes(s, reauth=True)
                return _vibes_client
            if st == "auth":
                saw_auth = True
        if not saw_auth:
            raise RuntimeError("temporary vibes.ai transport failure — kept the saved session; no password login attempted")
        # 2) fall back to any distinct meta_session candidate (env/file/embedded)
        #    — recovers when the materialized jar is poisoned but a raw cookie
        #    value still works.
        for val in _session_cookie_candidates():
            try:
                s2 = _try_vibes_cookie(val)
                if _probe_vibes(vibes_mod.Vibes(s2, reauth=False)) == "ok":
                    _vibes_client = vibes_mod.Vibes(s2, reauth=True)
                    return _vibes_client
            except Exception:  # noqa: BLE001
                continue
        try:
            st = vibes_mod.auth.login_status()
        except Exception:  # noqa: BLE001
            st = {}
        if st.get("checkpoint_active"):
            raise RuntimeError(f"Meta checkpoint active; automatic logins paused ({st.get('checkpoint_error', '')}) — verify once at auth.meta.com, then retry")
        # 3) exactly one quiet password login (no send-nonce email step)
        if hasattr(vibes_mod, "_fresh_client"):
            v = vibes_mod._fresh_client(quiet=True, attempts=1)
            if v is not None:
                _vibes_client = v
                return _vibes_client
        s = vibes_mod.auth.login_session(print_fn=lambda *a: print("[vibes-auth]", *a), attempts=1)
        if s is not None:
            vibes_mod.auth.save_session(s)
            _vibes_client = vibes_mod.Vibes(s, reauth=True)
            return _vibes_client
        try:
            st = vibes_mod.auth.login_status()
        except Exception:  # noqa: BLE001
            st = {}
        reason = (st.get("last_reason") or "login failed").strip()
        raise RuntimeError(f"no working vibes.ai session ({reason}) — POST the fresh browser cookie to /api/session, or re-login at auth.meta.com")


def _reset_vibes():
    global _vibes_client
    with _vibes_lock:
        _vibes_client = None


def _ensure_project(v):
    p = v.create_project(name="mcp-media")
    if not p:
        projects = v.list_projects(limit=5)
        p = projects[0] if projects else None
    if not p:
        raise RuntimeError("could not create vibes project")
    return p


def _pick_urls(items, want):
    out = []
    for it in items:
        url = it.get("videoUrl") if want == "video" else (
            it.get("imageUrl") or it.get("videoUrl"))
        thumb = it.get("thumbnailUrl") or ""
        err = it.get("error")
        if url:
            out.append({"url": url, "thumb": thumb,
                        "id": str(it.get("id", ""))[:40],
                        "type": "video" if it.get("videoUrl") else "image"})
        elif err:
            out.append({"error": str(err)[:160]})
    return out


def _upload_ref(v, project, image_path):
    """Upload a local image and register it as project content -> oref dict."""
    media = v.upload_media(image_path)
    citems = v.project_upload(project["id"], media)
    oref = {"mediaEntId": media["mediaEntId"],
            "cdnUrl": media.get("cdnUrl"),
            "_id": citems[0]["id"]}
    return oref


def _vibes_with_fresh_login(fn, *args, **kw):
    """Run a vibes operation; on 401 force a fresh login and retry once."""
    try:
        return fn(*args, **kw)
    except vibes_mod.VibesError as e:
        if e.status == 401:
            _reset_vibes()
            return fn(*args, **kw)
        raise


def _run_vibe_gen(prompt, aspect, resolution, model, n, max_sec,
                  reference_image_url=None, gen_type="t2v", hub_project_id=None, *_, **__):
    v = _get_vibes()
    tmp = None
    try:
        if hub_project_id and _get_hub_project(hub_project_id):
            project = _ensure_hub_vibes_project(hub_project_id, v)
        else:
            project = _ensure_project(v)
        oref = None
        # normalize generation type
        if reference_image_url:
            tmp = _download_temp(reference_image_url,
                                 _suffix_for_url(reference_image_url))
            oref = _upload_ref(v, project, tmp)
            gen_type = "i2v"
        try:
            items, batch_id = v.generate(
                project, prompt, n=max(1, min(4, n)), aspect=aspect,
                resolution=resolution, kind="videos", model=model,
                oref=oref, generation_type=gen_type, max_sec=max_sec)
        except vibes_mod.VibesError as e:
            if e.status == 401:
                _reset_vibes()
            raise RuntimeError(f"vibes generation failed: {e}") from e
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    results = _pick_urls(items, "video")
    if hub_project_id:
        with PROJECTS_LOCK:
            hub = PROJECTS.get(hub_project_id)
            if hub is not None:
                hub["history"].append({"prompt": prompt, "items": results, "at": __import__("time").time()})
    if not results:
        raise RuntimeError("vibes returned no usable items")
    return {"batch_id": batch_id, "items": results}


def vibes_lipsync(source_url, audio_url, prompt="", aspect="9:16",
                  resolution="480p", max_sec=420, hub_project_id=None):
    """Lip-sync: pair an audio track (URL) with a source image/video (URL)."""
    v = _get_vibes()
    tmp_src = tmp_aud = None
    try:
        project = _ensure_project(v)
        tmp_src = _download_temp(source_url, _suffix_for_url(source_url))
        src_media = v.upload_media(tmp_src)
        src_items = v.project_upload(project["id"], src_media)
        src_content_id = src_items[0]["id"]

        tmp_aud = _download_temp(audio_url, _suffix_for_url(audio_url))
        aud = v.upload_audio_direct(tmp_aud)

        bid = v.make_batch(project, prompt or "lip sync", n=1, aspect=aspect)
        res = v.generate_lipsync(project, bid, aud["mediaEntId"],
                                 src_content_id, prompt=prompt or "",
                                 aspect=aspect, resolution=resolution)
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(str(res.get("error"))[:200])
        items = v.wait_generation(bid, want="video", max_sec=max_sec,
                                  project=project)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"lipsync failed: {e}") from e
    finally:
        for t in (tmp_src, tmp_aud):
            if t and os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass
    results = _pick_urls(items, "video")
    if hub_project_id:
        with PROJECTS_LOCK:
            hub = PROJECTS.get(hub_project_id)
            if hub is not None:
                hub["history"].append({"prompt": prompt, "items": results, "at": __import__("time").time()})
    if not results:
        raise RuntimeError("lipsync produced no usable items")
    return {"items": results}


def vibes_list_voices(limit=100):
    v = _get_vibes()
    voices = v.studio_voices(limit=limit)
    return [{"id": vo.get("id"), "name": vo.get("name"),
             "description": (vo.get("description") or "")[:80]}
            for vo in voices]


def vibes_media_library(media_type="video", limit=25):
    v = _get_vibes()
    t = {"video": "video", "image": "images", "images": "images",
         "audio": "audio"}.get((media_type or "video").lower(), "video")
    items, _page = v.media_library(types=t, limit=max(1, min(50, limit)))
    out = []
    for it in items:
        url = it.get("fullUrl") or it.get("videoUrl") or it.get("imageUrl")
        if url:
            out.append({"id": str(it.get("id", ""))[:40],
                        "type": it.get("type"),
                        "url": url.split("?")[0],
                        "favorited": bool(it.get("isFavorited"))})
    return out


def vibes_set_favorite(item_id, is_favorited=True):
    v = _get_vibes()
    j = v.set_favorite(item_id, bool(is_favorited))
    return {"ok": isinstance(j, dict) and j.get("success", True),
            "item_id": item_id, "favorited": bool(is_favorited)}


def vibes_delete_media(item_id):
    v = _get_vibes()
    v.delete_content_item(item_id)
    return {"ok": True, "deleted": item_id}

# ── storage janitor ───────────────────────────────────────────────────────


def _janitor_sweep():
    """Delete stray temp/media files older than 2h; keeps disk forever clean."""
    now = time.time()
    removed = 0
    patterns = ["/tmp/tmp*", "/tmp/*.jpg", "/tmp/*.png", "/tmp/*.mp4",
                "/tmp/*.mp3"]
    root = os.path.dirname(os.path.abspath(__file__))
    for sub in ("media", "downloads"):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            patterns += [os.path.join(d, "*")]
    for pat in patterns:
        for f in glob.glob(pat):
            try:
                if os.path.isfile(f) and now - os.path.getmtime(f) > 7200:
                    os.remove(f)
                    removed += 1
            except OSError:
                pass
    return removed


def _refresh_vibes_session():
    """Keep the session alive WITHOUT forced re-login.

    A full password login every cycle was triggering Meta's 'new device'
    verification email repeatedly. Instead, this only does a light
    authenticated probe (which also makes Vibes rotate/persist any refreshed
    cookie, sliding the session forward). A single quiet password login is
    attempted ONLY after a real 401/403 — never for transport blips, and
    never while a Meta checkpoint is active.
    """
    global _vibes_client
    with _vibes_lock:
        client = _vibes_client
    saw_auth = False
    # 1) in-memory client still good?
    if client is not None:
        st = _probe_vibes(client)
        if st == "ok":
            return True
        if st == "auth":
            saw_auth = True
    # 2) on-disk session still good (reload + probe, no login)
    try:
        s = vibes_mod.auth.load_session()
        if s is not None:
            st = _probe_vibes(vibes_mod.Vibes(s, reauth=False))
            if st == "ok":
                with _vibes_lock:
                    _vibes_client = vibes_mod.Vibes(s, reauth=True)
                return True
            if st == "auth":
                saw_auth = True
    except Exception:  # noqa: BLE001
        pass
    if not saw_auth:
        return False
    try:
        if vibes_mod.auth.login_status().get("checkpoint_active"):
            return False
    except Exception:  # noqa: BLE001
        pass
    # 3) last resort: exactly one quiet password login (no email-OTP step)
    s = vibes_mod.auth.login_session(print_fn=lambda *a: None, attempts=1)
    if s is not None:
        vibes_mod.auth.save_session(s)
        with _vibes_lock:
            _vibes_client = vibes_mod.Vibes(s, reauth=True)
        return True
    return False


async def _session_refresher_loop():
    # Probe often enough that the meta_session cookie slides forward on the
    # authenticated `me()` calls, so it effectively never expires while the
    # service is up. Only on a genuine multi-hour outage would a full
    # password login be needed.
    while True:
        await asyncio.sleep(20 * 60)
        try:
            await run_in_threadpool(_refresh_vibes_session)
        except Exception:  # noqa: BLE001
            pass


async def _janitor_loop():
    while True:
        try:
            await run_in_threadpool(_janitor_sweep)
            now = time.time()
            with _JOBS_LOCK:
                stale = [k for k, v in JOBS.items()
                         if now - v["created"] > 3600]
                for k in stale:
                    JOBS.pop(k, None)
            with PROJECTS_LOCK:
                for pr in PROJECTS.values():
                    if len(pr.get("history", [])) > 80:
                        pr["history"] = pr["history"][-50:]
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3600)

# ── hub projects (persistent context per Image/Video/Animate) ────────────

API_KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.json")
MASTER_API_KEY = os.environ.get("MASTER_API_KEY", os.environ.get("API_KEY", "")).strip()

def _load_api_keys():
    try:
        if os.path.exists(API_KEYS_FILE):
            import json as _js
            return set(_js.load(open(API_KEYS_FILE)).get("keys",[]))
    except: pass
    return set()

def _save_api_keys(keys):
    try:
        tmp=API_KEYS_FILE+".tmp"
        open(tmp,"w").write(__import__("json").dumps({"keys":sorted(keys)},indent=2))
        os.replace(tmp,API_KEYS_FILE)
        return True
    except: return False

PROJECTS: dict = {}
PROJECTS_LOCK = __import__("threading").Lock()


def _hub_projects(kind=None):
    with PROJECTS_LOCK:
        vals = list(PROJECTS.values())
    if kind:
        vals = [pr for pr in vals if pr["kind"] == kind]
    vals.sort(key=lambda x: x["created"], reverse=True)
    return vals


def _get_hub_project(pid):
    with PROJECTS_LOCK:
        return PROJECTS.get(pid)


def _create_hub_project(kind, name):
    import uuid as _uuid
    pid = _uuid.uuid4().hex[:10]
    rec = {"id": pid, "kind": kind, "name": name.strip() or f"{kind.title()} {pid[:4]}",
           "conversation_id": __import__("uuid").uuid4().hex, "vibes_project_id": None,
           "history": [], "created": __import__("time").time()}
    with PROJECTS_LOCK:
        PROJECTS[pid] = rec
    return rec


def _ensure_hub_vibes_project(hub_pid, vibes_client):
    hub = _get_hub_project(hub_pid)
    if hub is None:
        return _ensure_project(vibes_client)
    if hub.get("vibes_project_id"):
        try:
            pr = vibes_client.get_project(hub["vibes_project_id"])
            if pr:
                return pr
        except Exception:
            pass
    pr = _ensure_project(vibes_client)
    with PROJECTS_LOCK:
        if hub["id"] in PROJECTS:
            PROJECTS[hub["id"]]["vibes_project_id"] = pr["id"]
    return pr

# ── async job system (dispatch + poll) ────────────────────────────────────

JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _start_job(kind, fn, *args):
    """Run a long generation in a background thread; return job id at once."""
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "status": "running",
                     "created": time.time(), "result": None}
    def _run():
        try:
            r = fn(*args)
        except Exception as e:  # noqa: BLE001
            r = {"error": str(e)[:400]}
        with _JOBS_LOCK:
            JOBS[jid]["status"] = ("error" if isinstance(r, dict)
                                   and r.get("error") else "done")
            JOBS[jid]["result"] = r
    threading.Thread(target=_run, daemon=True).start()
    return jid


def _job_snapshot(jid):
    with _JOBS_LOCK:
        rec = JOBS.get(jid)
        return dict(rec) if rec else None

# ── MCP server + tools ────────────────────────────────────────────────────

mcp = FastMCP(
    "media-gen", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))

# ── core generation tools ────────────────────────────────────────────────


@mcp.tool()
async def generate_image(prompt: str, project_id: str = "") -> list:
    """CREATE a brand-new IMAGE from a TEXT description using Meta AI.
    ALWAYS use this when the user wants to create/make/generate/draw a
    picture, photo, artwork, logo, poster, sticker or any visual — it works
    fully from text, no source image needed. The result is returned inline
    (visible in chat) plus public download URLs. Takes ~10-30s.

    Args:
        prompt: Full description of the image: subject, style, lighting,
            mood, colors. Example: 'golden retriever puppy wearing
            sunglasses on a beach at sunset, photorealistic'.
    """
    try:
        hub = _get_hub_project(project_id) if project_id else None
        cid = hub["conversation_id"] if hub else None
        res = await meta_ask("/imagine " + prompt, mode="instant", timeout=180, conversation_id=cid)
        if hub:
            with PROJECTS_LOCK:
                hub["history"].append({"prompt": prompt, "urls": [m["url"] for m in res["media"] if m.get("url")], "at": __import__("time").time()})
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No image returned.\n{res['text'][:400]}")]
    return _media_response(f"Generated {len(urls)} image(s):", urls,
                           res["text"][:300])


@mcp.tool()
async def edit_image(image_url: str, instruction: str) -> list:
    """Edit an existing image with Meta AI: change style/background/clothes,
    combine images, restore, colorize, remove objects, etc.

    Args:
        image_url: Public URL of the image to edit.
        instruction: What to change, e.g. 'make the background snowy at night'.
    """
    try:
        att = await run_in_threadpool(_meta_attachment_from_url, image_url)
        res = await meta_ask(instruction, timeout=240, attachments=[att])
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No edited image returned.\n"
                                 f"{res['text'][:400]}")]
    return _media_response("Edited image:", urls)


@mcp.tool()
async def transparent_image(prompt: str) -> list:
    """Generate a logo/sticker/icon/badge/sprite with a TRANSPARENT background.
    Delivered as PNG with alpha channel.

    Args:
        prompt: What to draw, e.g. 'cute rocket mascot logo'.
    """
    msg = ("/imagine " + prompt +
           " - isolated asset on fully transparent background, PNG with "
           "alpha channel, no backdrop, sticker-style cutout, crisp edges")
    try:
        res = await meta_ask(msg, timeout=240)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No asset returned.\n{res['text'][:400]}")]
    return _media_response("Transparent asset(s):", urls)


@mcp.tool()
async def make_gif(prompt: str) -> list:
    """Create an animated GIF / flip-book animation (frame-by-frame).

    Args:
        prompt: What should animate, e.g. 'a cat chasing a laser pointer'.
    """
    msg = ("/imagine " + prompt +
           " - create this as a short animated GIF flip-book of frames")
    try:
        res = await meta_ask(msg, timeout=300)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No GIF returned.\n{res['text'][:400]}")]
    return _media_response("Animated GIF:", urls)


@mcp.tool()
async def generate_video(prompt: str, aspect_ratio: str = "9:16",
                         resolution: str = "480p", model: str = "",
                         count: int = 1, reference_image_url: str = "",
                         project_id: str = "") -> str:
    """CREATE a brand-new VIDEO from a TEXT description (no image needed).
    Use this whenever the user wants a new video made from scratch.
    For an ad/clip/scene: just describe it. Animating an existing photo?
    Put the photo URL in reference_image_url.

    ASYNC: returns a job_id IMMEDIATELY. Then call check_generation(job_id)
    every ~20s until status is "done" (takes 1-5 min).

    Args:
        prompt: Description of the video to create (subject, motion, style).
        aspect_ratio: One of 9:16, 16:9, 1:1, 4:5, 3:4, 4:3.
        resolution: 480p (fast), 720p, or 1080p (slowest).
        model: Optional: midjen-short (fast), midjen-long, midjen, sora, veo.
        count: How many variations (1-4).
        reference_image_url: Optional. Public URL of an image to animate
            (image-to-video). Leave empty for pure text-to-video.
        project_id: Optional hub project id to keep videos in one series.
    """
    jid = _start_job("video", _vibes_with_fresh_login, _run_vibe_gen, prompt,
                     aspect_ratio, resolution, model or None,
                     max(1, min(4, count)), 420,
                     reference_image_url or None, "t2v", project_id or None)
    return (f"Video generation STARTED.\njob_id: {jid}\n"
            f"NEXT STEP: call check_generation(\"{jid}\") — wait ~20 seconds "
            f"between calls until status is \"done\" (typically 60-240s).")


@mcp.tool()
async def animate_image(image_url: str, prompt: str = "",
                        aspect_ratio: str = "9:16",
                        resolution: str = "480p",
                        project_id: str = "") -> str:
    """Turn an EXISTING image into a short video (image-to-video).

    ASYNC: returns a job_id IMMEDIATELY. Then call check_generation(job_id)
    every ~20s until status is "done" (takes 1-5 min).

    Args:
        image_url: Public URL of the source image to animate.
        prompt: How the scene should move (optional).
        aspect_ratio: Output ratio (9:16 default).
        resolution: 480p, 720p, or 1080p.
    """
    jid = _start_job("animate", _vibes_with_fresh_login, _run_vibe_gen,
                     prompt or "animate this image", aspect_ratio,
                     resolution, None, 1, 420, image_url, "i2v",
                     project_id or None)
    return (f"Image animation STARTED.\njob_id: {jid}\n"
            f"NEXT STEP: call check_generation(\"{jid}\") — wait ~20 seconds "
            f"between calls until status is \"done\".")


@mcp.tool()
async def create_lipsync(source_url: str, audio_url: str,
                         prompt: str = "", aspect_ratio: str = "9:16",
                         resolution: str = "480p") -> str:
    """Lip-sync a source face/image/video to an audio track.

    ASYNC: returns a job_id IMMEDIATELY. Then call check_generation(job_id)
    every ~20s until status is "done" (takes 2-7 min).

    Args:
        source_url: Public URL of the face/source image or video.
        audio_url: Public URL of the audio (mp3/wav/m4a).
        prompt: Optional hint text.
        aspect_ratio: Output ratio (9:16 default).
        resolution: 480p, 720p, or 1080p.
    """
    jid = _start_job("lipsync", _vibes_with_fresh_login, vibes_lipsync,
                     source_url, audio_url, prompt, aspect_ratio, resolution)
    return (f"Lipsync STARTED.\njob_id: {jid}\n"
            f"NEXT STEP: call check_generation(\"{jid}\") — wait ~30 seconds "
            f"between calls until status is \"done\".")


@mcp.tool()
async def check_generation(job_id: str) -> list:
    """Check the result of a video/animation/lipsync job started with
    generate_video, animate_image or create_lipsync.

    Args:
        job_id: The job_id returned when the generation was started.
    """
    rec = _job_snapshot(job_id)
    if rec is None:
        return [TextContent(type="text",
                            text=f"ERROR: unknown job_id {job_id} "
                                 f"(jobs expire after 1 hour).")]
    if rec["status"] == "running":
        waited = int(time.time() - rec["created"])
        return [TextContent(type="text",
                            text=f"status: running ({waited}s elapsed). "
                                 f"Call check_generation again in ~20s.")]
    if rec["status"] == "error":
        return [TextContent(type="text",
                            text=f"status: FAILED\n{rec['result'].get('error', '')}")]
    items = rec["result"].get("items", [])
    ok = [i for i in items if "url" in i]
    bad = [i for i in items if "error" in i]
    lines = ["status: DONE"]
    thumbs = []
    for i in ok:
        kind = i.get("type", "media")
        name = os.path.basename(i["url"].split("?")[0]) or f"{kind}"
        lines.append(f"- [{name} ({kind})]({i['url']})")
        if i.get("thumb"):
            thumbs.append(i["thumb"])
    for i in bad:
        lines.append(f"- FAILED item: {i['error']}")
    blocks = [TextContent(type="text", text="\n".join(lines))]
    for t in thumbs[:2]:
        blk = _embed_media_block(t)
        if blk is not None:
            blocks.append(blk)
    return blocks

# ── meta.ai capability suite ─────────────────────────────────────────────


@mcp.tool()
async def meta_chat(message: str) -> list:
    """Talk to Meta AI with its FULL capability set. Use for anything the
    other tools don't cover: running Python analysis on data you describe,
    building documents/slides/PDFs (returned as downloadable file links),
    converting media formats, answering questions, math, code, etc.

    Args:
        message: What you want Meta AI to do. Any files it produces come
            back as public download URLs.
    """
    try:
        res = await meta_ask(message, timeout=300)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [f"{m.get('filename') or 'file'}: {m['url']}"
            for m in res["media"] if m.get("url")]
    text = res["text"]
    if urls:
        text += "\n\nFiles produced:\n- " + "\n- ".join(urls)
    blocks = [TextContent(type="text", text=text or "(empty response)")]
    img_urls = [m["url"] for m in res["media"]
                if m.get("url") and m.get("mime_type", "").startswith("image/")]
    for u in img_urls[:2]:
        blk = _embed_media_block(u)
        if blk is not None:
            blocks.append(blk)
    return blocks


@mcp.tool()
async def web_search(query: str) -> str:
    """Search the live web via Meta AI with citations. Good for current facts,
    news, prices, schedules, scores, docs, product specs.

    Args:
        query: What to search for.
    """
    try:
        res = await meta_ask(
            f"Search the web and answer with sources cited:\n{query}",
            timeout=240)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:8000]


@mcp.tool()
async def deep_research(topic: str) -> str:
    """Deep-research a topic via Meta AI: multiple search rounds, synthesis,
    formal structured report. Slower (~2-5 min) but thorough.

    Args:
        topic: The subject to research in depth.
    """
    try:
        res = await meta_ask(
            f"Do deep research on this topic: use multiple searches, compare "
            f"sources, then write a formal structured report with sections "
            f"and citations.\nTopic: {topic}", timeout=600)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:20000]


@mcp.tool()
async def social_search(query: str) -> str:
    """Semantic-search public social posts (Instagram/Facebook/Threads) via
    Meta AI. Great for trends, real people's recommendations, aesthetics.

    Args:
        query: What to look for, e.g. 'best budget espresso machines people
        recommend' or '@username recent posts'.
    """
    try:
        res = await meta_ask(
            f"Search public social posts (Instagram, Facebook, Threads) and "
            f"summarize what people are saying, citing accounts/posts:"
            f"\n{query}", timeout=240)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:8000]


@mcp.tool()
async def places_search(query: str) -> str:
    """Search real places (restaurants, cafes, gyms, shops, parks...) via
    Meta AI's Places graph: names, addresses, hours, price levels.

    Args:
        query: What and where, e.g. 'best ramen near Shibuya Tokyo'.
    """
    try:
        res = await meta_ask(
            f"Find real places matching this and give name/address/hours/"
            f"price level for each:\n{query}", timeout=240)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:8000]

# ── library management tools ─────────────────────────────────────────────


@mcp.tool()
async def list_voices() -> str:
    """List available TTS voices for lip-sync/voiceover on Vibes AI."""
    try:
        voices = await run_in_threadpool(vibes_list_voices)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if not voices:
        return "No voices available."
    lines = ["Available voices:"]
    lines += [f"- {v['id']}  ({v.get('name', '?')}) {v.get('description', '')}"
              for v in voices[:50]]
    return "\n".join(lines)


@mcp.tool()
async def media_library(media_type: str = "video", limit: int = 25) -> str:
    """List previously generated media with their URLs and item IDs.

    Args:
        media_type: 'video', 'image', or 'audio'.
        limit: Max items to return (1-50).
    """
    try:
        items = await run_in_threadpool(vibes_media_library, media_type, limit)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if not items:
        return f"No {media_type} media found."
    lines = [f"{len(items)} item(s):"]
    for it in items:
        fav = " ♥" if it["favorited"] else ""
        lines.append(f"- [{it['id']}] {it['type']}{fav}\n  {it['url']}")
    return "\n".join(lines)


@mcp.tool()
async def favorite_media(item_id: str, is_favorited: bool = True) -> str:
    """Favorite or unfavorite a generated media item by its ID.

    Args:
        item_id: The content item ID (from generate_* or media_library).
        is_favorited: True to favorite, False to unfavorite.
    """
    try:
        res = await run_in_threadpool(vibes_set_favorite, item_id, is_favorited)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    state = "favorited" if res["favorited"] else "unfavorited"
    return f"Item {res['item_id']} {state}." if res["ok"] else "Server rejected."


@mcp.tool()
async def delete_media(item_id: str) -> str:
    """Permanently delete a generated media item by its ID.

    Args:
        item_id: The content item ID (from generate_* or media_library).
    """
    try:
        res = await run_in_threadpool(vibes_delete_media, item_id)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return f"Deleted {res['deleted']}."


@mcp.tool()
async def media_status() -> str:
    """Check whether the image (Meta AI) and video (Vibes AI) backends are working."""
    parts = []
    tok = bool(meta_client.load_token())
    parts.append(f"meta.ai token (acct 1): {'OK' if tok else 'MISSING'}")
    tok2 = bool(meta_client.load_token_2())
    parts.append(f"meta.ai token (acct 2): {'OK' if tok2 else 'NOT CONFIGURED'}")
    try:
        v = await run_in_threadpool(_get_vibes)
        u = await run_in_threadpool(v.me)
        parts.append(f"vibes.ai session: OK (user {u.get('username', '?')})")
    except Exception as e:  # noqa: BLE001
        parts.append(f"vibes.ai session: FAILING ({str(e)[:120]})")
        try:
            st = vibes_mod.auth.login_status()
            if st.get("checkpoint_active"):
                parts.append("vibes.ai logins: PAUSED (Meta checkpoint — verify once in browser)")
            elif st.get("last_reason"):
                parts.append(f"vibes.ai logins: last attempt: {st.get('last_reason')}")
        except Exception:  # noqa: BLE001
            pass
    removed = _janitor_sweep()
    if removed:
        parts.append(f"janitor: purged {removed} stale file(s)")
    return "\n".join(parts)

# ── HTTP app ──────────────────────────────────────────────────────────────

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        task = asyncio.create_task(_janitor_loop())
        refresh = asyncio.create_task(_session_refresher_loop())
        yield
        task.cancel()
        refresh.cancel()


app = FastAPI(title="media-gen-mcp", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({
        "service": "media-gen-mcp",
        "status": "running",
        "uptime_s": int(time.time() - START_TIME),
        "mcp_endpoint": "/mcp",
        "tools": [
            "generate_image", "edit_image", "transparent_image", "make_gif",
            "generate_video", "check_generation", "animate_image",
            "create_lipsync", "meta_chat", "web_search", "deep_research",
            "social_search", "places_search", "list_voices", "media_library",
            "favorite_media", "delete_media", "media_status"],
        "web_app": "/app",
    })


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"ok": True, "uptime_s": int(time.time() - START_TIME)})

# ── REST API for the web app ─────────────────────────────────────────────

from fastapi import Request, File, UploadFile
from fastapi.responses import HTMLResponse


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """Upload a file and get back usable handles for BOTH backends:
    - meta.ai:  `media_id` (for edit_image / image chat attachment)
    - vibes.ai: `url` + `mediaEntId` (use `url` as reference_image_url / image_url)
    """
    import mimetypes
    data = await file.read()
    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.write(fd, data)
    os.close(fd)
    fname = file.filename or "upload"
    mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    result = {"filename": fname, "mime_type": mime, "size": len(data)}
    try:
        token = meta_client.load_token()
        if token:
            result["media_id"] = meta_client.upload_file(tmp, token)
    except Exception as e:  # noqa: BLE001
        result["meta_error"] = str(e)[:120]
    try:
        v = _get_vibes()
        media = v.upload_media(tmp)
        result["url"] = media.get("cdnUrl") or media.get("imageUrl")
        result["mediaEntId"] = media.get("mediaEntId")
        result["uploadToken"] = media.get("uploadToken")
    except Exception as e:  # noqa: BLE001
        result["vibes_error"] = str(e)[:120]
    try:
        os.remove(tmp)
    except OSError:
        pass
    return JSONResponse(result)


async def _do_image(body, account=1):
    """Shared image-generation handler. account=1 -> /api/image (primary
    Meta account), account=2 -> /api/image2 (second Meta account)."""
    try:
        prompt = str(body.get("prompt", "")).strip()
        project_id = str(body.get("project_id") or "").strip() or None
        mode = str(body.get("mode") or "instant").strip()
        if not prompt:
            return JSONResponse({"error": "prompt required"}, status_code=400)
        hub = _get_hub_project(project_id) if project_id else None
        cid = hub["conversation_id"] if hub else None
        res = await meta_ask("/imagine " + prompt, mode=mode if mode in ("instant","thinking") else "instant", timeout=int(body.get("timeout",180)), conversation_id=cid, account=account)
        urls = [m["url"] for m in res["media"] if m.get("url")]
        if hub:
            with PROJECTS_LOCK:
                hub["history"].append({"prompt": prompt, "urls": urls, "text": res.get("text","")[:300], "at": __import__("time").time()})
        if not urls:
            msg = (res.get("text") or "").strip() or "No image returned — try rephrasing the prompt."
            return JSONResponse({"urls": [], "error": msg[:600]})
        return JSONResponse({"urls": urls, "text": res.get("text","")[:600], "conversation_id": cid or res.get("conversation_id")})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:300]}, status_code=500)


@app.post("/api/image")
async def api_image(req: Request):
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    return await _do_image(body, account=1)


@app.post("/api/image2")
async def api_image2(req: Request):
    """Second-account image endpoint (META_TOKEN_2). Same request/response
    shape as /api/image. Use this when /api/image reports a quota/limit error
    — each Meta account has its own image-generation quota."""
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    return await _do_image(body, account=2)


@app.post("/api/video")
async def api_video(req: Request):
    try:
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            return JSONResponse({"error": "prompt required"}, status_code=400)
        project_id = str(body.get("project_id") or "").strip() or None
        jid = _start_job("video", _run_vibe_gen, prompt,
                         body.get("aspect_ratio", "9:16"),
                         body.get("resolution", "480p"),
                         body.get("model") or body.get("videoModel") or None,
                         int(body.get("count") or body.get("n") or 1), int(body.get("timeout",420)),
                         body.get("reference_image_url") or body.get("ref_url") or None, body.get("generationType") or body.get("gen_type") or "t2v",
                         project_id)
        return JSONResponse({"job_id": jid})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:300]}, status_code=500)


@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    rec = _job_snapshot(job_id)
    if rec is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    out = {"id": rec["id"], "kind": rec["kind"], "status": rec["status"],
           "elapsed_s": int(time.time() - rec["created"])}
    if rec["result"]:
        out["items"] = rec["result"].get("items")
        out["error"] = rec["result"].get("error")
    return JSONResponse(out)


import mimetypes as _mimetypes
from fastapi.responses import StreamingResponse

_DL_HOST_OK = ("fbcdn.net", "vibes.ai", "meta.ai", "cdninstagram.com", "amazonaws.com", "cloudfront.net", "googleapis.com", "googleusercontent.com", "fbcdn.com")


@app.get("/api/download")
async def api_download(url: str):
    """Proxy-download generated media with attachment headers, so the browser
    saves the file instead of navigating to the CDN link."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url if "://" in url else "https://" + url.lstrip("/"))
        host = (parsed.hostname or "").lower()
    except:
        return JSONResponse({"error": "cannot parse url"}, status_code=400)
    if host and not any(host == h or host.endswith("." + h) for h in _DL_HOST_OK):
        if not url.lower().startswith("https://"):
            return JSONResponse({"error": "host not allowed"}, status_code=400)
    try:
        from curl_cffi import requests as _rq
        r = _rq.get(url, timeout=300, impersonate="chrome")
        if r.status_code != 200:
            return JSONResponse({"error": f"fetch failed "
                                          f"({r.status_code})"},
                                status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    ctype = (r.headers.get("content-type") or "application/octet-stream"
             ).split(";")[0].strip()
    ext = _mimetypes.guess_extension(ctype) or ""
    base = os.path.basename(urlparse(url).path) or "media"
    if ext and not base.lower().endswith(ext):
        base += ext
    from io import BytesIO
    return StreamingResponse(
        BytesIO(r.content),
        media_type=ctype,
        headers={"Content-Disposition": f'attachment; filename="{base}"',
                 "Content-Length": str(len(r.content))})

@app.get("/api/projects")
async def api_list_projects(kind: str = ""):
    kind = (kind or "").lower()
    if kind not in ("image", "video", "animate"):
        kind = None
    return JSONResponse({"projects": _hub_projects(kind)})


@app.post("/api/projects")
async def api_create_project(req: __import__("fastapi").Request):
    body = await req.json()
    kind = str(body.get("kind", "image")).lower()
    if kind not in ("image", "video", "animate"):
        kind = "image"
    name = str(body.get("name", "")).strip()
    return JSONResponse(_create_hub_project(kind, name))


@app.get("/api/keys")
async def api_list_keys(req: Request):
    keys = _load_api_keys()
    return JSONResponse({"keys":[k[:8]+"…"+k[-4:] for k in keys], "count":len(keys)})

@app.post("/api/keys")
async def api_create_key(req: Request):
    keys = _load_api_keys()
    newk="sk-"+__import__("secrets").token_urlsafe(32)
    keys.add(newk); _save_api_keys(keys)
    return JSONResponse({"api_key":newk})


@app.post("/api/session")
async def api_inject_session(req: Request):
    """Heal video instantly by pasting a fresh browser cookie — no password login.

    Body: {"meta_session": "<value from DevTools → Application → Cookies →
    vibes.ai → meta_session"}. The cookie is verified with /auth/me BEFORE it
    is saved; a rejected value is never persisted.
    """
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    val = str(body.get("meta_session") or "").strip().strip('"').strip("'")
    if not val or len(val) < 8:
        return JSONResponse({"error": "meta_session required"}, status_code=400)

    def _verify_and_save():
        global _vibes_client
        from curl_cffi import requests as _rq
        s = _rq.Session(impersonate="chrome_android")
        s.cookies.set("meta_session", val, domain=".vibes.ai", path="/")
        s.cookies.set("cookie_ack", "true", domain=".vibes.ai", path="/")
        v = vibes_mod.Vibes(s, reauth=False)
        u = v.me()  # raises on failure — nothing saved yet
        vibes_mod.auth.save_session(s)
        with _vibes_lock:
            _vibes_client = vibes_mod.Vibes(s, reauth=True)
        try:
            vibes_mod.auth._clear_checkpoint()
        except Exception:  # noqa: BLE001
            pass
        return u

    try:
        u = await run_in_threadpool(_verify_and_save)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"cookie rejected ({str(e)[:120]}) — not saved"},
                            status_code=400)
    return JSONResponse({"ok": True, "username": u.get("username", "?")})

# ── web app ───────────────────────────────────────────────────────────────

_APP_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Media Gen Studio — Production Hub</title>
<style>
  :root{--bg:#0a0a0a;--card:#111214;--border:#262626;--txt:#ededed;--dim:#8f8f8f;--accent:#0070f3;--r:14px}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font-family:Inter,-apple-system,'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:36px 16px}
  .wrap{width:100%;max-width:820px}
  h1{font-size:26px;font-weight:600;letter-spacing:-.5px} .sub{color:var(--dim);font-size:13px;margin-top:4px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:22px;margin-top:22px}
  .tabs{display:flex;gap:4px;background:#161618;border-radius:10px;padding:4px;width:fit-content}
  .tab{padding:7px 18px;border-radius:8px;font-size:13.5px;color:var(--dim);cursor:pointer;border:none;background:none}
  .tab.on{background:#2c2c2e;color:#fff}
  .projbar{display:flex;gap:8px;align-items:center;margin-top:14px;flex-wrap:wrap}
  .projbar label{font-size:12px;color:var(--dim)} .projbar select{min-width:180px}
  .projbar button{background:#1f1f21;border:1px solid var(--border);color:var(--txt);border-radius:8px;padding:7px 12px;font-size:12.5px;cursor:pointer}
  .projbar button:hover{background:#252529}
  textarea{width:100%;background:#0d0d0e;border:1px solid var(--border);border-radius:10px;color:var(--txt);padding:13px;font-size:14px;resize:vertical;min-height:88px;margin-top:14px;font-family:inherit}
  textarea:focus{outline:none;border-color:var(--accent)}
  .opts{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  select,input.url{background:#0d0d0e;border:1px solid var(--border);color:var(--txt);border-radius:8px;padding:8px 10px;font-size:12.5px}
  input.url{width:100%;margin-top:10px}
  button.go{margin-top:14px;background:#fff;color:#000;border:none;border-radius:10px;padding:11px 26px;font-size:14px;font-weight:600;cursor:pointer}
  button.go:hover{background:#e5e5e5}button.go:disabled{opacity:.45;cursor:wait}
  #msg{margin-top:12px;font-size:13px;color:var(--dim);min-height:18px;white-space:pre-wrap}
  #msg.err{color:#f87171}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;margin-top:18px}
  .grid img,.grid video{width:100%;border-radius:12px;border:1px solid var(--border);display:block;background:#000}
  a.dl{font-size:12px;color:var(--accent);text-decoration:none;display:inline-block;margin-top:6px}
  .hist{margin-top:18px;border-top:1px solid var(--border);padding-top:14px}
  .hist h3{font-size:12px;color:var(--dim);letter-spacing:.4px;text-transform:uppercase;margin-bottom:8px}
  .hgrid{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}
  .hgrid img,.hgrid video{height:72px;width:auto;border-radius:8px;border:1px solid var(--border)}
  footer{margin-top:auto;padding-top:50px;color:#555;font-size:12px;text-align:center}
</style></head><body>
<div class="wrap">
  <h1>Media Gen Studio — Production Hub</h1>
  <div class="sub">Persistent projects with context · Images by Meta AI · Videos by Vibes AI · <a href="/health" style="color:var(--dim)">health</a></div>
  <div class="card">
    <div class="tabs">
      <button class="tab on" data-m="image">Image</button>
      <button class="tab" data-m="video">Video</button>
      <button class="tab" data-m="animate">Animate image</button>
    </div>
    <div class="projbar" id="projbar">
      <label>Project:</label>
      <select id="proj"></select>
      <button id="newproj">+ New project</button>
      <span id="projinfo" style="font-size:12px;color:var(--dim)"></span>
    </div>
    <textarea id="prompt" placeholder="Describe what you want — style stays consistent inside one project..."></textarea>
    <input class="url" id="refurl" placeholder="https://image-url-to-animate.jpg" style="display:none">
    <div class="opts">
      <select id="aspect"><option value="1:1">1:1 square</option><option value="9:16">9:16 vertical</option><option value="16:9">16:9 wide</option><option value="4:5">4:5 post</option><option value="3:4">3:4</option></select>
      <select id="res"><option value="480p">480p fast</option><option value="720p">720p</option><option value="1080p">1080p slow</option></select>
      <select id="model"><option value="">auto model</option><option value="midjen-short">midjen-short</option><option value="midjen-long">midjen-long</option><option value="midjen">midjen</option><option value="sora">sora</option><option value="veo">veo</option></select>
      <select id="count"><option value="1">×1</option><option value="2">×2</option><option value="3">×3</option><option value="4">×4</option></select>
    </div>
    <button class="go" id="go">Generate</button>
    <div id="msg"></div>
    <div class="grid" id="grid"></div>
    <div class="hist" id="hist" style="display:none"><h3>Project history</h3><div class="hgrid" id="hgrid"></div></div>
  </div>
</div>
<footer>media-gen-mcp · free tier · videos 1-5 min · context stays inside each project</footer>
<script>
let MODE='image';
let ACTIVE={image:null,video:null,animate:null};
let PROJECTS={image:[],video:[],animate:[]};
const $=id=>document.getElementById(id);
function addMedia(u,type){
  const d=document.createElement('div');
  if(type==='video'||/\.mp4($|\?)/.test(u)) d.innerHTML=`<video src="${u}" controls muted playsinline></video><a class="dl" href="/api/download?url=${encodeURIComponent(u)}" download>Download</a>`;
  else d.innerHTML=`<img src="${u}"><a class="dl" href="/api/download?url=${encodeURIComponent(u)}" download>Download</a>`;
  $('grid').prepend(d);
}
function histThumb(u){return /\.mp4/.test(u)?`<video src="${u}" muted></video>`:`<img src="${u}">`;}
async function loadProjects(){
  for(const k of ['image','video','animate']){
    const r=await fetch('/api/projects?kind='+k); const j=await r.json();
    PROJECTS[k]=j.projects||[];
    if(!ACTIVE[k] && j.projects.length) ACTIVE[k]=j.projects[0].id;
    if(!j.projects.length){
      const c=await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:k,name:k.charAt(0).toUpperCase()+k.slice(1)+' #1'})}).then(r=>r.json());
      PROJECTS[k]=[c]; ACTIVE[k]=c.id;
    }
  }
  renderProjBar();
}
function renderProjBar(){
  const list=PROJECTS[MODE]||[];
  $('proj').innerHTML=list.map(p=>`<option value="${p.id}" ${p.id===ACTIVE[MODE]?'selected':''}>${p.name}</option>`).join('');
  const cur=list.find(p=>p.id===ACTIVE[MODE]);
  $('projinfo').textContent=cur?`${(cur.history||[]).length} items · ${cur.id.slice(0,6)}`:'';
  const h=$('hgrid'); h.innerHTML='';
  const hist=(cur&&cur.history)||[];
  if(hist.length){$('hist').style.display='block';
    [...hist].reverse().slice(0,16).forEach(e=>{
      const urls=e.urls|| (e.items||[]).map(x=>x.url);
      urls.forEach(u=>{const a=document.createElement('a');a.href=u;a.target='_blank';a.innerHTML=histThumb(u);h.appendChild(a);});
    });
  } else $('hist').style.display='none';
  $('refurl').style.display=MODE==='animate'?'block':'none';
  $('res').style.display = MODE==='image'?'none':'block';
  $('model').style.display = MODE==='image'?'none':'block';
  $('count').style.display = MODE==='image'?'none':'block';
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  t.classList.add('on'); MODE=t.dataset.m;
  $('grid').innerHTML='';
  renderProjBar();
});
$('proj').onchange=e=>{ACTIVE[MODE]=e.target.value;$('grid').innerHTML='';renderProjBar();};
$('newproj').onclick=async()=>{
  const name=prompt('Project name:',''); if(name===null) return;
  const r=await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:MODE,name:name||''})});
  const p=await r.json(); PROJECTS[MODE].unshift(p); ACTIVE[MODE]=p.id; $('grid').innerHTML=''; renderProjBar();
};
async function poll(jid){
  const r=await fetch('/api/job/'+jid); const j=await r.json();
  if(j.status==='running'){$('msg').textContent=`Generating... ${j.elapsed_s}s`; setTimeout(()=>poll(jid),8000); return;}
  $('go').disabled=false;
  if(j.status==='error'||j.error){$('msg').textContent=j.error||'failed';$('msg').className='err';return;}
  $('msg').textContent='Done!';
  (j.items||[]).forEach(it=>{
    if(it.error){$('msg').textContent='Item failed: '+it.error;return;}
    const u=it.url||it.videoUrl||it.imageUrl; if(!u) return;
    addMedia(u,it.type||'video');
  });
  const pr=await fetch('/api/projects?kind='+MODE).then(r=>r.json());
  PROJECTS[MODE]=pr.projects; renderProjBar();
}
$('go').onclick=async()=>{
  const p=$('prompt').value.trim(); if(!p) return;
  const pid=ACTIVE[MODE];
  $('go').disabled=true; $('msg').className=''; $('msg').textContent='Starting...';
  try{
    if(MODE==='image'){
      const r=await fetch('/api/image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:p,project_id:pid})});
      const j=await r.json(); $('go').disabled=false;
      if(j.error&&(!j.urls||!j.urls.length)){$('msg').textContent=j.error;$('msg').className='err';return;}
      if(j.error){$('msg').textContent=j.error;$('msg').className='err';}
      if(!j.urls||!j.urls.length){$('msg').textContent=j.error||'No image returned';$('msg').className='err';return;}
      $('msg').textContent='Done!'; j.urls.forEach(u=>addMedia(u,'image'));
      const pr=await fetch('/api/projects?kind=image').then(r=>r.json()); PROJECTS.image=pr.projects; renderProjBar();
    } else {
      const body={prompt:p,aspect_ratio:$('aspect').value,resolution:$('res').value,model:$('model').value,count:parseInt($('count').value),project_id:pid};
      if(MODE==='animate'){body.reference_image_url=$('refurl').value.trim();}
      const r=await fetch('/api/video',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const j=await r.json(); if(j.error){$('msg').textContent=j.error;$('msg').className='err';$('go').disabled=false;return;}
      poll(j.job_id);
    }
  } catch(e){$('msg').textContent=String(e);$('msg').className='err';$('go').disabled=false;}
};
loadProjects();
</script></body></html>
"""


@app.api_route("/app", methods=["GET", "HEAD"])
async def web_app():
    return HTMLResponse(_APP_HTML)



# MCP transport last, so / and /health always win
app.mount("/", mcp_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
