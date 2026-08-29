# Camera Dashboard

A lightweight 1280x720 home/CCTV dashboard designed for old Android TV boxes and kiosk browsers.

The project intentionally avoids modern frontend frameworks, WebRTC, React, Vue, ES modules, and other browser features that can fail on old Android WebView/Chrome releases.

It provides:

- up to four Hikvision RTSP camera tiles;
- server-side H.264 -> MJPEG compatibility conversion using FFmpeg;
- LuxBridge solar telemetry from its REST endpoint;
- optional current weather using Open-Meteo;
- a local clock;
- a simple kiosk-friendly HTML page;
- one FFmpeg worker per configured camera, shared by every connected browser.

The cameras and Frigate remain independent. This dashboard only opens additional read-only RTSP connections to the existing camera sub-streams.

## Architecture

~~~text
Hikvision camera main stream ---------> Frigate / recording
Hikvision camera H.264 sub-stream ----> Frigate / go2rtc (unchanged)
                                  \
                                   +--> Camera Dashboard
                                         |
                                         +-- FFmpeg -> 640x360 MJPEG
                                         +-- LuxBridge REST
                                         +-- Weather cache
                                         |
                                         +--> old Android kiosk browser
~~~

The browser loads the HTML page once. MJPEG camera connections remain open continuously and send successive JPEG frames over the same HTTP connection. The page is not refreshed eight or ten times per second.

Solar data is polled by the server and exposed as a tiny normalized JSON response. The browser requests that JSON every few seconds.

## Recommended camera settings

The dashboard does not change camera settings.

A good Hikvision sub-stream configuration is:

~~~text
Resolution:       1280x720
Encoding:         H.264
Frame rate:       20 fps
Bitrate type:     Variable
Quality:          Highest
Max bitrate:      2048-4096 Kbps
I-frame interval: 20
Channel:          102
~~~

The dashboard makes a lower-bandwidth compatibility copy only for the kiosk display. Frigate and other clients continue using the original stream.

Default dashboard conversion:

~~~text
640x360
8 fps
MJPEG
~~~

For four 640x360 tiles on a 1280x720-class display, 8-10 fps is a good starting point.

## Quick start

Requirements:

- Docker Engine
- Docker Compose plugin
- network access from the Docker host to the cameras
- optional LuxBridge REST endpoint
- optional internet access for weather

Clone the repository:

~~~bash
git clone https://github.com/sameerakhtari/camera-dashboard.git
cd camera-dashboard
~~~

Create your private environment file:

~~~bash
cp .env.example .env
nano .env
~~~

At minimum, configure the cameras you currently have:

~~~text
CAM1_NAME=Gate
CAM1_HOST=192.168.1.101
CAM1_USER=admin
CAM1_PASS=your_real_password
CAM1_CHANNEL=102
~~~

Repeat for CAM2 and CAM3.

Leave CAM4_HOST empty until a fourth camera exists.

Do not URL-encode passwords in CAMx_PASS. The application encodes RTSP credentials safely when constructing the camera URL.

Start the stack:

~~~bash
docker compose up -d --build
~~~

Check status:

~~~bash
docker compose ps
docker compose logs --tail=100
~~~

Open:

~~~text
http://DOCKER_HOST_IP:8085/
~~~

For example, if the Docker host is 192.168.100.43:

~~~text
http://192.168.100.43:8085/
~~~

## Environment configuration

The real .env file is intentionally ignored by Git.

The tracked .env.example file contains every supported setting.

### Dashboard

~~~text
DASHBOARD_PORT=8085
TZ=Asia/Karachi
~~~

### LuxBridge solar

Default:

~~~text
SOLAR_URL=http://host.docker.internal:8099/api/v1/live
SOLAR_INTERVAL=3
~~~

docker-compose.yml maps host.docker.internal to Docker's host-gateway on Linux, so a LuxBridge container/service published on the Docker host at port 8099 can be reached without putting this project on the same Docker network.

The dashboard currently uses these LuxBridge fields:

~~~text
telemetry.pv_power_w
telemetry.battery_soc_pct
telemetry.battery_state
telemetry.battery_charge_power_w
telemetry.battery_discharge_power_w
telemetry.today_pv_yield_kwh
telemetry.grid_import_w
telemetry.grid_export_w
~~~

For displayed load, it uses this fallback order:

~~~text
consumption_load_w
-> house_load_w
-> eps_power_w
~~~

This is useful for inverter modes where the first two load fields can be zero while EPS power reflects the actual active load.

### Camera conversion

~~~text
CAMERA_WIDTH=640
CAMERA_HEIGHT=360
CAMERA_FPS=8
CAMERA_JPEG_QUALITY=5
CAMERA_RECONNECT_DELAY=2
~~~

FFmpeg JPEG quality uses the q:v scale. Lower values mean higher JPEG quality. A value around 5 is a reasonable starting point.

Increasing MJPEG frame rate or JPEG quality increases:

- Docker host CPU usage;
- network traffic;
- old Android browser decoding load.

Test 8 fps first, then try 10 or 12 if the kiosk device remains smooth.

### Weather

Weather is optional.

Set:

~~~text
WEATHER_LAT=YOUR_LATITUDE
WEATHER_LON=YOUR_LONGITUDE
WEATHER_INTERVAL=600
~~~

The backend retrieves current conditions from Open-Meteo and caches them. The old Android box never talks directly to the weather service.

If the weather API or internet fails, camera feeds, LuxBridge data, and the clock continue independently.

## Useful endpoints

Main dashboard:

~~~text
/
~~~

Normalized status JSON:

~~~text
/api/status
~~~

Health check:

~~~text
/health
~~~

MJPEG camera streams:

~~~text
/camera/cam1.mjpeg
/camera/cam2.mjpeg
/camera/cam3.mjpeg
/camera/cam4.mjpeg
~~~

Latest single JPEG frame:

~~~text
/camera/cam1.jpg
/camera/cam2.jpg
/camera/cam3.jpg
/camera/cam4.jpg
~~~

The single-frame endpoints are useful when diagnosing whether FFmpeg can receive and decode a camera before testing MJPEG in the kiosk browser.

## Testing after deployment

Check the app:

~~~bash
curl http://127.0.0.1:8085/health
~~~

Check LuxBridge/dashboard state:

~~~bash
curl -s http://127.0.0.1:8085/api/status | python3 -m json.tool
~~~

Test a still frame:

~~~bash
curl -o cam1.jpg http://127.0.0.1:8085/camera/cam1.jpg
file cam1.jpg
~~~

Test the continuous MJPEG stream:

~~~bash
curl -v --max-time 5 -o /dev/null \
  http://127.0.0.1:8085/camera/cam1.mjpeg
~~~

A five-second curl timeout is normal for MJPEG because the stream is intentionally endless. During those five seconds, the received byte counter should continuously increase.

Monitor resource use:

~~~bash
docker stats camera-dashboard
~~~

Follow logs:

~~~bash
docker compose logs -f
~~~

## Fully Kiosk / old Android setup

After the dashboard works from a normal desktop browser, set the kiosk browser Start URL/Home URL to:

~~~text
http://DOCKER_HOST_IP:8085/
~~~

Recommended kiosk behavior:

- start automatically after boot;
- load the Start URL on launch;
- fullscreen;
- keep the screen on if this is a permanent monitor;
- hide browser navigation/status bars;
- reload after connection recovery;
- do not configure aggressive periodic full-page refreshes.

The dashboard already refreshes the small status JSON automatically. Camera feeds stay connected continuously.

## Old browser compatibility

The frontend deliberately uses conservative HTML/CSS/JavaScript.

It uses:

- plain HTML;
- table-based layout rather than CSS Grid;
- XMLHttpRequest instead of fetch;
- var and classic functions instead of ES modules/async-await;
- multipart MJPEG instead of WebRTC/MSE;
- no external JavaScript or font dependencies.

The goal is reliable operation on Android 7-era kiosk browsers/WebViews.

## Camera behavior and reconnection

Each configured camera starts exactly one FFmpeg worker inside the single Gunicorn worker process.

~~~text
Camera 1 -> one FFmpeg
Camera 2 -> one FFmpeg
Camera 3 -> one FFmpeg
Camera 4 -> one FFmpeg, only when configured
~~~

Every browser client receives frames from those shared workers.

Opening the dashboard on another PC does not create another RTSP/FFmpeg transcode for each camera.

If FFmpeg exits because a camera temporarily disconnects, the worker waits CAMERA_RECONNECT_DELAY seconds and starts it again.

The browser also retries a broken MJPEG image connection.

## Adding a fourth camera later

Edit .env:

~~~text
CAM4_NAME=Roof
CAM4_HOST=192.168.1.104
CAM4_USER=admin
CAM4_PASS=your_password
CAM4_CHANNEL=102
~~~

Recreate the container:

~~~bash
docker compose up -d --force-recreate
~~~

The reserved fourth tile becomes live automatically. No HTML changes are needed.

## Changing only environment values

If only .env changed, rebuilding the Docker image is unnecessary:

~~~bash
docker compose up -d --force-recreate
~~~

If application code, Dockerfile, or requirements changed:

~~~bash
docker compose up -d --build
~~~

## Updating from Git

~~~bash
git pull
docker compose up -d --build
~~~

Your private .env remains local and is not overwritten by Git.

## Security notes

Never commit the real .env file.

The repository tracks only .env.example with dummy values.

Camera credentials are required by FFmpeg to open RTSP streams. They remain inside the local .env/container environment and are not sent to the kiosk browser.

The browser only receives dashboard HTML, normalized solar/weather data, and MJPEG frames.

If credentials have previously been pasted into chats, terminals recorded by other systems, or public logs, rotate those credentials.

## Troubleshooting

### Dashboard starts but a camera is OFFLINE

Check:

~~~bash
docker compose logs -f
~~~

Then verify the RTSP stream independently from the Docker host:

~~~bash
ffplay -rtsp_transport tcp \
  'rtsp://USER:PASSWORD@CAMERA_IP:554/Streaming/Channels/102'
~~~

If the password contains special URL characters, using CAMx_HOST/CAMx_USER/CAMx_PASS in .env is easier than constructing the RTSP URL manually.

### LuxBridge is offline in the dashboard

From the Docker host:

~~~bash
curl http://127.0.0.1:8099/api/v1/live
~~~

From inside the dashboard container:

~~~bash
docker exec camera-dashboard python -c \
  "import requests; print(requests.get('http://host.docker.internal:8099/api/v1/live', timeout=5).status_code)"
~~~

### MJPEG works on desktop but not the Android box

First test a single JPEG:

~~~text
http://DOCKER_HOST_IP:8085/camera/cam1.jpg
~~~

Then test MJPEG:

~~~text
http://DOCKER_HOST_IP:8085/camera/cam1.mjpeg
~~~

If a single stream works but the four-camera dashboard is too heavy, lower:

~~~text
CAMERA_FPS=5
~~~

or temporarily reduce CAMERA_JPEG_QUALITY by increasing the q:v number, for example:

~~~text
CAMERA_JPEG_QUALITY=7
~~~

Then recreate the container.

## License

MIT. See LICENSE.
