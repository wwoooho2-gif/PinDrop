# PinDrop

PinDrop is a simple local app for downloading public Pinterest pins.

## Requirements

- Python 3.9+
- Node.js and npm
- FFmpeg (only needed for GIF downloads)

## Quick start

From the project root:

```sh
npm install
npm start
```

Then open:

```text
http://127.0.0.1:8765
```

Paste one or more Pinterest pin links, one per line, and choose a download option:

- Download to browser: saves the original media file
- Download as GIF: converts videos to GIF and downloads photos normally

A single pin downloads as a file. Multiple pins are bundled into a ZIP.

## Notes

- The GIF conversion uses the first 15 seconds, up to 720px wide, at 25 fps.
- FFmpeg must be installed and available in your `PATH`.
- This app only supports public Pinterest pins.

## Tests

```sh
npm test
```

Windows users can run:

```sh
npm run start:windows
npm run test:windows
```
