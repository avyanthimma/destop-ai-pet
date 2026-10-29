"""
agent.py - Ollama communication layer for Opus.

Public API
    load_settings()            -> Settings    (reads STACKY_* environment variables)
    check_ollama(settings)     -> (bool, str) (blocking health probe, GET /api/tags)
    compose_system_prompt()    personality + shared rules + live extras (mood, offline, ...)
    Tool, ToolBox              tools the model may call (web search, notes, clipboard, ...)
    Conversation               bounded, thread-safe chat history
    HealthCheck(QObject)       emits result(ok, message) from a daemon thread
    AskRequest(QObject)        streams POST /api/chat with tool calls; emits token / mood / completed / failed / cancelled
    ModelTask(QObject)         one non-streamed question for background jobs; emits done / failed

Threading model
    The workers are QObjects created on the GUI thread. start() spawns a daemon thread
    that does blocking network I/O and emits Qt signals. Slots that live on GUI-thread
    QObjects receive those emissions as queued connections, so widgets are only ever
    touched on the GUI thread.

    Connect these signals to methods of GUI-thread QObjects, so delivery is clearly queued
    to the GUI thread. Tool handlers run on the worker thread, so they must be thread-safe.

Offline mode
    Every request asks companion.network first. While offline, only localhost (the
    local Ollama server) can be contacted, and tools that need the internet are hidden.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable

import requests
from PyQt6.QtCore import QObject, pyqtSignal

from companion import OfflineError, network

log = logging.getLogger("stacky.agent")

# Mood tags the model is asked to emit. The tag is stripped from the text before it is shown.
MOODS = ("idle", "happy", "idea", "think", "error")

DEFAULT_PERSONA = (
    "You are Opus, a small robot desktop companion living on the user's Windows desktop. "
    "Be friendly and genuinely helpful. Answer the question first, then give the facts, example or "
    "reason that makes the answer clear. Explain what a thing is, what it does and where it is used, "
    "so the user learns something. Skip greetings, filler and restating the question."
)

# Rules shared by every personality
BASE_RULES = (
    "Begin every reply with exactly one mood tag in square brackets, chosen from "
    "[idle], [happy], [idea], [think], [error]. Use [error] when something went wrong, "
    "[idea] for suggestions, [think] when you are unsure, and [happy] otherwise. Never mention the tag itself.\n"
    "Length: a simple fact or yes/no gets one to two sentences. An explanation or 'what is' question gets "
    "about four to six sentences, with one concrete example. Stop when the answer is complete; do not pad it, "
    "and do not cut it so short that it only names the topic.\n"
    "When a tool result answers the question, name the source: the site, the file name, or 'your clipboard'.\n"
    "For money, investing, legal or medical questions, give clear general information and the main trade-offs, "
    "but do not pick specific stocks or promise outcomes. Say that limit in one short sentence, not a lecture."
)

# A question gets the tools only when it is about the user's own things, or needs a live lookup or arithmetic.
# Small models call tools on general questions and then answer from an unrelated result, so this is decided in code.
# Speed settings. Keeping the model loaded avoids a reload after each pause, and the reply cap stops
# rambling past a complete answer. Neither changes what the model knows or how it reasons.
KEEP_ALIVE = "30m"
MAX_REPLY_TOKENS = 400

_TOOL_HINTS = re.compile(
    r"\b(my|mine|me|our|i|i'm|i've|i'd|copied|copy|clipboard|pasted|notes?|files?|pdfs?|documents?|projects?|"
    r"timer|remind(er)?s?|minutes?|cpu|memory|ram|disk|battery|computer|pc|laptop|usage|spent|today|yesterday|"
    r"this week|latest|news|recent|current|currently|price|search|look up|lookup|google|calculate|sqrt|"
    r"plus|minus|times|divided|percent)\b|\d\s*[-+*/^×x%]\s*\d|%",
    re.IGNORECASE,
)


def needs_tools(question: str) -> bool:
    """True if the question is about the user's own things or needs a lookup or arithmetic."""
    return bool(_TOOL_HINTS.search(question))

# Default model: Llama 3.2 (3B) is small enough to answer quickly on a CPU and supports tool calls.
# The larger models (qwen3-coder:30b, devstral:24b) were too slow here; set STACKY_MODEL to use one.
DEFAULT_MODEL = "llama3.2"

# Ollama hosted web search. It needs an API key from https://ollama.com (OLLAMA_API_KEY).
WEB_SEARCH_URL = "https://ollama.com/api/web_search"

# How many rounds of tool calls one question may use before the model must answer.
TOOL_ROUNDS = 3

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def compose_system_prompt(persona: str | None = None, extras: list[str] | None = None) -> str:
    """Personality first, then the shared rules, then live context such as mood or offline mode."""
    parts = [(persona or DEFAULT_PERSONA).strip(), BASE_RULES]
    parts += [extra.strip() for extra in (extras or []) if extra and extra.strip()]
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    ollama_url: str
    model: str
    timeout: float        # seconds to wait for the next chunk of a reply
    max_turns: int        # user/assistant exchanges kept in history
    hotkey: str
    temperature: float
    ollama_api_key: str   # enables web search when set; empty means search is off


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_number(name: str, default, cast, minimum, maximum):
    """Read a numeric environment variable, falling back to the default if it is missing or invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %r", name, raw, default)
        return default
    return min(max(value, minimum), maximum)


def load_settings() -> Settings:
    """Build Settings from STACKY_* environment variables, using the documented defaults."""
    return Settings(
        ollama_url=_env_str("STACKY_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        model=_env_str("STACKY_MODEL", DEFAULT_MODEL),
        timeout=_env_number("STACKY_TIMEOUT", 120.0, float, 1.0, 3600.0),
        max_turns=_env_number("STACKY_MAX_TURNS", 16, int, 1, 200),
        hotkey=_env_str("STACKY_HOTKEY", "ctrl+alt+space"),
        temperature=_env_number("STACKY_TEMPERATURE", 0.75, float, 0.0, 2.0),
        ollama_api_key=_env_str("OLLAMA_API_KEY", ""),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class AgentError(Exception):
    """A user-facing error raised by the Ollama layer."""


def _error_from_response(resp: requests.Response) -> str:
    """Pull Ollama's {"error": "..."} message out of a non-200 response, if there is one."""
    message = None
    try:
        message = resp.json().get("error")
    except (ValueError, AttributeError):
        pass
    return str(message) if message else f"Ollama returned HTTP {resp.status_code}."


def model_available(wanted: str, installed: list[str]) -> bool:
    """True if `wanted` is installed. Untagged names match their ':latest' tag."""
    names = {name for name in installed if name}
    if wanted in names:
        return True
    return ":" not in wanted and f"{wanted}:latest" in names


def web_search(settings: Settings, query: str, max_results: int = 5) -> str:
    """
    Run a web search through Ollama's hosted search API.

    Returns plain text the model can read. Failures come back as text too, so the
    model can say what went wrong instead of the request failing.
    """
    if not settings.ollama_api_key:
        return "Web search is off because no OLLAMA_API_KEY is set. Answer from what you know and say so."
    if not network.allows(WEB_SEARCH_URL):
        return "Web search is off because offline mode is on. Answer from what you know and say so."
    try:
        resp = requests.post(
            WEB_SEARCH_URL,
            json={"query": query, "max_results": max_results},
            headers={"Authorization": f"Bearer {settings.ollama_api_key}"},
            timeout=(5.0, 15.0),
        )
    except requests.RequestException as exc:
        return f"Web search failed: {exc}"
    if resp.status_code != 200:
        return f"Web search failed: HTTP {resp.status_code}."
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return "Web search returned an unreadable response."
    if not results:
        return "No results found."
    blocks = []
    for number, item in enumerate(results[:max_results], 1):
        blocks.append(f"[{number}] {item.get('title', '')}\n{item.get('url', '')}\n{item.get('content', '')}")
    return "\n\n".join(blocks)


def check_ollama(settings: Settings) -> tuple[bool, str]:
    """Blocking health probe: is the server up, and is the configured model pulled?"""
    url = f"{settings.ollama_url}/api/tags"
    if not network.allows(url):
        return False, f"Offline mode blocks the Ollama server at {settings.ollama_url}. Use a local server or go online."
    try:
        resp = requests.get(url, timeout=(3.0, 5.0))
    except requests.ConnectionError:
        return False, f"Can't reach Ollama at {settings.ollama_url}. Is it running?"
    except requests.Timeout:
        return False, "Ollama did not answer in time."
    except requests.RequestException as exc:
        return False, f"Health check failed: {exc}"

    if resp.status_code != 200:
        return False, _error_from_response(resp)

    try:
        installed = [m.get("name", "") for m in resp.json().get("models", [])]
    except (ValueError, AttributeError):
        return False, "Unexpected response from Ollama."

    if not model_available(settings.model, installed):
        return False, f"Model '{settings.model}' is not installed. Run: ollama pull {settings.model}"
    return True, f"Connected. Model '{settings.model}' is ready."


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                     # JSON schema of the arguments
    handler: Callable[[dict], str]       # runs on the request's worker thread
    needs_network: bool = False          # hidden while offline


class ToolBox:
    """The tools the model may call during a chat."""

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict]:
        """Tool definitions for /api/chat. Network tools are left out while offline."""
        return [
            {"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in self._tools.values()
            if not (t.needs_network and network.offline)
        ]

    def run(self, name: str, arguments: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}"
        if tool.needs_network and network.offline:
            return f"The {name} tool needs the internet, and offline mode is on."
        try:
            return str(tool.handler(arguments))[:8000]
        except Exception as exc:  # noqa: BLE001 - a failing tool must not end the chat
            log.exception("Tool %s failed", name)
            return f"The {name} tool failed: {exc}"


def schema(required: dict[str, str] | None = None, optional: dict[str, tuple[str, str]] | None = None) -> dict:
    """
    JSON schema helper. `required` maps name -> description (strings);
    `optional` maps name -> (type, description).
    """
    properties = {name: {"type": "string", "description": desc} for name, desc in (required or {}).items()}
    for name, (kind, desc) in (optional or {}).items():
        properties[name] = {"type": kind, "description": desc}
    return {"type": "object", "properties": properties, "required": list((required or {}).keys())}


def web_search_tool(settings: Settings) -> Tool:
    return Tool(
        name="web_search",
        description="Search the web and return the top results with titles, URLs and snippets.",
        parameters=schema({"query": "What to search for"}),
        handler=lambda args: web_search(settings, str(args.get("query", ""))),
        needs_network=True,
    )


# --------------------------------------------------------------------------- #
# Conversation history
# --------------------------------------------------------------------------- #
class Conversation:
    """
    Bounded, thread-safe chat history.

    Only completed exchanges are stored. A cancelled or failed request leaves no
    trace, so the next question does not inherit half-written answers.
    """

    def __init__(self, max_turns: int):
        self._max_messages = max(1, max_turns) * 2
        self._messages: list[dict] = []
        self._lock = threading.Lock()

    def snapshot(self, prompt: str, system_prompt: str | None = None) -> list[dict]:
        """Messages to send for a new prompt: system prompt, history, then the prompt."""
        with self._lock:
            history = list(self._messages)
        return [
            {"role": "system", "content": system_prompt or compose_system_prompt()},
            *history,
            {"role": "user", "content": prompt},
        ]

    def commit(self, prompt: str, reply: str) -> None:
        with self._lock:
            self._messages.append({"role": "user", "content": prompt})
            self._messages.append({"role": "assistant", "content": reply})
            if len(self._messages) > self._max_messages:
                del self._messages[: -self._max_messages]

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)


# --------------------------------------------------------------------------- #
# Health check worker
# --------------------------------------------------------------------------- #
class HealthCheck(QObject):
    """Runs check_ollama() on a daemon thread and emits the outcome."""

    result = pyqtSignal(bool, str)

    def __init__(self, settings: Settings, parent: QObject | None = None):
        super().__init__(parent)
        self._settings = settings

    def start(self) -> None:
        threading.Thread(target=self._run, name="stacky-health", daemon=True).start()

    def _run(self) -> None:
        ok, message = check_ollama(self._settings)
        self.result.emit(ok, message)  # last statement: nothing touches self after emitting


# --------------------------------------------------------------------------- #
# Streaming reply parser
# --------------------------------------------------------------------------- #
class _ReplyParser:
    """
    Splits the leading [mood] tag off a streamed reply.

    Text is held back until the parser knows whether the reply starts with a
    tag, then emitted clean. The tag is never shown to the user.
    """

    _MAX_TAG_PROBE = 16  # characters to wait for a closing bracket before giving up

    def __init__(self, on_mood, on_text):
        self._on_mood = on_mood
        self._on_text = on_text
        self._head = ""       # buffered raw text while undecided
        self._settled = False
        self.text = ""        # clean reply accumulated so far

    def feed(self, piece: str) -> None:
        if self._settled:
            self._emit(piece)
            return

        self._head += piece
        stripped = self._head.lstrip()
        if not stripped:
            return
        if stripped[0] != "[":
            self._settle(None, stripped)
            return

        close = stripped.find("]")
        if close == -1:
            if len(stripped) >= self._MAX_TAG_PROBE:
                self._settle(None, stripped)
            return

        tag = stripped[1:close].strip().lower()
        if tag in MOODS:
            self._settle(tag, stripped[close + 1:].lstrip())
        else:
            self._settle(None, stripped)

    def flush(self) -> None:
        """Call once the stream ends. Settles the parser if the reply was very short."""
        if not self._settled:
            self._settle(None, self._head.lstrip())

    def _settle(self, mood: str | None, rest: str) -> None:
        self._settled = True
        self._head = ""
        self._on_mood(mood or "")
        if rest:
            self._emit(rest)

    def _emit(self, piece: str) -> None:
        self.text += piece
        self._on_text(piece)


# --------------------------------------------------------------------------- #
# Chat request worker
# --------------------------------------------------------------------------- #
class AskRequest(QObject):
    """
    One streamed question/answer exchange with Ollama, including any tool calls.

    Signals
        token(str)      each text fragment as it arrives (mood tag removed)
        mood(str)       once per reply, before the first token; '' if no tag was given
        completed(str)  full clean reply; history has already been updated
        failed(str)     user-facing error message
        cancelled()     the request was stopped by cancel()

    Exactly one of completed / failed / cancelled is emitted per request.

    `history_prompt` is what gets stored in the conversation, when it differs from
    the prompt sent (for example a question sent together with note excerpts).
    """

    token = pyqtSignal(str)
    mood = pyqtSignal(str)
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, settings: Settings, conversation: Conversation, prompt: str,
                 parent: QObject | None = None, *, system_prompt: str | None = None,
                 toolbox: ToolBox | None = None, history_prompt: str | None = None):
        super().__init__(parent)
        self._settings = settings
        self._conversation = conversation
        self._prompt = prompt
        self._history_prompt = history_prompt or prompt
        self._system_prompt = system_prompt
        self._toolbox = toolbox
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._response: requests.Response | None = None
        self._started = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def start(self) -> None:
        if self._started:
            raise RuntimeError("AskRequest can only be started once")
        self._started = True
        threading.Thread(target=self._run, name="stacky-ask", daemon=True).start()

    def cancel(self) -> None:
        """Safe to call from any thread, any number of times."""
        self._cancel_event.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()  # unblocks the worker's pending socket read
            except Exception:  # noqa: BLE001 - best effort; the event is what matters
                pass

    # -- worker thread -------------------------------------------------------- #
    def _run(self) -> None:
        try:
            outcome, detail = self._execute()
        except Exception as exc:  # noqa: BLE001 - the GUI must always hear back
            log.exception("Ask request crashed")
            outcome, detail = ("cancelled", "") if self._cancel_event.is_set() else ("failed", f"Something went wrong: {exc}")
        # Emit last. Once a signal lands, the GUI may delete this object.
        if outcome == "completed":
            self.completed.emit(detail)
        elif outcome == "failed":
            self.failed.emit(detail)
        else:
            self.cancelled.emit()

    def _stream_round(self, messages: list[dict], parser: _ReplyParser, tools) -> list[dict]:
        """One streamed request to /api/chat. Returns any tool calls the model asked for in this round."""
        s = self._settings
        url = f"{s.ollama_url}/api/chat"
        network.check(url)
        payload = {
            "model": s.model,
            "messages": messages,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": s.temperature, "num_predict": MAX_REPLY_TOKENS},
        }
        if tools:
            payload["tools"] = tools

        calls: list[dict] = []
        # Connect timeout is 5 s. The read timeout applies to each chunk, so a
        # slow model load does not count as a stall once tokens are flowing.
        with requests.post(url, json=payload, stream=True, timeout=(5.0, s.timeout)) as resp:
            with self._lock:
                self._response = resp
            if self._cancel_event.is_set():
                return []
            if resp.status_code != 200:
                raise AgentError(_error_from_response(resp))

            for line in resp.iter_lines():
                if self._cancel_event.is_set():
                    break
                if not line:
                    continue
                chunk = json.loads(line)  # ValueError on malformed JSON
                if "error" in chunk:
                    raise AgentError(str(chunk["error"]))
                message = chunk.get("message") or {}
                piece = message.get("content") or ""
                if piece:
                    parser.feed(piece)
                calls.extend(message.get("tool_calls") or [])
                if chunk.get("done"):
                    break
        return calls

    def _run_tool(self, call: dict) -> str:
        """Carry out one tool call and return its result as text for the model."""
        function = call.get("function") or {}
        name = function.get("name", "")
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        if self._toolbox is None:
            return f"Unknown tool: {name}"
        return self._toolbox.run(name, arguments)

    def _execute(self) -> tuple[str, str]:
        s = self._settings
        messages = self._conversation.snapshot(self._prompt, self._system_prompt)
        tools = (self._toolbox.definitions() if self._toolbox and needs_tools(self._prompt) else None) or None
        parser = _ReplyParser(on_mood=self.mood.emit, on_text=self.token.emit)

        try:
            for round_no in range(TOOL_ROUNDS + 1):
                # The last round is answer-only, so the model cannot keep calling tools forever
                round_tools = tools if round_no < TOOL_ROUNDS else None
                try:
                    calls = self._stream_round(messages, parser, round_tools)
                except AgentError as exc:
                    if round_tools and "tool" in str(exc).lower():
                        # This model cannot call tools. Carry on as a plain chat model.
                        log.warning("Model %s does not support tools; tools are off", s.model)
                        tools = None
                        calls = self._stream_round(messages, parser, None)
                    else:
                        raise
                if not calls or self._cancel_event.is_set():
                    break
                messages.append({"role": "assistant", "content": "", "tool_calls": calls})
                for call in calls:
                    name = (call.get("function") or {}).get("name", "")
                    messages.append({"role": "tool", "content": self._run_tool(call), "tool_name": name})

        except OfflineError as exc:
            return self._failure(str(exc))
        except AgentError as exc:
            return self._failure(str(exc))
        except requests.Timeout:
            return self._failure(f"No reply from Ollama within {s.timeout:g} seconds.")
        except requests.ConnectionError:
            return self._failure(f"Can't reach Ollama at {s.ollama_url}. Is it running?")
        except requests.RequestException as exc:
            return self._failure(f"Request failed: {exc}")
        except ValueError:
            return self._failure("Ollama sent a malformed response.")
        except Exception as exc:  # noqa: BLE001 - e.g. a read on a socket that cancel() just closed
            if not self._cancel_event.is_set():
                log.exception("Unexpected error while streaming")
            return self._failure(f"Something went wrong: {exc}")
        finally:
            with self._lock:
                self._response = None

        if self._cancel_event.is_set():
            return "cancelled", ""

        parser.flush()
        reply = _THINK_BLOCK.sub("", parser.text).strip()
        if not reply:
            return self._failure("Ollama returned an empty reply.")

        self._conversation.commit(self._history_prompt, reply)
        return "completed", reply

    def _failure(self, message: str) -> tuple[str, str]:
        # A cancel() closes the socket, so the read fails. That is a cancellation, not an error.
        if self._cancel_event.is_set():
            return "cancelled", ""
        log.warning("Ask failed: %s", message)
        return "failed", message


# --------------------------------------------------------------------------- #
# One-shot background task
# --------------------------------------------------------------------------- #
class ModelTask(QObject):
    """
    One non-streamed question with its own system prompt, for background jobs: code
    reviews, failure explanations, clipboard topics and mini-scripts. It does not touch
    the chat history. Emits exactly one of done(text) or failed(message).
    """

    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, settings: Settings, system: str, user: str, parent: QObject | None = None, *,
                 temperature: float = 0.2, json_format: bool = False):
        super().__init__(parent)
        self._settings = settings
        self._system = system
        self._user = user
        self._temperature = temperature
        self._json_format = json_format

    def start(self) -> None:
        threading.Thread(target=self._run, name="stacky-task", daemon=True).start()

    def _call(self) -> str:
        s = self._settings
        url = f"{s.ollama_url}/api/chat"
        network.check(url)
        payload = {
            "model": s.model,
            "messages": [{"role": "system", "content": self._system}, {"role": "user", "content": self._user}],
            "stream": False,
            "options": {"temperature": self._temperature},
        }
        if self._json_format:
            payload["format"] = "json"
        resp = requests.post(url, json=payload, timeout=(5.0, max(s.timeout, 60.0)))
        if resp.status_code != 200:
            raise AgentError(_error_from_response(resp))
        content = ((resp.json().get("message") or {}).get("content") or "")
        return _THINK_BLOCK.sub("", content).strip()

    def _run(self) -> None:
        try:
            text = self._call()
        except (OfflineError, AgentError) as exc:
            self.failed.emit(str(exc))
            return
        except requests.Timeout:
            self.failed.emit("Ollama took too long to answer.")
            return
        except requests.ConnectionError:
            self.failed.emit(f"Can't reach Ollama at {self._settings.ollama_url}.")
            return
        except Exception as exc:  # noqa: BLE001 - the GUI must always hear back
            log.exception("Model task failed")
            self.failed.emit(f"Something went wrong: {exc}")
            return
        self.done.emit(text)
