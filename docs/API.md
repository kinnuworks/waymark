# Waymark HTTP API

The server is a single FastAPI app (`server/waymark/api.py`). It serves the static web app from `web/` at `/` and the JSON API under `/api`. All coordinates are metres in the building frame, defined by the printed entrance board: `x` to the right along the board, `y` down (gravity), `z` away from the wall into the building. The floor plan uses `(x, z)`.

Images are sent as `multipart/form-data` with a JPEG under the field `image`. The client sends at most about 1 frame per second, with the long side at 960 px.

## Buildings and mapping (volunteer app, `web/map.html`)

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/buildings` | | `[{id, name, created, status, n_walkers, n_pois, has_map}]` |
| `POST /api/buildings` | `{name, board_height_m}` | `{id}`. `board_height_m` is the height of the board centre above the floor. |
| `GET /api/board.png` | | The printable ChArUco entrance board (A4, 300 dpi). |
| `POST /api/buildings/{bid}/walkers` | `{name}` | `{walker_id, colour}` |
| `POST /api/buildings/{bid}/walkers/{wid}/calib` | `image` | `{board_found, views, needed, done, rms_px}`. Camera calibration from board views. It is optional; without it, the server estimates the focal length. |
| `POST /api/buildings/{bid}/walkers/{wid}/frame` | `image`, `heading_deg` (optional), `t_ms` | `{state, pose: {x, z, yaw_deg} or null, quality: {sharpness, brightness, ok}, keyframes, points, say, code}`. `state` is one of `need_board`, `tracking`, `lost`, `rejected`. `say` is a short sentence the phone speaks. `code` is a machine-readable reason. |
| `POST /api/buildings/{bid}/walkers/{wid}/poi` | `{label, kind}` | `{poi_id, x, z}`. `kind` is one of `room`, `door`, `stairs`, `lift`, `restroom`, `exit`, `desk`, `other`. |
| `POST /api/buildings/{bid}/walkers/{wid}/finish` | | `{ok}` |
| `GET /api/buildings/{bid}/coordinator` | | `{coverage_pct, frontiers: [...], assignments: {wid: {target: {x, z}, say}}}`. The coordinator splits the unmapped frontier between the walkers. |
| `POST /api/buildings/{bid}/build` | | `{ok, stats}`. Runs bundle adjustment, the cross-walker merge, the floor plan and the route graph. |
| `GET /api/buildings/{bid}/map` | | `{bounds, walls: [[x1, z1, x2, z2]], nodes: [{id, x, z}], edges: [[a, b]], pois: [...], trajectories: {wid: [[x, z]]}, stats}` |
| `GET /api/buildings/{bid}/floorplan.png` | | Rendered floor plan. |

## Visits (guidance app, `web/go.html`)

| Method and path | Body | Returns |
|---|---|---|
| `POST /api/buildings/{bid}/visits` | `{device}` | `{visit_id}` |
| `POST /api/visits/{vid}/destination` | `{poi_id}` | `{ok, route_m}` |
| `POST /api/visits/{vid}/frame` | `image`, `heading_deg` (optional), `t_ms` | `{state, fix: {x, z, yaw_deg, confidence, inliers} or null, say, haptic, route: {remaining_m, next_turn_deg, next_label} or null, helper_text}` |
| `POST /api/visits/{vid}/ask` | `{text}` | `{say}`. Free-form question to the agent, such as "where am I?" or "take me to the lift". |
| `POST /api/visits/{vid}/help` | | `{ok}`. Asks for a human helper. |
| `POST /api/visits/{vid}/checkpoint` | `{checkpoint_id}` | `{ok}`. Evaluation only: a sighted evaluator marks that the visitor is standing on a tape-measured checkpoint. |
| `POST /api/visits/{vid}/end` | `{arrived: bool, notes}` | `{ok}` |

`state` is one of:

- `locating`
- `guiding`
- `low_confidence`
- `stairs_caution`
- `off_route`
- `lost`
- `helper`
- `arrived`

`haptic` is one of `none`, `left`, `right`, `straight`, `stop` or `arrived`.

## Helper console (`web/helper.html`)

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/visits` | | Active and recent visits. |
| `GET /api/visits/{vid}/live` | | `{state, fix, destination, route_polyline, trace_tail: [...], help_requested}` |
| `POST /api/visits/{vid}/say` | `{text}` | `{ok}`. The text is spoken on the visitor's phone with the next response. |

## System

| Method and path | Returns |
|---|---|
| `GET /api/health` | `{ok, opencv, build: {kleidicv, cool}, instance, arch, features: {aliked, lightglue}}` |
| `GET /api/eval` | Latest evaluation results (benchmark tables), also written to `eval/out/results.json`. |
| `GET /api/trace/{id}` | Full decision trace for a walker or visit. |
