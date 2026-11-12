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

# Cada tarea: title, desc, tag (categoría que se muestra en la ficha), cmd y,
# opcionalmente, confirm (muestra una ventana de confirmación antes de ejecutar).
# El icono pixel-art de cada ficha se define en el JS (objeto ICO) usando la misma clave.
SCRIPTS = {
    "pipeline": {
        "icon": "🎵", "tag": "Biblioteca",
        "title": "Audio Pipeline",
        "desc": "Procesa metadatos en Staging y mueve la música limpia a la biblioteca organizada",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/audio_pipeline.py"],
    },
    "bpm": {
        "icon": "🥁", "tag": "Análisis",
        "title": "Analizar BPM",
        "desc": "Calcula e incrusta el tempo (BPM) en los metadatos ID3 con Essentia/Librosa",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/bpm.py"],
    },
    "autostar": {
        "icon": "⭐", "tag": "Navidrome",
        "title": "AutoStar Navidrome",
        "desc": "Marca como favoritas las canciones que están en tus playlists de Navidrome",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/autostar.py"],
    },
    "covers": {
        "icon": "🖼️", "tag": "Biblioteca",
        "title": "Descargar carátulas",
        "desc": "Busca portadas en cascada (MBZ → IT → DG) y las incrusta en ID3 / cover.jpg",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/download_covers.py"],
    },
    "wantlist": {
        "icon": "📋", "tag": "Discogs",
        "title": "Wantlist de Discogs",
        "desc": "Sincroniza los álbumes de tus playlists de Navidrome con tu Wantlist de Discogs",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/wantlist_from_navidrome.py"],
    },
    "move_dry": {
        "icon": "🔍", "tag": "Playlists",
        "title": "Simular mover playlists",
        "desc": "Muestra qué canciones de cada playlist se moverían a su carpeta, sin tocar nada",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/move_playlist.py", "--dry-run"],
    },
    "move": {
        "icon": "📦", "tag": "Playlists",
        "title": "Mover playlists a carpetas",
        "desc": "Mueve las canciones de cada playlist a su carpeta y reescanea Navidrome",
        "cmd": [VENV_PYTHON, "-u", f"{SCRIPTS_DIR}/move_playlist.py", "--scan"],
        "confirm": "Esto moverá archivos de música a otras carpetas. Conviene haber probado antes la simulación.",
    },
    "clean": {
        "icon": "🧹", "tag": "Mantenimiento",
        "title": "Limpiar carpetas vacías",
        "desc": "Elimina carpetas vacías o que solo contienen cover.jpg en la biblioteca organizada",
        "cmd": ["bash", "-c", CLEAN_SCRIPT, "_", ORGANIZADO_PATH],
        "confirm": "Esto borrará carpetas vacías o con solo cover.jpg en la biblioteca.",
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
        icon = SCRIPTS[job.key].get("icon", "⚙️")
        push(job, f"> {icon} {job.title}")
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
# INTERFAZ  ·  SOFT CLUB × VECTORHEART × CHROMECORE × RETRO WEB  ·  BAYER DITHER
# ---------------------------------------------------------------------------
HTML_LAYOUT = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#cfd9df">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 100 100%27><circle cx=%2750%27 cy=%2750%27 r=%2746%27 fill=%27%23ff5a1f%27/><circle cx=%2750%27 cy=%2750%27 r=%2728%27 fill=%27%23fff%27/><circle cx=%2750%27 cy=%2750%27 r=%2713%27 fill=%27%23ff5a1f%27/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;700;900&amp;family=Silkscreen:wght@400;700&amp;family=IBM+Plex+Mono:wght@400;500&amp;display=swap">
<title>NAVIDROME · PIPELINE</title>
{% raw %}
<style>
:root{--d:"Unbounded","Arial Black",Impact,system-ui,sans-serif;--px:"Silkscreen","Courier New",ui-monospace,monospace;--ui:"Helvetica Neue",Helvetica,Arial,system-ui,sans-serif;
--m:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--d25:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='4' height='4' shape-rendering='crispEdges'><path d='M0 0h1v1H0zM2 0h1v1H2zM0 2h1v1H0zM2 2h1v1H2z'/></svg>");
--d50:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='4' height='4' shape-rendering='crispEdges'><path d='M0 0h1v1H0zM2 0h1v1H2zM1 1h1v1H1zM3 1h1v1H3zM0 2h1v1H0zM2 2h1v1H2zM1 3h1v1H1zM3 3h1v1H3z'/></svg>");
/* skin por defecto: VECTOR (gris azulado + naranja + azul marino) */
--bg:#cfd9df;--paper:#eef3f6;--paper2:#dde6ec;--ink:#14224a;--ink2:#51648c;--line:#14224a;--sd:#14224a;--acc:#ff5a1f;--hd:#ff5a1f;--onacc:#14224a;--ok:#1f8a5a;--err:#d63a1a;--warn:#e0a000;--info:#1d3f9a}
body[data-skin=lime]{--bg:#e8efdc;--paper:#fbfdf6;--paper2:#eef4de;--ink:#17220e;--ink2:#566644;--line:#17220e;--sd:#17220e;--acc:#a8cc3a;--hd:#6e9a14;--onacc:#17220e;--ok:#3f8a1c;--err:#c0432f;--warn:#c99000;--info:#2f6f8a}
body[data-skin=chrome]{--bg:#041412;--paper:#08201e;--paper2:#0c2c29;--ink:#c9f5ea;--ink2:#6fb3a6;--line:#3bb59c;--sd:#010605;--acc:#2fd3ae;--hd:#2fd3ae;--onacc:#03100f;--ok:#6ff0a0;--err:#ff7a6a;--warn:#e6c35a;--info:#7fd6ff}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;min-height:100dvh;background:var(--bg);color:var(--ink);font:400 14px/1.5 var(--ui);overflow-x:hidden;touch-action:manipulation}
:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
#gl{position:fixed;inset:0;width:100%;height:100%;z-index:0;image-rendering:crisp-edges;image-rendering:pixelated}
.app{position:relative;z-index:2;max-width:1320px;margin:0 auto;padding:max(10px,env(safe-area-inset-top)) max(12px,env(safe-area-inset-right)) calc(76px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}
@media(min-width:1000px){.app{padding-bottom:24px}}

/* ── Barra de dirección (retro web) ── */
.bar{display:flex;align-items:center;gap:10px;padding:6px 10px;margin-bottom:12px;background:var(--paper);border:2px solid var(--line);border-radius:10px;box-shadow:3px 3px 0 var(--sd)}
.dots{display:none;gap:4px}.dots i{width:10px;height:10px;border:2px solid var(--line);background:var(--acc)}.dots i:nth-child(2){background:var(--paper2)}.dots i:nth-child(3){background:var(--line)}
@media(min-width:620px){.dots{display:flex}}
.addr{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;padding:6px 10px;font:700 .56rem var(--px);letter-spacing:.08em;background:var(--paper2);border:2px solid var(--line);color:var(--ink2)}
.skins{display:flex;gap:4px;flex:none}
.skins button{appearance:none;cursor:pointer;height:32px;padding:0 9px;border:2px solid var(--line);background:var(--paper2);color:var(--ink);font:700 .48rem var(--px);letter-spacing:.08em}
.skins button[aria-pressed=true]{background:var(--acc);color:var(--onacc);box-shadow:inset 0 0 0 2px var(--paper)}
@media(pointer:coarse){.skins button{height:38px}}
.led{width:12px;height:12px;flex:none;border:2px solid var(--line);background:var(--ok)}body.off .led{background:var(--err);animation:blink .6s steps(2) infinite}
.dm{font:700 1.5rem/1 var(--px);letter-spacing:.06em;color:transparent;-webkit-text-fill-color:transparent;background:radial-gradient(circle,var(--hd) 42%,transparent 46%) 0 0/3px 3px;-webkit-background-clip:text;background-clip:text}
.dm.s{font-size:.95rem;background-size:2px 2px}

/* ── Layout ── */
.main{display:grid;gap:14px}
@media(min-width:1000px){.main{grid-template-columns:minmax(0,1fr) 440px;align-items:start;gap:18px}}
.left{min-width:0}

/* ── Hero: túnel dithered + placa de título ── */
.hero{position:relative;overflow:hidden;min-height:190px;margin-bottom:14px;padding:14px;display:flex;flex-direction:column;justify-content:space-between;gap:14px;border:2px solid var(--line);border-radius:14px;box-shadow:4px 4px 0 var(--sd);
-webkit-backdrop-filter:contrast(1.5) saturate(1.25);backdrop-filter:contrast(1.5) saturate(1.25)}
.target{position:absolute;right:-28px;top:-28px;width:136px;height:136px;border-radius:50%;background:radial-gradient(circle,var(--acc) 0 17%,var(--paper) 17% 44%,var(--acc) 44% 100%);animation:tg 6s steps(12) infinite}
body.run .target{animation-duration:1.2s}
.r1,.r2{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px}
.chip{padding:3px 8px;font:700 .5rem var(--px);letter-spacing:.1em;text-transform:uppercase;background:var(--paper);border:2px solid var(--line);color:var(--ink)}
.chip.o{background:var(--acc);color:var(--onacc)}
.rule{font:400 .48rem var(--px);letter-spacing:.1em;color:var(--paper);text-shadow:1px 1px 0 var(--line)}
.hero h1{position:relative;align-self:flex-start;margin:0;padding:0 14px 7px;font:900 clamp(1.9rem,9vw,4.2rem)/1.05 var(--d);letter-spacing:-.03em;color:var(--hd);background:var(--paper);border:2px solid var(--line);box-shadow:5px 5px 0 var(--sd)}
.jp{position:relative;align-self:flex-start;margin:0;padding:2px 10px;font:700 .72rem "Hiragino Sans","Yu Gothic","Noto Sans JP",var(--ui);letter-spacing:.2em;color:var(--paper);background:var(--line)}
.plate{padding:3px 8px;background:var(--paper);border:2px solid var(--line);display:flex;align-items:center;gap:8px}
.plate small{font:700 .46rem var(--px);letter-spacing:.1em;color:var(--ink2)}

/* ── Fichas de tareas (todas a la vez) ── */
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
@media(min-width:620px){.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
@media(min-width:1000px) and (max-width:1239px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile{position:relative;display:flex;flex-direction:column;gap:8px;min-width:0;padding:11px;overflow:hidden;background:var(--paper);border:2px solid var(--line);border-radius:10px;box-shadow:4px 4px 0 var(--sd);transition:transform .15s,box-shadow .15s}
.tile::before{content:"";position:absolute;right:0;top:0;width:26px;height:26px;background:var(--acc);clip-path:polygon(0 0,100% 0,100% 100%)}
.tile::after{content:"";position:absolute;inset:0;pointer-events:none;background:var(--acc);-webkit-mask:var(--d25) 0 0/8px 8px;mask:var(--d25) 0 0/8px 8px;opacity:0;transition:opacity .2s}
@media(hover:hover){.tile:hover{transform:translate(-2px,-2px);box-shadow:6px 6px 0 var(--sd)}.tile:hover::after{opacity:.28}.tile:hover .ico{animation:jit .4s steps(3)}}
.tile[data-s=run]{border-color:var(--acc);box-shadow:4px 4px 0 var(--acc)}
.th{position:relative;display:flex;align-items:center;gap:6px;padding-right:22px;font:700 .5rem var(--px);letter-spacing:.08em;text-transform:uppercase}
.no{padding:1px 5px;background:var(--line);color:var(--paper)}.tg{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--ink2)}
.st{position:relative;font:700 .5rem var(--px);letter-spacing:.08em;color:var(--ink2)}
.tile[data-s=run] .st{color:var(--warn)}.tile[data-s=ok] .st{color:var(--ok)}.tile[data-s=err] .st,.tile[data-s=stop] .st{color:var(--err)}
.th{flex-wrap:wrap}
.ico{width:42px;height:42px;color:var(--ink)}.ico svg{width:100%;height:100%;fill:currentColor;filter:drop-shadow(2px 2px 0 var(--acc))}
.tile h3{margin:0;font:700 .76rem/1.2 var(--d);letter-spacing:-.01em;text-transform:uppercase}
.tile p{margin:0;flex:1;font-size:.72rem;line-height:1.4;color:var(--ink2);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.warnl{font:700 .48rem var(--px);letter-spacing:.1em;color:var(--warn)}
.run{appearance:none;display:flex;align-items:center;justify-content:center;gap:8px;width:100%;min-height:44px;cursor:pointer;font:700 .62rem var(--px);letter-spacing:.14em;text-transform:uppercase;color:var(--onacc);background:var(--acc);border:2px solid var(--line);border-radius:6px;box-shadow:3px 3px 0 var(--sd);transition:transform .08s,box-shadow .08s}
.run:active:not(:disabled){transform:translate(3px,3px);box-shadow:0 0 0 var(--sd)}
.run:disabled{opacity:.45;cursor:not-allowed}.tile[data-s=run] .run:disabled{opacity:1;background:var(--paper2);color:var(--ink)}
.rs{min-height:1em;font:400 .5rem var(--px);letter-spacing:.06em;color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pg{position:absolute;left:0;right:0;bottom:0;height:7px;background:var(--acc);-webkit-mask:var(--d50) 0 0/4px 4px;mask:var(--d50) 0 0/4px 4px;opacity:0}
.tile[data-s=run] .pg{opacity:1;animation:pgm .5s steps(2) infinite}
@media(max-width:619px){.tile{padding:10px;gap:7px}.tile h3{font-size:.68rem}.tile p{font-size:.68rem;-webkit-line-clamp:2}.ico{width:36px;height:36px}}

/* ── Pie retro ── */
.foot{margin-top:16px;padding:10px;background:var(--paper);border:2px solid var(--line);border-radius:10px;box-shadow:3px 3px 0 var(--sd);display:grid;gap:10px}
.mq{overflow:hidden;white-space:nowrap;border:2px solid var(--line);background:var(--paper2);font:700 .5rem var(--px);letter-spacing:.12em;color:var(--ink)}
.mqt{display:inline-flex;animation:mq 26s linear infinite}.mqt span{padding:5px 0}
.badges{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.b88{display:grid;place-items:center;width:88px;height:31px;border:2px solid var(--line);background:var(--acc);color:var(--onacc);font:700 8px var(--px);letter-spacing:.06em;box-shadow:2px 2px 0 var(--sd)}
.b88.p{background:var(--paper2);color:var(--ink)}.b88.k{background:var(--line);color:var(--paper)}
.haz{height:10px;background:repeating-linear-gradient(-45deg,var(--warn) 0 10px,var(--ink) 10px 20px);border:2px solid var(--line)}
.fn{margin:0;font:400 .5rem var(--px);letter-spacing:.08em;color:var(--ink2)}

/* ── Consola: ventana retro (escritorio) / lámina arrastrable (móvil) ── */
.term{display:flex;flex-direction:column;min-height:0;overflow:hidden;background:var(--paper);border:2px solid var(--line);border-radius:12px;box-shadow:4px 4px 0 var(--sd)}
.tb{appearance:none;display:flex;align-items:center;gap:10px;width:100%;height:44px;flex:none;padding:0 10px;border:0;border-bottom:2px solid var(--line);color:var(--ink);font:inherit;text-align:left;cursor:grab;touch-action:none;
background:repeating-linear-gradient(0deg,var(--line) 0 1px,transparent 1px 4px),var(--paper2)}
.ttl{padding:2px 10px;font:700 .58rem var(--px);letter-spacing:.1em;background:var(--paper);border:2px solid var(--line)}
#state{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font:700 .52rem var(--px);letter-spacing:.08em;color:var(--ink2);background:var(--paper);padding:2px 6px}
#state[data-k=busy]{color:var(--warn)}#state[data-k=ok]{color:var(--ok)}#state[data-k=err]{color:var(--err)}
.tools{display:flex;gap:8px;padding:8px 10px;flex:none;border-bottom:2px solid var(--line);background:var(--paper2)}
.sb{appearance:none;flex:1;min-height:36px;cursor:pointer;font:700 .52rem var(--px);letter-spacing:.1em;text-transform:uppercase;color:var(--ink);background:var(--paper);border:2px solid var(--line);box-shadow:2px 2px 0 var(--sd)}
.sb:active:not(:disabled){transform:translate(2px,2px);box-shadow:none}.sb:disabled{opacity:.4;cursor:not-allowed}
.sb.ok{background:var(--acc);color:var(--onacc)}
@media(pointer:coarse){.sb{min-height:42px}}
.jw{position:relative;flex:1;min-height:0;background:var(--paper)}
#con{position:absolute;inset:0;margin:0;padding:10px 12px 18px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:400 12px/1.65 var(--m);color:var(--ink);touch-action:pan-y;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:var(--line) transparent;
background-image:repeating-linear-gradient(0deg,transparent 0 3px,rgba(128,128,128,.06) 3px 4px)}
#con:empty::before{content:attr(data-e);color:var(--ink2);animation:blink 1s steps(2) infinite}
#con .err{color:var(--err)}#con .warn{color:var(--warn)}#con .ok{color:var(--ok)}#con .info{color:var(--info);font-weight:500}#con .muted{color:var(--ink2);opacity:.7}
.jump{position:absolute;right:12px;bottom:12px;width:40px;height:40px;cursor:pointer;color:var(--onacc);font:700 .8rem var(--px);background:var(--acc);border:2px solid var(--line);box-shadow:2px 2px 0 var(--sd);opacity:0;pointer-events:none;transition:opacity .2s}
.jump.show{opacity:1;pointer-events:auto}
.f2{display:flex;align-items:center;gap:10px;flex:none;padding:6px 10px calc(6px + env(safe-area-inset-bottom,0px));border-top:2px solid var(--line);background:var(--paper2);font:700 .5rem var(--px);letter-spacing:.1em;color:var(--ink2)}
.blk{letter-spacing:.02em;color:var(--hd)}
@media(max-width:999px){
  .term{position:fixed;z-index:30;left:50%;bottom:0;width:min(100%,780px);height:640px;transform:translate(-50%,596px);border-bottom:0;border-radius:12px 12px 0 0;box-shadow:0 -4px 0 var(--sd)}
}
@media(min-width:1000px){
  .term{position:sticky;top:12px;height:calc(100vh - 100px);height:calc(100dvh - 100px);max-height:780px}
  .tb{cursor:default;touch-action:auto}
}

/* ── Ventana de confirmación ── */
dialog{width:min(92vw,420px);padding:0;color:var(--ink);background:var(--paper);border:2px solid var(--line);border-radius:12px;box-shadow:6px 6px 0 var(--sd)}
dialog::backdrop{background:var(--bg);opacity:.7}
dialog[open]{animation:pop .2s steps(4)}
.wtb{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-bottom:2px solid var(--line);font:700 .6rem var(--px);letter-spacing:.12em;background:repeating-linear-gradient(0deg,var(--line) 0 1px,transparent 1px 4px),var(--paper2)}
.wtb span{padding:1px 8px;background:var(--paper);border:2px solid var(--line)}
.wbd{display:flex;gap:14px;align-items:flex-start;padding:18px 16px}
.wic{flex:none;width:44px;height:44px;display:grid;place-items:center;font:900 1.4rem var(--d);color:var(--onacc);background:var(--acc);border:2px solid var(--line);box-shadow:3px 3px 0 var(--sd)}
.wbd p{margin:0;font-size:.9rem}
.wbt{display:flex;gap:10px;padding:14px 16px}
@media(max-width:599px){dialog{width:100%;max-width:100%;margin:auto 0 0;border-radius:12px 12px 0 0;border-bottom:0;padding-bottom:env(safe-area-inset-bottom)}}
#toast{position:fixed;z-index:99;left:50%;top:max(12px,env(safe-area-inset-top));max-width:calc(100% - 24px);padding:8px 14px;font:700 .56rem var(--px);letter-spacing:.1em;text-transform:uppercase;color:var(--onacc);background:var(--acc);border:2px solid var(--line);box-shadow:3px 3px 0 var(--sd);opacity:0;pointer-events:none;transform:translate(-50%,-8px);transition:opacity .15s,transform .15s}
#toast.show{opacity:1;transform:translate(-50%,0)}

@keyframes blink{50%{opacity:0}}@keyframes tg{50%{transform:scale(1.08)}}@keyframes jit{33%{transform:translate(2px,-1px)}66%{transform:translate(-2px,1px)}}
@keyframes pgm{to{-webkit-mask-position:4px 0;mask-position:4px 0}}@keyframes mq{to{transform:translateX(-50%)}}@keyframes pop{from{opacity:0;transform:translateY(8px)}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition-duration:.01ms!important}}
</style>
{% endraw %}
</head>
<body>
{% raw %}<script>try{document.body.dataset.skin=localStorage.getItem('skin')||'vector'}catch(e){document.body.dataset.skin='vector'}</script>{% endraw %}
<canvas id="gl"></canvas>

<div class="app">
  <header class="bar">
    <span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
    <span class="addr">navidrome://pipeline/control</span>
    <span class="skins" role="group" aria-label="Skin">
      <button type="button" data-skin="lime" aria-pressed="false">LIME</button>
      <button type="button" data-skin="vector" aria-pressed="true">VECTOR</button>
      <button type="button" data-skin="chrome" aria-pressed="false">CHROME</button>
    </span>
    <span class="led" id="led" title="Enlace"></span><b class="dm s" id="clk">00:00</b>
  </header>

  <div class="main">
    <div class="left">
      <section class="hero">
        <div class="target" aria-hidden="true"></div>
        <div class="r1"><span class="chip">PIPELINE 32-BIT</span><span class="chip o">FILE UNDER: LIBRARY RUSH</span><span class="rule">-0.5 -0.4 -0.3 -0.2 -0.1 0.0</span></div>
        <div style="display:grid;gap:8px;position:relative">
          <h1>navidrome</h1>
          <p class="jp">ナビドローム ・ パイプライン</p>
        </div>
        <div class="r2"><span class="chip o">MP3·320 44.1KHZ</span><span class="plate"><b class="dm s" id="cnt">0000</b><small>TAREAS LANZADAS</small></span><span class="plate"><b class="dm s" id="tm">00:00</b><small>CRONO</small></span></div>
      </section>

      <section class="grid" id="grid" aria-label="Tareas">
        {% for s in scripts %}
        <article class="tile" data-key="{{ s.key }}" data-s="idle">
          <div class="th"><span class="no">{{ "%02d"|format(loop.index) }}</span><span class="tg">{{ s.tag }}</span><span class="st">LISTO</span></div>
          <div class="ico" data-ico="{{ s.key }}" aria-hidden="true"></div>
          <h3>{{ s.title }}</h3>
          <p>{{ s.desc }}</p>
          {% if s.confirm %}<span class="warnl">▲ PIDE CONFIRMACION</span>{% endif %}
          <button type="button" class="run" data-key="{{ s.key }}" data-title="{{ s.title }}" data-confirm="{{ s.confirm }}"><span>▶</span><span class="t">EJECUTAR</span></button>
          <div class="rs" aria-live="polite"></div>
          <i class="pg"></i>
        </article>
        {% endfor %}
      </section>

      <footer class="foot">
        <div class="mq" aria-hidden="true"><div class="mqt"><span>★ MEJOR VISTO EN CUALQUIER NAVEGADOR ★ EN CONSTRUCCION ★ NAVIDROME PIPELINE ★ 44.1KHZ ★ NINGUN GIF ANIMADO FUE DAÑADO ★&nbsp;</span><span>★ MEJOR VISTO EN CUALQUIER NAVEGADOR ★ EN CONSTRUCCION ★ NAVIDROME PIPELINE ★ 44.1KHZ ★ NINGUN GIF ANIMADO FUE DAÑADO ★&nbsp;</span></div></div>
        <div class="badges"><span class="b88">NAVIDROME</span><span class="b88 p">FLASK</span><span class="b88 k">BAYER 8x8</span><span class="b88 p">800x600 OK</span></div>
        <div class="haz" aria-hidden="true"></div>
        <p class="fn">© 2000 NAVIDROME PIPELINE · TODOS LOS DERECHOS RESERVADOS · FILE UNDER: ADRENALIN RUSH</p>
      </footer>
    </div>

    <section class="term" id="log" aria-label="Consola">
      <button type="button" class="tb" id="grab" aria-expanded="false" aria-controls="con">
        <span class="ttl">CONSOLE.LOG</span><span id="state" role="status" aria-live="polite">LISTO</span>
      </button>
      <div class="tools">
        <button type="button" class="sb" id="stop" disabled>■ DETENER</button>
        <button type="button" class="sb" id="copy">COPIAR</button>
        <button type="button" class="sb" id="clear">LIMPIAR</button>
      </div>
      <div class="jw">
        <pre id="con" data-e="> esperando instrucciones_"></pre>
        <button type="button" class="jump" id="jump" aria-label="Ir al final">▼</button>
      </div>
      <div class="f2"><span class="blk" id="blk">░░░░░░░░░░</span><span id="st2">IDLE</span></div>
    </section>
  </div>
</div>

<dialog id="dlg" aria-labelledby="dT">
  <form method="dialog">
    <div class="wtb"><span id="dT">ALERTA</span><i aria-hidden="true">▪ ▪</i></div>
    <div class="wbd"><div class="wic" aria-hidden="true">!</div><p id="dM"></p></div>
    <div class="haz" style="margin:0 16px"></div>
    <div class="wbt"><button class="sb" value="cancel">CANCELAR</button><button class="sb ok" value="ok" autofocus>CONTINUAR</button></div>
  </form>
</dialog>
<div id="toast" role="status" aria-live="polite"></div>

{% raw %}
<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const RM=matchMedia('(prefers-reduced-motion:reduce)').matches,body=document.body,BT=document.title,clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
const DESK=()=>innerWidth>=1000,pad=(n,l)=>String(n).padStart(l,'0');
const fmt=s=>{s=Math.round(s);const m=Math.floor(s/60);return m?`${m}m ${s%60}s`:`${s}s`},mmss=s=>pad(Math.floor(s/60)%100,2)+':'+pad(Math.floor(s%60),2);
let W=innerWidth,H=innerHeight,run=false,PX=innerWidth<700?3:4;

/* ═══ ICONOS PIXEL-ART 8×8 ═══ */
const ICO={
pipeline:['..######','..#....#','..#....#','..#....#','.##...##','###..###','.#...##.','........'],
bpm:['......#.','..#...#.','..#.#.#.','#.#.#.#.','#.#.###.','#####.#.','########','........'],
autostar:['...##...','...##...','########','.######.','..####..','.######.','.##..##.','.#....#.'],
covers:['########','#......#','#..##..#','#..##.##','#....###','#..#####','#.######','########'],
wantlist:['##.#####','........','##.#####','........','##.#####','........','##.#####','........'],
move_dry:['.#####..','#.....#.','#.....#.','#.....#.','.#####..','.....##.','......##','.......#'],
move:['###.....','#.######','#......#','#..##..#','#..##..#','#.####.#','#..##..#','########'],
clean:['..####..','########','.#.##.#.','.#.##.#.','.#.##.#.','.#.##.#.','.######.','........']};
const icon=r=>{let s='<svg viewBox="0 0 8 8" shape-rendering="crispEdges">';r.forEach((row,y)=>[...row].forEach((c,x)=>{if(c==='#')s+=`<rect x="${x}" y="${y}" width="1" height="1"/>`}));return s+'</svg>'};
$$('.ico').forEach(el=>el.innerHTML=icon(ICO[el.dataset.ico]||ICO.pipeline));

/* ═══ FONDO: túnel procedural con Bayer dithering 8×8 (WebGL, baja resolución) ═══ */
const FS=`#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif
uniform vec2 R;uniform vec2 M;uniform float T;uniform vec3 C0;uniform vec3 C1;uniform vec3 C2;uniform vec3 C3;uniform vec4 P[4];
float sq(float x){return x*x;}
float b2(vec2 a){a=floor(a);return fract(a.x/2.+a.y*a.y*.75);}
float b4(vec2 a){return b2(.5*a)*.25+b2(a);}
float b8(vec2 a){return b4(.5*a)*.25+b2(a);}
void main(){
  vec2 fc=gl_FragCoord.xy;
  vec2 p=(fc-vec2(.5*R.x,.7*R.y))/R.y;
  p-=((M-.5*R)/R.y)*.22;
  float r=length(p)+.0001,a=atan(p.y,p.x);
  float z=.28/(r+.02)+T*.45;
  float ribs=.5+.5*sin(z*9.4247);
  float seg=smoothstep(.02,.1,abs(fract(a/6.2831*10.+.5)-.5));
  float v=.30+.34*ribs*seg;
  v+=.55*exp(-r*r*22.);
  float strip=smoothstep(.16,.1,abs(a-1.5708))*step(.45,fract(z*.8));
  v+=.5*strip*smoothstep(.1,.5,r);
  v*=.55+.7*smoothstep(1.1,.15,r);
  float rg=0.;
  for(int i=0;i<4;i++){
    vec4 q=P[i];
    if(q.w>0.){float age=T-q.z;float d=length(fc-q.xy)/R.y;rg+=exp(-sq((d-age*.5)*16.))*exp(-age*1.2)*q.w;}
  }
  v+=rg*.7;
  v+=.22*exp(-sq(length(fc-M)/R.y*5.));
  v=clamp(v,0.,1.);
  vec2 q8=mod(floor(fc),8.);
  float lv=clamp(v+(b8(q8)-.5)*.42,0.,.999);
  vec3 c=C0;c=mix(c,C1,step(.25,lv));c=mix(c,C2,step(.5,lv));c=mix(c,C3,step(.75,lv));
  gl_FragColor=vec4(c,1.);
}`;
const SK={lime:['#9fbe34','#c2d968','#e2eeb0','#f6faec'],vector:['#2b4a9a','#6f8fcf','#b4c6e6','#e1e8f0'],chrome:['#020b0a','#0a3330','#1c8f7c','#86ead0']};
const TC={lime:'#e8efdc',vector:'#cfd9df',chrome:'#041412'},hx=h=>[1,3,5].map(i=>parseInt(h.substr(i,2),16)/255);
const cv=$('#gl'),U={},pulses=[],pa=new Float32Array(16);let gl=null,glok=false,pal=SK.vector.map(hx),tt=0;
try{gl=cv.getContext('webgl',{antialias:false,alpha:false,powerPreference:'low-power'})}catch(e){}
if(gl){
  const sh=(t,s)=>{const o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);return o},pg=gl.createProgram();
  gl.attachShader(pg,sh(gl.VERTEX_SHADER,'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}'));gl.attachShader(pg,sh(gl.FRAGMENT_SHADER,FS));gl.linkProgram(pg);
  if(gl.getProgramParameter(pg,gl.LINK_STATUS)){
    gl.useProgram(pg);const bf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,bf);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
    const al=gl.getAttribLocation(pg,'a');gl.enableVertexAttribArray(al);gl.vertexAttribPointer(al,2,gl.FLOAT,false,0,0);
    for(const n of['R','M','T','C0','C1','C2','C3','P'])U[n]=gl.getUniformLocation(pg,n);glok=true}}
if(!glok)cv.style.display='none';
function size(){W=innerWidth;H=innerHeight;PX=W<700?3:4;cv.width=Math.ceil(W/PX);cv.height=Math.ceil(H/PX)}size();
function pulse(x,y,a){if(RM||!glok)return;pulses.push({x,y,t:tt,a});if(pulses.length>4)pulses.shift()}
let mx=W/2,my=H*.3,tx=mx,ty=my,lpx=0,lpy=0,lpt=0;
function draw(){
  if(!glok)return;const t=Math.floor(tt*12)/12;   // el tiempo avanza a 12 fps: animación escalonada tipo GIF
  while(pulses.length&&t-pulses[0].t>4)pulses.shift();
  for(let i=0;i<4;i++){const p=pulses[i];if(p){pa[i*4]=p.x/PX;pa[i*4+1]=(H-p.y)/PX;pa[i*4+2]=p.t;pa[i*4+3]=p.a}else pa[i*4+3]=0}
  gl.viewport(0,0,cv.width,cv.height);gl.uniform2f(U.R,cv.width,cv.height);gl.uniform2f(U.M,mx/PX,(H-my)/PX);gl.uniform1f(U.T,t);
  gl.uniform3fv(U.C0,pal[0]);gl.uniform3fv(U.C1,pal[1]);gl.uniform3fv(U.C2,pal[2]);gl.uniform3fv(U.C3,pal[3]);gl.uniform4fv(U.P,pa);gl.drawArrays(gl.TRIANGLES,0,3)}
addEventListener('pointerdown',e=>pulse(e.clientX,e.clientY,.9),{passive:true});
addEventListener('pointermove',e=>{tx=e.clientX;ty=e.clientY;const n=performance.now(),d=Math.hypot(tx-lpx,ty-lpy);
  if(d>120&&n-lpt>180){pulse(tx,ty,.4);lpx=tx;lpy=ty;lpt=n}},{passive:true});

/* ═══ SKINS ═══ */
function setSkin(k){
  if(!SK[k])k='vector';body.dataset.skin=k;try{localStorage.setItem('skin',k)}catch(e){}
  $$('.skins button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.skin===k));$('meta[name=theme-color]').content=TC[k];pal=SK[k].map(hx);pulse(W/2,H*.3,1)}
$$('.skins button').forEach(b=>b.onclick=()=>setSkin(b.dataset.skin));

/* ═══ HUD: reloj, contador de visitas retro ═══ */
const clk=()=>{const d=new Date();$('#clk').textContent=pad(d.getHours(),2)+':'+pad(d.getMinutes(),2)};clk();setInterval(clk,15000);
let runs=0;try{runs=+localStorage.getItem('runs')||0}catch(e){}
const showRuns=()=>$('#cnt').textContent=pad(runs%10000,4);showRuns();

/* ═══ LÁMINA DE CONSOLA (móvil): muelle con inercia ═══ */
const log=$('#log'),grab=$('#grab');
let PEEK=44,SH=640,snaps=[44,300,640],sn=0,vis=44,vv=0,lvis=-1,sdrag=false,smoved=false,sy0=0,sv0=0,svel=0,sly=0;
function sheetLayout(){
  if(DESK()){log.style.height='';log.style.transform='';lvis=-1;return}
  PEEK=44;const mid=Math.max(PEEK+150,Math.round(H*.45));SH=Math.max(mid+120,Math.round(Math.min(H*.86,720)));snaps=[PEEK,mid,SH];log.style.height=SH+'px';lvis=-1}
grab.addEventListener('pointerdown',e=>{if(DESK())return;sdrag=true;smoved=false;sy0=sly=e.clientY;sv0=vis;svel=0;grab.setPointerCapture(e.pointerId)});
grab.addEventListener('pointermove',e=>{
  if(!sdrag)return;const dy=sy0-e.clientY;if(Math.abs(dy)>6)smoved=true;if(!smoved)return;
  vis=clamp(sv0+dy,PEEK-12,SH+24);svel=sly-e.clientY;sly=e.clientY});
const sUp=()=>{if(!sdrag)return;sdrag=false;
  if(!smoved){sn=sn===0?1:0;return}
  const p=vis+svel*9;let b=0;snaps.forEach((s,i)=>{if(Math.abs(s-p)<Math.abs(snaps[b]-p))b=i});sn=b};
grab.addEventListener('pointerup',sUp);grab.addEventListener('pointercancel',sUp);
grab.addEventListener('keydown',e=>{if(!DESK()&&(e.key==='Enter'||e.key===' ')){e.preventDefault();sn=sn===0?1:0}});
addEventListener('keydown',e=>{if(e.key==='Escape')sn=0});
function sheetStep(){
  if(DESK())return;
  if(!sdrag){vv=(vv+(snaps[sn]-vis)*.12)*.74;vis+=vv;if(Math.abs(vv)<.02&&Math.abs(snaps[sn]-vis)<.3){vis=snaps[sn];vv=0}}
  if(vis!==lvis){log.style.transform=`translate(-50%,${(SH-vis).toFixed(1)}px)`;lvis=vis;grab.setAttribute('aria-expanded',vis>PEEK+30)}}

/* ═══ Toast + confirmación ═══ */
let toastT=null;
function toast(m){const el=$('#toast');el.textContent=m;el.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),3000)}
const dlg=$('#dlg');
function ask(title,msg){
  if(typeof dlg.showModal!=='function')return Promise.resolve(confirm(msg));
  $('#dT').textContent=title.toUpperCase();$('#dM').textContent=msg;dlg.returnValue='';
  return new Promise(res=>{dlg.addEventListener('close',()=>res(dlg.returnValue==='ok'),{once:true});dlg.showModal()})}
async function post(url){
  try{const r=await fetch(url,{method:'POST'});if(r.status===409){toast('Ya hay una tarea en curso');return false}if(!r.ok)throw new Error('HTTP '+r.status);return true}
  catch(err){toast('Sin enlace: '+err.message);return false}}

/* ═══ Consola y tareas ═══ */
const con=$('#con'),stateEl=$('#state'),jump=$('#jump'),byKey=Object.fromEntries($$('.tile').map(t=>[t.dataset.key,t]));
let queue=[],qraf=0,stick=true,ticker=null,blkT=null,curJob=null;
function classify(t){
  if(/\[(ERROR|FAIL)\]|\bTraceback\b|\bException\b|❌|✗ /.test(t))return'err';
  if(/\[(DUPLICATE|EXISTS|DETENIDO|WARN(ING)?)\]|⚠/i.test(t))return'warn';
  if(/\[(OK|REPLACED|REMOVED|BORRADO)\b|✅|✓|✔/.test(t))return'ok';
  if(/^>|^[─━-]{6,}|^\s*━━|\[dry-run\]|🔄/i.test(t))return'info';
  if(/\[SKIP\]/.test(t))return'muted';return''}
function addLine(t){queue.push(t);if(!qraf)qraf=requestAnimationFrame(flush)}
function flush(){
  qraf=0;const fr=document.createDocumentFragment();
  for(const t of queue){const s=document.createElement('span'),c=classify(t);if(c)s.className=c;s.textContent=t+'\n';fr.appendChild(s)}
  queue=[];con.appendChild(fr);while(con.childElementCount>3000)con.firstElementChild.remove();
  if(stick)con.scrollTop=con.scrollHeight}
con.addEventListener('scroll',()=>{stick=con.scrollHeight-con.scrollTop-con.clientHeight<60;jump.classList.toggle('show',!stick)},{passive:true});
jump.onclick=()=>{stick=true;con.scrollTop=con.scrollHeight;jump.classList.remove('show')};
function resetOut(){con.replaceChildren();queue=[];stick=true;jump.classList.remove('show')}
$('#clear').onclick=resetOut;$('#stop').onclick=()=>post('/stop');
$('#copy').onclick=async()=>{
  const t=con.textContent;
  try{await navigator.clipboard.writeText(t)}catch{const a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;opacity:0';body.appendChild(a);a.select();document.execCommand('copy');a.remove()}
  toast('Consola copiada')};
function setState(t,k){stateEl.textContent=t.toUpperCase();stateEl.dataset.k=k||''}
const SL={idle:'LISTO',run:'EN CURSO',ok:'HECHO',err:'ERROR',stop:'PARADO'};
function mark(k,s,txt){const t=byKey[k];if(!t)return;s=s||'idle';t.dataset.s=s;t.querySelector('.st').textContent=SL[s];t.querySelector('.rs').textContent=txt||''}
function setRunning(on,job){
  run=on;body.classList.toggle('run',on);$$('.run').forEach(b=>b.disabled=on);$('#stop').disabled=!on;
  clearInterval(ticker);clearInterval(blkT);document.title=on?`▶ ${job.title} · ${BT}`:BT;
  if(!on){$('#blk').textContent='░░░░░░░░░░';$('#st2').textContent='IDLE';return}
  const t0=performance.now()-job.elapsed*1000;
  const tick=()=>{const s=(performance.now()-t0)/1000;$('#tm').textContent=mmss(s);setState(`${job.title} · ${fmt(s)}`,'busy')};tick();ticker=setInterval(tick,1000);
  let n=0;$('#st2').textContent='BUSY';blkT=setInterval(()=>{n=(n+1)%11;$('#blk').textContent='█'.repeat(n)+'░'.repeat(10-n)},180)}

function connect(){
  const es=new EventSource('/stream');
  es.onopen=()=>body.classList.remove('off');
  es.addEventListener('job',e=>{
    const j=JSON.parse(e.data);curJob=j;resetOut();
    for(const k in byKey)if(k!==j.key&&byKey[k].dataset.s==='run')mark(k,'');
    mark(j.key,j.running?'run':'',j.running?'PROCESANDO…':'');setRunning(j.running,j);
    if(j.running){if(!DESK()&&sn<1)sn=1}else{setState(j.title,'');$('#tm').textContent=mmss(j.elapsed)}});
  es.onmessage=e=>addLine(JSON.parse(e.data));
  es.addEventListener('done',e=>{
    const d=JSON.parse(e.data),k=curJob&&curJob.key;setRunning(false);$('#tm').textContent=mmss(d.elapsed);
    if(d.returncode===0){setState(`Hecho · ${fmt(d.elapsed)}`,'ok');mark(k,'ok',`OK ${fmt(d.elapsed)}`)}
    else if(d.returncode<0){setState(`Parado · ${fmt(d.elapsed)}`,'err');mark(k,'stop',`PARADO ${fmt(d.elapsed)}`)}
    else{setState(`Error ${d.returncode} · ${fmt(d.elapsed)}`,'err');mark(k,'err',`ERR ${d.returncode} · ${fmt(d.elapsed)}`)}});
  es.onerror=()=>{body.classList.add('off');if(es.readyState===EventSource.CLOSED)setTimeout(connect,3000)}}

$('#grid').addEventListener('click',async e=>{
  const b=e.target.closest('.run');if(!b||b.disabled)return;
  if(b.dataset.confirm&&!(await ask(b.dataset.title,b.dataset.confirm)))return;
  if(await post('/run/'+b.dataset.key)){runs++;try{localStorage.setItem('runs',runs)}catch(err){}showRuns()}});

/* ═══ Bucle ═══ */
let lt=performance.now(),fr=0;
function frame(){
  const n=performance.now(),dt=Math.min(.1,(n-lt)/1000);lt=n;
  if(!RM)tt+=dt*(run?2.2:.55);
  mx+=(tx-mx)*.15;my+=(ty-my)*.15;
  if(++fr%2===0)draw();
  sheetStep();requestAnimationFrame(frame)}
addEventListener('resize',()=>{size();sheetLayout()});
setSkin(body.dataset.skin);sheetLayout();vis=snaps[0];connect();frame();
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
        {
            "key": k,
            "title": v["title"],
            "desc": v["desc"],
            "tag": v.get("tag", "Tarea"),
            "confirm": v.get("confirm", ""),
        }
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