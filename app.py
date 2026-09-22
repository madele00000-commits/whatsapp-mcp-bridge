import asyncio
import os
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

import gradio as gr
import httpx
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.background import BackgroundTask
from starlette.middleware.wsgi import WSGIMiddleware

GOWA_PORT = 3000
SPACE_PORT = int(os.getenv("PORT", "7860"))
GOWA = Path("./whatsapp")
GOWA_API = f"http://127.0.0.1:{GOWA_PORT}"


def ensure_gowa():
    if GOWA.exists():
        GOWA.chmod(GOWA.stat().st_mode | stat.S_IEXEC)
        return
    # Latest release v9.4.0, Linux x86_64/amd64 asset confirmed from GitHub.
    url = "https://github.com/aldinokemal/go-whatsapp-web-multidevice/releases/download/v9.4.0/whatsapp_9.4.0_linux_amd64.zip"
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "gowa.zip"
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with archive.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
        import zipfile
        with zipfile.ZipFile(archive) as z:
            candidates = [n for n in z.namelist() if not n.endswith("/")]
            binary = next((n for n in candidates if Path(n).name == "whatsapp"), candidates[0])
            with z.open(binary) as src, GOWA.open("wb") as dst:
                dst.write(src.read())
    GOWA.chmod(GOWA.stat().st_mode | stat.S_IEXEC)


def start_gowa():
    ensure_gowa()
    env = os.environ.copy()
    env.update({"APP_PORT": str(GOWA_PORT), "MCP_ENABLED": "true"})
    return subprocess.Popen([str(GOWA), "rest"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


try:
    gowa_process = start_gowa()
except Exception as exc:
    gowa_process = None
    startup_error = str(exc)
else:
    startup_error = ""

app = FastAPI(title="WhatsApp MCP Bridge")


@app.get("/status")
async def status():
    if startup_error:
        return {"ok": False, "error": startup_error}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{GOWA_API}/app/login", timeout=10)
        return {"ok": True, "gowa_status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.api_route("/mcp", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def mcp_proxy(request: Request, path: str = ""):
    target = f"{GOWA_API}/mcp" + (f"/{path}" if path else "")
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}
    async with httpx.AsyncClient() as client:
        upstream = await client.request(request.method, target, content=body, headers=headers, params=request.query_params, timeout=None)
    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
    return Response(upstream.content, status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))


@app.api_route("/gowa/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def gowa_proxy(request: Request, path: str):
    target = f"{GOWA_API}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}
    async with httpx.AsyncClient() as client:
        upstream = await client.request(request.method, target, content=body, headers=headers, params=request.query_params, timeout=None)
    return Response(upstream.content, upstream.status_code, headers={k: v for k, v in upstream.headers.items() if k.lower() not in {"content-length", "connection"}})


def ui():
    note = "GOWA is starting; refresh after a few seconds." if not startup_error else f"Startup error: {startup_error}"
    return f"""<h1>WhatsApp MCP Bridge</h1><p>{note}</p><p>Scan the QR code below with WhatsApp → Linked devices.</p><iframe src=\"/gowa/app/login\" style=\"width:100%;height:700px;border:1px solid #ddd\"></iframe><p>MCP endpoint: <code>/mcp</code></p>"""


gradio_app = gr.Interface(fn=lambda: ui(), inputs=None, outputs=gr.HTML(), title="WhatsApp MCP Bridge", allow_flagging="never")
app = gr.mount_gradio_app(app, gradio_app, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SPACE_PORT)
