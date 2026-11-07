import os
import sys
import time
import threading
import subprocess
from pathlib import Path
from flask import Flask, render_template_string, Response
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

app = Flask(__name__)

# Ruta explícita del ejecutable de Python dentro del entorno virtual (venv)
VENV_PYTHON = "/home/escriva/venv/bin/python3"

# Ruta de monitoreo automático para descargas entrantes
DOWNLOADS_PATH = "/home/escriva/music/baldman/ingresos"

# Configuración de los scripts del pipeline de Navidrome
# NOTA: Se añade "-u" a los comandos de Python para desactivar el buffering de stdout/stderr
SCRIPTS = {
    "pipeline": {
        "title": "🎵 Ejecutar Audio Pipeline",
        "desc": "Procesa metadatos en Staging y mueve la música limpia a la biblioteca organizada",
        "cmd": [VENV_PYTHON, "-u", "scripts/audio_pipeline.py"]
    },
    "bpm": {
        "title": "🥁 Analizar BPM",
        "desc": "Calcula e incrusta el tempo (BPM) en los metadatos ID3 usando Essentia/Librosa",
        "cmd": [VENV_PYTHON, "-u", "scripts/bpm.py"]
    },
    "autostar": {
        "title": "⭐ AutoStar Navidrome",
        "desc": "Califica o marca con estrellas automáticamente las canciones/álbumes según reproducción",
        "cmd": [VENV_PYTHON, "-u", "scripts/autostar.py"]
    },
    "covers": {
        "title": "🖼️ Descargar Carátulas (MBZ → IT → DG)",
        "desc": "Búsqueda en cascada de portadas de los álbumes de Navidrome e incrustado en ID3 / cover.jpg",
        "cmd": [VENV_PYTHON, "-u", "scripts/download_covers.py"]
    },
    "wantlist": {
        "title": "📋 Wantlist Discogs desde Navidrome",
        "desc": "Sincroniza álbumes de tus playlists de Navidrome directamente con tu Wantlist de Discogs",
        "cmd": [VENV_PYTHON, "-u", "scripts/wantlist_from_navidrome.py"]
    },
    "clean": {
        "title": "🧹 Limpiar Carpetas Vacías / Solo cover.jpg",
        "desc": "Elimina directorios huérfanos o carpetas residuales en la biblioteca organizada",
        "cmd": ["bash", "-c", '''find /home/escriva/music/baldman/SINFI/Organizado -type d -exec bash -c '
for dir; do
  count=$(find "$dir" -mindepth 1 -maxdepth 1 | wc -l)
  if [ "$count" -eq 0 ]; then
    echo "[BORRADO] Carpeta vacía: $dir"
    rm -rf "$dir"
  elif [ "$count" -eq 1 ] && [ -f "$dir/cover.jpg" -o -f "$dir/COVER.JPG" -o -f "$dir/Cover.jpg" ]; then
    echo "[BORRADO] Carpeta con solo cover.jpg: $dir"
    rm -rf "$dir"
  fi
done' _ {} +''']
    }
}

# --- LÓGICA DE AUTO-EJECUCIÓN AL DETECTAR NUEVOS ARCHIVOS ---
is_pipeline_running = False

def run_pipeline_automatically(filepath):
    global is_pipeline_running
    if is_pipeline_running:
        print(f"⚠️ Detectado nuevo archivo '{filepath}', pero el pipeline ya está ejecutándose. Omitiendo...")
        return

    is_pipeline_running = True
    print(f"\n⚡ [AUTO-TRIGGER] Nuevo archivo detectado: {filepath}")
    print("⏳ Esperando 5 segundos a que finalice la copia/descarga del archivo...")
    time.sleep(5)

    print(f"🚀 Ejecutando Audio Pipeline automáticamente con {VENV_PYTHON}...")
    try:
        cmd = SCRIPTS["pipeline"]["cmd"]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"  # Desactiva buffering en variables de entorno

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        for line in iter(process.stdout.readline, ''):
            if line:
                print(f"[AUTO-PIPELINE] {line.strip()}", flush=True)
        process.stdout.close()
        process.wait()
        print("✅ [AUTO-TRIGGER] Pipeline finalizado con éxito.\n", flush=True)
    except Exception as e:
        print(f"❌ [AUTO-TRIGGER] Error ejecutando el pipeline automático: {e}\n", flush=True)
    finally:
        is_pipeline_running = False

class AutoPipelineHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        filename = Path(event.src_path).name
        if filename.startswith(".") or filename.endswith(".tmp") or filename.endswith(".part"):
            return

        threading.Thread(target=run_pipeline_automatically, args=(event.src_path,), daemon=True).start()

def start_folder_watcher():
    path_to_watch = DOWNLOADS_PATH
    if not os.path.exists(path_to_watch):
        os.makedirs(path_to_watch, exist_ok=True)

    event_handler = AutoPipelineHandler()
    observer = Observer()
    observer.schedule(event_handler, path=path_to_watch, recursive=True)
    observer.start()
    print(f"👁️ Monitor activo escuchando cambios en: {path_to_watch}", flush=True)

start_folder_watcher()

# --- DISEÑO HTML CON SALIDA DE CONSOLA EN TIEMPO REAL ---
HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Navidrome Pipeline Control</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
        .container { max-width: 950px; margin: 0 auto; }
        h1 { color: #fff; margin-bottom: 5px; }
        p.subtitle { color: #888; margin-top: 0; margin-bottom: 15px; }
        .status-badge { display: inline-block; background: #1b3a20; color: #00ff66; border: 1px solid #2e6635; padding: 6px 12px; border-radius: 20px; font-size: 0.85rem; margin-bottom: 25px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .card { background: #1e1e1e; border: 1px solid #333; border-radius: 8px; padding: 15px; display: flex; flex-direction: column; justify-content: space-between; }
        .card h3 { margin-top: 0; margin-bottom: 8px; font-size: 1.1rem; }
        .card p { color: #aaa; font-size: 0.85rem; margin: 0 0 15px 0; flex-grow: 1; }
        .btn { background: #007bff; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-weight: bold; text-align: center; text-decoration: none; }
        .btn:hover { background: #0056b3; }
        .btn:disabled { background: #444; cursor: not-allowed; }
        .console-container { background: #0d0d0d; border: 1px solid #333; border-radius: 8px; padding: 15px; }
        .console-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; border-bottom: 1px solid #222; padding-bottom: 8px; }
        .console-status { font-size: 0.85rem; color: #888; }
        pre#console { background: transparent; color: #00ff66; padding: 0; margin: 0; font-family: monospace; font-size: 0.85rem; height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎛️ Navidrome Pipeline Control</h1>
        <p class="subtitle">Panel Web de gestión de scripts accesible por Tailscale</p>
        <div class="status-badge">🟢 Auto-Monitor Activo: <code>/home/escriva/music/baldman/ingresos</code></div>

        <div class="grid">
            {% for key, script in scripts.items() %}
            <div class="card">
                <div>
                    <h3>{{ script.title }}</h3>
                    <p>{{ script.desc }}</p>
                </div>
                <button class="btn" onclick="runScript('{{ key }}')">Ejecutar</button>
            </div>
            {% endfor %}
        </div>

        <div class="console-container">
            <div class="console-header">
                <strong>Salida de Consola en Tiempo Real</strong>
                <span id="status" class="console-status">Estado: Inactivo</span>
            </div>
            <pre id="console">> Presiona un botón para ejecutar una tarea manualmente...</pre>
        </div>
    </div>

    <script>
        let currentEventSource = null;

        function runScript(key) {
            const consoleEl = document.getElementById("console");
            const statusEl = document.getElementById("status");
            const buttons = document.querySelectorAll(".btn");

            if (currentEventSource) {
                currentEventSource.close();
            }

            buttons.forEach(b => b.disabled = true);
            consoleEl.textContent = `> Iniciando ejecución de ${key}...\n----------------------------------------\n`;
            statusEl.textContent = "Estado: Ejecutando...";

            currentEventSource = new EventSource(`/run/${key}`);

            currentEventSource.onmessage = function(event) {
                consoleEl.textContent += event.data + "\n";
                consoleEl.scrollTop = consoleEl.scrollHeight;
            };

            currentEventSource.onerror = function() {
                currentEventSource.close();
                currentEventSource = null;
                statusEl.textContent = "Estado: Finalizado / Inactivo";
                buttons.forEach(b => b.disabled = false);
                consoleEl.textContent += "\n[PROCESO FINALIZADO]";
                consoleEl.scrollTop = consoleEl.scrollHeight;
            };
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_LAYOUT, scripts=SCRIPTS)

@app.route("/run/<script_key>")
def run_script(script_key):
    if script_key not in SCRIPTS:
        return Response("Script no válido", status=400)

    cmd = SCRIPTS[script_key]["cmd"]

    def generate():
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )

        for line in iter(process.stdout.readline, ''):
            if line:
                # Transmite en formato Server-Sent Events (SSE) al navegador
                yield f"data: {line.rstrip()}\n\n"

        process.stdout.close()
        process.wait()

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)