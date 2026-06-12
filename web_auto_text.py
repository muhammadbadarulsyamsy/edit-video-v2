import os
import sys
import json
import uuid
import shutil
import subprocess
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote
import cgi
from io import BytesIO

BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
PROJECT_DIR = BASE_DIR / "projects"
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

UPLOAD_DIR.mkdir(exist_ok=True)
PROJECT_DIR.mkdir(exist_ok=True)

# Registry job render
JOBS = {}
JOBS_LOCK = threading.Lock()


def format_timestamp(seconds):
    """Konversi detik ke format HH:MM:SS yang dikenali auto_edit_from_text.py"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_script_from_form(data):
    """
    Mengubah input dari form UI (yang mudah diisi orang awam)
    menjadi format script blok yang dikenali auto_edit_from_text.py.
    FUNGSI INI HANYA TRANSLATOR - tidak mengubah logika render.
    """
    lines = []

    # Judul proyek
    title = data.get("title", "Proyek Saya").strip()
    lines.append(f"JUDUL: {title}")
    lines.append("")

    # Pengaturan global (disederhanakan untuk orang awam)
    style = data.get("style", "normal")
    style_map = {
        "normal": "",
        "cinematic": "FORMULA EDITING: Cut 3-5s, zoom subtle, grade cinematic, film grain ringan",
        "dynamic": "FORMULA EDITING: Cut 2-4s, zoom dinamis, grade kontras, glitch halus",
        "calm": "FORMULA EDITING: Cut 4-6s, tanpa zoom, grade warm, tanpa grain",
        "podcast": "FORMULA EDITING: Cut 5-8s, tanpa zoom, grade natural",
    }
    if style_map.get(style):
        lines.append(style_map[style])
        lines.append("")

    # Audio handling
    audio_mode = data.get("audio_mode", "vo_only")
    if audio_mode == "mute_original":
        lines.append("AUDIO: mute original")
    elif audio_mode == "keep_original":
        lines.append("AUDIO: keep original")
    elif audio_mode == "mix_bgm":
        lines.append("AUDIO: mix BGM 30%")
    lines.append("")

    # Scenes (ini bagian utama - hasil dari wizard scene builder)
    scenes = data.get("scenes", [])
    for i, scene in enumerate(scenes, 1):
        start = format_timestamp(float(scene.get("start", 0)))
        end = format_timestamp(float(scene.get("end", 10)))
        narration = scene.get("narration", "").strip()
        subtitle = scene.get("subtitle", "").strip() or narration
        cutaway = scene.get("cutaway", "").strip()

        lines.append(f"=== SCENE {i} ===")
        lines.append(f"TIMESTAMP: {start} – {end}")
        if narration:
            lines.append(f"NARASI: {narration}")
        if subtitle and subtitle != narration:
            lines.append(f"SUBTITLE: {subtitle}")
        if cutaway:
            lines.append(f"CUTAWAY: {cutaway}")
        lines.append("")

    return "\n".join(lines)


class Job:
    def __init__(self, job_id, project_dir):
        self.id = job_id
        self.project_dir = project_dir
        self.status = "queued"
        self.progress = 0
        self.logs = []
        self.output_file = None
        self.error = None
        self.lock = threading.Lock()
        self.created = time.time()

    def log(self, msg):
        with self.lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]

    def to_dict(self):
        with self.lock:
            return {
                "id": self.id,
                "status": self.status,
                "progress": self.progress,
                "logs": self.logs[-100:],
                "output_file": self.output_file,
                "error": self.error,
            }


def run_render(job, script_path, source_path, bgm_path=None, out_name=None):
    """Eksekusi auto_edit_from_text.py di background thread"""
    try:
        job.status = "running"
        job.log("Memulai proses render...")
        job.log(f"Script: {script_path}")
        job.log(f"Video sumber: {source_path}")

        cmd = [
            sys.executable,
            str(BASE_DIR / "auto_edit_from_text.py"),
            "--script", str(script_path),
            "--source", str(source_path),
            "--out", str(job.project_dir / (out_name or "output.mp4")),
        ]
        if bgm_path and os.path.exists(bgm_path):
            cmd.extend(["--bgm", str(bgm_path)])
            job.log(f"BGM: {bgm_path}")

        job.log(f"Perintah: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(BASE_DIR),
        )

        for line in process.stdout:
            line = line.rstrip()
            if line:
                job.log(line)
                # Estimasi progress sederhana berdasarkan keyword
                if "scene" in line.lower() or "cut" in line.lower():
                    job.progress = min(job.progress + 3, 90)
                if "final" in line.lower() or "done" in line.lower() or "selesai" in line.lower():
                    job.progress = 95

        process.wait()
        out_path = job.project_dir / (out_name or "output.mp4")
        if process.returncode == 0 and out_path.exists():
            job.status = "done"
            job.progress = 100
            job.output_file = out_path.name
            job.log("✅ Render selesai!")
        else:
            job.status = "error"
            job.error = f"Proses keluar dengan kode {process.returncode}"
            job.log(f"❌ Gagal: {job.error}")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.log(f"❌ Exception: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # diam

    def _html(self, code, html):
        data = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, mime):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            tpl = TEMPLATE_DIR / "index.html"
            if tpl.exists():
                self._html(200, tpl.read_text(encoding="utf-8"))
            else:
                self._html(500, "<h1>Template tidak ditemukan</h1><p>Jalankan dari folder proyek.</p>")
            return

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            fp = STATIC_DIR / rel
            if fp.exists() and fp.is_file():
                mime = "application/octet-stream"
                if rel.endswith(".css"): mime = "text/css"
                elif rel.endswith(".js"): mime = "application/javascript"
                elif rel.endswith(".png"): mime = "image/png"
                elif rel.endswith(".svg"): mime = "image/svg+xml"
                self._file(fp, mime)
                return

        if path == "/api/jobs":
            with JOBS_LOCK:
                out = {jid: j.to_dict() for jid, j in JOBS.items()}
            self._json(200, out)
            return

        if path.startswith("/api/job/"):
            jid = path.split("/")[-1]
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if job:
                self._json(200, job.to_dict())
            else:
                self._json(404, {"error": "job not found"})
            return

        if path.startswith("/download/"):
            parts = path.split("/")
            if len(parts) >= 4:
                jid = parts[2]
                fname = unquote("/".join(parts[3:]))
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                if job:
                    fp = job.project_dir / fname
                    if fp.exists():
                        self._file(fp, "video/mp4")
                        return
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/build-script":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                self._json(400, {"error": "JSON tidak valid"})
                return
            script_text = build_script_from_form(data)
            self._json(200, {"script": script_text})
            return

        if path == "/api/render":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._json(400, {"error": "Harus multipart/form-data"})
                return

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
            )

            job_id = str(uuid.uuid4())[:8]
            job_dir = PROJECT_DIR / job_id
            job_dir.mkdir(exist_ok=True)

            # Simpan file upload
            source_path = None
            bgm_path = None
            for key in ("source", "bgm"):
                item = form.getvalue(key) if False else form[key] if key in form else None
                if item is None:
                    continue
                if isinstance(item, list):
                    item = item[0]
                if hasattr(item, "filename") and item.filename:
                    fname = Path(item.filename).name
                    dest = job_dir / fname
                    with open(dest, "wb") as f:
                        f.write(item.file.read())
                    if key == "source":
                        source_path = dest
                    elif key == "bgm":
                        bgm_path = dest

            if not source_path:
                shutil.rmtree(job_dir, ignore_errors=True)
                self._json(400, {"error": "Video sumber wajib diunggah"})
                return

            # Ambil data form
            script_text = form.getvalue("script_text") or ""
            form_json = form.getvalue("form_json") or ""
            title = form.getvalue("title") or "Proyek Saya"

            if not script_text and form_json:
                try:
                    data = json.loads(form_json)
                    script_text = build_script_from_form(data)
                except Exception as e:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    self._json(400, {"error": f"Form tidak valid: {e}"})
                    return

            if not script_text.strip():
                shutil.rmtree(job_dir, ignore_errors=True)
                self._json(400, {"error": "Script kosong"})
                return

            script_path = job_dir / "script.txt"
            script_path.write_text(script_text, encoding="utf-8")

            out_name = f"{Path(title).stem or 'output'}_{job_id}.mp4"

            job = Job(job_id, job_dir)
            with JOBS_LOCK:
                JOBS[job_id] = job

            t = threading.Thread(
                target=run_render,
                args=(job, script_path, source_path, bgm_path, out_name),
                daemon=True,
            )
            t.start()

            self._json(200, {"job_id": job_id, "status": "queued"})
            return

        self.send_response(404)
        self.end_headers()


def main():
    host = "127.0.0.1"
    port = int(os.environ.get("PORT", 7860))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"=" * 60)
    print(f"  🎬 Video Editor - Web Interface")
    print(f"  Server berjalan di: http://{host}:{port}")
    print(f"  Buka alamat di atas pada browser Anda.")
    print(f"=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")


if __name__ == "__main__":
    main()