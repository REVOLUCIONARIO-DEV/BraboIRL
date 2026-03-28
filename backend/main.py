"""
Brabo IRL - Backend Principal
Modo servidor: relay SRT→RTMP sobe automaticamente no startup
Modo transmissor: envia stream via SRT para o servidor
"""

import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
import glob
import re
from pathlib import Path
import shutil
from typing import Optional

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ─── Detecta .exe (PyInstaller) ───────────────────────────────────────────────
IS_FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys._MEIPASS) if IS_FROZEN else Path(__file__).parent.parent

# ─── Configuração ─────────────────────────────────────────────────────────────
APP_MODE = os.getenv("BRABO_MODE", "server")
DISCOVERY_PORT = 5353
CONTROL_PORT = 8080
SRT_PORT = 9999
RTMP_PORT = 1935
BROADCAST_INTERVAL = 2

app = FastAPI(title="Brabo IRL", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Modelos ──────────────────────────────────────────────────────────────────

class StreamConfig(BaseModel):
    bitrate: int = 4000
    codec: str = "h264"
    resolution: str = "1920x1080"
    fps: int = 30
    audio_bitrate: int = 128
    drop_buffer: bool = False


class ModeRequest(BaseModel):
    mode: str


class DeviceConfig(BaseModel):
    source: str = "camera"
    camera_id: Optional[str] = None
    mic_id: Optional[str] = None

    camera_name: Optional[str] = None
    mic_name: Optional[str] = None


# ─── Estado Global ─────────────────────────────────────────────────────────────

state = {
    "mode": APP_MODE,
    "streaming": False,
    "relay_active": False,
    "client_connected": False,
    "connected_server": None,
    "config": StreamConfig(),
    "device": {
        "source": "camera",
        "camera_id": None,
        "mic_id": None,
        "camera_name": None,
        "mic_name": None,
    },
    "stats": {
        "bitrate_kbps": 0,
        "latency_ms": 0,
        "dropped_packets": 0,
        "uptime_seconds": 0,
        "cpu_percent": 0,
        "mem_percent": 0,
    },
    "discovered_servers": {},
    "ffmpeg_proc": None,
    "relay_proc": None,
    "start_time": None,
    "device_id": str(uuid.uuid4())[:8],
    "device_name": f"Brabo-{socket.gethostname()}",
    # ── Reconexão automática ──────────────────────────────────────────
    "reconnect_target": None,   # {"host": str, "port": int} salvo ao iniciar stream
    "reconnect_enabled": True,  # False quando o usuário para manualmente
    "reconnect_attempt": 0,     # contador de tentativas consecutivas
    "stream_start_time": None,  # hora em que o stream foi iniciado (preservada entre reconexões)
}

connected_clients: list[WebSocket] = []
_bg_tasks: list = []

# ─── FFmpeg helpers ────────────────────────────────────────────────────────────

def ffmpeg_bin() -> str:
    if IS_FROZEN:
        candidate = BASE_DIR / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"


def popen_kwargs() -> dict:
    """Oculta janela do console no Windows."""
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

def clean_device_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None

    # Remove qualquer coisa entre parênteses (ex: "(13d3:5415)")
    name = re.sub(r"\s*\(.*?\)", "", name)

    return name.strip()


def build_transmitter_cmd(config: StreamConfig, target: str, is_buffer: bool = False) -> list:
    ff = ffmpeg_bin()
    system = platform.system()
    dev = state.get("device", {})
    source = dev.get("source", "camera")

    video_input: list[str] = []
    audio_input: list[str] = []

    if source == "screen":
        if system == "Windows":
            video_input = ["-f", "gdigrab", "-framerate", str(config.fps), "-i", "desktop"]
        elif system == "Linux":
            display = os.environ.get("DISPLAY", ":0")
            video_input = ["-f", "x11grab", "-framerate", str(config.fps), "-video_size", config.resolution, "-i", display]
        else:
            video_input = ["-f", "avfoundation", "-framerate", str(config.fps), "-i", "1:none"]
    else:
        cam_id = dev.get("camera_id")
        mic_id = dev.get("mic_id")
        cam_name = dev.get("camera_name")
        mic_name = dev.get("mic_name")

        if system == "Windows":
            cam = clean_device_name(cam_name) or "Integrated Camera"
            video_input = ["-f", "dshow", "-framerate", str(config.fps), "-i", f"video={cam}"]
            if mic_name:
                audio_input = ["-f", "dshow", "-i", f"audio={mic_name}"]

        elif system == "Linux":
            cam = cam_id or "/dev/video0"
            video_input = ["-f", "v4l2", "-framerate", str(config.fps), "-video_size", config.resolution, "-i", cam]
            mic = mic_id or "default"
            audio_input = ["-f", "alsa", "-i", mic]

        else:
            cam = cam_id or "0"
            video_input = ["-f", "avfoundation", "-framerate", str(config.fps), "-video_size", config.resolution, "-i", f"{cam}:none"]
            if mic_id:
                audio_input = ["-f", "avfoundation", "-i", f"none:{mic_id}"]

    vcodec = "libx264" if config.codec == "h264" else "libx265"
    
    cmd = [
        ff, "-y", # Sobrescreve arquivos existentes
        *video_input,
        *audio_input,
        "-c:v", vcodec,
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-b:v", f"{config.bitrate}k",
        "-maxrate", f"{config.bitrate}k",
        "-bufsize", f"{config.bitrate * 2}k",
        "-r", str(config.fps),
        "-g", str(config.fps * 2),
    ]

    if audio_input:
        cmd += ["-c:a", "aac", "-b:a", f"{config.audio_bitrate}k"]
    else:
        cmd += ["-an"]

    if is_buffer:
        # Segmenta o vídeo em arquivos .ts de 2 segundos no SSD
        cmd += [
            "-f", "segment",
            "-segment_time", "2",
            "-segment_format", "mpegts",
            "-reset_timestamps", "1",
            target
        ]
    else:
        # Envia direto sem buffer
        cmd += ["-f", "mpegts", target]

    return cmd


def build_relay_cmd() -> list:
    """Relay SRT listener → UDP (Para o OBS Studio local)."""
    ff = ffmpeg_bin()
    return [
        ff,
        "-i", f"srt://0.0.0.0:{SRT_PORT}?mode=listener&timeout=0",
        "-c", "copy",
        "-f", "mpegts",
        "udp://127.0.0.1:8282?pkt_size=1316",
    ]


# ─── Relay automático do servidor ─────────────────────────────────────────────

async def run_relay():
    """
    Sobe o relay SRT→RTMP e fica monitorando.
    Quando o transmissor conecta, o FFmpeg começa a processar automaticamente.
    Se cair, reinicia após 3 segundos.
    """
    print(f"[Relay] Iniciando relay SRT:{SRT_PORT} → RTMP:{RTMP_PORT}")

    while state["mode"] == "server":
        try:
            proc = subprocess.Popen(
                build_relay_cmd(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                **popen_kwargs(),
            )
            state["relay_proc"] = proc
            state["relay_active"] = True
            await broadcast_ws({"type": "relay_ready", "port": SRT_PORT})
            print(f"[Relay] Aguardando transmissor na porta SRT {SRT_PORT}...")

            loop = asyncio.get_event_loop()

            while proc.poll() is None:
                line = await loop.run_in_executor(None, proc.stderr.readline)
                if not line:
                    await asyncio.sleep(0.1)
                    continue

                if "Connection" in line and "accepted" in line.lower():
                    state["client_connected"] = True
                    state["start_time"] = time.time()
                    await broadcast_ws({"type": "client_connected"})
                    print("[Relay] Transmissor conectado!")

                if "Closing" in line or "connection closed" in line.lower():
                    state["client_connected"] = False
                    state["start_time"] = None
                    state["stats"] = {k: 0 for k in state["stats"]}
                    await broadcast_ws({"type": "client_disconnected"})
                    print("[Relay] Transmissor desconectado.")

                if "bitrate=" in line:
                    try:
                        val = line.split("bitrate=")[1].split("kbits")[0].strip()
                        state["stats"]["bitrate_kbps"] = int(float(val))
                        if not state["client_connected"]:
                            state["client_connected"] = True
                            state["start_time"] = state["start_time"] or time.time()
                            await broadcast_ws({"type": "client_connected"})
                    except Exception:
                        pass

                if "drop=" in line:
                    try:
                        val = line.split("drop=")[1].split()[0].strip()
                        state["stats"]["dropped_packets"] = int(val)
                    except Exception:
                        pass

            state["relay_proc"] = None
            state["relay_active"] = False
            state["client_connected"] = False
            state["stats"] = {k: 0 for k in state["stats"]}
            await broadcast_ws({"type": "relay_stopped"})
            print("[Relay] Processo encerrou. Reiniciando em 3s...")

        except FileNotFoundError:
            print("[Relay] ERRO: FFmpeg não encontrado!")
            await broadcast_ws({"type": "error", "msg": "FFmpeg não encontrado"})
            return
        except Exception as e:
            print(f"[Relay] Erro: {e}")

        await asyncio.sleep(3)


# ─── Parsing FFmpeg (transmissor) ──────────────────────────────────────────────

async def monitor_transmitter(proc):
    """
    Monitora o processo FFmpeg do transmissor.
    Se cair inesperadamente (queda de rede/troca de internet), tenta reconectar
    automaticamente com backoff exponencial (3s → 6s → 12s … até 60s).
    """
    loop = asyncio.get_event_loop()

    while state["streaming"] and proc.poll() is None:
        line = await loop.run_in_executor(None, proc.stderr.readline)
        if not line:
            await asyncio.sleep(0.1)
            continue

        print(f"[FFmpeg Log] {line.strip()}")

        try:
            if "bitrate=" in line:
                val = line.split("bitrate=")[1].split("kbits")[0].strip()
                state["stats"]["bitrate_kbps"] = int(float(val))
                # Transmissão confirmada — reseta contador de tentativas
                state["reconnect_attempt"] = 0
            if "drop=" in line:
                val = line.split("drop=")[1].split()[0].strip()
                state["stats"]["dropped_packets"] = int(val)
        except Exception:
            pass

    # FFmpeg encerrou — verifica se foi intencional ou queda de rede
    if not state["streaming"]:
        # Parada intencional (usuário clicou em Parar) — não reconecta
        return

    # Queda inesperada — tenta reconectar
    target = state.get("reconnect_target")
    if not target or not state.get("reconnect_enabled", True):
        # Sem alvo salvo ou reconexão desabilitada — para definitivamente
        state["streaming"] = False
        state["ffmpeg_proc"] = None
        state["start_time"] = None
        state["stats"] = {k: 0 for k in state["stats"]}
        await broadcast_ws({"type": "stream_stopped", "reason": "ffmpeg_exited"})
        print("[Transmissor] FFmpeg encerrou. Nenhum alvo para reconectar.")
        return

    attempt = state["reconnect_attempt"] + 1
    state["reconnect_attempt"] = attempt
    delay = min(3 * (2 ** (attempt - 1)), 60)  # 3, 6, 12, 24, 48, 60, 60…

    print(f"[Transmissor] Conexão perdida. Tentativa {attempt} em {delay}s...")
    await broadcast_ws({
        "type": "reconnecting",
        "attempt": attempt,
        "delay": delay,
        "host": target["host"],
        "port": target["port"],
    })

    # Mantém streaming=True e start_time intactos para o uptime continuar no frontend
    state["ffmpeg_proc"] = None
    state["stats"]["bitrate_kbps"] = 0

    await asyncio.sleep(delay)

    if not state.get("reconnect_enabled", True):
        # Usuário parou durante a espera
        state["streaming"] = False
        state["start_time"] = None
        state["stats"] = {k: 0 for k in state["stats"]}
        await broadcast_ws({"type": "stream_stopped", "reason": "user_stopped"})
        return

    # Tenta subir o FFmpeg novamente
    cmd = build_transmitter_cmd(state["config"], target["host"], target["port"])
    print(f"[Transmissor] Reconectando em {target['host']}:{target['port']}...")
    try:
        new_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs(),
        )
        state["ffmpeg_proc"] = new_proc
        await broadcast_ws({"type": "reconnect_ok", "attempt": attempt})
        asyncio.create_task(monitor_transmitter(new_proc))
    except Exception as e:
        print(f"[Transmissor] Falha ao reconectar: {e}")
        # Agenda nova tentativa recursivamente via task separada
        asyncio.create_task(_schedule_retry(attempt))


async def _schedule_retry(attempt: int):
    """Reagenda uma tentativa de reconexão quando o Popen falha."""
    if not state.get("reconnect_enabled", True) or not state["streaming"]:
        return
    delay = min(3 * (2 ** attempt), 60)
    await asyncio.sleep(delay)
    target = state.get("reconnect_target")
    if not target or not state["streaming"]:
        return
    state["reconnect_attempt"] = attempt + 1
    await broadcast_ws({"type": "reconnecting", "attempt": attempt + 1, "delay": delay,
                        "host": target["host"], "port": target["port"]})
    try:
        cmd = build_transmitter_cmd(state["config"], target["host"], target["port"])
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, **popen_kwargs())
        state["ffmpeg_proc"] = proc
        await broadcast_ws({"type": "reconnect_ok", "attempt": attempt + 1})
        asyncio.create_task(monitor_transmitter(proc))
    except Exception as e:
        print(f"[Retry] Falha: {e}")
        asyncio.create_task(_schedule_retry(attempt + 1))




async def collect_stats():
    while True:
        is_active = (
            (state["mode"] == "server" and state["client_connected"]) or
            (state["mode"] == "transmitter" and state["streaming"])
        )

        if is_active and state["start_time"]:
            state["stats"]["uptime_seconds"] = int(time.time() - state["start_time"])
            state["stats"]["cpu_percent"] = psutil.cpu_percent(interval=None)
            state["stats"]["mem_percent"] = psutil.virtual_memory().percent
            await broadcast_ws({"type": "stats", "stats": state["stats"]})

        await asyncio.sleep(1)


# ─── Descoberta UDP ────────────────────────────────────────────────────────────

async def broadcast_presence():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    payload = json.dumps({
        "type": "BRABO_SERVER",
        "id": state["device_id"],
        "name": state["device_name"],
        "srt_port": SRT_PORT,
        "version": "0.1.0",
        "platform": platform.system(),
    }).encode()

    while True:
        try:
            sock.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
        except Exception:
            pass
        await asyncio.sleep(BROADCAST_INTERVAL)


async def listen_for_servers():
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        print(f"[Discovery] Erro: {e}")
        return

    sock.setblocking(False)

    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 1024)
            msg = json.loads(data.decode())

            if msg.get("type") == "BRABO_SERVER":
                srv = {**msg, "host": addr[0], "last_seen": time.time()}
                is_new = msg["id"] not in state["discovered_servers"]
                state["discovered_servers"][msg["id"]] = srv

                if is_new:
                    await broadcast_ws({
                        "type": "servers_updated",
                        "servers": list(state["discovered_servers"].values()),
                    })

        except BlockingIOError:
            await asyncio.sleep(0.1)
        except Exception:
            await asyncio.sleep(1)


# ─── WebSocket ─────────────────────────────────────────────────────────────────

async def broadcast_ws(msg: dict):
    dead = []

    for ws in connected_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)

    for ws in dead:
        connected_clients.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)

    await ws.send_json({
        "type": "hello",
        "mode": state["mode"],
        "streaming": state["streaming"],
        "relay_active": state["relay_active"],
        "client_connected": state["client_connected"],
        "platform": platform.system(),
    })

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in connected_clients:
            connected_clients.remove(ws)


# ─── API REST ──────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "mode": state["mode"],
        "streaming": state["streaming"],
        "relay_active": state["relay_active"],
        "client_connected": state["client_connected"],
        "device_id": state["device_id"],
        "device_name": state["device_name"],
        "platform": platform.system(),
        "config": state["config"].model_dump(),
        "stats": state["stats"],
        "reconnect_target": state.get("reconnect_target"),
        "reconnect_attempt": state.get("reconnect_attempt", 0),
        "start_time": state.get("start_time"),
    }


@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    if req.mode not in ("server", "transmitter"):
        return {"ok": False, "error": "Modo inválido"}

    if state["streaming"] or state["relay_active"]:
        return {"ok": False, "error": "Pare o stream antes de trocar o modo"}

    state["mode"] = req.mode

    for t in _bg_tasks:
        t.cancel()
    _bg_tasks.clear()

    await _start_background_tasks()
    await broadcast_ws({"type": "mode_changed", "mode": req.mode})
    return {"ok": True, "mode": req.mode}


@app.get("/api/servers")
async def get_servers():
    now = time.time()
    state["discovered_servers"] = {
        k: v for k, v in state["discovered_servers"].items()
        if now - v["last_seen"] < 10
    }
    return list(state["discovered_servers"].values())


@app.get("/api/devices")
async def list_devices():
    system = platform.system()
    cameras, mics = [], []

    try:
        ff = ffmpeg_bin()

        if system == "Windows":
            result = subprocess.run(
                [ff, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            current = None

            for line in result.stderr.splitlines():
                if "video" in line.lower() and "device" in line.lower():
                    current = "video"
                elif "audio" in line.lower() and "device" in line.lower():
                    current = "audio"
                elif '"' in line and current:
                    name = line.split('"')[1]
                    if current == "video":
                        cameras.append({"id": name, "name": name})
                    else:
                        mics.append({"id": name, "name": name})

        elif system == "Linux":
            for dev in sorted(glob.glob("/dev/video*")):
                idx = dev.replace("/dev/video", "")
                cameras.append({"id": dev, "name": f"Câmera {idx} ({dev})"})

            result = subprocess.run(
                ["arecord", "-L"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and not line.startswith(" "):
                    mics.append({"id": line, "name": line})

    except Exception as e:
        print(f"[Devices] Erro: {e}")

    return {
        "cameras": cameras or [{"id": "0", "name": "Câmera padrão"}],
        "mics": mics or [{"id": "default", "name": "Microfone padrão"}],
        "current": state["device"],
    }


@app.post("/api/devices")
async def save_devices(cfg: DeviceConfig):
    state["device"] = {
        "source": cfg.source,
        "camera_id": cfg.camera_id,
        "mic_id": cfg.mic_id,
        "camera_name": cfg.camera_name,
        "mic_name": cfg.mic_name,
    }
    return {"ok": True}


async def buffer_sender_task(ip: str, port: int):
    buf_dir = Path("stream_buffer")
    if buf_dir.exists():
        shutil.rmtree(buf_dir, ignore_errors=True)
    buf_dir.mkdir(exist_ok=True)

    target_path = str(buf_dir / "chunk_%05d.ts")
    cap_cmd = build_transmitter_cmd(state["config"], target_path, is_buffer=True)

    print("[Transmissor] Iniciando captura para SSD (Buffer ativado)...")
    cap_proc = subprocess.Popen(cap_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
    state["ffmpeg_proc"] = cap_proc

    srt_url = f"srt://{ip}:{port}?mode=caller&latency=2000&pkt_size=1316"
    ff = ffmpeg_bin()
    push_proc = None
    chunk_index = 0

    try:
        while state["streaming"]:
            chunks = sorted(buf_dir.glob("chunk_*.ts"))

            # Ignora o último arquivo porque o FFmpeg ainda está escrevendo nele
            if len(chunks) <= 1:
                await asyncio.sleep(0.5)
                continue

            chunks_to_send = [c for c in chunks[:-1] if int(c.stem.split('_')[1]) >= chunk_index]

            if not chunks_to_send:
                await asyncio.sleep(0.5)
                continue

            # Garante que o FFmpeg de envio (Push) está rodando
            if not push_proc or push_proc.poll() is not None:
                print(f"[Buffer] Conectando ao servidor SRT {ip}:{port}...")
                push_cmd = [
                    ff, "-re", "-f", "mpegts", "-i", "pipe:0",
                    "-c", "copy", "-f", "mpegts", srt_url
                ]
                push_proc = subprocess.Popen(push_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
                await broadcast_ws({"type": "reconnect_ok", "attempt": state["reconnect_attempt"]})

            for chunk_path in chunks_to_send:
                if not state["streaming"]:
                    break

                try:
                    if push_proc.poll() is not None:
                        raise Exception("Processo de push encerrou (rede caiu)")

                    with open(chunk_path, "rb") as f:
                        data = f.read()
                        push_proc.stdin.write(data)
                        push_proc.stdin.flush()

                    # Calcula e atualiza o bitrate na interface gráfica
                    size_bits = len(data) * 8
                    state["stats"]["bitrate_kbps"] = int((size_bits / 2) / 1000) # 2 segundos por chunk

                    # Sucesso! Apaga o chunk para não lotar o HD e avança
                    chunk_path.unlink()
                    chunk_index = int(chunk_path.stem.split('_')[1]) + 1
                    state["reconnect_attempt"] = 0

                except Exception as e:
                    print(f"[Buffer] Queda de rede detectada ao enviar {chunk_path.name}. Retentando...")
                    if push_proc:
                        push_proc.kill()
                        push_proc = None

                    state["reconnect_attempt"] += 1
                    await broadcast_ws({
                        "type": "reconnecting",
                        "attempt": state["reconnect_attempt"],
                        "delay": 3,
                        "host": ip, "port": port
                    })
                    state["stats"]["bitrate_kbps"] = 0
                    await asyncio.sleep(3)
                    break # Quebra o loop para reiniciar o push_proc e tentar O MESMO chunk
    finally:
        if cap_proc:
            cap_proc.kill()
        if push_proc:
            push_proc.kill()
        print("[Transmissor] Buffer e envio encerrados.")

@app.post("/api/stream/start")
async def start_stream(server_id: Optional[str] = None):
    if state["mode"] == "server":
        return {"ok": False, "error": "Servidor inicia automaticamente"}
    if state["streaming"]:
        return {"ok": False, "error": "Stream já está ativo"}
    if not server_id:
        return {"ok": False, "error": "Selecione um servidor antes de iniciar"}

    if server_id.startswith("manual-"):
        ip = server_id.replace("manual-", "").replace("-", ".")
        port = SRT_PORT
    elif server_id in state["discovered_servers"]:
        srv = state["discovered_servers"][server_id]
        ip = srv["host"]
        port = srv["srt_port"]
    else:
        return {"ok": False, "error": "Servidor não encontrado. Tente novamente."}

    state["streaming"] = True
    state["start_time"] = time.time()
    state["reconnect_target"] = {"host": ip, "port": port}
    state["reconnect_enabled"] = True
    state["reconnect_attempt"] = 0

    if state["config"].drop_buffer:
        # Sistema Profissional: Grava e Transmite
        task = asyncio.create_task(buffer_sender_task(ip, port))
        state["buffer_task"] = task
    else:
        # Sistema Antigo: Direto (Menor Latência, mas suscetível a quedas)
        srt_url = f"srt://{ip}:{port}?mode=caller&latency=2000&pkt_size=1316&rcvbuf=26214400&sndbuf=26214400&peerlatency=2000"
        cmd = build_transmitter_cmd(state["config"], srt_url, is_buffer=False)
        print(f"[Transmissor] Enviando direto para {ip}:{port}")
        
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **popen_kwargs())
            state["ffmpeg_proc"] = proc
            asyncio.create_task(monitor_transmitter(proc))
        except FileNotFoundError:
            state["streaming"] = False
            return {"ok": False, "error": "FFmpeg não encontrado."}
        except Exception as e:
            state["streaming"] = False
            return {"ok": False, "error": str(e)}

    await broadcast_ws({"type": "stream_started"})
    return {"ok": True}

@app.post("/api/stream/stop")
async def stop_stream():
    if state["mode"] == "server":
        return {"ok": False, "error": "Servidor não pode ser parado por aqui"}
    if not state["streaming"]:
        return {"ok": False, "error": "Nenhum stream ativo"}

    state["streaming"] = False
    state["reconnect_enabled"] = False
    state["reconnect_target"] = None
    state["reconnect_attempt"] = 0
    state["start_time"] = None
    state["stats"] = {k: 0 for k in state["stats"]}

    # Cancela a tarefa assíncrona do buffer se ela existir
    if state.get("buffer_task"):
        state["buffer_task"].cancel()
        state["buffer_task"] = None

    proc = state.get("ffmpeg_proc")
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    state["ffmpeg_proc"] = None

    await broadcast_ws({"type": "stream_stopped"})
    return {"ok": True}


@app.post("/api/config")
async def update_config(config: StreamConfig):
    state["config"] = config
    return {"ok": True, "config": config.model_dump()}


# ─── Startup ───────────────────────────────────────────────────────────────────

async def _start_background_tasks():
    if state["mode"] == "server":
        _bg_tasks.append(asyncio.create_task(broadcast_presence()))
        _bg_tasks.append(asyncio.create_task(run_relay()))
    else:
        _bg_tasks.append(asyncio.create_task(listen_for_servers()))


@app.on_event("startup")
async def startup():
    asyncio.create_task(collect_stats())
    await _start_background_tasks()
    print(f"[Brabo IRL] {state['mode'].upper()} | porta {CONTROL_PORT}")


# ─── Frontend estático ─────────────────────────────────────────────────────────

frontend_path = BASE_DIR / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="static")
else:
    @app.get("/")
    async def root():
        return HTMLResponse("<h1>Frontend não encontrado</h1>")


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    import threading

    mode = sys.argv[1] if len(sys.argv) > 1 else "server"
    os.environ["BRABO_MODE"] = mode
    state["mode"] = mode

    url = f"http://localhost:{CONTROL_PORT}"
    print(f"\n Brabo IRL | {mode.upper()} | {url}\n")

    def _open():
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=CONTROL_PORT,
        reload=False,
        log_level="warning",
    )
