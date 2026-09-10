# Troubleshooting

This file covers only the problems that live BETWEEN the pieces. Each piece owns its own deeper guide: `ai-memory-vault/TROUBLESHOOTING.md`, `backtalk/TROUBLESHOOTING.md`, `barehands/TROUBLESHOOTING.md`, `ai-visualizer/TROUBLESHOOTING.md`.

## I closed the window in the middle of setup

Anything already installed remains on disk. Open a new terminal (PowerShell on Windows), return to the toolbox folder (`cd ~/my-agent/fullstack-agent`, or on Windows `cd $HOME\my-agent\fullstack-agent`), restore your API-key environment variable, and run:

```
./agent.sh "set me up"
```

On Windows use `.\agent.bat "set me up"`. Tell it "we got cut off, inspect what is already installed, and continue." The installer finds completed work and skips it.

## It says no API key was found

Set `GEMINI_API_KEY` for Gemini or `OPENAI_API_KEY` for OpenAI in the same terminal that launches the runner. Environment variables set in one terminal do not appear retroactively in another. The custom-provider path uses `AI_API_KEY`, plus `AI_PROVIDER=compatible`, `AI_BASE_URL`, and `AI_MODEL`.

## The provider returns model-not-found

Set `AI_MODEL` to a model enabled for your account and restart. Provider model catalogs change independently of this repository, so the model is intentionally configurable.

## The Mac says "xcrun: error: invalid active developer path"

Your Mac is missing Apple's Command Line Tools, which git needs. One command fixes it: run `xcode-select --install` in the same terminal, click Install on the popup, wait the few minutes it takes, then paste the install command again. This also shows up on Macs that recently upgraded macOS, because the upgrade can clear the tools; the same command puts them back.

## Backtalk says it cannot reach the configured brain

Confirm that Backtalk was patched by checking that `backtalk/backtalk/brain.py` mentions `OpenAI-compatible`, then confirm the API key exists in the environment that launched `start.sh` or `start.bat`. The key is never read from `backtalk.json`. Also check `provider`, `model`, and (for a custom endpoint) `base_url` in that config.

## The face sits at idle while the voice talks

The wiring is one config line, plus a restart. Check both:

1. `ai-visualizer/ai-visualizer.json` should have `"bus_dir"` pointing at your backtalk folder. (The same wire can run from the other side instead: `"signals_dir"` in `backtalk/backtalk.json` pointing at the visualizer folder. One direction, not both.)
2. Restart the visualizer server after any config change (Ctrl-C the stack, run start.sh again). Config edits only take effect on restart.

While the agent speaks, the backtalk folder should contain fresh `.voice_state` and `.voice_waveform` files. If they are not appearing, the problem is on the voice side; work backtalk's own guide.

## The greeting doesn't speak on launch

The greeting line lives in `backtalk/backtalk.json` under `"greeting"`. If it is missing or empty, the launch is silent by configuration. The voice piece itself failing to start is a different problem; its terminal output says why, and its guide covers the classics.

## start.sh says a piece is starting but nothing appears

- The face opens a browser tab automatically, on whichever face your `ai-visualizer.json` names. If no tab appears, open `http://127.0.0.1:8790/` yourself and click your face from the gallery. That address is the picker, not a face, so going straight there and expecting the animation is the usual confusion.
- The hands never open a tab automatically (the camera page should be opened deliberately): `http://127.0.0.1:8794/` in Chrome.
- Two stacks can't run at once. If a port is already busy from an earlier session, Ctrl-C the old terminal or close it, then start again.

## My agent forgot who it is

Your agent's identity lives in the provider instruction files in your HOME folder (the folder containing all the tool folders). The included runner reads `AGENT.md`, `AGENTS.md`, `GEMINI.md`, or `CLAUDE.md` there. Opening the runner inside a tool subfolder loads that tool's instructions instead. Daily habit: work from the home folder.

## I moved my agent folder somewhere else

Everything is wired with paths, so a move breaks the wires. Launch `fullstack-agent/agent.py` in the new location and say: "read fullstack-agent/fullstack-agent.md and re-run the wiring phase." Rewiring takes a minute and touches only config paths.

## Updates

`./fullstack-agent/update.sh` pulls every piece. Your files (your CLAUDE.md, your vault, your notes) are never inside the repos' tracked files, so updates cannot touch them. If git complains about a config file you edited (backtalk.json, ai-visualizer.json), your edit wins; keep your version.
