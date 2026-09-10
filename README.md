# fullstack-agent

> **No Claude account required.** This edition runs on an OpenAI API key, a Gemini API key, or another OpenAI-compatible endpoint.

**Runs on:** Python 3.11+ and an API key. The included local agent runner operates the installer and the finished typed assistant. Gemini uses Google's official OpenAI-compatible endpoint, so both providers share one implementation.

Not an agent that writes full-stack code. **An agent that HAS a full stack: memory, voice, and face, plus an optional set of hands.** This repo assembles my whole setup on your machine in one guided conversation, and when it finishes, your screen is a living circuit board with your agent's name on the chip, and it speaks first:

> "Hello [you], what are we working on today?"

[![Watch the tour: My Jarvis AI Assistant, free on GitHub](https://img.youtube.com/vi/FiOTrxq9ckM/maxresdefault.jpg)](https://www.youtube.com/watch?v=FiOTrxq9ckM)

**Nine minutes shows you everything you're about to get** (the voice, the face, the memory, and the hands): the tour video above, straight from my own desk.

That's not a demo clip. That's minute one.

## What you get

Four pieces, each its own open repo, each excellent alone, assembled here into one agent:

- **The mind: [ai-memory-vault](https://github.com/jaredrhod/ai-memory-vault).** A real, persistent memory built on plain text files your AI reads and writes. It remembers you, your work, and every lesson, across every session, with no size ceiling.
- **The mouth: [backtalk](https://github.com/jaredrhod/backtalk).** Hold a key, talk out loud, and your agent answers through your speakers about a second later, with all its tools and its whole personality.
- **The face: [ai-visualizer](https://github.com/jaredrhod/ai-visualizer).** Full-screen visualizers that idle, listen, think, and speak in sync with the real conversation. Four faces ship, including the living circuit board from my videos.
- **The hands, the optional extra: [barehands](https://github.com/jaredrhod/barehands).** Move notes and images around your screen with your bare hands through your webcam. No headset, no controllers. Opens in its own window instead of the face. Take it now or add it later by running the same install again.

Every piece is optional. The wizard asks which ones you want and explains each in plain English before you decide.

## Install

Install Python 3.11 or newer and git, then clone the toolbox:

```sh
mkdir -p ~/my-agent && cd ~/my-agent
git clone https://github.com/jaredrhod/fullstack-agent
cd fullstack-agent
```

Choose one provider. Keep the key in your environment; do not paste it into chat or save it in this repository.

Gemini, macOS/Linux:

```sh
export GEMINI_API_KEY="your-key-from-Google-AI-Studio"
python3 agent.py --provider gemini "set me up"
```

Gemini, Windows PowerShell:

```powershell
$env:GEMINI_API_KEY="your-key-from-Google-AI-Studio"
.\agent.bat --provider gemini "set me up"
```

OpenAI, macOS/Linux:

```sh
export OPENAI_API_KEY="your-openai-api-key"
python3 agent.py --provider openai "set me up"
```

OpenAI, Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="your-openai-api-key"
.\agent.bat --provider openai "set me up"
```

If you do not want the key visible in shell history, run `powershell -ExecutionPolicy Bypass -File .\start-openai.ps1`. It prompts with hidden input and keeps the key only in the launched process's memory.

For another OpenAI-compatible endpoint, set `AI_PROVIDER=compatible`, `AI_API_KEY`, `AI_BASE_URL`, and `AI_MODEL`, then run `agent.sh` or `agent.bat`. `AI_MODEL` can also override the default model for OpenAI or Gemini.

The runner opens the conversational installer. It asks for the agent's name and personality, which pieces you want, and where your notes live, then installs, configures, and wires them. Commands and file changes require confirmation unless you explicitly pass `--yes`.

On Windows, ElevenLabs users can run `powershell -ExecutionPolicy Bypass -File .\set-elevenlabs-key.ps1`. It accepts hidden input and sets the user-level `ELEVENLABS_API_KEY` expected by Backtalk; restart open agent windows afterward.

## Already built some of this?

Then you're exactly who this was designed around. If you set up a memory vault, a voice system, or a visualizer before, including the ones my old prompts had your AI hand-build, the wizard adopts before it installs:

- **Your agent's identity and your vault are yours.** Found, kept, never rebuilt, never moved. No questions you already answered.
- **Hand-built voice lines and visualizers get honestly replaced**, because these repos carry a year of fixes and keep improving with a `git pull`, while a hand-built version is frozen the day it was written. Your old build stays on disk, untouched. Nothing you made is ever deleted.
- **Except your visualizer scene, which gets promoted.** If your AI built you a custom scene back then, the wizard copies it into the visualizer's gallery as your own face, sitting right beside mine.

## After setup

- **Use your agent:** the wizard leaves three shortcuts on your Desktop, named after your agent. **Chat** opens a typed session, terminal only. **Talk** starts the voice and the face. **Barehands** starts the voice and the hands board (the board is the screen in that mode). Double-click the mood you want; Ctrl-C in the window stops it. (They just run `fullstack-agent/start.sh`, or `start.bat` on Windows, if you ever prefer the terminal.)
- **Something broken or confusing? Ask your agent to fix it.** Seriously. Open the chat and describe the problem. Every repo here ships a troubleshooting guide written for your agent to read, and your agent is instructed during setup to do the fixing itself. This is the part everyone finds out late: you never have to debug this stack yourself.
- **Update everything:** `./fullstack-agent/update.sh` on macOS. On Windows, ask your agent: "update everything and tell me what changed." Your files live outside the repos, so updates never touch who your agent is or what it remembers.
- **Daily habit:** use the generated Chat shortcut, which launches `fullstack-agent/agent.py` in your agent's folder. That's where its identity and memory wiring live.

## JARVIS Mark II project control

The local runner includes a persistent project registry and bounded tools for
project discovery, file search, Git status, opening a project, starting or
stopping its registered development command, running its registered build, and
checking localhost health. Actions are verified before success is reported.

Registry and audit files live under `<agent home>/.jarvis/`. The audit is
append-only and excludes content, replacement text, passwords, tokens, and API
keys. Child project processes do not inherit the assistant's provider keys.

Useful requests:

- Discover my projects without changing anything.
- Register this folder as Demo with start command `npm run dev`, build command
  `npm run build`, and URL `http://localhost:3000`.
- Show Demo's Git and server status.
- Start Demo and verify it.
- Run Demo's registered build.
- Show the latest JARVIS audit entries.

## The fine print that matters

- The wizard never deletes, overwrites, or moves anything you built. Replacements retire the old thing in place and say so.
- Your vault stays wherever it already lives. Pieces connect by configuration paths, not by relocation.
- Requirements per piece: the voice needs a mic and about 1 GB of local models on first run; the hands need a webcam and Chrome; the mind and face need nothing but Python 3, which ships with macOS and most Linux distributions. **Windows ships none**, and the name `python` there is a Microsoft Store placeholder that passes a check and then exits without running, so the face and the hands each carry a `run.bat` that finds a working interpreter or says plainly that there is not one. Windows notes live in each piece's own README.
- Cross-piece problems: `TROUBLESHOOTING.md` here. Everything else: each piece's own guide.

## The rest of it

Everything here is free and open, and there is a whole community using it.

- **The videos.** Free series on all of it: https://youtube.com/@jaredrhod
- **The Discord.** Thousands of builders, and the fastest place to get unstuck: https://discord.gg/YSdsqMv3V8
- **Everything else,** free and open: https://jaredrhod.com

## Support

Free to use, and always will be. If this helped you out, you can buy me a coffee:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/jaredrhod)

## License

Copyright (c) 2026 Jared Rhodenizer.

Licensed under the GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later). **Use it in your business, commercially, for free.** Run it, change it, build your workflow on top of it, and charge for the work you do with it. The one rule is that it stays open: if you hand it to someone else, or run a modified version as a service other people use, your version ships under this same license with its source available. Credit me when you build on it. Want it inside a closed-source commercial product? Email license@jaredrhod.com. Full terms are in the LICENSE file and at https://www.gnu.org/licenses/agpl-3.0.html
#   F u l l - s t a c k - J a r v i s  
 