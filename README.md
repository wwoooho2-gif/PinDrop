# PinDrop

PinDrop is a small local website for downloading images and videos from
publicly accessible Pinterest pins.

## Requirements

- Python 3.9 or newer
- Node.js and npm
- FFmpeg (only needed for **Download as GIF**)

PinDrop does not use any npm packages. `npm install` does not create a Python
virtual environment; use your system Python, or create and activate your own
environment if you prefer.

## Run

From the project root:

```sh
npm install
npm start
```

PinDrop opens in your browser at <http://127.0.0.1:8765>. Paste one or more
Pinterest pin links, one per line, then choose a download button:

- **Download to browser** saves images/videos in their original media format.
- **Download as GIF** converts video pins to GIF and downloads photos normally.

A single pin downloads directly as a file. Multiple pins are bundled into a ZIP.
The pasted links clear after a successful download starts. Stop the server with
Ctrl+C in the terminal.

The GIF conversion uses the first 15 seconds, up to 720 pixels wide, at 25 fps,
with a 100 MB output limit. FFmpeg must be installed and available on `PATH`.

On Windows, run `npm run start:windows` and `npm run test:windows`.

## Tests

```sh
npm test
```

## Limitations

Only publicly accessible Pinterest pins are supported. PinDrop does not sign
in, bypass access restrictions, or download private content. Please respect
creators' rights and Pinterest's terms.
