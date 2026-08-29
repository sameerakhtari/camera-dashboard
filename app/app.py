import logging
import os
import subprocess
import threading
import time
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, render_template, stream_with_context


APP_VERSION = "1.0.0"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)
log = logging.getLogger("camera-dashboard")

app = Flask(__name__)


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


SOLAR_URL = os.getenv(
    "SOLAR_URL",
    "http://host.docker.internal:8099/api/v1/live",
).strip()
SOLAR_INTERVAL = max(1.0, env_float("SOLAR_INTERVAL", 3.0))

CAMERA_WIDTH = max(160, env_int("CAMERA_WIDTH", 640))
CAMERA_HEIGHT = max(90, env_int("CAMERA_HEIGHT", 360))
CAMERA_FPS = max(1, env_int("CAMERA_FPS", 8))
CAMERA_JPEG_QUALITY = min(31, max(2, env_int("CAMERA_JPEG_QUALITY", 5)))
CAMERA_RECONNECT_DELAY = max(1.0, env_float("CAMERA_RECONNECT_DELAY", 2.0))

WEATHER_LAT = os.getenv("WEATHER_LAT", "").strip()
WEATHER_LON = os.getenv("WEATHER_LON", "").strip()
WEATHER_INTERVAL = max(60, env_int("WEATHER_INTERVAL", 600))


class CameraWorker:
    def __init__(self, key, name, host, username, password, channel):
        self.key = key
        self.name = name or key
        self.host = (host or "").strip()
        self.username = username or ""
        self.password = password or ""
        self.channel = str(channel or "102").strip()

        self.frame = None
        self.frame_seq = 0
        self.last_frame_at = 0.0
        self.last_error = None
        self.last_ffmpeg_message = None
        self.process = None

        self.condition = threading.Condition()
        self.configured = bool(self.host)

        if self.configured:
            thread = threading.Thread(
                target=self._run_forever,
                daemon=True,
                name="camera-" + self.key,
            )
            thread.start()
        else:
            log.info("%s is not configured", self.key)

    def rtsp_url(self):
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")

        auth = ""
        if user:
            auth = user
            if self.password != "":
                auth += ":" + password
            auth += "@"

        return (
            "rtsp://"
            + auth
            + self.host
            + ":554/Streaming/Channels/"
            + self.channel
        )

    def _set_error(self, message):
        self.last_error = str(message)
        log.warning("%s: %s", self.key, self.last_error)

    def _drain_stderr(self, pipe):
        try:
            for raw in iter(pipe.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self.last_ffmpeg_message = text
                    log.debug("%s ffmpeg: %s", self.key, text)
        except Exception as exc:
            log.debug("%s stderr reader ended: %s", self.key, exc)

    def _run_forever(self):
        while True:
            try:
                self._run_ffmpeg()
            except Exception as exc:
                self._set_error(exc)

            time.sleep(CAMERA_RECONNECT_DELAY)

    def _run_ffmpeg(self):
        filter_string = (
            "fps="
            + str(CAMERA_FPS)
            + ",scale="
            + str(CAMERA_WIDTH)
            + ":"
            + str(CAMERA_HEIGHT)
            + ":force_original_aspect_ratio=decrease,"
            + "pad="
            + str(CAMERA_WIDTH)
            + ":"
            + str(CAMERA_HEIGHT)
            + ":(ow-iw)/2:(oh-ih)/2"
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.rtsp_url(),
            "-an",
            "-vf",
            filter_string,
            "-q:v",
            str(CAMERA_JPEG_QUALITY),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]

        log.info(
            "%s starting RTSP -> MJPEG (%sx%s @ %sfps)",
            self.key,
            CAMERA_WIDTH,
            CAMERA_HEIGHT,
            CAMERA_FPS,
        )

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.process = process

        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process.stderr,),
            daemon=True,
            name=self.key + "-ffmpeg-stderr",
        )
        stderr_thread.start()

        buffer = b""

        try:
            while True:
                chunk = process.stdout.read(65536)

                if not chunk:
                    break

                buffer += chunk

                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(buffer) > 2_000_000:
                            buffer = buffer[-100_000:]
                        break

                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            buffer = buffer[start:]
                        break

                    frame = buffer[start : end + 2]
                    buffer = buffer[end + 2 :]

                    with self.condition:
                        self.frame = frame
                        self.frame_seq += 1
                        self.last_frame_at = time.time()
                        self.last_error = None
                        self.condition.notify_all()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

            return_code = process.poll()
            self.process = None

            message = "FFmpeg stream ended"
            if return_code is not None:
                message += " (exit " + str(return_code) + ")"
            if self.last_ffmpeg_message:
                message += ": " + self.last_ffmpeg_message

            self._set_error(message)

    def status(self):
        age = None

        if self.last_frame_at:
            age = round(time.time() - self.last_frame_at, 1)

        online = (
            self.configured
            and self.frame is not None
            and age is not None
            and age < 10
        )

        return {
            "name": self.name,
            "configured": self.configured,
            "online": online,
            "last_frame_age_sec": age,
            "error": self.last_error,
        }


def load_cameras():
    loaded = {}

    for number in range(1, 5):
        prefix = "CAM" + str(number)
        key = "cam" + str(number)

        loaded[key] = CameraWorker(
            key=key,
            name=os.getenv(prefix + "_NAME", "Camera " + str(number)),
            host=os.getenv(prefix + "_HOST", ""),
            username=os.getenv(prefix + "_USER", ""),
            password=os.getenv(prefix + "_PASS", ""),
            channel=os.getenv(prefix + "_CHANNEL", "102"),
        )

    return loaded


cameras = load_cameras()


solar_lock = threading.Lock()

solar_state = {
    "available": False,
    "pv_w": 0,
    "load_w": 0,
    "battery_pct": 0,
    "battery_state": "unknown",
    "battery_power_w": 0,
    "today_pv_kwh": 0,
    "grid_import_w": 0,
    "grid_export_w": 0,
    "system_status": "unknown",
    "updated_at": None,
    "error": None,
}


def choose_load(telemetry):
    consumption = telemetry.get("consumption_load_w") or 0
    house = telemetry.get("house_load_w") or 0
    eps = telemetry.get("eps_power_w") or 0

    if consumption > 0:
        return consumption

    if house > 0:
        return house

    if eps > 0:
        return eps

    return 0


def solar_loop():
    session = requests.Session()

    while True:
        try:
            response = session.get(SOLAR_URL, timeout=5)
            response.raise_for_status()

            data = response.json()
            telemetry = data.get("telemetry", {})

            battery_state = telemetry.get("battery_state") or "unknown"

            if battery_state == "charging":
                battery_power = telemetry.get("battery_charge_power_w") or 0
            elif battery_state == "discharging":
                battery_power = telemetry.get("battery_discharge_power_w") or 0
            else:
                battery_power = 0

            update = {
                "available": True,
                "pv_w": telemetry.get("pv_power_w") or 0,
                "load_w": choose_load(telemetry),
                "battery_pct": telemetry.get("battery_soc_pct") or 0,
                "battery_state": battery_state,
                "battery_power_w": battery_power,
                "today_pv_kwh": telemetry.get("today_pv_yield_kwh") or 0,
                "grid_import_w": telemetry.get("grid_import_w") or 0,
                "grid_export_w": telemetry.get("grid_export_w") or 0,
                "system_status": telemetry.get("status") or "unknown",
                "updated_at": data.get("generated_at"),
                "error": None,
            }

            with solar_lock:
                solar_state.update(update)
        except Exception as exc:
            with solar_lock:
                solar_state["available"] = False
                solar_state["error"] = str(exc)

            log.warning("Solar poll failed: %s", exc)

        time.sleep(SOLAR_INTERVAL)


threading.Thread(
    target=solar_loop,
    daemon=True,
    name="solar-poller",
).start()


weather_lock = threading.Lock()

weather_state = {
    "configured": bool(WEATHER_LAT and WEATHER_LON),
    "available": False,
    "temperature_c": None,
    "description": "--",
    "updated_at": None,
    "error": None,
}


WEATHER_CODES = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Cloudy",
    45: "Fog",
    48: "Fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers",
    81: "Rain showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}


def weather_loop():
    if not WEATHER_LAT or not WEATHER_LON:
        log.info("Weather is not configured")
        return

    session = requests.Session()

    while True:
        try:
            response = session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": WEATHER_LAT,
                    "longitude": WEATHER_LON,
                    "current": "temperature_2m,weather_code",
                    "timezone": "auto",
                },
                timeout=10,
            )
            response.raise_for_status()

            data = response.json()
            current = data.get("current", {})
            code = current.get("weather_code")

            update = {
                "configured": True,
                "available": True,
                "temperature_c": current.get("temperature_2m"),
                "description": WEATHER_CODES.get(code, "Weather"),
                "updated_at": current.get("time"),
                "error": None,
            }

            with weather_lock:
                weather_state.update(update)
        except Exception as exc:
            with weather_lock:
                weather_state["available"] = False
                weather_state["error"] = str(exc)

            log.warning("Weather poll failed: %s", exc)

        time.sleep(WEATHER_INTERVAL)


threading.Thread(
    target=weather_loop,
    daemon=True,
    name="weather-poller",
).start()


def generate_mjpeg(worker):
    previous_seq = -1

    while True:
        with worker.condition:
            worker.condition.wait_for(
                lambda: worker.frame_seq != previous_seq,
                timeout=5,
            )

            frame = worker.frame
            seq = worker.frame_seq

        if frame is None:
            time.sleep(0.25)
            continue

        if seq == previous_seq:
            continue

        previous_seq = seq

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: "
            + str(len(frame)).encode("ascii")
            + b"\r\n\r\n"
            + frame
            + b"\r\n"
        )


@app.route("/")
def index():
    camera_list = []

    for key in ("cam1", "cam2", "cam3", "cam4"):
        worker = cameras[key]
        camera_list.append(
            {
                "key": key,
                "name": worker.name,
                "configured": worker.configured,
            }
        )

    return render_template(
        "index.html",
        cameras=camera_list,
        app_version=APP_VERSION,
    )


@app.route("/camera/<key>.mjpeg")
def camera_feed(key):
    worker = cameras.get(key)

    if worker is None:
        return "Unknown camera", 404

    if not worker.configured:
        return "Camera not configured", 404

    return Response(
        stream_with_context(generate_mjpeg(worker)),
        content_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/camera/<key>.jpg")
def camera_snapshot(key):
    worker = cameras.get(key)

    if worker is None:
        return "Unknown camera", 404

    if not worker.configured:
        return "Camera not configured", 404

    with worker.condition:
        frame = worker.frame

    if frame is None:
        return "No frame available yet", 503

    return Response(
        frame,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/api/status")
def status():
    with solar_lock:
        solar = dict(solar_state)

    with weather_lock:
        weather = dict(weather_state)

    camera_status = {}
    for key, worker in cameras.items():
        camera_status[key] = worker.status()

    response = jsonify(
        {
            "version": APP_VERSION,
            "solar": solar,
            "weather": weather,
            "cameras": camera_status,
            "server_time": time.time(),
        }
    )
    response.headers["Cache-Control"] = "no-store"

    return response


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "version": APP_VERSION,
        }
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8085,
        threaded=True,
    )
