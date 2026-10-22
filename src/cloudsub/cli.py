"""A small synchronous CLI; private remote names are hidden by default."""

import argparse
from contextlib import ExitStack
import hashlib
import logging
from pathlib import Path, PurePosixPath
import sys
import tempfile

from .config import Config, load_config
from .sources import CloudSubError, HttpSource, RemoteFile, WebDAVSource
from .subtitles import Transcriber, Translator, read_srt, write_srt


MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}


def task_id(config: Config, remote: RemoteFile) -> str:
    identity = f"{config.source.type}\0{config.source.url}\0{config.source.username}\0{remote.path}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def open_source(config: Config):
    source = config.source
    if source.type == "http" and (source.folder or source.recursive):
        raise CloudSubError("HTTP is a single media URL; remove folder/recursive settings.")
    implementation = WebDAVSource if source.type == "webdav" else HttpSource
    return implementation(source.url, source.username, source.password, source.allow_http)


def media_files(config: Config, source):
    if config.source.type == "http":
        yield RemoteFile("")
    else:
        for remote in source.files(config.source.folder, config.source.recursive):
            if PurePosixPath(remote.path).suffix.lower() in MEDIA_EXTENSIONS:
                yield remote


def run(config: Config, args) -> int:
    if args.upload and config.source.type != "webdav":
        raise CloudSubError("HTTP sources cannot upload subtitles; use WebDAV or local output.")
    if args.limit is not None and args.limit < 1:
        raise CloudSubError("Limit must be a positive integer.")
    if args.id and (len(args.id) != 24 or any(c not in "0123456789abcdef" for c in args.id)):
        raise CloudSubError("Choose a task ID from cloudsub list.")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(".local/work")
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    transcriber = Transcriber(config.transcription, Path(".local/models"))
    completed = failed = processed = matched = 0
    with ExitStack() as stack:
        source = stack.enter_context(open_source(config))
        translator = None if args.original_only else stack.enter_context(Translator(config.translation))
        if translator is not None and translator.remote:
            print("Translation uses an external API: subtitle text is sent to your configured provider.", file=sys.stderr)
        for remote in media_files(config, source):
            identity = task_id(config, remote)
            if args.id and identity != args.id:
                continue
            matched += 1
            # --limit counts tasks that need new work, so reruns move past finished ones.
            if args.limit is not None and processed >= args.limit:
                break
            print(f"Processing {identity}", flush=True)
            original = output / f"{identity}.original.srt"
            chinese = output / f"{identity}.zh.srt"
            worked = args.upload
            try:
                if original.is_symlink() or chinese.is_symlink():
                    raise CloudSubError("Subtitle output must not be a symbolic link.")
                if original.exists():
                    segments = read_srt(original)
                else:
                    worked = True
                    with tempfile.TemporaryDirectory(prefix="task-", dir=work) as temporary:
                        media = Path(temporary) / "input.media"
                        source.download(remote.path, media)
                        segments = transcriber.transcribe(media)
                        write_srt(original, segments)
                subtitles = [(original, ".original.srt")]
                if translator is not None:
                    if chinese.exists():
                        read_srt(chinese)
                    else:
                        worked = True
                        write_srt(chinese, translator.translate(segments))
                    subtitles.append((chinese, ".zh.srt"))
                if args.upload:
                    for local, suffix in subtitles:
                        destination = str(PurePosixPath(remote.path).with_suffix(suffix))
                        source.upload(destination, local.read_bytes())
                completed += 1
                print(f"Done {identity}", flush=True)
            except CloudSubError as error:
                failed += 1
                print(f"Failed {identity}: {error}", file=sys.stderr)
            except (OSError, UnicodeError) as error:
                failed += 1
                print(f"Failed {identity}: local file operation failed ({type(error).__name__}).", file=sys.stderr)
            processed += worked
    if matched == 0:
        raise CloudSubError("No matching media files were found.")
    print(f"Completed: {completed}; failed: {failed}.")
    return 1 if failed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cloudsub", description="Private local subtitles for WebDAV/OpenList and HTTP media.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List media task IDs without private filenames.")
    listing.add_argument("--config", type=Path, default=Path("config.local.toml"))
    listing.add_argument("--show-paths", action="store_true", help="Explicitly display private remote paths.")
    running = commands.add_parser("run", help="Generate local subtitles, optionally upload new WebDAV subtitles.")
    running.add_argument("--config", type=Path, default=Path("config.local.toml"))
    running.add_argument("--original-only", action="store_true", help="Skip Chinese translation.")
    running.add_argument("--upload", action="store_true", help="Write new subtitles beside WebDAV media; never delete source media.")
    running.add_argument("--limit", type=int)
    running.add_argument("--id", help="Process only the task ID shown by list.")
    running.add_argument("--output", type=Path, default=Path(".local/subtitles"))
    args = parser.parse_args(argv)
    # Third-party network/model loggers can include signed URLs or subtitle text.
    logging.disable(logging.CRITICAL)
    try:
        config = load_config(args.config)
        if args.command == "run":
            return run(config, args)
        count = 0
        with open_source(config) as source:
            for remote in media_files(config, source):
                size = str(remote.size) if remote.size is not None else "unknown"
                line = f"{task_id(config, remote)}\t{size} bytes"
                if args.show_paths:
                    line += "\t" + remote.path
                print(line)
                count += 1
        print(f"Media files: {count}")
        return 0
    except CloudSubError as error:
        print(f"Error: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("Cancelled; temporary media is cleaned up, completed subtitles are retained.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: operation failed ({type(error).__name__}); private details suppressed.", file=sys.stderr)
    return 1
