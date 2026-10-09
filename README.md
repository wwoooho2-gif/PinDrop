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

Paste one or more Pinterest pin links, one per line, and choose a download option:

- Download to browser: saves the original media file
- Download as GIF: converts videos to GIF and downloads photos normally

A single pin downloads as a file. Multiple pins are bundled into a ZIP.