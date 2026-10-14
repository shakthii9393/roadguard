# RoadGuard - AI-Powered Road Safety & Navigation Platform

## Quick start (easiest)

* **Windows:** double-click `start.bat`
* **Mac / Linux:** run `bash start.sh`

It creates the virtual environment, installs everything, creates `.env`, and starts the server.
Then open <http://localhost:8000/night-rider.html> (phone: you need HTTPS for the camera - see the Night Rider section).
In VS Code you can also press **F5** (the "RoadGuard" launch config is included).
To log in to the admin page the first time, run `python backend/create_admin.py` once.

Two ways to report a hazard: upload a photo for AI detection, or file a manual community
report. Every report starts as **Pending** and an admin marks it **Verified** or **Rejected**,
so the map can tell AI predictions, unconfirmed community reports, and confirmed hazards apart.
Admins also track the separate operational lifecycle: Reported -> In Progress -> Resolved.

> This README covers **Phase 0 (restructure), Phase 1 (data model, verification, real
> admin login), Phase 2 (navigation), Phase 3 (Night Rider Mode + live camera
> detection) and Phase 4 (Voice Safety Assistant + night-time detection upgrades)**. The heatmap, analytics charts, and full UI redesign are next - see the
> roadmap at the bottom.

## Project structure

```
roadguard/
├── backend/
│   ├── main.py              # FastAPI app; wires routers, serves uploads + frontend
│   ├── config.py            # settings from .env
│   ├── create_admin.py      # CLI: create/reset an admin account (no public sign-up endpoint)
│   ├── api/                 # HTTP layer only - no DB/business logic here
│   │   ├── auth.py          #   POST /api/auth/login
│   │   ├── hazards.py       #   detect / report / list / stats (public)
│   │   ├── admin.py         #   status + verify + delete (JWT protected)
│   │   ├── navigation.py    #   place search + route options
│   │   ├── live_detect.py   #   live camera frame -> detections (no DB write)
│   │   └── deps.py          #   shared "get_current_user" auth dependency
│   ├── models/               # SQLAlchemy tables
│   │   ├── hazard_report.py  #   one row per AI detection OR user report
│   │   └── user.py           #   admin accounts
│   ├── schemas/              # Pydantic request/response shapes
│   ├── services/             # business logic, framework-agnostic
│   │   ├── detector.py       #   YOLO wrapper (demo mode until you train a model)
│   │   ├── severity.py       #   Low / Medium / High scoring
│   │   ├── storage.py        #   upload validation + saving
│   │   ├── geocoding.py      #   place-name search via Nominatim
│   │   ├── routing.py        #   OSRM routing + hazard-aware labelling
│   │   ├── geo.py            #   haversine distance, point-to-route proximity
│   │   └── distance.py       #   rough "how far away" estimate from box size + per-hazard advice
│   ├── database/              # engine/session
│   ├── utils/security.py      # password hashing (bcrypt) + JWT
│   ├── ml/                    # data.yaml, train.py, weights/
│   └── uploads/                # saved images (gitignored)
├── frontend/
│   ├── index.html             # public map + AI-detect / manual-report tabs
│   ├── navigate.html          # start/destination search + route options
│   ├── night-rider.html       # live camera hazard detection + voice alerts
│   ├── admin.html             # dashboard: sign in, verify, change status
│   └── static/{css,js}/       # voice.js (speech queue), guidance.js (turn + hazard voice engine)
├── tests/{test_api.py, test_navigation.py, test_night_rider.py, test_js.py, js/*.test.js}
├── requirements.txt
└── .env.example
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then edit JWT_SECRET (see below)
python backend/create_admin.py                          # create your first admin login
uvicorn main:app --reload --app-dir backend
```

Generate a real `JWT_SECRET` instead of the placeholder:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

* Public app: http://localhost:8000
* Admin dashboard: http://localhost:8000/admin.html - sign in with the account you just created
* API docs: http://localhost:8000/docs
* Tests: `pytest` (also runs the browser-side tests in `tests/js/` if Node.js is installed)

> **Demo mode:** until you train a model, AI detection returns a fake "pothole" for any photo,
> so the rest of the app is fully testable. Once `backend/ml/weights/best.pt` exists and
> `ultralytics` is installed, real detection switches on automatically - nothing else to change.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/login` | - | username + password -> JWT |
| POST | `/api/hazards/detect` | - | multipart `image`, optional `latitude`/`longitude`/`description` -> AI detects and stores (source=`ai_detection`) |
| POST | `/api/hazards/report` | - | form `hazard_type` (see list below), optional `image`/`latitude`/`longitude`/`description` -> stores (source=`user_report`) |
| GET | `/api/hazards` | - | list; filters: `status`, `verification_status`, `hazard_type`, `severity`, `source` |
| GET | `/api/hazards/{id}` | - | one report |
| GET | `/api/hazards/stats/summary` | - | counts by status / type / severity / source / verification |
| PATCH | `/api/admin/hazards/{id}/status` | JWT | Reported -> In Progress -> Resolved |
| PATCH | `/api/admin/hazards/{id}/verify` | JWT | Pending -> Verified / Rejected |
| DELETE | `/api/admin/hazards/{id}` | JWT | remove a report |
| GET | `/api/navigation/search?q=...` | - | place-name search (autocomplete) via Nominatim |
| POST | `/api/navigation/route` | - | `{start_lat, start_lon, end_lat, end_lon}` -> route alternatives, each labelled Fastest / Shortest / Fewer hazards. Each step carries `spoken`, `road`, `offset_m`; each route lists its verified, unresolved `hazards` with `offset_m` (distance from route start) |
| POST | `/api/detect/live` | - | multipart `image` (one camera frame) -> detections with an estimated distance and a short instruction (`urgency`), plus `low_light`; NOT saved to the database |

Hazard types: `pothole`, `crack`, `waterlogging`, `debris`, `construction_barrier`,
`parked_vehicle`, `animal`, `pedestrian`, `speed_breaker`, `damaged_sign`,
`broken_streetlight`, `accident`, `other`. AI detection currently classifies all of these
except `accident` and `other` (manual-report-only, since they're not easily boxed by a
generic object detector).

## Phase 2: how navigation works

* **Search** (`/api/navigation/search`) proxies to the free Nominatim (OpenStreetMap)
  geocoder - type a place name, get back candidates with coordinates. Debounced in the
  frontend (350ms) so it doesn't fire on every keystroke.
* **Routing** (`/api/navigation/route`) calls the public OSRM demo server for driving
  directions between two points, asking for alternative routes. Only the `driving`
  profile is available on the public demo server.
* Each alternative is scored against **admin-verified hazard reports only** - an
  unverified AI guess or pending community report never influences route choice. A hazard
  counts as "on" a route if it's within 40 m of any point on that route's path
  (`HAZARD_PROXIMITY_M` in `services/routing.py`).
* Routes are labelled Fastest / Shortest / Fewer hazards (a single route can win more than
  one label). **RoadGuard never auto-selects a route** - it only labels the options, per
  the project brief.
* Both the public OSRM demo server and Nominatim are rate-limited and meant for light
  development use - self-host both before any real deployment (OSRM needs a `.osm.pbf`
  extract of your region; Nominatim needs a full database import).

## Phase 3: how Night Rider Mode works

Open `/night-rider.html`, tap **Start**, and grant camera access.

* The page captures a downscaled frame from your camera roughly every 1.8 seconds
  (`CAPTURE_INTERVAL_MS` in `night-rider.js`) and posts it to `/api/detect/live`.
* That endpoint runs the same YOLO detector as photo reports, but **writes nothing to
  the database** - it's a detect-and-discard loop, not a reporting flow. Use the existing
  "AI photo detection" report form when you want something saved and reviewable.
* Each detection gets a bounding box drawn on a canvas overlay, colour-coded by severity,
  and an **estimated** distance in metres.
* At most one alert fires per detection cycle (the closest hazard), and each hazard type
  has a 12-second cooldown (`ALERT_COOLDOWN_MS`) so the same pothole doesn't trigger a new
  voice alert every 1.8 seconds while it's still in frame.
* Alerts are spoken with the browser's built-in `SpeechSynthesis` API - no external voice
  service, works offline once the page is loaded. A banner and a scrollable log back up
  the voice alert, but neither requires the driver to look at or touch the screen to stay
  safe, per the brief.

### On the distance estimate - please read this before treating it as real

`services/distance.py` estimates distance purely from **how large the bounding box looks**
in the frame - a bigger box is assumed closer. This has no real calibration: it doesn't
know your camera's focal length, mounting angle, or the true size of the hazard. It's
useful for a demo ("something is close" vs "something is far") and to match the
`Pothole detected, 35 meters ahead` style your brief asked for, but **the number itself is
not accurate** and must not be used for anything safety-critical. A real implementation
needs either a calibrated single camera (known focal length + assumed object size) or
stereo/LiDAR input - worth a dedicated future phase if this needs to be trustworthy rather
than illustrative.

### Testing it locally

Camera access requires a secure context - `localhost` works, but testing on a phone over
plain `http://<your-pc-ip>:8000` will not, because `getUserMedia` is blocked outside HTTPS
(browser security, not a RoadGuard limitation). Use an HTTPS tunnel (e.g. ngrok) to test
on a phone during development.

## Phase 4: Voice Safety Assistant + night upgrades

### What Night Rider does differently at night

* **Low-light boost.** Frames darker than `LOW_LIGHT_MEAN_THRESHOLD` (in `services/detector.py`)
  get a gamma lift plus local contrast before detection. Only the copy fed to the model is
  changed. The response has `low_light: true` and the page shows "low-light boost on".
* **Traffic signals** are a new live-only class (`traffic_signal`). They are announced calmly
  ("Traffic signal ahead...") and are never saved as hazard reports or map markers.
* **Advice fits the hazard** (`ADVICE_BY_LABEL` in `services/distance.py`): potholes, speed
  breakers, waterlogging, debris and obstructions say "Reduce speed"; animals and pedestrians
  say "Slow down"; signals say "Be ready to stop". Advice appears from 50 m inward.
* Construction/barriers are one class, spoken as **"Road obstruction"**.
* Spoken distances round to the nearest 5 m; the estimate isn't precise enough for more.

### Detecting people, animals and signals without training data

Your own model handles road damage. To also get pedestrians, animals and traffic signals
before you have labelled data for them, set in `.env`:

```
USE_GENERAL_MODEL=true
GENERAL_MODEL_PATH=yolov8n.pt     # downloads on first use (needs ultralytics + internet once)
```

A general COCO model then runs alongside yours. Its classes are mapped in `GENERAL_LABEL_MAP`;
anything your own model already detects is not reported twice. **Vehicles are deliberately not
mapped**: one frame can't tell a parked vehicle from a moving one, so `parked_vehicle` must come
from your own trained model. `ml/data.yaml` now lists all classes (indices 0-4 unchanged); label
only the ones you want your model to learn. This mapping is unit-tested with fakes but has not
been run against real ultralytics weights.

### Voice Safety Assistant

* **`voice.js`** is the one place speech goes through. Priorities: live camera hazard (4) >
  turn / off-route (3) > hazard on route (2) > info (1). A more urgent message interrupts;
  equal or lower waits its turn; anything waiting too long is dropped, because a late
  "turn left in 200 meters" is worse than silence. Voice preference is remembered.
* **`guidance.js`** follows the rider along the chosen route from GPS and speaks:
  * "Turn left in 500 / 200 meters onto X", then "Turn left now", then arrival.
  * "Caution. Road obstruction on your route, 300 meters ahead." for **admin-verified,
    unresolved** hazards only (same rule as route comparison). Repeat reports of the same
    hazard type within 40 m are announced once.
  * "You appear to be off your route." after 3 fixes more than 60 m away.
  * Distances are measured *along* the route, not straight-line, using offsets computed by
    the server (`offset_m` on steps and hazards).
* **Navigation page:** pick a route, then **Start voice guidance**. **Try a demo drive**
  replays a simulated GPS drive so you can demo it without moving.
* **Camera + directions together:** on the Navigation page choose "Ride with the Night Rider
  camera on this route". The route is handed over via `sessionStorage` (ignored after 6 hours)
  and Night Rider runs camera alerts and turn directions at the same time.
* Night Rider requests a screen wake lock so the phone doesn't sleep mid-ride.
* RoadGuard still never auto-picks a route, and does not reroute when you go off it; it only
  tells you you're off route.

### Limits worth knowing

* Browsers only allow speech after a tap, so the Start buttons prime it. If a browser has no
  speech support, everything still shows on screen.
* GPS fixes with accuracy worse than 80 m are ignored.
* Voice language is `en-IN` (falls back to any English voice); change `LANG` in `voice.js`.
* Route guidance needs `https://` (or localhost) for GPS, same as the camera.

### Tests

```bash
pytest                                   # Python tests + the JS tests below
node --test tests/js/guidance.test.js    # simulated rides along a synthetic route
node --test tests/js/voice.test.js       # speech queue against a fake speechSynthesis
```

Not covered by automated tests: a real phone, real GPS, a real speech engine, and live
OSRM/Nominatim.

## Phase 5: Context-aware collision risk (Night Rider)

Night Rider no longer just says "Pothole detected". Every camera frame now goes through:

```
detect (YOLO) -> track -> distance & lateral position -> movement -> vehicle-path check
  -> scene relationships -> risk score (LOW/MEDIUM/HIGH/CRITICAL) -> road event
  -> recommended action -> voice + HUD
```

All of the reasoning lives in one dependency-free file, `backend/services/roadguard_context.py`.
`/api/detect/live` accepts three optional form fields - `session_id`, `t_ms`, `speed_mps` - and returns an extra `context` block
(risk level/score, event, action, spoken sentence, every tracked object). Old clients that don't send them still work.

| Output | Examples |
|---|---|
| Road events | pedestrian crossing, animal entering your path, pothole ahead, vehicle approaching, road obstruction, possible collision |
| Actions | stay alert, reduce speed, change lane (only if a side is clear), prepare to brake, brake immediately |
| Voice | "Warning. Pedestrian crossing ahead. Reduce speed." / "Caution. Large pothole detected 20 meters ahead in your path. Reduce speed." |

The server decides *when* to speak (new event, escalation, or a cooldown - 3 s at CRITICAL, 10 s at HIGH), so the rider isn't nagged.
HIGH and CRITICAL moments are saved to the `risk_events` table (with GPS if allowed) and appear in the admin dashboard.

**Settings** (`.env`): `DETECT_VEHICLES`, `CAMERA_HEIGHT_M`, `CAMERA_HFOV_DEG`, `HORIZON_Y_RATIO`, `PATH_HALF_WIDTH_M`, `PATH_NEAR_MARGIN_M`, `DEMO_SCENARIO`.
Pedestrians, animals and vehicles need `USE_GENERAL_MODEL=true` (and `pip install ultralytics`).
No trained model yet? `DEMO_SCENARIO` is on by default: Night Rider plays a looping pothole / pedestrian / animal / oncoming-vehicle scene so you can see the full pipeline.

**Calibrate it:** distance comes from one camera using ground-plane geometry (level phone, flat road). Put a cone 10 m ahead, check
the number on screen, and adjust `CAMERA_HEIGHT_M` and `HORIZON_Y_RATIO` until it matches. Distances stay estimates, and the risk weights
(top of the file) are a first calibration, not trained values. This is a rider-assistance aid, not a certified collision-avoidance system.

**Not yet built:** steering-aware path prediction from GPS heading, risk-event data feeding the route safety score, and Tamil voice alerts.

## What changed from the original single-file layout

* `backend/app/` was split into `api/`, `models/`, `schemas/`, `services/`, `database/`,
  `utils/`, per the requested modular architecture. Run command changed from
  `uvicorn app.main:app` to `uvicorn main:app` (still `--app-dir backend`).
* `Report` model -> `HazardReport`, with a `source` field (`ai_detection` / `user_report`),
  a `verification_status` field (`Pending` / `Verified` / `Rejected`), and `verified_by`/
  `verified_at`. The old `status` field (Reported/In Progress/Resolved) is unchanged.
* Hazard classes expanded from 5 to 11 AI-detectable types.
* `/api/reports` -> `/api/hazards` (`/detect` for AI, `/report` for manual). Old admin
  token header replaced with `Authorization: Bearer <JWT>`, issued by `/api/auth/login`.
  There's no public sign-up endpoint - run `python backend/create_admin.py` on the server.
* Frontend: report form is now tabbed (AI photo vs manual report with a hazard-type picker
  and optional photo); map markers and the report list show source and verification state;
  admin dashboard has a username/password login and Verify/Reject buttons per report.

## Data model note

Individual detections/reports are **not** pre-clustered into a separate "Hazard" aggregate
table yet - each AI detection or user report is its own row. The heatmap/route-hazard-count
features (Phase 2-3) will group nearby reports by `(hazard_type, ~15m radius)` on the fly.
Revisit with a real aggregate table if report volume grows large enough that on-the-fly
grouping gets slow.

## Roadmap (not yet built)

1. **GPS hazard mapping upgrade** - heatmap, click-to-see detail panel with report count.
2. **Admin analytics charts + risk analytics module** (Low/Moderate/High, clearly labeled as
   a project estimate).
3. **User dashboard** and a full visual redesign pass.
4. Duplicate-report detection (same `hazard_type` within ~15 m of an existing open report) -
   marked with a `TODO` in `backend/api/hazards.py`.
5. Rerouting when the rider leaves the route (currently only announced), and a calibrated distance estimate.
6. Optionally: let Night Rider Mode auto-file a real report (via `/api/hazards/detect`,
   with GPS) the first time it sees a High-severity hazard in a session, so drives
   organically build up the hazard map without the driver touching the screen.

## Notes

* Photo EXIF often contains GPS; Pillow can read it as a fallback when the user denies
  browser location access (not yet implemented).
* Uploaded photos may contain faces or number plates - consider blurring them before storing,
  especially once this handles pedestrian/parked-vehicle detections.
* SQLite by default; switch to PostgreSQL by changing `DATABASE_URL` in `.env`.
