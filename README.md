# Opus

A small always-on-top AI desktop pet for Windows 11, powered by a local Ollama model.

## Setup

1. Install Python 3.12+ from python.org (tick "Add python.exe to PATH").
2. Install Ollama from https://ollama.com, start it, then pull the model once:
   ```powershell
   ollama pull llama3.2
   ```
3. Install the Python packages:
   ```powershell
   pip install -r requirements.txt
   ```
4. Start Opus:
   ```powershell
   python app.py        # or double-click app.pyw to run without a console window
   ```

## Optional settings (environment variables)

```powershell
$env:STACKY_MODEL = "qwen3-coder:30b"      # another installed model
$env:OLLAMA_API_KEY = "..."                # turns on web search
$env:STACKY_HOTKEY = "ctrl+alt+space"      # open the prompt box
$env:STACKY_MOVE_HOTKEY = "ctrl+alt+m"     # move mode
$env:STACKY_DUCK_HOTKEY = "ctrl+alt+d"     # rubber duck mode
```

## My notes (search your own files)

Right-click Opus > **My notes** > **Add a notes folder…** and pick a folder of notes, PDFs or old projects.
Opus indexes it (use **Rebuild the index** after changes), then ask with `/notes <question>`.

- Plain text, Markdown and code files are indexed directly.
- PDFs need `pypdf`, which is listed in `requirements.txt`. Without it, PDFs are skipped and Opus tells you so.

## Command-line runner

`opus_run.py` runs any command and reports the result to the pet:

```powershell
python opus_run.py npm run build
python opus_run.py -- make all
```

The exit code is passed through, so scripts and CI see the same result.
