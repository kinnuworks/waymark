# Judges' guide

## 30 seconds

1. Open the [helper console](https://5hfibd6grf.execute-api.us-east-1.amazonaws.com/helper.html). It opens the sample building: two volunteer walks merged into one map, named places, and a replayed visit. Pick the visit to see where the visitor was, the route, and every decision the guide made, with what it saw and why.
2. On a phone, open the [guidance app](https://5hfibd6grf.execute-api.us-east-1.amazonaws.com/go.html). It runs in the browser; nothing to install.
3. Check the build that is running: [/api/health](https://5hfibd6grf.execute-api.us-east-1.amazonaws.com/api/health) reports the OpenCV version, CPU architecture and instance type.

## 5 minutes

- [README](../README.md) for the results table.
- [Technical report](REPORT.md), sections 5 (agentic loop) and 7 (evaluation).
- The demo video on the Devpost page.

## Where each criterion is answered

| Criterion | Evidence |
|---|---|
| Technical execution (30%) | REPORT §4: about 20 OpenCV 5 functions across the whole pipeline, including the new DNN engine's ALIKED and LightGlue. §7: benchmarks against ground truth with per-stage error, and an honest record of what failed (`eval/`) |
| Innovation (20%) | Two people with phones and one sheet of paper replace beacons or a vendor survey. Multi-walker mapping directed by an agent, then voice guidance on the merged map |
| Real-world impact (20%) | Indoor wayfinding for blind and low-vision people in buildings that will never get beacons. REPORT §7.3: field test with blind and low-vision participants |
| User experience (10%) | The guidance app is built for VoiceOver and TalkBack first: one instruction at a time, haptics, "repeat" and "where am I?". Volunteers need no training; the coordinator tells them where to walk |
| Documentation and presentation (10%) | This guide, README, REPORT, API.md, architecture diagram, video |
| Cloud delivery, reproducibility, responsible operation (10%) | One CDK stack, deploy and update scripts, traces in DynamoDB, CloudWatch dashboard, budget alarm, no SSH. Every number re-runs from `eval/`. Safety rules live in code (REPORT §9) |
| Agentic Vision award | REPORT §5: OpenCV measurements decide each next action (instruct, ask to pan, slow down, re-route, stop at stairs, flag for re-mapping, hand off). See the trace in the helper console |
