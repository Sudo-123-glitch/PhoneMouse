# PhoneMouse

Use your phone as a wireless **air mouse and keyboard** for a Windows PC.
Point the phone to move the cursor, tap to click, swipe to scroll, and type with the phone keyboard.

There is no app to install on the phone. It is a web page served by one small Python file running on your PC, and everything stays on your own Wi-Fi.

## What you need

- A **Windows** PC with [Python 3](https://www.python.org/downloads/) (tick **"Add python.exe to PATH"** in the installer)
- A phone with a motion sensor (built and tested with an **iPhone** and Safari)
- Both on the **same Wi-Fi network**

## How to run it

**1. Download** this repository (green **Code** button → **Download ZIP**, then unzip), or:

```
git clone https://github.com/Sudo-123-glitch/PhoneMouse.git
cd PhoneMouse
```

**2. Install the one required package** (first time only):

```
python -m pip install cryptography
```

**3. Start PhoneMouse:**

```
python phone_mouse.py
```

If `python` is not recognised, use `py` instead. If Windows Firewall asks, click **Allow** (private networks).

The window shows an address and a PIN, like this:

```
  2. Open Safari on the iPhone and go to:

        https://192.168.1.5:8443

  3. Enter this PIN:   123456
```

**4. On your phone**, open that `https://` address in Safari.

You will see a warning such as **"This Connection Is Not Private"**. That is expected: PhoneMouse creates its own security certificate on your PC, and browsers do not recognise home-made certificates. Tap **Show Details → visit this website → Visit Website** (in Chrome: **Advanced → Proceed**).

**5. Enter the PIN**, tap **Start**, and tap **Allow** when asked for Motion & Orientation access.

Leave the window on the PC open while you use it. Press `Ctrl+C` in that window to stop.

## How to use it

| On the phone | What it does |
|---|---|
| Hold the big pad and turn / tilt the phone | Moves the cursor |
| Quick tap on the big pad | Left click |
| **Left** / **Right** buttons | Mouse buttons (hold **Left** while pointing to drag) |
| Swipe on the **SCROLL** strip | Scrolls |
| Text box or ⌨ button | Opens the phone keyboard; what you type appears on the PC |
| Row of small keys | Enter, Backspace, Esc, Tab, arrows, Copy, Paste, Undo, Alt+Tab, volume, and more |

### Settings (gear icon)

- **Mode**
  - **Hold** – cursor moves only while you hold the pad (default)
  - **Toggle** – tap once to start pointing, tap again to stop
  - **Laser** – cursor sits where the phone points; tap **Center** while pointing at the middle of the screen to calibrate, and again whenever it drifts
  - **Touchpad** – drag your finger instead of moving the phone
- **Speed**, **Steadiness**, **Shake filter**, **Acceleration** – tune how the pointing feels
- **Flip left/right**, **Flip up/down** – use these if a direction is reversed
- **Swap sensor axes** – try this if tilting the phone moves the cursor sideways

## Options

```
python phone_mouse.py --port 9443     use a different port
python phone_mouse.py --new-pin       create a new PIN
python phone_mouse.py --dry-run       print actions instead of controlling the PC
```

## Troubleshooting

- **The page will not load on the phone** – check that both devices are on the same Wi-Fi, and that Windows Firewall allowed Python. Guest Wi-Fi networks often block devices from reaching each other.
- **Pointing does nothing** – motion access was not allowed. Reload the page, tap **Start**, and tap **Allow**. Motion only works over `https://`.
- **The cursor moves the wrong way** – use the Flip or Swap options in Settings.
- **Some windows ignore the phone** – apps running as administrator (Task Manager, installers) only accept input if PhoneMouse is also started as administrator.

## How it works

1. `phone_mouse.py` runs a small HTTPS web server on the PC and serves the controller page to the phone.
2. The page reads the phone's gyroscope (how fast the phone is turning) about 60 times a second and converts that into cursor movement, with filtering for hand shake.
3. Movement, clicks, scrolling and typed text are sent to the PC over Wi-Fi.
4. The PC program passes them to Windows as normal mouse and keyboard input.

## Security

- Every message from the phone must carry the PIN. Ten wrong attempts lock that device out for a minute.
- Traffic is encrypted with a certificate generated on your own PC on first run.
- Anyone on your Wi-Fi who knows the PIN can control your PC while PhoneMouse is running. Use it on networks you trust, and stop it when you are done.
- The generated `phonemouse_cert.pem`, `phonemouse_key.pem` and `phonemouse_pin.txt` are private to your PC. They are listed in `.gitignore`; do not share them.
