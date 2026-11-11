import json
import os
import re
import signal
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, render_template_string, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
VENV_PYTHON = "/home/escriva/venv/bin/python3"
ORGANIZADO_PATH = "/home/escriva/music/baldman/SINFI/Organizado"
SCRIPTS_DIR = "/home/escriva/Navidrome-pipeline/scripts"

HOST = os.environ.get("PANEL_HOST", "0.0.0.0")  # pon aquí tu IP de Tailscale para no exponerlo a toda la LAN
PORT = int(os.environ.get("PANEL_PORT", "5001"))

MAX_LINES = 20000           # límite de líneas guardadas por ejecución

CLEAN_SCRIPT = r'''
ROOT="$1"
if [ ! -d "$ROOT" ]; then echo "[ERROR] No existe la ruta: $ROOT"; exit 1; fi
removed=0
# -depth: primero las subcarpetas, así los padres que quedan vacíos también se borran
while IFS= read -r -d '' dir; do
  count=$(find "$dir" -mindepth 1 -maxdepth 1 | wc -l)
  if [ "$count" -eq 0 ]; then
    echo "[BORRADO] Carpeta vacía: $dir"
    rmdir -- "$dir" && removed=$((removed+1))
  elif [ "$count" -eq 1 ] && [ -n "$(find "$dir" -mindepth 1 -maxdepth 1 -type f -iname 'cover.jpg')" ]; then
    echo "[BORRADO] Carpeta con solo cover.jpg: $dir"
    rm -rf -- "$dir" && removed=$((removed+1))
  fi
done < <(find "$ROOT" -mindepth 1 -depth -type d -print0)
echo "[OK] Listo. Carpetas eliminadas: $removed"
'''

SCRIPTS = {
    "pipeline": {
        "title": "🎵 Audio Pipeline",
        "desc": "Procesa metadatos en Staging y mueve la música limpia a la biblioteca organizada",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/audio_pipeline.py"],
    },
    "bpm": {
        "title": "🥁 Analizar BPM",
        "desc": "Calcula e incrusta el tempo (BPM) en los metadatos ID3 con Essentia/Librosa",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/bpm.py"],
    },
    "move_dry": {
        "title": "🔍 Simular mover playlists",
        "desc": "Muestra qué canciones de cada playlist se moverían a su carpeta, sin tocar nada",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/move_playlist.py", "--dry-run"],
    },
    "move": {
        "title": "📦 Mover playlists a carpetas",
        "desc": "Mueve las canciones de cada playlist a su carpeta (según PLAYLIST_FOLDERS) y reescanea Navidrome",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/move_playlist.py", "--scan"],
        "confirm": "Esto moverá archivos de música a otras carpetas. ¿Continuar? (conviene haber probado antes la simulación)",
    },
    "autostar": {
        "title": "⭐ AutoStar Navidrome",
        "desc": "Califica con estrellas las canciones y álbumes según las reproducciones",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/autostar.py"],
    },
    "covers": {
        "title": "🖼️ Descargar carátulas",
        "desc": "Busca portadas en cascada (MBZ → IT → DG) y las incrusta en ID3 / cover.jpg",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/download_covers.py"],
    },
    "wantlist": {
        "title": "📋 Wantlist de Discogs",
        "desc": "Sincroniza los álbumes de tus playlists de Navidrome con tu Wantlist de Discogs",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/wantlist_from_navidrome.py"],
    },
    "clean": {
        "title": "🧹 Limpiar carpetas vacías",
        "desc": "Elimina carpetas vacías o que solo contienen cover.jpg en la biblioteca organizada",
        "cmd": ["bash", "-c", CLEAN_SCRIPT, "_", ORGANIZADO_PATH],
        "confirm": "Esto borrará carpetas vacías o con solo cover.jpg en la biblioteca. ¿Continuar?",
    },
}

# ---------------------------------------------------------------------------
# GESTOR DE TAREAS (una sola a la vez; la salida se comparte con todos los clientes)
# ---------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

cond = threading.Condition()
current_job = None


class Job:
    def __init__(self, key):
        self.id = int(time.time() * 1000)
        self.key = key
        self.title = SCRIPTS[key]["title"]
        self.lines = []
        self.truncated = False
        self.running = True
        self.returncode = None
        self.proc = None
        self.started = time.time()
        self.finished = None

    def info(self):
        end = self.finished or time.time()
        return {
            "id": self.id,
            "key": self.key,
            "title": self.title,
            "running": self.running,
            "elapsed": round(end - self.started, 1),
        }


def push(job, text):
    text = ANSI_RE.sub("", text)
    with cond:
        if len(job.lines) < MAX_LINES:
            job.lines.append(text)
        elif not job.truncated:
            job.truncated = True
            job.lines.append("[... salida truncada ...]")
        cond.notify_all()


def _run_job(job):
    rc = -1
    try:
        push(job, f"> {job.title}")
        push(job, "-" * 40)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            SCRIPTS[job.key]["cmd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=True,  # permite parar también los procesos hijos
        )
        job.proc = proc
        for line in proc.stdout:
            push(job, line.rstrip("\n"))
        proc.stdout.close()
        rc = proc.wait()
    except Exception as exc:  # noqa: BLE001
        push(job, f"[ERROR] No se pudo ejecutar la tarea: {exc}")
    finally:
        with cond:
            job.running = False
            job.returncode = rc
            job.finished = time.time()
            cond.notify_all()


def start_job(key):
    global current_job
    with cond:
        if current_job is not None and current_job.running:
            return None
        job = Job(key)
        current_job = job
        cond.notify_all()
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


def stop_job():
    with cond:
        job = current_job
    if job is None or not job.running or job.proc is None:
        return False
    try:
        os.killpg(job.proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    push(job, "[DETENIDO] Tarea cancelada desde el panel")
    return True


# ---------------------------------------------------------------------------
# INTERFAZ
# ---------------------------------------------------------------------------
HTML_LAYOUT = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#101214">
<title>Navidrome Pipeline Control</title>
{% raw %}
<style>
  :root {
    --bg: #101214; --panel: #181b1f; --line: #2b3036; --text: #e7e9ec; --muted: #8e97a3;
    --accent: #2f7df6; --accent-hover: #4a8ff7; --ok: #3ddc84; --warn: #f5b942; --err: #ff6b6b;
    --console-bg: #0b0c0d; --console-text: #7dffb0;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; line-height: 1.45;
    padding: max(16px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
             max(24px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
  }
  .container { max-width: 1000px; margin: 0 auto; }
  header { margin-bottom: 20px; }
  h1 { margin: 0 0 4px; font-size: clamp(1.35rem, 4.5vw, 1.9rem); }
  .subtitle { margin: 0 0 14px; color: var(--muted); font-size: clamp(.85rem, 2.6vw, .95rem); }

  .grid {
    display: grid; gap: 14px; margin-bottom: 22px;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 260px), 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px;
    display: flex; flex-direction: column; justify-content: space-between; gap: 14px;
  }
  .card h3 { margin: 0 0 6px; font-size: 1.05rem; }
  .card p { margin: 0; color: var(--muted); font-size: .88rem; }

  .btn {
    appearance: none; border: 0; border-radius: 8px; cursor: pointer; font: inherit; font-weight: 600;
    min-height: 44px; padding: 0 16px; color: #fff; background: var(--accent);
    transition: background .15s;
  }
  .btn:hover:not(:disabled) { background: var(--accent-hover); }
  .btn:disabled { background: #343a42; color: #7d8590; cursor: not-allowed; }
  .btn.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); font-weight: 500; }
  .btn.ghost:hover:not(:disabled) { background: #22262b; }
  .btn.danger { background: #b3363a; }
  .btn.danger:hover:not(:disabled) { background: #cc4448; }
  .btn.small { min-height: 38px; padding: 0 12px; font-size: .88rem; }
  .btn:focus-visible { outline: 2px solid #9cc2ff; outline-offset: 2px; }

  .console-box { background: var(--console-bg); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  .console-head {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px;
    padding: 12px 14px; border-bottom: 1px solid #1e2226;
  }
  .console-title { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1 1 260px; }
  .console-title strong { white-space: nowrap; }
  #state { color: var(--muted); font-size: .85rem; overflow-wrap: anywhere; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; background: #596069; }
  .dot.busy { background: var(--warn); animation: pulse 1.2s ease-in-out infinite; }
  .dot.ok { background: var(--ok); }
  .dot.err { background: var(--err); }
  @keyframes pulse { 50% { opacity: .35; } }

  pre#console {
    margin: 0; padding: 14px; color: var(--console-text);
    font: .85rem/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    height: 420px; height: clamp(260px, 55dvh, 560px);
    overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
    overscroll-behavior: contain; -webkit-overflow-scrolling: touch;
  }
  pre#console:empty::before { content: attr(data-empty); color: #5d6671; }
  pre#console .err { color: var(--err); }
  pre#console .warn { color: var(--warn); }
  pre#console .ok { color: var(--ok); }

  #conn {
    display: none; margin: 0 14px 12px; padding: 6px 10px; border-radius: 6px;
    background: #3a2d12; color: var(--warn); font-size: .82rem;
  }
  #conn.show { display: block; }
  #toast {
    position: fixed; left: 50%; bottom: max(20px, env(safe-area-inset-bottom)); transform: translateX(-50%);
    background: #2a2f36; border: 1px solid var(--line); color: var(--text);
    padding: 10px 16px; border-radius: 8px; font-size: .9rem; max-width: calc(100% - 32px);
    opacity: 0; pointer-events: none; transition: opacity .2s;
  }
  #toast.show { opacity: 1; }

  @media (max-width: 520px) {
    .card { padding: 14px; }
    .card .btn { width: 100%; }
    .actions { width: 100%; }
    .actions .btn { flex: 1; }
    pre#console { padding: 12px; font-size: .8rem; }
  }
  @media (prefers-reduced-motion: reduce) {
    .dot.busy { animation: none; }
    .btn, #toast { transition: none; }
  }
</style>
{% endraw %}
</head>
<body>
<div class="container">
  <header>
    <h1>🎛️ Navidrome Pipeline Control</h1>
    <p class="subtitle" style="margin-bottom:0">Gestión de scripts accesible por Tailscale</p>
  </header>

  <section class="grid" id="grid" aria-label="Tareas disponibles">
    {% for s in scripts %}
    <article class="card">
      <div>
        <h3>{{ s.title }}</h3>
        <p>{{ s.desc }}</p>
      </div>
      <button type="button" class="btn run" data-key="{{ s.key }}" data-confirm="{{ s.confirm }}">Ejecutar</button>
    </article>
    {% endfor %}
  </section>

  <section class="console-box" aria-label="Salida de consola">
    <div class="console-head">
      <div class="console-title">
        <span id="dot" class="dot"></span>
        <strong>Consola</strong>
        <span id="state" role="status" aria-live="polite">Inactivo</span>
      </div>
      <div class="actions">
        <button type="button" id="stop" class="btn danger small" disabled>Detener</button>
        <button type="button" id="copy" class="btn ghost small">Copiar</button>
        <button type="button" id="clear" class="btn ghost small">Limpiar</button>
      </div>
    </div>
    <div id="conn">Conexión perdida, reconectando…</div>
    <pre id="console" data-empty="Pulsa «Ejecutar» en una tarea para ver aquí su salida."></pre>
  </section>
</div>
<div id="toast" role="status" aria-live="polite"></div>

{% raw %}
<script>
  const $ = (s) => document.querySelector(s);
  const out = $("#console"), stateEl = $("#state"), dotEl = $("#dot"), connEl = $("#conn");
  const stopBtn = $("#stop"), runBtns = [...document.querySelectorAll(".run")];
  const MAX_NODES = 3000;
  let ticker = null, queue = [], raf = 0, toastTimer = null;

  const fmt = (s) => { s = Math.round(s); const m = Math.floor(s / 60); return m ? `${m}m ${s % 60}s` : `${s}s`; };

  function classify(t) {
    if (/error|traceback|exception|❌/i.test(t)) return "err";
    if (/warn|⚠️|\[DETENIDO\]/i.test(t)) return "warn";
    if (/\[OK\]|✅/.test(t)) return "ok";
    return "";
  }

  function addLine(text) {
    queue.push(text);
    if (!raf) raf = requestAnimationFrame(flush);
  }

  function flush() {
    raf = 0;
    const nearBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    const frag = document.createDocumentFragment();
    for (const t of queue) {
      const span = document.createElement("span");
      const c = classify(t);
      if (c) span.className = c;
      span.textContent = t + "\n";
      frag.appendChild(span);
    }
    queue = [];
    out.appendChild(frag);
    while (out.childElementCount > MAX_NODES) out.firstElementChild.remove();
    if (nearBottom) out.scrollTop = out.scrollHeight;
  }

  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
  }

  function setRunning(running, job) {
    runBtns.forEach((b) => (b.disabled = running));
    stopBtn.disabled = !running;
    clearInterval(ticker);
    dotEl.className = "dot" + (running ? " busy" : "");
    if (!running) return;
    const start = performance.now() - job.elapsed * 1000;
    const tick = () => { stateEl.textContent = `Ejecutando ${job.title} · ${fmt((performance.now() - start) / 1000)}`; };
    tick();
    ticker = setInterval(tick, 1000);
  }

  function connect() {
    const es = new EventSource("/stream");   // reconecta solo y retoma desde el último evento

    es.onopen = () => connEl.classList.remove("show");

    es.addEventListener("job", (e) => {
      const job = JSON.parse(e.data);
      out.replaceChildren();
      queue = [];
      setRunning(job.running, job);
      if (!job.running) stateEl.textContent = job.title;
    });

    es.onmessage = (e) => addLine(JSON.parse(e.data));

    es.addEventListener("done", (e) => {
      const d = JSON.parse(e.data);
      setRunning(false);
      if (d.returncode === 0) {
        dotEl.className = "dot ok";
        stateEl.textContent = `Finalizado correctamente (${fmt(d.elapsed)})`;
      } else if (d.returncode < 0) {
        dotEl.className = "dot err";
        stateEl.textContent = `Detenido (${fmt(d.elapsed)})`;
      } else {
        dotEl.className = "dot err";
        stateEl.textContent = `Terminó con error, código ${d.returncode} (${fmt(d.elapsed)})`;
      }
    });

    es.onerror = () => {
      connEl.classList.add("show");
      if (es.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
    };
  }

  async function post(url) {
    try {
      const r = await fetch(url, { method: "POST" });
      if (r.status === 409) { toast("Ya hay una tarea en ejecución."); return false; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return true;
    } catch (err) {
      toast("No se pudo completar la acción: " + err.message);
      return false;
    }
  }

  $("#grid").addEventListener("click", async (e) => {
    const btn = e.target.closest(".run");
    if (!btn || btn.disabled) return;
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    if (await post(`/run/${btn.dataset.key}`)) {
      const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
      out.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "center" });
    }
  });

  stopBtn.addEventListener("click", () => post("/stop"));
  $("#clear").addEventListener("click", () => { out.replaceChildren(); queue = []; });

  $("#copy").addEventListener("click", async () => {
    const text = out.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Sin HTTPS (p. ej. http://IP-tailscale) navigator.clipboard no existe: usamos el método clásico
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
    }
    toast("Salida copiada");
  });

  connect();
</script>
{% endraw %}
</body>
</html>
"""


def sse(data, event=None, id=None):
    msg = ""
    if id:
        msg += f"id: {id}\n"
    if event:
        msg += f"event: {event}\n"
    msg += f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    return msg


@app.route("/")
def index():
    scripts = [
        {"key": k, "title": v["title"], "desc": v["desc"], "confirm": v.get("confirm", "")}
        for k, v in SCRIPTS.items()
    ]
    return render_template_string(HTML_LAYOUT, scripts=scripts)


@app.route("/run/<script_key>", methods=["POST"])
def run_script(script_key):
    if script_key not in SCRIPTS:
        return jsonify(error="Script no válido"), 404
    job = start_job(script_key)
    if job is None:
        return jsonify(error="Ya hay una tarea en ejecución"), 409
    return jsonify(job.info())


@app.route("/stop", methods=["POST"])
def stop():
    return jsonify(stopped=stop_job())


@app.route("/status")
def status():
    with cond:
        job = current_job
    return jsonify(job.info() if job else {"running": False})


@app.route("/stream")
def stream():
    """Server-Sent Events: envía la salida de la tarea actual.
    Al reconectar, retoma desde el último evento recibido (cabecera Last-Event-ID)."""
    seen_job, idx = 0, 0
    last = request.headers.get("Last-Event-ID", "")
    if ":" in last:
        try:
            a, b = last.split(":", 1)
            seen_job, idx = int(a), int(b)
        except ValueError:
            pass

    def gen():
        nonlocal seen_job, idx
        done_sent = None
        yield "retry: 3000\n\n"
        while True:
            chunks = []
            with cond:
                job = current_job
                if job is not None:
                    if job.id != seen_job:
                        seen_job, idx = job.id, 0
                        chunks.append(sse(job.info(), "job"))
                    while idx < len(job.lines):
                        idx += 1
                        chunks.append(sse(job.lines[idx - 1], id=f"{job.id}:{idx}"))
                    if not job.running and done_sent != job.id:
                        done_sent = job.id
                        info = job.info()
                        chunks.append(sse(
                            {"returncode": job.returncode, "elapsed": info["elapsed"]},
                            "done", id=f"{job.id}:{idx}"))
                if not chunks:
                    if not cond.wait(timeout=15):
                        chunks.append(": ping\n\n")  # latido para detectar clientes desconectados
            for chunk in chunks:
                yield chunk

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(gen(), mimetype="text/event-stream", headers=headers)


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
