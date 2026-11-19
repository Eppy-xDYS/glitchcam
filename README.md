# GlitchCam

A glitch camera for the M5Stack CardputerZero. It takes the IMX219's raw Bayer
frames and decodes them wrong on purpose: bad stride, wrong bit depth, wrong Bayer
order, emulated bent ADC wires, flipped JPEG bytes, and H.264 with the keyframes
removed. Optionally it also bends the sensor's own registers over I2C.

## Install / remove

```bash
sudo ./install.sh
```

It shows up in the launcher as **GlitchCam**. If it doesn't, run
`systemctl --user restart APPLaunch`.

```bash
sudo /usr/share/APPLaunch/apps/glitchcam/uninstall.sh
```

Uninstalling removes the app, its icon and its launcher entry. Your photos and
clips in `~/Pictures/GlitchCam` are kept.

## Keys

| Key | Does |
|---|---|
| `,` `/` (or ← →) | previous / next glitch mode |
| `1`–`8` | jump to a mode (except in BEND, see below) |
| `;` `.` (or ↑ ↓) | intensity 1–9 (each mode remembers its own) |
| `SPACE` / `ENTER` | take a still |
| `V` | start / stop a video clip (max 5 min) |
| `S` | stack the current mode, then pick another to layer on top |
| `C` | clear the stack |
| `R` | new seed (the same seed always gives the same glitch) |
| `1`–`9` in BEND | **hold** to connect that wire, like the buttons on a bend box |
| `SHIFT`+`1`–`9` in BEND | latch / unlatch that one wire |
| `Y` | arm / disarm SENSOR mode |
| `TAB` | hide / show the HUD |
| `H` | help |
| `ESC` / `Q` | quit |

## Modes

| # | Mode | What breaks |
|---|---|---|
| 1 | CLEAN | nothing: plain raw decode |
| 2 | TEAR | wrong line stride in bands: tearing, shearing, smeared diagonals |
| 3 | DEPTH | 10-bit samples read at the wrong bit depth: values wrap into contour bands |
| 4 | SWAP | wrong Bayer order, R/B planes slip apart, LSB bytes leak in |
| 5 | BEND | circuit-bending emulation: ADC data bits bridged, stuck or crossed (B5–B9), CLPOB clamp banding, SHP sample-hold ghosts, ADCCK clock dropouts, XV smear, XSG row repeats. Intensity connects wires 1..n; the HUD lists the connected wires |
| 6 | JPEG | bytes flipped inside the JPEG entropy data |
| 7 | MOSH | live: motion pushes stale pixels around. Clip: real H.264 with keyframes cut |
| 8 | SENSOR | **experimental**: writes IMX219 registers over I2C (digital gain, mirror/flip Bayer slip, readout skipping, CSI format, test patterns). Needs `Y` to arm |

Stacking: TEAR, DEPTH, SWAP and BEND all happen in a single pass over the raw
bytes. JPEG is applied after that, and MOSH last. SENSOR changes the sensor
itself, underneath everything else.

## BEND wires

BEND works like a bend box with nine wires. The image gets every wire that is
connected right now:

- **Latched by intensity:** `;` `.` (or ↑ ↓) set how many stay connected.
  Level *n* connects wires 1 to *n* with no keys held.
- **Latched one at a time:** `SHIFT`+`1`–`9` latches that wire, or unlatches
  it if it's already on. This works on top of the intensity setting, so you
  can latch wire 5 alone or switch wire 1 off. Changing the intensity again
  goes back to plain wires 1 to *n*.
- **Held:** while BEND is the current mode, holding `1`–`9` connects that wire
  only while the key is down, on top of the latched ones. Holding a wire that
  is already latched changes nothing.

Example: level 2 while holding `5` and `7` connects wires 1, 2, 5 and 7. Wires
always apply in number order, so key order doesn't matter. The top bar lists
the connected wires. In BEND, the intensity bar shows one cell per wire: lit
for latched, magenta for held.

When you've latched wires one at a time, the filename lists them instead of
the level, e.g. `BEND_W25` for wires 2 and 5.

The sensor gives each pixel as a 10-bit value (0–1023); bit 9 is the biggest
step in brightness.

| Wire | Imitates | Looks like |
|---|---|---|
| **B*x*~B*y*** (always wire 1) | two high data lines shorted | if either bit is on, both are: mid-tones jump brighter in hard steps |
| **B5 HI** or **B6 HI** | a data line stuck on | a fixed bit added everywhere: washed-out, stepped lift |
| **B9 LO** | the top data line stuck off | anything above half brightness drops to dark: highlights flip to shadows |
| **B*c*<>B*d*** | two data lines crossed | the two bits trade places: tones scramble into contour bands |
| **CLPOB** | black clamp firing on the wrong lines | horizontal bands shifted lighter or darker |
| **SHP** | sample-and-hold timing off | colour ghosts slid sideways: red shifted, blue smeared, green doubled |
| **ADCCK** | ADC clock dropouts | in some bands, pixels repeat across columns: blocky streaks |
| **XV2** | vertical clock off | bright areas smear downward like bleeding charge |
| **XSG** | transfer-gate glitch | rows repeat in bands: stepped vertical stretch |

The seed (`R`) decides which wire sits at keys 2–9 and the details of each
(which bits, where the bands fall, how far the ghosts shift). The same seed,
intensity and held keys always give the same image.

To hold wires over another mode, stack that mode first (`S`), then switch to
BEND: on any other mode, `1`–`8` jump between modes instead.

Stills keep the wires connected at the moment you press SPACE. Clips record
every frame as it was, so pressing and releasing wires while recording plays
back like working a bend box live.

## Output

Everything is saved to `~/Pictures/GlitchCam/`. Filenames record the mode(s),
intensity and seed, e.g. `GC_20261118-202027_DEPTH4-BEND2_S2765.png`.

- **Stills:** 820×616 PNG from the full sensor frame. In JPEG mode the file is
  the corrupted `.jpg` bytes themselves. In MOSH mode it's the live mosh buffer
  at 640×480.
- **Clips (MOSH alone):** the camera's hardware H.264 at 640×480/30 fps. Then
  keyframes are cut and some frames repeated ("bloom"). Intensity sets how many
  keyframes go.
- **Clips (any other mode or stack):** what the glitch pipeline renders,
  320×170 at viewfinder rate, upscaled 2× to 640×340.

Clips are encoded after you stop recording, in a background process. The HUD
shows `ENC` while that runs, and quitting the app doesn't interrupt it.

## SENSOR mode safety

- Only whitelisted, volatile registers are written. OTP/NVM is never touched.
- Original values are read first and restored when you change level, leave the
  mode, or quit.
- The kernel driver rewrites every register whenever streaming restarts, so
  closing the app always resets the sensor.
- If frames stop for 2 s, GlitchCam restarts the camera and disarms the mode.
- Known hang: shrinking LINE_LENGTH (0x0162) stops frames entirely. It's
  deliberately left out.

## Notes

- Startup takes 5–15 s: importing numpy and picamera2 on a 350 MB Pi Zero 2 is
  slow. A splash shows meanwhile.
- The viewfinder runs at about 6–16 fps depending on the mode.
- Logs: `~/.cache/glitchcam.log`.
- Env knobs: `GLITCHCAM_DIR` (save folder), `GLITCHCAM_CLIP_MAX` (seconds),
  `GLITCHCAM_FLIP=0` (if the image is upside down on your unit), `GLITCHCAM_DEBUG=1`.
