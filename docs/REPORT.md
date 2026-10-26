# Waymark: technical report

OpenCV AI Competition 2026, powered by AWS. Entered for the overall prizes and the Agentic Vision award.

## 1. Problem

GPS does not work indoors. A blind or low-vision person arriving at an unfamiliar library, clinic or college building has to ask strangers or learn the route with an orientation and mobility instructor beforehand. Indoor guidance systems that work today depend on hardware or on someone else mapping the venue first:

- NavCog3 guided 43 visually impaired participants through a shopping mall, but it relies on Bluetooth beacons installed throughout the building ([Sato et al.](https://pmc.ncbi.nlm.nih.gov/articles/PMC6497170)).
- Smith-Kettlewell's camera-only approach needs a map of the building's signs ([Fusco and Coughlan](https://WWW.SKI.ORG/publications/indoor-localization-visually-impaired-travelers-using-computer-vision-smartphone)).
- Commercial indoor views exist only where the vendor has mapped the venue.

Most buildings will never get beacons or a vendor visit. Waymark asks what can be done with what every building already has: two people with phones and a printer.

## 2. Users

| User | What they do | What they need |
|---|---|---|
| Blind or low-vision visitor | Asks for a place and walks there with their cane or guide dog | Short spoken instructions they can trust, and a person when the system is unsure |
| Volunteer mapper (staff, students, a friend) | Walks the building once with a phone for about 15 minutes | Clear instructions on where to walk next, no training |
| Human helper (front desk, family) | Watches the visitor's position and answers when called | A live view and a reason for every decision |

## 3. Architecture

![Architecture](architecture.svg)

Phones run a web page only: no app store, no install. All vision runs in OpenCV 5.0 on one AWS Graviton4 instance (c8g.large), behind API Gateway for HTTPS. The decision trace of every walker and visitor goes to DynamoDB, walk frames go to S3 so any walk can be replayed through the pipeline, and logs and a dashboard live in CloudWatch. Everything is one CDK stack; updates go through SSM with no SSH.

## 4. OpenCV 5 implementation

| Step | OpenCV 5 functions | Notes |
|---|---|---|
| Building frame | `aruco.CharucoDetector`, `solvePnP` (SQPnP) + `solvePnPRefineLM` | Board pose requires at least 12 corners spread over 12% of the image; fewer gives ambiguous poses (found in testing) |
| Lens calibration (optional for volunteers) | `calibrateCamera` on ChArUco views | Visitors never calibrate: focal length is chosen from five candidates by inlier count on the first fixes |
| Image quality gate | `Laplacian` variance, mean intensity | Rejects blur and bad exposure before any matching |
| Keypoints and descriptors | `ALIKED_create` (new DNN engine, ONNX) | 1,000 points per frame at a fixed 640×640 input |
| Fast matching | `BFMatcher.knnMatch` + mutual check + ratio test | Frame-to-frame tracking, a few ms |
| Precise matching | `LightGlueMatcher_create` | Keyframes, loop closures and merging only; capped at the 600 strongest points, which keeps most of its accuracy at about a third of the cost |
| Camera pose | `solvePnPRansac` with `UsacParams` (MAGSAC++, sigma local optimisation) + `solvePnPRefineLM` | Inlier mask re-checked after refinement |
| 3D points | `triangulatePoints` | Cheirality, reprojection under 3 px in both views, parallax over 1.5° |
| Place recognition | `kmeans` vocabulary + VLAD | Trained per building from its own keyframes |
| Floor plan | `GaussianBlur`, `morphologyEx`, `HoughLinesP`, `connectedComponentsWithStats`, `polylines` | Wall band 0.3 to 2.0 m above the floor |
| Rendering | `imencode`, drawing functions | Floor plan PNG for the helper console |

Bundle adjustment is written with SciPy's sparse least squares on top of OpenCV's Rodrigues convention. Anchors come from the board corners (known building coordinates) or, in benchmarks, from reference poses. A first version used a tight Huber loss and stalled at 9 px on a synthetic test with a known answer. The current version solves in two linear stages with outlier culling between them, and recovers poses to 4.6 mm with 15% gross outliers in 0.4 s.

## 5. Agentic loop

Two agents act on what OpenCV measures.

**Coordinator (volunteers).** After every frame it rebuilds a preview map, casts rays from each keyframe to find floor that has been seen but not walked, groups it into regions (`connectedComponentsWithStats`), assigns regions to walkers with the Hungarian algorithm and speaks one instruction to each walker ("Next, head about 6 metres to your left. Nobody has walked that area yet."). When tracking is lost it tells the walker how to recover.

**Guide (visitors).** Each frame gives it image quality, a position and a confidence built from PnP inliers, reprojection error and whether the move since the last fix is physically plausible. Its actions and the measurement that triggers each one:

| Action | Triggered by |
|---|---|
| Give the next instruction (turn, walk N metres, arrived and on which side) | Confident fix on the route |
| Ask the visitor to pan slowly | Fewer than 2 frames without a confident fix |
| Ask the visitor to stop walking | 3 or more frames without a confident fix |
| Hand over to the human helper | 6 frames without a confident fix |
| Add "slowly" to the instruction | Confidence between 0.35 and 0.6 |
| Re-route, then prefer fewer turns after 3 re-routes | More than 1.6 m from the route |
| Stop and warn before stairs | Stairs within 3 m ahead and confidence below 0.8 |
| Flag the area for re-mapping | Sharp images that retrieve the right place but no longer match it |

Every decision is stored with what the agent saw, what it decided and why, and shown in the helper console.

## 6. AWS deployment

| Component | Service | Why |
|---|---|---|
| Compute | EC2 c8g.large (Graviton4, arm64), Ubuntu 24.04 | OpenCV 5's arm64 build ships KleidiCV; Graviton costs less per frame (see 7.4) |
| HTTPS front door | API Gateway HTTP API | Phones need HTTPS for the camera. The origin accepts only requests carrying a secret header added by the gateway |
| Traces | DynamoDB (on demand) | Every decision, keyed by walker or visit |
| Walk archive | S3, 30-day expiry on frames | Replay any walk through the pipeline |
| Operations | CloudWatch logs and dashboard, AWS Budget, SSM | No SSH; updates are `git pull` and a restart over SSM |
| Infrastructure as code | AWS CDK (Python) | `scripts/deploy.sh` builds the whole stack |

## 7. Evaluation

### 7.1 Mapping and merging (Microsoft 7-Scenes, "heads")

Each sequence plays one volunteer. Only the first and last 10 frames get their reference pose, standing in for the board at the start and end of a walk. Everything else is tracked.

| Stage | Keyframe position error (median) |
|---|---|
| Live tracking, before any optimisation | 13.5 cm (walker A), 23.2 cm (walker B) |
| After board-anchored bundle adjustment | 2.0 cm (A), 7.5 cm (B) |
| After merging both walkers | **1.2 cm** (worst 4.2 cm); reprojection error 28.4 px → 2.3 px |

### 7.2 Relocalisation (7-Scenes protocol)

RELOC_TABLE

### 7.3 Field test in a real building

FIELD_TABLE

### 7.4 Speed and cost on Graviton

SPEED_TABLE

### 7.5 Failure cases

FAILURES

## 8. Limitations

- The board must be in view at the start and end of every walk; walks that never return to it drift (7.1 shows how much).
- Waymark does not detect obstacles, people or moving things. It assumes a cane or guide dog.
- Visual localisation fails on blank walls, glass and in the dark. The agent detects this and stops, but it cannot see through it.
- Buildings change. Waymark notices when a place no longer matches and asks for a re-walk of that corridor, but it does not update the map by itself.
- One floor per map. Lifts and stairs end a route.
- The field study is small and is reported as such.

## 9. Responsible use

- Waymark adds directions. It never replaces a mobility aid, and says so before every visit.
- The guide stops before stairs unless certain and hands over to a person after repeated failures. These rules are in code and cannot be overridden by the visitor's requests.
- Participants in the field test gave verbal consent, were accompanied by a sighted spotter, kept their own cane or dog, and could stop at any time.
- Walk frames are kept for 30 days for replay and then deleted. Visitor frames are not stored.
- No result in this report is projected: every number comes from a script in `eval/` or from the field traces.
