# DSN Live Viewer

A browser-based 3D viewer for live NASA Deep Space Network activity.

## What It Shows

- Live DSN downlinks from NASA/JPL's DSN XML feed
- DSN dish azimuth/elevation beams for active connections
- Inbound data pulses scaled by data rate
- Earth, stars, constellations, and apparent-sky ephemeris markers
- Optional JPL Horizons angle-audit readout for selected connections

## Local Run

From `D:\DSN`:

```powershell
python server.py
```

Then open:

```text
http://127.0.0.1:8000
```

The server also prints a LAN URL for sharing on your local network.

## Main Files

- `dsn_live_viewer.html`: the client app
- `server.py`: local/Render web server and proxy endpoints
- `Earth_1_12756.obj`: Earth mesh
- `earth_diff.png`: Earth texture atlas

## Live Data Sources

- NASA/JPL DSN XML feed: `https://eyes.nasa.gov/dsn/data/dsn.xml`
- JPL Horizons API for Sun/Moon/Mars/Jupiter and angle audits

## Git Workflow

Local edits are **not** synced automatically.

To publish changes:

```powershell
git -C D:\DSN add .
git -C D:\DSN commit -m "Describe the change"
git -C D:\DSN push
```

That updates GitHub, and Render should redeploy automatically from `main`.

## Render Deployment

This repo includes `render.yaml`, so Render can deploy it as a Python web service.

Important:

- Render must run `python server.py`
- `server.py` reads Render's `PORT` env var automatically

## Notes

- The DSN XML updates roughly every 5 seconds, so the app polls on that cadence.
- If there are no active downlinks, the UI now shows current dish activity summaries like engineering upgrades or maintenance when available in the XML.
