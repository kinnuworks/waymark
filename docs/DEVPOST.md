# Devpost entry

**Project name:** Waymark

**Tagline:** Two volunteers walk a building with their phones for 15 minutes. After that, a blind visitor can be guided through it by voice. OpenCV 5 on AWS Graviton.

**Built with:** opencv, opencv5, aliked, lightglue, python, fastapi, numpy, scipy, javascript, html5, aws, amazon-ec2, aws-graviton, amazon-api-gateway, amazon-dynamodb, amazon-s3, amazon-cloudwatch, aws-cdk, aws-systems-manager

**Try it out links:**
- https://5hfibd6grf.execute-api.us-east-1.amazonaws.com
- https://github.com/kinnuworks/waymark

## Inspiration

GPS stops at the front door. Inside an unfamiliar library, clinic or college building, a blind visitor has to find someone to ask. Indoor guidance does exist, but it needs beacons installed throughout the building, or a company that has mapped the venue, so it is missing from almost every building people actually go to. We wanted to know how far a printed sheet of paper, two phones and OpenCV 5 could get instead.

## What it does

1. **Map.** Two volunteers tape a printed board by the entrance and walk the building with their phones for about 15 minutes. Waymark tracks each phone from its photos. An agent watches what has been covered and tells each volunteer where to walk next, so they don't cover the same corridor twice. They tap "Mark this place" at rooms, doors, stairs and lifts.
2. **Merge.** Waymark finds the places both volunteers saw, fuses their maps into one, and produces a measured floor plan and a route graph built only from paths a sighted person actually walked.
3. **Guide.** A blind or low-vision visitor opens a web page, picks a destination by tapping or speaking, and holds the phone at chest height. Once a second, Waymark works out where the phone is and speaks one instruction at a time: "Walk straight for 6 metres, then turn left." "You have arrived. Room 104 is on your right."
4. **Stay safe.** A guidance agent acts on how sure it is. It asks the visitor to pan the phone when it can't see enough, slows them down when unsure, and re-routes with fewer turns after repeated mistakes. It stops before stairs unless certain, and calls a human helper instead of guessing. A helper console shows the visitor's position and every decision with its reason.

## How we built it

- **Building frame.** A ChArUco board fixes one origin and metric scale for every walk (`CharucoDetector`, `solvePnP`).
- **Tracking.**
  - ALIKED keypoints run on OpenCV 5's new DNN engine.
  - Fast mutual-nearest-neighbour matching plus MAGSAC++ PnP (`solvePnPRansac` with `UsacParams`) tracks each frame.
  - LightGlue only runs where a wrong match is expensive: new keyframes, loop closures and the merge.
  - New 3D points come from `triangulatePoints`, with reprojection and parallax checks.
- **Merge.** VLAD retrieval (`kmeans` vocabulary) finds overlaps between walkers. LightGlue and PnP verify them. A sparse bundle adjustment anchored to the board pulls everyone into agreement.
- **Floor plan.** An occupancy grid, `morphologyEx`, `HoughLinesP` and `connectedComponentsWithStats`. The coordinator assigns unwalked regions with the Hungarian algorithm.
- **Locating visitors.** VLAD shortlist, PnP, LightGlue verification when the evidence is weak, and focal-length self-calibration, so visitors never calibrate their phone.
- **AWS.**
  - EC2 c8g.large (Graviton4) running OpenCV 5's arm64 build, which ships KleidiCV.
  - API Gateway for HTTPS, DynamoDB for decision traces, S3 for replayable walks, and CloudWatch.
  - One CDK stack, updated over SSM with no SSH.
- **Front end.** Plain HTML and JavaScript, built for VoiceOver and TalkBack first.

## Results

RESULTS_BLOCK

## Challenges we ran into

- **Bundle adjustment.** Our first version used a tight Huber loss and stalled at 9 px on a synthetic test where we knew the answer. Solving in two linear stages with outlier culling in between brought it to under 1 px. With 15% gross outliers, pose error is now 4.6 mm.
- **Board pose.** A board seen from too far away gave confident but wrong poses from 6 corners. We now require 12 corners spread over the image, and ask volunteers to start an arm's length from it.
- **Speed.** LightGlue on every frame was too slow for two walkers on one Graviton instance. Measuring each stage showed where it actually mattered.

## Accomplishments that we're proud of

FIELD_BLOCK

## What we learned

- On Graviton the stock OpenCV 5 wheel already includes KleidiCV, and nearly all of our frame time goes to the neural network. Measuring each stage mattered more than guessing.
- Volunteers do better with one instruction at a time, and so do blind visitors.

## What's next

- Multi-floor maps with lifts and stairs.
- Re-mapping only the corridors the agent flags as changed.
- An offline mode that runs entirely on the phone.
- A study with an orientation and mobility instructor.
