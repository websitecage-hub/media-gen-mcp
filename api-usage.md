# Final API usage — all working endpoints

Two live services. Everything below is open (no auth required unless noted).

- **LLM chat:** `https://meta-api-u04m.onrender.com` — Meta AI text chat, always `thinking` mode.
- **Media:** `https://media-gen-mcp.onrender.com` — images with Meta AI, videos with Vibes AI.

Notes:
- Long calls need generous timeouts: chat/image **10–60s+**, video is async (**1–5 min**, poll the job).
- `media-gen-mcp` keeps image/video only. The LLM lives in `meta-api`.
- GUI: `GET /app` on both services.

---

## A. Meta API — LLM chat

Base: `https://meta-api-u04m.onrender.com`
Model: `meta-ai-thinking`

| Method | Path | Use |
|---|---|---|
| `GET`/`HEAD` | `/` | service info |
| `GET`/`HEAD` | `/health` | keep-alive probe: `{"ok":true}` |
| `GET`/`HEAD` | `/app` | chat GUI with saved conversation sidebar |
| `GET` | `/v1/models` | returns `meta-ai-thinking` |
| `POST` | `/api/chat` | main chat endpoint |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat endpoint |
| `POST` | `/api/upload` | upload a file, get `media_id` to attach |
| `GET` | `/api/download?url=` | download any file Meta produced (forced file save) |
| `GET` | `/api/conversations` | list saved chats |
| `GET` | `/api/conversations/{id}` | full message history for one chat |

### Simple chat

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the capital of France?"}'
```

Response:

```json
{"text":"…","model":"meta-ai-thinking","mode":"thinking","conversation_id":"…","media":[],"files":[{"url":"https://…","mime_type":"text/csv","filename":"todo.csv"}]}
```

When Meta attaches produced files (CSV, TXT, HTML, PNG, docs, …), they arrive in
`files[]`. Download any of them:

```bash
curl -L "https://meta-api-u04m.onrender.com/api/download?url=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("https://…/todo.csv?…"))')" -o todo.csv
```

Notes: use `files[]` records whose `url` starts with `https://`; Meta CDN links are
signed and can expire, so download promptly or re-ask Meta to regenerate.

Keep context by echoing back `conversation_id`:

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"My name is Alex.","conversation_id":"<CID>"}'
```

`/api/chat` fields: `message` (also accepts `prompt`) required; optional
`conversation_id`, `timeout` (default 300), `attachments[]`
(`{media_id, mime_type, filename}`), `image_url`/`image_urls` (auto-fetched and
attached). Errors: `400` for bad input, `500` with `{"error":"…"}` on backend failure.

### Upload + attach an image

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/upload \
  -F "file=@photo.jpg"
# {"media_id":123,"filename":"photo.jpg","mime_type":"image/jpeg","size":52301,"attachment":{...}}

curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is in this image?","attachments":[{"media_id":123,"mime_type":"image/jpeg","filename":"photo.jpg"}]}'
```

Or send a public URL and let the API fetch/attach it:

```bash
curl -X POST https://meta-api-u04m.onrender.com/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Describe this","image_url":"https://example.com/photo.jpg"}'
```

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="https://meta-api-u04m.onrender.com/v1", api_key="sk-anything")
r = client.chat.completions.create(model="meta-ai-thinking",
    messages=[{"role":"user","content":"Tell me a joke"}])
print(r.choices[0].message.content)
```

Note: `/v1/chat/completions` is stateless. For multi-turn context use `/api/chat` with `conversation_id`.

### Saved chat history

```bash
curl https://meta-api-u04m.onrender.com/api/conversations
curl https://meta-api-u04m.onrender.com/api/conversations/<CID>
```

---

## B. Media API — image + video

Base: `https://media-gen-mcp.onrender.com`

| Method | Path | Use |
|---|---|---|
| `GET`/`HEAD` | `/` | service info + tool list |
| `GET`/`HEAD` | `/health` | keep-alive probe: `{"ok":true}` |
| `GET`/`HEAD` | `/app` | production hub GUI |
| `GET` | `/api/projects?kind=image\|video\|animate` | list projects; omit `kind` for all |
| `POST` | `/api/projects` | create project: `{"kind":"image","name":"…"}` |
| `POST` | `/api/image` | generate image from text |
| `POST` | `/api/video` | start video job, returns `job_id` |
| `GET` | `/api/job/{job_id}` | poll video job until `done` |
| `POST` | `/api/upload` | upload file, get `url` + `media_id` handles |
| `GET` | `/api/download?url=<encoded>` | download a generated file as attachment |
| `GET` | `/api/keys` | list masked API keys |
| `POST` | `/api/keys` | mint a new `sk-…` key |
| `POST` | `/mcp` | MCP tools for Claude/ChatGPT |

### Image

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/image \
  -H "Content-Type: application/json" \
  -d '{"prompt":"minimal flat logo of a fox","mode":"instant"}'
```

Fields: `prompt` required; optional `project_id`, `mode` (`instant` default, or `thinking`), `timeout` (default 180). Response has `urls[]`, `text`, `conversation_id`, or `{"urls":[],"error":"…"}` when the prompt is refused (rephrase it) — never a non-200 on refusal.

### Video (async)

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/video \
  -H "Content-Type: application/json" \
  -d '{"prompt":"drone over ocean","resolution":"480p","aspect_ratio":"16:9"}'

curl https://media-gen-mcp.onrender.com/api/job/<job_id>
```

`/api/video` fields: `prompt` required; optional `project_id`, `aspect_ratio` (also `aspect`) default `9:16`, `resolution` default `480p`, `model` (also `videoModel`), `count`/`n` 1–4, `reference_image_url`/`ref_url` for image-to-video, `generationType`/`gen_type` default `t2v`, `timeout` default 420. `/api/job/{id}` returns `running|done|error` (plus `elapsed_s`); when `done`, `items[]` has `{url,type,thumb}`; unknown id → `404 {"error":"unknown job"}`.

### Upload, then use the file

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/upload \
  -F "file=@photo.jpg"
# {"filename":"photo.jpg","mime_type":"image/jpeg","size":…,"media_id":…,"url":"https://…","mediaEntId":"…","uploadToken":"…"}

curl -X POST https://media-gen-mcp.onrender.com/api/video \
  -H "Content-Type: application/json" \
  -d '{"prompt":"animate this","reference_image_url":"<URL_FROM_UPLOAD>"}'
```

Use `url` as `reference_image_url`/`image_url`; use `media_id` for Meta-side editing.

### Projects and keys

```bash
curl "https://media-gen-mcp.onrender.com/api/projects?kind=image"
curl -X POST https://media-gen-mcp.onrender.com/api/projects \
  -H "Content-Type: application/json" \
  -d '{"kind":"video","name":"Campaign"}'
curl -X POST https://media-gen-mcp.onrender.com/api/keys -H "Content-Type: application/json" -d '{}'
```

### MCP tools

`POST /mcp` is Streamable HTTP, stateless, no auth. Image edit/transparent/GIF,
all search tools, voices/library/favorite/delete/status, and lipsync are MCP-only
(no REST equivalent). Media tools:

`generate_image(prompt, project_id)`, `edit_image(image_url, instruction)`,
`transparent_image(prompt)`, `make_gif(prompt)`,
`generate_video(prompt, aspect_ratio, resolution, model, count, reference_image_url, project_id)` → returns `job_id` text,
`check_generation(job_id)`, `animate_image(image_url, prompt, aspect_ratio, resolution, project_id)`,
`create_lipsync(source_url, audio_url, prompt, aspect_ratio, resolution)`,
`meta_chat(message)`, `web_search(query)`, `deep_research(topic)`,
`social_search(query)`, `places_search(query)`,
`list_voices()`, `media_library(media_type, limit)` (`media_type`: `video`|`image`|`audio`),
`favorite_media(item_id, is_favorited)`, `delete_media(item_id)`, `media_status()`.

Video tools are async: take the returned `job_id`, call `check_generation(job_id)` about every 20s until `done`.
