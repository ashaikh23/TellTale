import board
import displayio
import terminalio
import time
from adafruit_display_text import label

# --- PROTOTYPE DATA (shape matches /decision response) ---
MOCK_DECISION = {
    "status": "ok",
    "leave_in_min": 18,
    "next_train": {"route_id": "1", "in_min": 4},
    "next_class": {"name": "ADVANCED UX", "start_local": "2026-02-08T13:15:00"},
    "weather_summary": "34°F, snow likely",
    "weather_badges": ["SNOW", "COLD"],
    "feed_age_sec": 22,
    "warning": None,
}

# --- DISPLAY SETUP ---
display = board.DISPLAY
display.rotation = 180
main_group = displayio.Group()
display.root_group = main_group

# 1. Background & Separator Line (No library needed)
# We create a 1-pixel high bitmap to act as our "Line"
line_bmp = displayio.Bitmap(220, 1, 1)
line_palette = displayio.Palette(1)
line_palette[0] = 0x333333
line_grid = displayio.TileGrid(line_bmp, pixel_shader=line_palette, x=10, y=30)
main_group.append(line_grid)

# 2. Subway Icon Box (18x18 Square)
sub_bmp = displayio.Bitmap(18, 18, 1)
sub_palette = displayio.Palette(1)
sub_palette[0] = 0xEE352E # MTA Red
sub_grid = displayio.TileGrid(sub_bmp, pixel_shader=sub_palette, x=10, y=108)
main_group.append(sub_grid)

# 3. UI Labels
# Header
weather_label = label.Label(terminalio.FONT, text="--", color=0x00D4FF, x=10, y=15)
time_label = label.Label(terminalio.FONT, text="--", color=0xFFFFFF, x=175, y=15)
main_group.append(weather_label)
main_group.append(time_label)

# Center Countdown
main_group.append(label.Label(terminalio.FONT, text="LEAVE IN", color=0x888888, x=90, y=45))
countdown = label.Label(terminalio.FONT, text="--", scale=5, color=0x00FF00, x=85, y=80)
main_group.append(countdown)
main_group.append(label.Label(terminalio.FONT, text="MINS", color=0x888888, x=155, y=85))

# Footer
route_label = label.Label(terminalio.FONT, text="--", color=0xFFFFFF, x=16, y=117)
train_eta_label = label.Label(terminalio.FONT, text="Train in --", color=0xBBBBBB, x=35, y=117)
class_label = label.Label(terminalio.FONT, text="📚 --", color=0xFFFF00, x=10, y=128)
main_group.append(route_label)
main_group.append(train_eta_label)
main_group.append(class_label)

# Status/warning line (small, optional)
status_label = label.Label(terminalio.FONT, text="", color=0xFFAA00, x=10, y=60)
main_group.append(status_label)


def format_time(local_iso: str) -> str:
    # Minimal parser for HH:MM in ISO; on-device, keep it simple
    try:
        return local_iso.split("T")[1][:5]
    except Exception:
        return "--"


def apply_decision(data: dict):
    # Header
    weather_label.text = data.get("weather_summary", "--")
    time_label.text = time.strftime("%I:%M %p")

    # Leave-in
    leave_in = data.get("leave_in_min")
    if leave_in is None:
        countdown.text = "--"
    else:
        countdown.text = str(int(leave_in))

    # Color logic for urgency
    if leave_in is None:
        countdown.color = 0xCCCCCC
    elif leave_in <= 5:
        countdown.color = 0xFF0000
    elif leave_in <= 10:
        countdown.color = 0xFF8000
    else:
        countdown.color = 0x00FF00

    # Train info
    next_train = data.get("next_train") or {}
    route_label.text = next_train.get("route_id", "--")
    in_min = next_train.get("in_min")
    train_eta_label.text = f"Train in {in_min} min" if in_min is not None else "Train in --"

    # Class info
    next_class = data.get("next_class") or {}
    class_name = next_class.get("name", "--")
    class_time = format_time(next_class.get("start_local", "--"))
    class_label.text = f"📚 {class_name} @ {class_time}"

    # Status / warning
    status = data.get("status", "ok")
    warning = data.get("warning")
    if status in ("feed_error", "stale_feed"):
        status_label.text = status.replace("_", " ").upper()
    elif warning:
        status_label.text = warning[:20]
    else:
        status_label.text = ""

# --- SIMPLE ANIMATION LOOP ---
apply_decision(MOCK_DECISION)
last_tick = time.monotonic()

while True:
    # Demo: decrement leave_in_min every 5 seconds
    if time.monotonic() - last_tick > 5:
        last_tick = time.monotonic()
        if MOCK_DECISION.get("leave_in_min") is not None:
            MOCK_DECISION["leave_in_min"] -= 1
            if MOCK_DECISION["leave_in_min"] < 0:
                MOCK_DECISION["leave_in_min"] = 20
        apply_decision(MOCK_DECISION)
