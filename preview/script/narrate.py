#!/usr/bin/env python3
"""
narrate.py - turn a Jekyll article into an MP3.

Two ways to run it:

    ./narrate.py _posts/project_eclipse.md
        Writes assets/audio/texts/project_eclipse_narration.md, then
        assets/audio/project_eclipse.mp3

    ./narrate.py --audio-only assets/audio/texts/project_eclipse_narration.md
        Regenerates assets/audio/project_eclipse.mp3 from the edited text

The narration file is never overwritten once it exists, so hand-typed
pronunciation fixes survive. Use --force to rebuild it from the article.

Useful flags:
    --script-only     write the narration text and stop
    --dry-run         print the script and cost estimate, generate nothing
    --engine openai   paid API instead of local Kokoro (needs OPENAI_API_KEY)
    --voice af_bella  see --help for the lists

Two engines:
  kokoro (default)  free, runs locally on CPU, no account, no network
  openai            paid API, ~20-25c per article

One-time setup for the free engine:
    pip install kokoro-onnx soundfile
    curl -L -o kokoro.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
    curl -L -o voices.bin  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

Paths default to assets/audio and assets/audio/texts, overridable with the
NARRATE_AUDIO_DIR and NARRATE_TEXT_DIR environment variables.

Requires: python 3.9+, ffmpeg on your PATH (optional - falls back to WAV).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave

API_URL = "https://api.openai.com/v1/audio/speech"

# Where the Kokoro weights live. Download once (see --help) and they work offline.
KOKORO_MODEL = os.environ.get("KOKORO_MODEL", "kokoro.onnx")
KOKORO_VOICES = os.environ.get("KOKORO_VOICES", "voices.bin")

DEFAULT_VOICE = {"openai": "alloy", "kokoro": "af_heart"}

# Where things live, relative to wherever you run the script (your site root).
AUDIO_DIR = os.environ.get("NARRATE_AUDIO_DIR", "../assets/audio")
TEXT_DIR = os.environ.get("NARRATE_TEXT_DIR", "../assets/audio/texts")
NARRATION_SUFFIX = "_narration"


def article_stem(path):
    """The article's base name, with any _narration suffix removed."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.endswith(NARRATION_SUFFIX):
        stem = stem[: -len(NARRATION_SUFFIX)]
    return stem


def audio_path(stem):
    return os.path.join(AUDIO_DIR, stem + ".mp3")


def narration_path(stem):
    return os.path.join(TEXT_DIR, stem + NARRATION_SUFFIX + ".md")


SCRIPT_HEADER = """---
# Narration script generated from: {source}
#
# Edit freely, then regenerate the audio from THIS file:
#     ./narrate.py --audio-only {out}
#
# Blank lines separate paragraphs, and each break becomes a short pause.
# This front matter block is not read aloud.
#
# Worth fixing before you generate:
#   - phrases written for the eye ("as the table below shows")
#   - abbreviations you want spoken in full
#   - place names the voice is likely to mangle - spell them phonetically
---

"""

# Stay well under the per-request limits of every current model.
MAX_CHUNK_CHARS = 3000

DISCLOSURE = (
    "This is an A.I. generated narration of the article {title}. "
    "The voice you are hearing is synthetic, not the author's."
)


# --------------------------------------------------------------------------
# Turning markdown into something worth listening to
# --------------------------------------------------------------------------

def strip_front_matter(text):
    """Remove YAML front matter, returning (body, title_if_found).

    Titles may be quoted and may contain colons, so split on the first colon
    only: `title: 'Project Eclipse: How I Chased...'` must keep its subtitle.
    """
    title = None
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if match:
        for line in match.group(1).splitlines():
            if re.match(r"^title\s*:", line, re.IGNORECASE):
                title = line.split(":", 1)[1].strip().strip("\"'")
        text = text[match.end():]
    return text, title


def strip_liquid(text):
    """Remove Liquid tags.

    Jekyll includes are the big one: a gallery include carries every image
    path and caption as arguments, spread over many lines. Read aloud that
    becomes minutes of file names. `{% ... %}` can span lines, hence DOTALL.
    """
    text = re.sub(r"\{%.*?%\}", "\n\n", text, flags=re.DOTALL)
    text = re.sub(r"\{\{.*?\}\}", "", text, flags=re.DOTALL)
    return text


# Elements where the *contents* should go too, not just the tags.
_HTML_BLOCK = re.compile(
    r"<(script|style|iframe|video|audio|svg|canvas|noscript|form|table)\b.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)

# Embeds with no closing tag.
_HTML_VOID = re.compile(
    r"<(iframe|img|source|embed|object|input|hr)\b[^>]*/?>",
    re.IGNORECASE,
)


def strip_html(text):
    """Drop raw HTML.

    Embedded maps, videos and image tags carry no spoken content, so those
    elements go entirely. For ordinary inline tags the tag goes but the text
    inside stays, since people do wrap prose in <em> or <div>.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = _HTML_BLOCK.sub("\n\n", text)
    text = _HTML_VOID.sub("\n\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return text


def extract_footnotes(text):
    """Pull footnote definitions out of the body.

    Returns (text_without_definitions, [footnote_text, ...]). A definition
    read in place would interrupt mid-paragraph, so they are collected and
    can be spoken at the end instead of being lost.
    """
    notes = []

    def take(match):
        notes.append(" ".join(match.group(2).split()))
        return ""

    text = re.sub(r"^\s*\[\^([^\]]+)\]:\s*(.+?)(?=\n\s*\n|\n\s*\[\^|\Z)",
                  take, text, flags=re.MULTILINE | re.DOTALL)
    text = re.sub(r"\[\^[^\]]+\]", "", text)      # inline markers
    return text, notes


def to_speakable(text, keep_headings=True):
    """Strip markdown down to prose a narrator can actually read aloud."""

    # Liquid first: include arguments can contain characters that would
    # confuse the HTML and markdown passes.
    text = strip_liquid(text)
    text = strip_html(text)

    # Fenced code becomes a spoken placeholder rather than gibberish.
    text = re.sub(
        r"```[^\n]*\n.*?```",
        "\n\nA code example follows in the written version of this article.\n\n",
        text,
        flags=re.DOTALL,
    )

    text, _ = extract_footnotes(text)

    # Images: read the alt text if it says something, otherwise drop entirely.
    def image_repl(m):
        alt = m.group(1).strip()
        return f"\n\nImage: {alt}.\n\n" if alt else "\n\n"

    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", image_repl, text)

    # Links: keep the anchor text, drop the URL.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"^\s*\[[^\]]+\]:\s*\S+.*$", "", text, flags=re.MULTILINE)

    # Headings become their own paragraph, so the narrator pauses around them
    # instead of running the section title into the first sentence.
    def heading_repl(m):
        heading = m.group(2).strip()
        if not keep_headings or not heading:
            return "\n\n"
        if heading[-1] not in ".!?:":
            heading += "."
        return f"\n\n{heading}\n\n"

    text = re.sub(r"^(#{1,6})\s+(.*)$", heading_repl, text, flags=re.MULTILINE)

    # Blockquotes and list items each become their own paragraph, with
    # terminal punctuation, so the narrator pauses instead of running them
    # together into one breathless sentence.
    def standalone(m):
        line = m.group(1).strip()
        if line and line[-1] not in ".!?:;,":
            line += "."
        return f"\n\n{line}\n\n"

    text = re.sub(r"^\s*>\s?(.*)$", standalone, text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+(.*)$", standalone, text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+(.*)$", standalone, text, flags=re.MULTILINE)

    # Horizontal rules.
    text = re.sub(r"^\s*([-*_]\s*){3,}$", "", text, flags=re.MULTILINE)

    # Inline code, bold, italics, strikethrough.
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)

    # Collapse the blank lines all of this has left behind.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def read_script(text):
    """Take an edited transcript as-is.

    Only front matter and HTML comments are removed, so the file can carry
    editing notes without them being spoken. Nothing else is touched: what
    you typed is what gets read.
    """
    body, _ = strip_front_matter(text)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def build_narration(markdown_text, title=None, disclosure=True,
                    read_title=True, footnotes=False):
    """Markdown in, finished narration script out.

    Footnotes are skipped by default: a marker like [^1] is silent in speech,
    so a note read at the end arrives with no indication of what it refers to.
    Pass footnotes=True to append them under a "Notes" heading instead.

    Assembled in one place so the CLI, the GUI and the comparison tool can
    never drift apart.
    """
    body, front_title = strip_front_matter(markdown_text)
    name = title or front_title

    # Many posts repeat the title as an H1 at the top of the body. Reading the
    # front-matter title and then that heading says it twice.
    if name:
        def normalise(value):
            return re.sub(r"[^a-z0-9]+", "", value.lower())

        def drop_duplicate(match):
            return "" if normalise(match.group(1)) == normalise(name) else match.group(0)

        body = re.sub(r"\A\s*#\s+(.*)$", drop_duplicate, body,
                      count=1, flags=re.MULTILINE)

    _, notes = extract_footnotes(strip_html(strip_liquid(body)))
    script = to_speakable(body)

    opening = []
    if disclosure:
        opening.append(DISCLOSURE.format(
            title=f"'{name}'" if name else "that follows"))
    if read_title and name:
        spoken = name if name[-1] in ".!?" else name + "."
        opening.append(spoken)

    if footnotes and notes:
        script += "\n\nNotes.\n\n" + "\n\n".join(
            n if n[-1:] in ".!?" else n + "." for n in notes)

    return "\n\n".join(opening + [script]) if opening else script


def chunk(text, limit=MAX_CHUNK_CHARS):
    """Split on paragraph breaks so the seams land in natural pauses."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""

    for para in paragraphs:
        # A single paragraph longer than the limit gets split on sentences.
        if len(para) > limit:
            if current:
                chunks.append(current)
                current = ""
            for sentence in re.split(r"(?<=[.!?])\s+", para):
                if len(current) + len(sentence) + 1 > limit:
                    if current:
                        chunks.append(current)
                    current = sentence
                else:
                    current = f"{current} {sentence}".strip()
            continue

        if len(current) + len(para) + 2 > limit:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}".strip()

    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# Talking to the API
# --------------------------------------------------------------------------

def synthesize(text, api_key, model, voice, instructions=None):
    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    # `instructions` steers delivery, and is ignored by the older tts-1 models.
    if instructions and model.startswith("gpt-"):
        payload["instructions"] = instructions

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        sys.exit(f"API returned {err.code}: {detail}")


_kokoro = None


def kokoro_model():
    """Load the Kokoro model once and reuse it across chunks."""
    global _kokoro
    if _kokoro is None:
        try:
            from kokoro_onnx import Kokoro
        except ImportError:
            sys.exit("Kokoro not installed. Run:\n"
                     "  pip install kokoro-onnx soundfile")
        for path in (KOKORO_MODEL, KOKORO_VOICES):
            if not os.path.exists(path):
                sys.exit(f"Missing {path}. Download the weights once:\n"
                         "  curl -L -o kokoro.onnx https://github.com/thewh1teagle/"
                         "kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx\n"
                         "  curl -L -o voices.bin https://github.com/thewh1teagle/"
                         "kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin")
        print("  loading Kokoro model...", file=sys.stderr)
        _kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
    return _kokoro


def synthesize_kokoro(text, voice, speed, lang):
    """Return (samples, sample_rate) for one chunk."""
    return kokoro_model().create(text, voice=voice, speed=speed, lang=lang)


def write_wav(chunks, sample_rate, destination):
    """Join float samples into one 16-bit WAV, with a beat between chunks."""
    import numpy as np

    gap = np.zeros(int(sample_rate * 0.35), dtype=np.float32)
    joined = []
    for index, samples in enumerate(chunks):
        if index:
            joined.append(gap)
        joined.append(np.asarray(samples, dtype=np.float32))
    audio = np.concatenate(joined)

    peak = float(np.max(np.abs(audio))) or 1.0
    audio = (audio / peak * 0.95 * 32767).astype("<i2")

    with wave.open(destination, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(audio.tobytes())


def load_dotenv(*extra_dirs):
    """Load KEY=VALUE pairs from a .env file into the environment.

    Looks next to this script and in the current directory. A variable already
    set in the real environment always wins, so an explicit
    `export OPENAI_API_KEY=...` still overrides the file.

    Deliberately dependency-free: `pip install python-dotenv` is a lot of
    ceremony for reading one line out of one file.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    seen = set()
    loaded = []

    for directory in [here, os.getcwd(), *extra_dirs]:
        path = os.path.join(directory, ".env")
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(path):
            continue
        seen.add(real)

        try:
            with open(path, encoding="utf-8-sig") as handle:
                lines = handle.readlines()
        except OSError:
            continue

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]

            # Real environment beats the file.
            if key and key not in os.environ:
                os.environ[key] = value

        loaded.append(path)

    return loaded


def warn_if_env_tracked(paths):
    """Say something if a .env holding secrets isn't ignored by git."""
    for path in paths:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        try:
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", os.path.basename(path)],
                cwd=directory, capture_output=True,
            ).returncode == 0
            in_repo = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=directory, capture_output=True,
            ).returncode == 0
        except (OSError, FileNotFoundError):
            continue

        if in_repo and not ignored:
            print(f"  warning: {path} is inside a git repo and NOT gitignored.",
                  file=sys.stderr)
            print(f"           echo '.env' >> .gitignore", file=sys.stderr)


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def save_audio(wav_path, destination, bitrate="64k"):
    """Write the finished audio, as MP3 if ffmpeg exists and WAV if it doesn't.

    Returns the path actually written, which may differ from `destination`.
    Kokoro produces WAV natively, so a missing ffmpeg is an inconvenience
    (bigger files) rather than a reason to throw the generated audio away.
    """
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)

    if not have_ffmpeg():
        fallback = os.path.splitext(destination)[0] + ".wav"
        shutil.copyfile(wav_path, fallback)
        return fallback

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
         "-codec:a", "libmp3lame", "-b:a", bitrate, "-ac", "1", destination],
        check=True,
    )
    return destination


# Kept for backwards compatibility with earlier copies of the scripts.
wav_to_mp3 = save_audio


def stitch(parts, destination):
    """Concatenate the MP3 chunks into one file, re-encoding nothing."""
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)

    # A single chunk is already a complete MP3 from the API - no tool needed.
    if len(parts) == 1:
        with open(destination, "wb") as handle:
            handle.write(parts[0])
        return destination

    if not have_ffmpeg():
        sys.exit("This article needs joining into one file, which requires "
                 "ffmpeg.\n  macOS:  brew install ffmpeg\n"
                 "  Debian: sudo apt install ffmpeg")

    with tempfile.TemporaryDirectory() as workdir:
        listing = os.path.join(workdir, "parts.txt")
        with open(listing, "w") as handle:
            for index, data in enumerate(parts):
                path = os.path.join(workdir, f"{index:03d}.mp3")
                with open(path, "wb") as chunk_file:
                    chunk_file.write(data)
                handle.write(f"file '{path}'\n")

        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", listing,
             "-c", "copy", destination],
            check=True,
        )
    return destination


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?",
                        help="path to the markdown article")
    parser.add_argument("--audio-only", metavar="NARRATION_MD",
                        help="regenerate audio from an edited "
                             "<article>_narration.md, changing nothing else")
    parser.add_argument("-o", "--output",
                        help=f"destination MP3 (default: {AUDIO_DIR}/<article>.mp3)")
    parser.add_argument("--engine", default="kokoro", choices=["kokoro", "openai"],
                        help="kokoro = free and local (default); openai = paid API")
    parser.add_argument("--voice", default=None,
                        help="openai: alloy, echo, fable, nova, onyx, shimmer... | "
                             "kokoro: af_heart, af_bella, am_michael, bf_emma, bm_george...")
    parser.add_argument("--speed", type=float, default=1.0, help="kokoro only")
    parser.add_argument("--lang", default="en-us", help="kokoro only")
    parser.add_argument("--model", default="gpt-4o-mini-tts")
    parser.add_argument("--instructions",
                        default="Read as an unhurried, warm blog narration. "
                                "Pause at paragraph breaks. Do not sound like an advertisement.",
                        help="delivery direction (gpt-* models only)")
    parser.add_argument("--title", help="title used in the spoken disclosure")
    parser.add_argument("--no-disclosure", action="store_true",
                        help="omit the spoken AI-narration notice")
    parser.add_argument("--no-title", action="store_true",
                        help="don't read the article title at the start")
    parser.add_argument("--footnotes", action="store_true",
                        help="read footnotes at the end (skipped by default)")
    parser.add_argument("--script-only", action="store_true",
                        help="write the narration text and stop, without "
                             "generating audio")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing narration file "
                             "(your edits would be lost)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the narration script and cost estimate, call nothing")
    args = parser.parse_args()

    env_files = load_dotenv()
    if env_files and args.engine == "openai":
        warn_if_env_tracked(env_files)

    if args.voice is None:
        args.voice = DEFAULT_VOICE[args.engine]

    if bool(args.source) == bool(args.audio_only):
        sys.exit("Give either an article (narrate.py posts/my-post.md) or "
                 "--audio-only <article_narration.md>, not both.")

    source = args.audio_only or args.source
    stem = article_stem(source)

    with open(source, encoding="utf-8") as handle:
        raw = handle.read()

    if args.audio_only:
        # An edited transcript is authoritative: read it verbatim.
        script = read_script(raw)
    else:
        script = build_narration(
            raw,
            title=args.title,
            disclosure=not args.no_disclosure,
            read_title=not args.no_title,
            footnotes=args.footnotes,
        )

        text_out = narration_path(stem)
        paragraphs = len([p for p in script.split("\n\n") if p.strip()])

        if os.path.exists(text_out) and not args.force:
            # Never silently clobber pronunciation fixes someone typed by hand.
            print(f"{text_out} already exists - leaving it alone.")
            print("  To regenerate audio from your edited version:")
            print(f"    ./narrate.py --audio-only {text_out}")
            print("  To rebuild it from the article and discard your edits:")
            print(f"    ./narrate.py {source} --force")
            return

        os.makedirs(TEXT_DIR, exist_ok=True)
        with open(text_out, "w", encoding="utf-8") as handle:
            handle.write(SCRIPT_HEADER.format(source=source, out=text_out))
            handle.write(script + "\n")

        print(f"Wrote {text_out}")
        print(f"  {len(script):,} characters, {paragraphs} paragraphs, "
              f"~{len(script) / 900:.1f} min of audio")

        if args.script_only:
            print("\nEdit it, then:")
            print(f"  ./narrate.py --audio-only {text_out}")
            return

    pieces = chunk(script)
    characters = len(script)
    minutes = characters / 900  # ~150 words per minute of speech

    if args.dry_run:
        print(script)
        print("\n" + "-" * 60)
        print(f"{characters:,} characters, {len(pieces)} chunk(s), "
              f"~{minutes:.1f} min of audio")
        if args.engine == "kokoro":
            print(f"engine: kokoro (free, local) - expect roughly "
                  f"{minutes * 0.8:.0f}-{minutes * 1.5:.0f} min to generate")
        else:
            print(f"engine: openai - roughly ${characters / 1000 * 0.02:.2f}")
        return

    destination = args.output or audio_path(stem)
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)

    # Say this BEFORE spending minutes of CPU or cents of credit.
    if not have_ffmpeg():
        if args.engine == "openai" and len(pieces) > 1:
            sys.exit(f"This article splits into {len(pieces)} chunks, and "
                     "joining them needs ffmpeg.\n"
                     "  macOS:  brew install ffmpeg\n"
                     "  Debian: sudo apt install ffmpeg")
        print("  note: ffmpeg not found, writing WAV instead of MP3.",
              file=sys.stderr)
        print("        install it for smaller files: brew install ffmpeg",
              file=sys.stderr)

    if args.engine == "kokoro":
        chunks, sample_rate = [], 24000
        for index, piece in enumerate(pieces, start=1):
            print(f"  synthesizing {index}/{len(pieces)}...", file=sys.stderr)
            samples, sample_rate = synthesize_kokoro(
                piece, args.voice, args.speed, args.lang)
            chunks.append(samples)

        with tempfile.TemporaryDirectory() as workdir:
            wav_path = os.path.join(workdir, "joined.wav")
            write_wav(chunks, sample_rate, wav_path)
            destination = save_audio(wav_path, destination)
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            sys.exit(
                "OPENAI_API_KEY not found.\n"
                "  Put it in a .env file next to this script:\n"
                "    OPENAI_API_KEY=sk-...\n"
                "  or export it:  export OPENAI_API_KEY=sk-...\n"
                "  or use the free local engine: --engine kokoro")

        audio = []
        for index, piece in enumerate(pieces, start=1):
            print(f"  synthesizing {index}/{len(pieces)}...", file=sys.stderr)
            audio.append(synthesize(piece, api_key, args.model,
                                    args.voice, args.instructions))
        destination = stitch(audio, destination)
    size_mb = os.path.getsize(destination) / 1_000_000
    print(f"Wrote {destination} ({size_mb:.1f} MB, ~{minutes:.0f} min)")


if __name__ == "__main__":
    main()
