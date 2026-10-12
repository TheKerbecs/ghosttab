# ghosttab

System-wide inline autocomplete for Windows, backed by a local
[llama.cpp](https://github.com/ggml-org/llama.cpp) server.

While you type in any text field (browsers, Discord, Slack, Notepad, Office,
...), a grey continuation appears after the caret.

| Key                  | Effect                                              |
|----------------------|-----------------------------------------------------|
| `Tab`                | insert the whole suggestion                          |
| `Shift+Tab`          | insert the next word                                 |
| keep typing          | if you type what was suggested, the rest stays shown |
| `Esc`, arrows, Enter, Ctrl/Alt shortcuts, clicking elsewhere | dismiss |

`Tab` is only intercepted while a suggestion is visible.

Nothing leaves the machine: the field contents and the visible text around it
are sent only to the llama-server you run locally.

The idea comes from the Magic Tab feature in AnythingLLM Desktop.

## Requirements

- Windows 10 or 11
- Python 3.11+
- A llama.cpp build recent enough for the model you use. Gemma 4 needs a build
  from late 2026; b8115 fails with `unknown model architecture: 'gemma4'`.
  `start-server.ps1` uses a build unpacked under `vendor\llama-b*\` if present,
  otherwise `llama-server` from `PATH`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# Optional: a pinned llama.cpp build (Vulkan shown; pick the archive for your GPU)
# from https://github.com/ggml-org/llama.cpp/releases, unpacked to vendor\llama-<tag>\
```

## Usage

```powershell
.\start-server.ps1                     # terminal 1: downloads the model on first run
.\.venv\Scripts\python -m ghosttab     # terminal 2; add --debug for a trace
```

The default model is `unsloth/gemma-4-E2B-it-GGUF:Q4_K_M` (about 3 GB). Any
instruction-tuned GGUF works:

```powershell
.\start-server.ps1 -Model unsloth/gemma-4-E4B-it-GGUF:Q4_K_M
```

The first requests after a server start can take several seconds while the
Vulkan backend compiles its shaders; ghosttab sends a warm-up request at
startup.

## Configuration

Copy `ghosttab.example.json` to `ghosttab.json`. All keys are listed in
`ghosttab/config.py`. The most useful:

| Key                   | Default | Meaning                                             |
|-----------------------|---------|-----------------------------------------------------|
| `debounce_ms`         | 400     | pause after the last keystroke before predicting    |
| `max_tokens`          | 48      | maximum suggestion length                           |
| `screen_context`      | true    | include the visible text above the field            |
| `suggestion_chaining` | true    | predict again after a full accept                    |
| `personalization`     | []      | standing instructions, e.g. tone or sign-off        |
| `ignored_apps`        | []      | executable or window-title substrings to skip       |

ghosttab never runs in password fields, terminals, IDEs, Explorer, the Start
menu, Settings, UAC and credential prompts, or password managers.

## How it works

```
keyboard --(WH_KEYBOARD_LL)--> hook thread: classify key; swallow Tab only
                                            while a suggestion is shown
                                   |
                                   v
                     engine thread (UI Automation, state machine)
   idle -> debouncing -> generating -> ready -> Tab -> SendInput
                                   |
                                   v
     llama-server: /apply-template (cached) + /completion (streamed)
                                   |
                                   v
     overlay window at the caret (click-through, never takes focus)
```

- **Reading the field.** UI Automation `TextPattern` gives the text before and
  after the caret and the caret rectangle. `ValuePattern` and the Win32 system
  caret are fallbacks.
- **Prompt.** A system prompt frames the model as a keyboard prediction engine.
  The user turn carries the app and window title, text after the caret,
  personalisation and screen context. The draft is appended after the rendered
  chat template, so the model continues it as its own reply.
- **Screen context.** The text elements above the field and in its column are
  fetched in one cached UIA call, scoped to the web document or window. In a
  chat app that is the message history. Timestamps and reaction counts are
  removed and the last 2,400 characters kept. The result is cached per field
  for 4 s, so the prompt prefix stays identical between keystrokes.
- **Latency.** `/completion` runs with `cache_prompt` on a single slot, so a
  keystroke only evaluates the new tokens. Superseded requests are cancelled by
  closing the stream.
- **Token healing.** A draft ending mid-word tokenises badly. The partial word
  is removed from the prompt and a GBNF grammar forces the output to start with
  it.
- **Overlay.** A Tk window with a chroma-key background sampled from the field,
  so text edges blend correctly. The font size follows the caret height, and
  wrapped lines start at the field's text margin.

On an RTX 4070 with Gemma 4 E2B Q4_K_M, a suggestion takes 130-170 ms to
generate, and an extra keystroke adds about 50 ms of prompt processing.

## Limitations

- Windows only.
- Completes only at the end of a line.
- Apps that expose no UI Automation text (some games, custom-drawn editors) get
  no suggestions.
- The overlay approximates the field's font and line wrapping.

## Testing

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m unittest discover -s tests
```

`tests/uia_probe.py` checks the UI Automation reader against a RichEdit window
(`--edit` for a classic EDIT control).

`tests/live_drive.py` is an end-to-end test. With ghosttab running under
`--debug` and its output in a log file, it types into a test window and checks
suggestion, accept, partial accept, type-ahead, dismissal and Tab pass-through.
It refuses to start if the PC was used in the last 15 seconds, and aborts if
the test window loses focus.

```powershell
.\.venv\Scripts\python -m ghosttab --debug > ghosttab.log 2>&1
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target richedit
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target chromium
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target chat
```

The browser targets use Brave with a temporary profile that is deleted
afterwards.
