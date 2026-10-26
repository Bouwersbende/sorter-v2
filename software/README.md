# Setup

## Prerequisites

- [Git](https://git-scm.com/)
- [Node.js](https://nodejs.org/) (v20+) and npm
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Raspberry Pi Pico SDK](https://github.com/raspberrypi/pico-sdk) (for firmware builds)

## Clone

```bash
git clone https://github.com/basicallysource/sorter-v2.git
cd sorter-v2/software
```

## Firmware

Flash firmware to each Raspberry Pi Pico using the Makefile in `firmware/sorter_interface_firmware/`. Put one Pico into BOOTSEL mode at a time (so it mounts as `RPI-RP2`), then run the appropriate target — the build happens automatically:

```bash
cd firmware/sorter_interface_firmware
make flash-feeder       # builds and flashes feeder firmware
make flash-distribution # builds and flashes distribution firmware
```

Flash each Pico separately, one at a time in BOOTSEL mode.

## Environment

```bash
cp .env.example .env
cp machine.example.toml machine.toml
```

Edit the path in `.env` to match the real path to `machine.toml`.

### One layer with a permanently closed flap

If the only active storage layer has a mechanically fixed closed flap and no
servo wiring, set this in `machine.toml`:

```toml
[servo]
fixed_closed_single_layer = true
channels = []
```

Enable only layer 1 in the storage layout. The backend skips servo setup and
calibration, but still homes and moves the chute. It holds the classification
channel if another layer is enabled or the physical discard bin is unavailable.
The fixed closed position is a physical setup assertion; the machine cannot
sense whether the flap has moved.


Camera assignment happens in the UI: open the running frontend and use the Settings → Cameras page to map each OpenCV device index to its role (feeder, classification top/bottom, carousel, etc). The resulting assignments are written back to `machine_params.toml` under `[cameras]`.

## UI Dependencies

```bash
cd sorter/frontend
pnpm install --frozen-lockfile
```

---

# Running

You'll need two terminal tabs, both from `sorter-v2/software`.

## Terminal 1: UI

```bash
cd sorter/frontend
npm run dev
```

## Terminal 2: Client

```bash
cd sorter/backend
uv run python main.py
```

Or from `sorter-v2/software`, you can use the bundled dev runner:

```bash
./dev.sh backend
```

That starts the full machine client with the controller, hardware bindings, and API. If you only want the lightweight API shell for UI work, use:

```bash
./dev.sh api
```

`uv` will install Python 3.13 and all dependencies on first run. The `.env` file is loaded automatically.

On startup the client will:
1. Discover all connected Pico devices over USB serial
2. Scan each bus for SorterInterface firmware devices
3. Aggregate stepper and servo actuators across all discovered boards
4. Bind actuators to logical roles (carousel, chute, rotors) using firmware-reported names

## Machine investigation

The **Machine investigation** page in the Sorter UI records the classification
camera whenever the controller is sorting. It uses the camera's existing
capture thread; it does not open another USB camera connection. The recordings
are timestamp-indexed JPEG frames in rotating `.mjpg` segments, played by the
page's frame viewer. It records the original JPEGs when the camera provides
MJPEG and encodes frames on the recorder thread for other capture formats.
The page shows recording gaps and dropped frames rather than repeating a frame
as if it were a new observation.

All local evidence is under `sorter/backend/machine_investigation/` beside
`local_state.sqlite` (or beside `LOCAL_STATE_DB_PATH` when that is set). The
SQLite database contains frame offsets, piece events, and request metadata.
The `requests/` directory contains the exact JPEGs submitted to Brickognize
and its original response bodies. Neither authentication headers nor API keys
are recorded. The stored response can include candidates the sorting pipeline
later filtered out.

The recorder starts automatically with the full backend (`./dev.sh backend` or
`./dev.sh`) and writes frames only while sorting is running. The defaults are
8 GiB for video, 1 GiB for Brickognize request files, and 30 days of age
retention. Override them before starting the backend:

```bash
SORTER_INVESTIGATION_MAX_GB=16 \
SORTER_INVESTIGATION_REQUEST_MAX_MB=2048 \
SORTER_INVESTIGATION_RETENTION_DAYS=14 \
./dev.sh backend
```

Disk use depends on the camera's JPEG size and frame rate. For example,
100 KiB per frame at 30 FPS is about 9.9 GiB per hour. Recording health,
current storage use, and dropped-frame counts appear at the top of the page.
Oldest closed segments and request files are removed when their respective
limits are exceeded; their absence is visible in the investigation page.
If the disk fills, the recorder reports an error and retries in its own thread;
the sorting control loop does not wait for video. A camera disconnect stops new
frames, marks recording unhealthy after two seconds, and creates a gap event.
When frames resume, the index continues at their actual capture timestamps.

To inspect a piece, open **Bins**, open a bin, select a part/color group, then
select its individual piece record. The link opens Machine investigation with
that piece's classification, exact Brickognize input and response, routing
decision, chute command and release timeline, and time-linked camera frames.
The displayed chute angle is calculated from commanded steps. The machine has
no independent angle sensor, so the page does not claim a measured physical
position or a physically confirmed landing bin.

**Windows**: Run PowerShell as Administrator to access serial ports.

---

# Further Reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — Multi-Pico hardware abstraction, firmware roles, client init flow
- [../docs/runtime-status.md](../docs/runtime-status.md) — Current detector benchmark conclusions, deployment recommendations, and canonical local artifacts to keep
- [../docs/model-artifacts.md](../docs/model-artifacts.md) — Which detector exports and compiled formats exist, what they are for, and how they map to targets
- [../docs/device-benchmarking.md](../docs/device-benchmarking.md) — Reproducible detector benchmarking across Mac, Raspberry Pi, and Orange Pi targets
- [../docs/hailo-hef-workflow.md](../docs/hailo-hef-workflow.md) — `ONNX -> HEF` workflow for Raspberry Pi 5 AI HAT deployments
- [firmware/sorter_interface_firmware/README.md](firmware/sorter_interface_firmware/README.md) — Firmware build options and flashing
