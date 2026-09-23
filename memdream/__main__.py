"""MemDream's command line.

    python -m memdream prompts ./prompts      copy the starter prompts to edit
    python -m memdream prompts --list         show what each prompt decides

MemDream ships no prompts: every deployment writes its own, because what is
worth remembering depends on the product. That makes "where do I start?" the
first question anyone has, and this is the answer — a set of working prompts,
copied into your project, yours to edit.

They are templates, not defaults. Nothing loads them until you point
`DREAM_PROMPTS_DIR` (or `MemDream(prompts=...)`) at the directory.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

#: Copied verbatim from the repository into the wheel at build time.
TEMPLATE_PACKAGE_DIR = Path(__file__).resolve().parent / "templates" / "prompts"
#: Running from a checkout, where the templates are still at the repo root.
TEMPLATE_REPO_DIR = Path(__file__).resolve().parent.parent / "prompts"


def template_dir() -> Path:
    for candidate in (TEMPLATE_PACKAGE_DIR, TEMPLATE_REPO_DIR):
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    raise SystemExit(
        "Starter prompts are not bundled with this install. Copy them from\n"
        "https://github.com/Mamlesh18/MemDream/tree/main/prompts"
    )


def copy_prompts(target: Path, *, force: bool = False) -> int:
    source = template_dir()
    target.mkdir(parents=True, exist_ok=True)

    from app.core.prompts import PROMPT_NAMES

    # Named rather than globbed, so what lands in the directory is exactly the
    # set MemDream can ask for. The README beside them comes too, as its
    # documentation.
    written, skipped = [], []
    for name in [f"{prompt}.md" for prompt in PROMPT_NAMES] + ["README.md"]:
        path = source / name
        if not path.is_file():
            continue
        destination = target / name
        # Never overwrite an edited prompt: this command is run again whenever
        # someone forgets whether they already have them.
        if destination.exists() and not force:
            skipped.append(name)
            continue
        shutil.copyfile(path, destination)
        written.append(name)

    for name in written:
        print(f"  wrote   {target / name}")
    for name in skipped:
        print(f"  kept    {target / name}  (already there; --force to replace)")

    if written:
        copied = [name for name in written if name != "README.md"]
        print(f"\n{len(copied)} prompt(s) copied. Now point MemDream at them:")
        print(f"    DREAM_PROMPTS_DIR={target}")
        print(f"    MemDream(prompts={str(target)!r})")
        print("\nThey work as they are. Edit them for your product — start with")
        print("extraction.md, which decides what is worth remembering at all.")
    elif skipped:
        print(f"\nNothing to do: {target} already has prompts.")
    return 0


def list_prompts() -> int:
    from app.core.prompts import PROMPT_PURPOSE

    print("MemDream asks for these prompts. Which are required depends on your")
    print("configuration: staged decisions need supersede + merge instead of")
    print("decision, and synthesis and compaction only when enabled.\n")
    for name, purpose in PROMPT_PURPOSE.items():
        print(f"  {name:<11} {purpose}")
    print("\nEach must return the JSON shape MemDream appends to your prompt;")
    print("see docs/prompts.md.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memdream", description=__doc__.split("\n")[0])
    subcommands = parser.add_subparsers(dest="command", required=True)

    prompts = subcommands.add_parser("prompts", help="copy the starter prompts")
    prompts.add_argument("directory", nargs="?", default="./prompts",
                         help="where to copy them (default: ./prompts)")
    prompts.add_argument("--force", action="store_true", help="overwrite existing files")
    prompts.add_argument("--list", action="store_true", dest="show",
                         help="list the prompts and what each decides")

    args = parser.parse_args(argv)
    if args.command == "prompts":
        return list_prompts() if args.show else copy_prompts(Path(args.directory), force=args.force)
    return 1


if __name__ == "__main__":
    sys.exit(main())
