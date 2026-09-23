"""The prompts a deployment supplies.

**MemDream ships no prompts.** The engine knows how to extract, decide,
supersede, merge, synthesise and compact; what to say to the model is yours,
because it is the part that depends on your product. A memory layer for a
restaurant, a bank and a support desk want different things remembered, and a
built-in prompt would be a default nobody could see and everybody inherits.

So prompts are configuration, loaded here, and a deployment that has not
supplied them does not start. The error says exactly which are missing and the
three ways to provide them.

## Where they come from

In order of precedence:

1. ``settings.prompts`` — inline text, keyed by name. From the SDK
   (``MemDream(prompts={"extraction": "..."})``), from `config/dream.yaml`, or
   from ``DREAM_PROMPTS__EXTRACTION=...``.
2. ``settings.prompts_dir`` — a directory of files named after each prompt:
   ``extraction.md``, ``decision.md``, and so on. The **whole file** is the
   prompt; there is no front matter and no comment syntax, because anything in
   the file is sent to the model.

Starter templates live in ``prompts/`` in the repository, and ship inside the
package so ``python -m memdream prompts ./prompts`` can copy them into a project.
They are a starting point, not a default: nothing loads them unless you point
configuration at them.

## Which are required

Only the ones your configuration can actually reach. A deployment with synthesis
disabled is not asked for a synthesis prompt, and one using the unified decision
mode is not asked for the staged pair. That keeps the smallest working setup at
two files.

## The one substitution

``{domain}`` anywhere in a prompt is replaced with ``DREAM_DOMAIN`` as a short
context line, or with nothing when it is unset. It is a plain string replacement
rather than ``str.format`` on purpose — prompts contain JSON examples full of
braces, and formatting them would either fail or mangle the text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Every prompt the engine can ask for, and what it decides.
PROMPT_PURPOSE: dict[str, str] = {
    "extraction": "pull durable facts out of a message",
    "decision": "choose ADD / SUPERSEDE / MERGE / NOOP for one fact (unified mode)",
    "supersede": "decide SUPERSEDE or NOOP (staged mode)",
    "merge": "decide MERGE or NOOP (staged mode)",
    "synthesis": "find a pattern across a cluster of memories",
    "compaction": "fold a crowd of low-value memories into one summary",
}
PROMPT_NAMES: tuple[str, ...] = tuple(PROMPT_PURPOSE)

#: Accepted file extensions, in the order they are tried.
SUFFIXES: tuple[str, ...] = (".md", ".txt", ".prompt")

_DOMAIN_TOKEN = "{domain}"


def required_prompts(settings: Settings) -> tuple[str, ...]:
    """The prompts this configuration can actually reach.

    Asking for a synthesis prompt from a deployment that has synthesis switched
    off would be a startup failure over something that can never run.
    """
    names = ["extraction"]
    if settings.dream.decision_mode == "unified":
        names.append("decision")
    else:
        names += ["supersede", "merge"]
    if settings.dream.synthesis.enabled:
        names.append("synthesis")
    if settings.dream.compaction.enabled:
        names.append("compaction")
    return tuple(names)


@dataclass(frozen=True)
class Prompt:
    """One prompt and where it came from, so `/ready` can show it."""

    name: str
    text: str
    source: str


class PromptLibrary:
    """The prompts in force, resolved once at startup."""

    def __init__(self, prompts: dict[str, Prompt], *, domain: str = "") -> None:
        self._prompts = prompts
        self._domain = (domain or "").strip()

    # --- loading ------------------------------------------------------------ #

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, validate: bool = True
    ) -> PromptLibrary:
        settings = settings or get_settings()
        found: dict[str, Prompt] = {}

        directory = (settings.prompts_dir or "").strip()
        if directory:
            base = Path(directory).expanduser()
            if not base.is_dir():
                raise ConfigurationError(
                    f"DREAM_PROMPTS_DIR points at {base}, which is not a directory. "
                    "Create it and put your prompts in it, or copy the starter set "
                    f"with: python -m memdream prompts {base}",
                    details={"promptsDir": str(base)},
                )
            for name in PROMPT_NAMES:
                for suffix in SUFFIXES:
                    path = base / f"{name}{suffix}"
                    if path.is_file():
                        text = path.read_text(encoding="utf-8").strip()
                        if text:
                            found[name] = Prompt(name, text, str(path))
                        break

        # Inline text wins: it is the more specific instruction, and it is how a
        # single prompt gets overridden without copying the whole directory.
        unknown = set(settings.prompts) - set(PROMPT_NAMES)
        if unknown:
            raise ConfigurationError(
                f"Unknown prompt name(s): {', '.join(sorted(unknown))}. "
                f"Valid names are: {', '.join(PROMPT_NAMES)}",
                details={"unknown": sorted(unknown)},
            )
        for name, text in settings.prompts.items():
            if (text or "").strip():
                found[name] = Prompt(name, text.strip(), "configuration")

        library = cls(found, domain=settings.domain)
        if validate:
            library.validate(required_prompts(settings))
        return library

    # --- use ----------------------------------------------------------------- #

    def get(self, name: str) -> str:
        """The prompt text, with `{domain}` resolved."""
        prompt = self._prompts.get(name)
        if prompt is None:
            raise ConfigurationError(_missing_message([name]), details={"missing": [name]})
        return prompt.text.replace(_DOMAIN_TOKEN, self._domain_block())

    def _domain_block(self) -> str:
        return f"\n\nContext: {self._domain}" if self._domain else ""

    def validate(self, names: tuple[str, ...]) -> None:
        missing = [name for name in names if name not in self._prompts]
        if missing:
            raise ConfigurationError(
                _missing_message(missing), details={"missing": missing}
            )
        logger.info(
            "prompts_loaded",
            prompts={name: self._prompts[name].source for name in names},
        )

    def describe(self) -> dict[str, str]:
        """Prompt name to where it was loaded from. Shown by `/ready`."""
        return {name: prompt.source for name, prompt in sorted(self._prompts.items())}

    def __contains__(self, name: object) -> bool:
        return name in self._prompts


def _missing_message(missing: list[str]) -> str:
    listed = "\n".join(f"  {name} — {PROMPT_PURPOSE[name]}" for name in missing)
    return (
        f"No prompt configured for: {', '.join(missing)}.\n\n"
        "MemDream ships no prompts; each deployment writes its own, because what "
        "is worth remembering depends on your product.\n\n"
        f"{listed}\n\n"
        "Supply them in any of these ways:\n"
        "  1. python -m memdream prompts ./prompts   (copy the starter set, then edit)\n"
        "     and set DREAM_PROMPTS_DIR=./prompts\n"
        "  2. MemDream(prompts='./prompts')  or  MemDream(prompts={'extraction': '...'})\n"
        "  3. DREAM_PROMPTS__EXTRACTION='...' for a single prompt\n\n"
        "See docs/prompts.md for what each prompt must return."
    )
