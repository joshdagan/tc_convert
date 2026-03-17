# AFL Timecode Converter

Converts between BTGL/BTGR file-elapsed time, BCAST visible countdown, and time-of-day timecode — without transferring any media files.

---

## Setup (one time only)

1. Double-click **`install.bat`**
2. Wait for it to finish — it installs everything automatically
3. If you see **"pip is not recognized"**, close the window and double-click `install.bat` again
4. Done

---

## Daily use

1. Double-click **`run.bat`**
2. A terminal window opens — keep it running in the background
3. Your browser opens at `http://localhost:8765`
4. Click **Browse…** to select your match folder
5. Close the terminal when you're done

---

## Sharing with colleagues on your network

If one person wants to run the server so others can connect without installing anything:

1. Double-click **`run_network.bat`** instead
2. The terminal prints your IP address — share `http://[that-ip]:8765` with colleagues
3. They open it in any browser, no install needed

> Colleagues connecting remotely type their media path manually, e.g. `\\server\share\match\`

---

## Folder structure

One folder per match, files named with a quarter prefix:

```
Q1_BTGL.mxf   Q1_BTGR.mxf   Q1_BCAST.mxf
Q2_BTGL.mxf   Q2_BTGR.mxf   Q2_BCAST.mxf
Q3_BTGL.mxf   Q3_BTGR.mxf   Q3_BCAST.mxf
Q4_BTGL.mxf   Q4_BTGR.mxf   Q4_BCAST.mxf
```

Supported formats: `.mp4  .mov  .mxf  .avi  .mkv`
