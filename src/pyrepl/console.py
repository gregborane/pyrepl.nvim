import base64
import io
import json
import math
import os
import secrets
import shutil
import sys
from argparse import ArgumentParser
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Thread
from typing import Any

import pynvim
from jupyter_console.app import ZMQTerminalIPythonApp
from prompt_toolkit.styles import defaults as ptk_defaults
from traitlets.config import Config

PNG = "image/png"
JPG = "image/jpeg"
SVG = "image/svg+xml"

# First 64 entries from Kitty's stable row/column diacritics table.  The first
# placeholder in each row carries the row diacritic; following placeholders
# inherit that row and increment columns left-to-right.
ROW_DIACRITICS = tuple(
    chr(int(cp, 16))
    for cp in (
        "0305 030D 030E 0310 0312 033D 033E 033F 0346 034A 034B 034C 0350 0351 0352 0357 "
        "035B 0363 0364 0365 0366 0367 0368 0369 036A 036B 036C 036D 036E 036F 0483 0484 "
        "0485 0486 0487 0592 0593 0594 0595 0597 0598 0599 059C 059D 059E 059F 05A0 05A1 "
        "05A8 05A9 05AB 05AC 05AF 05C4 0610 0611 0612 0613 0614 0615 0616 0617 0657 0658"
    ).split()
)
PLACEHOLDER = "\U0010EEEE"


@dataclass
class ImageRequest:
    data: str
    inline: dict[str, int] | None
    done: Event = field(default_factory=Event)
    rendered_inline: bool = False


def log(msg: str):
    return f"[pyrepl] {msg}"


def normalize_payload(payload: Any) -> str | None:
    """Normalize image payload to a single string."""
    if isinstance(payload, str) and payload:
        return payload

    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(item, str) for item in payload)
    ):
        combined = "".join(payload)
        if combined:
            return combined

    return None


def pick_image_payload(data: dict[str, Any]) -> tuple[str, str] | None:
    """Pick first supported image payload in preferred order."""
    for image_mime in (PNG, JPG, SVG):
        payload = normalize_payload(data.get(image_mime))
        if payload:
            return image_mime, payload

    return None


def convert_image_to_png_base64(image_mime: str, image_data: str) -> str | None:
    """Convert supported image payloads to base64-encoded PNG."""
    if image_mime == PNG:
        return image_data

    if image_mime == SVG:
        try:
            import cairosvg

            raw = image_data.encode("utf-8")
            png_bytes = cairosvg.svg2png(bytestring=raw)
            return base64.b64encode(png_bytes).decode("utf-8")
        except Exception:
            return None
    if image_mime == JPG:
        try:
            from PIL import Image

            raw = base64.b64decode(image_data)
            img = Image.open(io.BytesIO(raw)).convert("RGBA")
            output = io.BytesIO()
            img.save(output, format="PNG")
            return base64.b64encode(output.getvalue()).decode("utf-8")

        except Exception:
            return None

    return None


def image_pipeline(data: Any):
    """Handle Jupyter image output and normalize it to base64 PNG."""
    if not isinstance(data, dict):
        return None

    selected = pick_image_payload(data)
    if selected is None:
        return None
    image_mime, image_data = selected

    return convert_image_to_png_base64(image_mime, image_data)


def png_dimensions(img_base64: str) -> tuple[int, int] | None:
    """Read PNG dimensions from IHDR without introducing another dependency."""
    try:
        raw = base64.b64decode(img_base64)
    except Exception:
        return None

    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None

    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def inline_geometry(img_base64: str) -> dict[str, int] | None:
    """Calculate a Jukit-style Kitty placeholder grid for the REPL PTY."""
    dimensions = png_dimensions(img_base64)
    if dimensions is None:
        return None

    width_px, height_px = dimensions
    term_cols, term_rows = shutil.get_terminal_size((80, 24))
    term_cols = max(1, term_cols)
    term_rows = max(1, term_rows)

    # Same defaults as the Ghostty/Kitty Jukit port: render close to terminal
    # width and model a text cell as roughly twice as tall as it is wide.
    scaling = 0.9
    cell_aspect = 2.0
    image_aspect = height_px / width_px

    max_width_cells = min(term_cols, term_rows * cell_aspect)
    cols = max(1, min(term_cols, int(math.floor(max_width_cells * scaling))))
    rows = max(1, int(math.ceil(cols * image_aspect / cell_aspect)))

    max_rows = min(term_rows, len(ROW_DIACRITICS))
    if rows > max_rows:
        rows = max_rows
        cols = max(
            1,
            min(term_cols, int(math.floor(rows * cell_aspect / image_aspect))),
        )

    # Keep live REPL image IDs out of the small 1..256 namespace used by the
    # existing image-history placeholder objects.
    image_id = 0x101 + secrets.randbelow(0xFFFEFF)
    return {
        "image_id": image_id,
        "cols": cols,
        "rows": rows,
        "term_cols": term_cols,
    }


def real_stdout():
    """Return the actual PTY stdout used by the embedded jupyter-console."""
    return sys.__stdout__ if sys.__stdout__ is not None else sys.stdout


def write_placeholders(spec: dict[str, int]):
    """Write Kitty Unicode placeholders into the REPL terminal flow."""
    image_id = spec["image_id"]
    cols = spec["cols"]
    rows = spec["rows"]
    term_cols = spec["term_cols"]

    red = (image_id >> 16) & 0xFF
    green = (image_id >> 8) & 0xFF
    blue = image_id & 0xFF
    foreground = f"\x1b[38;2;{red};{green};{blue}m"
    padding = max(0, (term_cols - cols) // 2)
    prefix = " " * padding

    out = real_stdout()
    for row in range(rows):
        line = prefix + PLACEHOLDER + ROW_DIACRITICS[row]
        if cols > 1:
            line += PLACEHOLDER * (cols - 1)
        out.write("\r" + foreground + line + "\x1b[39m\r\n")
    out.flush()


def image_worker(queue: Queue, dead: Event, nvim: pynvim.Nvim):
    """Worker that keeps all parent-Neovim RPC on the existing image thread."""
    try:
        while True:
            request: ImageRequest = queue.get()

            try:
                request.rendered_inline = bool(
                    nvim.exec_lua(
                        "return require('pyrepl.image').console_endpoint(...)",
                        request.data,
                        request.inline,
                    )
                )
            except Exception as e:
                print(log(f"failed to display image: {e}"))
            finally:
                request.done.set()
                queue.task_done()
    except Exception as e:
        print(log(f"image worker is dead {e}"))
        dead.set()


def main() -> None:
    """Run the Jupyter console with pyrepl integration."""
    path = os.environ.get("NVIM")
    assert path is not None

    nvim = pynvim.attach("socket", path=path)
    queue = Queue()
    dead = Event()
    thread = Thread(target=image_worker, args=(queue, dead, nvim), daemon=True)

    def image_handler(data):
        if dead.is_set():
            return False
        data = image_pipeline(data)
        if data is None:
            return False

        inline = inline_geometry(data)
        request = ImageRequest(data=data, inline=inline)
        queue.put(request)

        # Live rendering must upload/create the outer-terminal placement before
        # the matching placeholder cells are printed into the inner REPL PTY.
        if not request.done.wait(timeout=10):
            print(log("timed out while preparing image display"))
            return True

        if request.rendered_inline and inline is not None:
            try:
                write_placeholders(inline)
            except Exception as e:
                print(log(f"failed to write image placeholders: {e}"))

        return True

    config = Config()
    config.ZMQTerminalInteractiveShell.image_handler = "callable"
    config.ZMQTerminalInteractiveShell.callable_image_handler = image_handler
    app = ZMQTerminalIPythonApp.instance(config=config)
    parser = ArgumentParser("Pyrepl console.", add_help=False)
    parser.add_argument("--prompt-toolkit-overrides", type=str, default=None)
    known, args = parser.parse_known_args()

    try:
        app.initialize(args)

        if known.prompt_toolkit_overrides is not None:
            overrides = dict(ptk_defaults.PROMPT_TOOLKIT_STYLE)
            overrides.update(json.loads(known.prompt_toolkit_overrides))
            ptk_defaults.PROMPT_TOOLKIT_STYLE[:] = list(overrides.items())
        thread.start()
        app.start()  # type: ignore

    except Exception as e:
        nvim.exec_lua("vim.notify(..., vim.log.levels.ERROR)", log(str(e)))


if __name__ == "__main__":
    main()
