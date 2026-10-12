# ghosttab

Inline autocomplete, dictation and a selection assistant for Windows, all
running on one small local model through
[llama.cpp](https://github.com/ggml-org/llama.cpp).

Everything stays on the machine: field contents, selected text, screen context
and audio are sent only to the llama-server you run locally.

The features follow Magic Tab, Magic Echo and Magic Beacon in AnythingLLM
Desktop.

## Features

### Autocomplete

While you type in any text field (browsers, Discord, Slack, Notepad, Office,
...), a grey continuation appears after the caret.

| Key                  | Effect                                              |
|----------------------|-----------------------------------------------------|
| `Tab`                | insert the whole suggestion                          |
| `Shift+Tab`          | insert the next word                                 |
| keep typing          | if you type what was suggested, the rest stays shown |
| `Esc`, arrows, Enter, Ctrl/Alt shortcuts, clicking elsewhere | dismiss |

`Tab` is only intercepted while a suggestion is visible. The visible text above
the field (in a chat app, the conversation) is used as context.

### Dictation

Press `Ctrl+Alt+Space` and speak. A small pill under the cursor shows the
microphone level. Recording stops when you press the hotkey again or pause for
1.5 s; `Esc` cancels. The model transcribes the audio itself, a second pass
removes filler words, applies self-corrections ("at ten, actually no, nine"
becomes "at 9") and spoken punctuation, and the text is typed at the cursor. If
focus moved in the meantime, it goes to the clipboard.

### Selection assistant

Select text anywhere and press `Ctrl+Alt+A`, or click the small sparkle button
that appears at the end of a mouse selection. A compact popover opens with a
request box and the most common actions: improve, fix, shorter, formal and a
translation (to English for German text, to German otherwise). `...` reveals
casual, explain, summarize and the other translation; `Ctrl+1` to `Ctrl+9` run
them from the keyboard. The answer streams in; further requests refine it.
`Ctrl+Enter` replaces the selection (when it is editable), `Ctrl+Shift+C`
copies, `Esc` or clicking elsewhere closes. With nothing selected, the answer
is inserted at the cursor.

All three work in the language you write or speak, as far as the model knows
it. German is tested, including umlauts, `ß`, German quotation marks and AltGr
characters.

## Requirements

- Windows 10 or 11
- Python 3.11+
- A microphone for dictation
- A llama.cpp build recent enough for the model. Gemma 4 needs a build from
  late 2026; b8115 fails with `unknown model architecture: 'gemma4'`.
  `start-server.ps1` uses a build unpacked under `vendor\llama-b*\` if present,
  otherwise `llama-server` from `PATH`.

## Install

Optionally unpack a pinned llama.cpp build (Vulkan, CUDA, ... from
https://github.com/ggml-org/llama.cpp/releases) to `vendor\llama-<tag>\`, then:

```powershell
.\install.ps1
```

This creates the virtual environment, adds ghosttab to the Startup folder so it
runs at every login, and starts it now. It runs in the background without a
window: llama-server first (the first start downloads the model), then ghosttab.
Logs are in `%LOCALAPPDATA%\ghosttab`; they record events and timings, not what
you type or say.

```powershell
.\ghosttab.ps1 -Stop     # stop ghosttab and its llama-server
.\ghosttab.ps1           # start again
.\uninstall.ps1          # stop and remove from the Startup folder
```

ghosttab runs with normal user rights, so Windows does not let it see input in
apps running as administrator.

## Running from a terminal

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\start-server.ps1                     # terminal 1
.\.venv\Scripts\python -m ghosttab     # terminal 2; --debug also logs typed and dictated text
```

The default model is `unsloth/gemma-4-E2B-it-GGUF:Q4_K_M` (about 3 GB) plus its
multimodal projector (about 1 GB), which provides the audio input for
dictation. Gemma 4 E4B works the same way; other instruction-tuned models work
for autocomplete and the assistant, and dictation is switched off when the model
has no audio input.

```powershell
.\start-server.ps1 -Model unsloth/gemma-4-E4B-it-GGUF:Q4_K_M
.\start-server.ps1 -NoAudio            # skip the projector, no dictation
```

The server runs two slots: autocomplete keeps one to itself, so dictation and
assistant requests don't evict its cached prompt.

The first requests after a server start can take several seconds while the
Vulkan backend compiles its shaders; ghosttab sends a warm-up request at
startup.

## Configuration

Copy `ghosttab.example.json` to `ghosttab.json`. All keys are listed in
`ghosttab/config.py`. The most useful:

| Key                       | Default            | Meaning                                         |
|---------------------------|--------------------|-------------------------------------------------|
| `debounce_ms`             | 400                | pause after the last keystroke before predicting |
| `max_tokens`              | 48                 | maximum suggestion length                       |
| `screen_context`          | true               | include the visible text above the field        |
| `suggestion_chaining`     | true               | predict again after a full accept                |
| `personalization`         | []                 | standing instructions, e.g. tone or sign-off    |
| `ignored_apps`            | []                 | executable or window-title substrings to skip   |
| `autocomplete`            | true               | enable autocomplete                             |
| `dictation`               | true               | enable dictation                                |
| `dictation_hotkey`        | `ctrl+alt+space`   | start and stop recording                        |
| `dictation_cleanup`       | true               | clean up the transcript (off: raw transcript)   |
| `dictation_vocabulary`    | []                 | names and terms to spell exactly                |
| `dictation_silence_ms`    | 1500               | stop after this long without speech; 0 = only by hotkey |
| `assistant`               | true               | enable the selection assistant                  |
| `assistant_hotkey`        | `ctrl+alt+a`       | open the assistant                              |
| `assistant_selection_button` | true           | show the button after mouse selections          |

Autocomplete and the selection button never run in password fields, terminals,
IDEs, Explorer, the Start menu, Settings, UAC and credential prompts, or
password managers.

## How it works

```
keyboard --(WH_KEYBOARD_LL)--> hotkeys (dictation, assistant), then autocomplete:
                               classify key; swallow Tab only while a suggestion is shown
mouse    --(WH_MOUSE_LL)-----> selection button after a drag or double-click
                                   |
                                   v
            UI Automation: focused field, text around the caret, selection,
            caret and selection geometry, editability, screen context
                                   |
                                   v
 llama-server slot 0: /apply-template (cached) + /completion   (autocomplete)
 llama-server slot 1: /v1/chat/completions, audio input        (dictation, assistant)
                                   |
                                   v
      overlay at the caret, dictation indicator, assistant panel (Tk);
      text is typed with SendInput (line breaks as Shift+Enter)
```

- **Reading the field.** UI Automation `TextPattern` gives the text before and
  after the caret, the selection and the caret rectangle. `ValuePattern` tells
  editable fields from read-only text such as a web page; the Win32 system caret
  is a fallback. When UI Automation exposes no selection, the assistant copies
  it with Ctrl+C and restores the clipboard.
- **Autocomplete prompt.** A system prompt frames the model as a keyboard
  prediction engine. The user turn carries the app and window title, text after
  the caret, personalisation and screen context. The draft is appended after the
  rendered chat template, so the model continues it as its own reply.
- **Screen context.** The text elements above the field and in its column are
  fetched in one cached UIA call, scoped to the web document or window. In a
  chat app that is the message history. Timestamps and reaction counts are
  removed and the last 2,400 characters kept, cached per field for 4 s so the
  prompt prefix stays identical between keystrokes.
- **Latency.** `/completion` runs with `cache_prompt`, so a keystroke only
  evaluates the new tokens. Superseded requests are cancelled by closing the
  stream.
- **Token healing.** A draft ending mid-word tokenises badly. The partial word
  is removed from the prompt and a GBNF grammar forces the output to start with
  it.
- **Dictation.** Audio is recorded at the device rate and resampled to 16 kHz.
  Gemma 4's audio encoder transcribes it; a text-only request with a few
  examples then cleans it up.
- **Overlay.** A Tk window with a chroma-key background sampled from the field,
  so text edges blend correctly. The font size follows the caret height, and
  wrapped lines start at the field's text margin.

Measured on an RTX 4070 with Gemma 4 E2B Q4_K_M:

| Step                                   | Time          |
|----------------------------------------|---------------|
| autocomplete suggestion                | 130-170 ms    |
| extra keystroke (prompt update)        | about 50 ms   |
| dictation, 8-10 s of speech: transcribe | 0.8-1.1 s    |
| dictation clean-up                     | 0.4-0.8 s     |
| assistant quick action (full answer)   | 0.4-1.0 s     |

## Limitations

- Windows only.
- Autocomplete completes only at the end of a line.
- Apps that expose no UI Automation text (some games, custom-drawn editors) get
  no suggestions; dictation and the assistant fall back to the clipboard there.
- The overlay approximates the field's font and line wrapping.
- llama.cpp marks audio input as experimental.

## Testing

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m unittest discover -s tests
.\.venv\Scripts\python tests\pipeline_check.py      # needs the server; no UI
```

`tests/pipeline_check.py` runs dictation on the recordings in `tests/audio`
(generated with Windows text-to-speech, English and German), every assistant
action on English and German text, and checks that the autocomplete cache
survives an assistant request.

`tests/ui_preview.py` renders every popup in each state (suggestion, dictation
pill, selection button, assistant popover) into one contact sheet, without
taking focus.

`tests/uia_probe.py` checks the UI Automation reader against a RichEdit window
(`--edit` for a classic EDIT control).

`tests/live_drive.py` is an end-to-end test. With ghosttab running under
`--debug` and its output in a log file, it types into a test window like a
person and checks the results. It refuses to start if the PC was used in the
last 15 seconds, and aborts if the expected window loses focus.

```powershell
.\.venv\Scripts\python -m ghosttab --debug > ghosttab.log 2>&1
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target richedit
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target chromium
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target chat
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target german
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target assistant

# dictation reads tests/audio/de_dictation.wav instead of the microphone:
.\.venv\Scripts\python -m ghosttab --debug --config tests\live_dictation.json > ghosttab.log 2>&1
.\.venv\Scripts\python tests\live_drive.py --log ghosttab.log --target dictation
```

The browser targets use Brave with a temporary profile that is deleted
afterwards.
