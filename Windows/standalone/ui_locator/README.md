# Standalone UI Locator

This is a completely separate utility from the main project.

It reads a screenshot, detects likely UI elements (buttons and input fields), extracts text labels with OCR, and can move the mouse cursor to a chosen element center.

## Where is the screenshot?

Your ready-to-test sample screenshot is here:

- `standalone/ui_locator/sample_ui.png`

## Quick Launch (Native desktop — no localhost)

Launchers open a **Qt (PySide6)** window. Nothing listens on a TCP port.

### macOS

```bash
cd standalone/ui_locator
./Launch_UI_Locator.command
```

Alias launcher (renamed):

```bash
./Launch_Kinkar.command
```

### Windows

```bat
cd standalone\ui_locator
Launch_UI_Locator.bat
```

Alias launcher (renamed):

```bat
Launch_Kinkar.bat
```

The launcher creates/uses `.venv`, installs dependencies, checks `tesseract`, then runs `native_app.py`.

The app opens as a **narrow side dock** (~300px) snapped to the **right edge** of your screen, with **Always on top** on by default so you can keep it beside a browser. Uncheck if you prefer normal stacking.

## Native UI flow

1. **Live screen:** click **Capture live screen**, then **Detect elements** — or type a name and click **Go to name on live screen** (captures, detects, moves the mouse in one step).
2. **From a file:** **Upload Screenshot** (or use the auto-loaded sample), then **Detect Elements**.
3. Pick an element in the list (or **Auto-select by name**).
4. **Dry Run (No Move)** or **Move Cursor To Selected** (when using a loaded image).

The preview draws boxes and labels over detected buttons/inputs.

## Additional feature: live training by cursor

You can teach Kinkar custom labels for live pages:

1. Put the website in front.
2. In Kinkar, type a training label in **Target name** (example: `publish_post`).
3. Click **Train from current cursor (3s)**.
4. During countdown, place your mouse over the exact button/input on the live page.
5. Kinkar stores mapping in `kinkar_training_map.json`.

After training, **Go to name (live)** will use your trained label directly.

## Offline-only (no cloud, no hosted service)

This tool does **not** call any remote API. Everything runs on your machine:

- **PySide6** — desktop window
- **OpenCV** — edge / contour hints for boxes
- **Tesseract** — local OCR for text on the screenshot
- **PyAutoGUI** — local mouse control

You only need internet when **installing** Python packages (`pip`), not at runtime.

## Toward higher accuracy (training your own detector)

The current detector is **heuristics + OCR**: fast and private, but not “perfect” on every custom UI.

To train something stronger, entirely offline:

1. **Collect screenshots** of the apps you care about (same resolution you will use live).
2. **Label** bounding boxes for classes you want (`button`, `text_field`, `checkbox`, …) using any local annotator (CVAT offline, Label Studio self-hosted, or even a simple JSON + rectangles).
3. **Train** an object-detection model on your GPU/CPU (for example **Ultralytics YOLO**, **Detectron2**, or **RT-DETR**) — training is local; no cloud required.
4. **Export** weights (for example ONNX or PyTorch `.pt`) and **swap** the `detect_elements` implementation in `ui_locator.py` to run inference on the screenshot instead of (or blended with) the heuristic pass.

If you want that integrated next, say whether you prefer **YOLO-style boxes** or **segmentation**, and which OS you will ship on first.

## Legacy Tk desktop (`app_gui.py`)

Older Tkinter UI (can be blank on some macOS setups):

```bash
cd standalone/ui_locator
source .venv/bin/activate
python app_gui.py
```

## CLI (optional)

### 1) Install manually

```bash
cd standalone/ui_locator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Install Tesseract OCR binary

`pytesseract` requires the `tesseract` binary installed on your machine.

On macOS:

```bash
brew install tesseract
```

### 3) Detect elements from screenshot

```bash
python ui_locator.py list --screenshot "/absolute/path/to/screenshot.png"
```

### 4) Move mouse to a named element

```bash
python ui_locator.py move \
  --screenshot "/absolute/path/to/screenshot.png" \
  --target "Submit"
```

### 5) Live screen: capture, detect, move to a name

Uses whatever is on screen **right now** (no image file). Arrange windows first, then run:

```bash
python ui_locator.py live --target "Submit"
```

Dry run (prints coordinates only):

```bash
python ui_locator.py live --target "Email" --dry-run
```

File-based move without moving the mouse:

```bash
python ui_locator.py move \
  --screenshot "/absolute/path/to/screenshot.png" \
  --target "Submit" \
  --dry-run
```

## Notes

- Accuracy depends on screenshot quality and contrast.
- This version is heuristics + OCR based (fast and local, no cloud dependency).
- Borderless/custom UI controls may need model-based detection in a next version.
