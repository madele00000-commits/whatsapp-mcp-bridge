import os
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

import gradio as gr
import httpx
import requests
from fastapi import FastAPI, Request
from fastapi.responses import Response

GOWA_PORT = 3000
SPACE_PORT = int(os.getenv("PORT", "7860"))
GOWA = Path("./whatsapp")
GOWA_API = f"http://127.0.0.1:{GOWA_PORT}"
GOWA_DOWNLOAD = (
    "https://github.com/aldinokemal/go-whatsapp-web-multidevice/"
    "releases/download/v9.4.0/whatsapp_9.4.0_linux_amd64.zip"
)


def ensure_gowa() -> None:
    """Download and unpack the GOWA binary exactly once."""
    if GOWA.is_file():
        GOWA.chmod(GOWA.stat().st_mode | stat.S_IEXEC)
        return

    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "whatsapp.zip"
        with requests.get(GOWA_DOWNLOAD, stream=True, timeout=120) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        output.write(chunk)

        with zipfile.ZipFile(archive) as bundle:
            member = next(
                (name for name in bundle.namelist() if Path(name).name == "whatsapp"),
                None,
            )
            if member is None:
                raise RuntimeError("The GOWA archive does not contain a whatsapp binary")
            with bundle.open(member) as source, GOWA.open("wb") as output:
                output.write(source.read())

    GOWA.chmod(GOWA.stat().st_mode | stat.S_IEXEC)


def start_gowa() -> subprocess.Popen:
    ensure_gowa()
    environment = os.environ.copy()
    environment.update(APP_PORT=str(GOWA_PORT), MCP_ENABLED="true")
    return subprocess.Popen(
        [str(GOWA), "rest"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


startup_error = ""
try:
    gowa_process = start_gowa()
except Exception as error:  # Keep the UI available so the error is diagnosable.
    gowa_process = None
    startup_error = f"{type(error).__name__}: {error}"

app = FastAPI(title="WhatsApp MCP Bridge")


@app.get("/status")
async def status():
    if startup_error:
        return {"ok": False, "error": startup_error}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{GOWA_API}/app/login", timeout=10)
        return {"ok": True, "gowa_status": response.status_code}
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


async def proxy(request: Request, target: str) -> Response:
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    async with httpx.AsyncClient() as client:
        upstream = await client.request(
            request.method,
            target,
            content=body,
            headers=headers,
            params=request.query_params,
            timeout=None,
        )
    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in excluded
    }
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.api_route("/mcp", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def mcp_proxy(request: Request, path: str = ""):
    suffix = f"/{path}" if path else ""
    return await proxy(request, f"{GOWA_API}/mcp{suffix}")


@app.api_route("/gowa/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def gowa_proxy(request: Request, path: str):
    return await proxy(request, f"{GOWA_API}/{path}")


def login_html() -> str:
    if startup_error:
        message = f"<p><strong>GOWA startup error:</strong> {startup_error}</p>"
    else:
        message = "<p>Scan the QR code with WhatsApp → Linked devices.</p>"
    return (
        '<h1>WhatsApp MCP Bridge</h1>'
        f"{message}"
        '<iframe src="/gowa/app/login" '
        'style="width:100%;height:700px;border:1px solid #ddd"></iframe>'
        '<p>MCP endpoint: <code>/mcp</code></p>'
    )


with gr.Blocks(title="WhatsApp MCP Bridge") as demo:
    gr.Markdown("# WhatsApp MCP Bridge")
    login = gr.HTML(login_html())
    refresh = gr.Button("Refresh login / QR")
    refresh.click(fn=login_html, inputs=None, outputs=login)

app = gr.mount_gradio_app(app, demo, path="/")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=SPACE_PORT)
