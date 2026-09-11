import mss
import mss.tools

# Adjust these numbers until the saved image perfectly frames your Discord alerts
monitor = {"top": 120, "left": 50, "width": 1200, "height": 1000}

with mss.mss() as sct:
    screenshot = sct.grab(monitor)
    mss.tools.to_png(screenshot.rgb, screenshot.size, output="discord_test.png")
    print("📸 Screenshot saved as discord_test.png")