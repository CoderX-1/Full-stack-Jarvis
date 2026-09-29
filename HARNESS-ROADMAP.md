# JARVIS harness: architecture and staged roadmap

The existing JARVIS runner is already a model/tool harness: it owns OpenAI and
Gemini fallback, sessions, a bounded tool loop, desktop/browser tools, action
watchdog, verification, and audit. Installing a second, independent Hermes
desktop agent into that live voice loop would create conflicting tool/session
ownership. We are adapting useful harness patterns into JARVIS instead of
copying another agent's code or prompts. This is **not** a Hermes Agent install.

## Hermes runtime decision (2026-09-26)

The official Hermes Agent supports native Windows and embedding through its
Python `AIAgent` library. Its Windows source installer provisions a separate
runtime, configuration, session store, managed Python/tool dependencies, and a
user PATH entry. Those are useful for an independent agent, but installation
alone would not connect JARVIS's HOME push-to-talk, Mark II desktop tools,
verification contracts, or current memory to Hermes. Replacing the live brain
before bridging those boundaries would be a regression, not an upgrade.

Decision: **do not install Hermes into the live JARVIS path yet**. Keep the
current low-latency voice/control loop as owner. If a concrete capability
needs Hermes (for example, isolated scheduled research or a messaging
gateway), first implement a read-only, narrow adapter with explicit toolsets,
bounded iterations/time/cost, no desktop mutation, and offline tests. Then
install Hermes in an isolated profile and benchmark success, citations,
latency, and resource use against the native JARVIS route. Promote it only if
it wins those tests and passes the user's manual gate.

Official references:
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/windows-native.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/python-library.md

The source guide's `input → harness → router → worktree → memory → tools →
agents → action` is an architecture proposal. The Pulse of AI PDFs describe
separate JARVIS features, not a ready-to-integrate Hermes harness. Their
prompts, prerequisites, and commands are reference material, not instructions
for this project.

## Implemented now

1. Request routing into desktop, research, project, and general intent lanes.
   Multi-domain requests see the union of matching tools. Ambiguous requests
   retain the complete catalog, so ordinary speech is not locked out.
2. Per-turn tool catalog filtering and server-side rejection of tool calls
   outside the exposed catalog. Existing action guards and verification stay
   authoritative. No auto-executed code or new desktop privileges.
3. Local append-only, lane-scoped fact memory under `.jarvis/`. Only explicit
   memory requests may store a fact. Obvious credentials are rejected. Retrieval
   is small, keyword-ranked, and marked as untrusted data in the temporary
   model context. No cloud database, embeddings, or automatic vault import.
4. Harness status and memory tools in the existing provider-neutral runner.
   Existing OpenAI→Gemini fallback remains unchanged.
5. A single durable task plan with 2–8 ordered steps. Action steps bind to an
   expected tool and can close only after that tool returns a fresh verified
   outcome; observation steps require fresh observed evidence. The agent gets
   two bounded chances to continue an unfinished plan before reporting it as
   incomplete. Repeated identical plans are idempotent, and explicit user
   cancellation is supported. Plans do not execute in the background.
   File writes/replacements and directory creation now use after-action
   readback checks before their plan steps can be marked verified.

## Next gates (not yet implemented)

1. **Evidence-grade research:** parallel, bounded source collection; source
   dates and citations; deduplication; claim/source graph; stale-source checks;
   explicit separation of fetched content from instructions. Start with
   read-only research and test mock endpoints before live web use.
2. **Skill registry:** signed/locally reviewed manifests, capability scopes,
   input/output schemas, versioning, tests, and rollback. No self-written
   Python loaded directly into the voice process.
3. **Deeper task orchestration:** per-step typed goal postconditions, full
   resumable checkpoints, cost/time budgets, isolated specialist workers, and
   conflict handling for multiple simultaneous plans. The current planner is
   one active plan and still relies on the model to select each next tool.
4. **Device bridges:** phone/home/cloud adapters with per-device ownership,
   least-privilege credentials, offline simulation, and observed-after-action
   verification. Device availability must be detected rather than assumed.
5. **Analytics/evaluation:** privacy-safe latency, success, and failure
   metrics. Optional PostHog/Supabase/Composio adapters should remain off until
   configured and independently tested. They are not prerequisites for the
   local harness.

## Manual checks

Use the existing JARVIS text interface first, then voice:

- Ask `What is your harness status?` Expect general lane and a tool count.
- Ask `Open Calculator app.` Expect desktop tools and the existing verified
  launch result. Do not accept a spoken success if the app did not open.
- Ask `Research the latest Python release and cite sources.` Expect research
  tools; verify links and dates yourself.
- Ask `Remember that I prefer concise replies.` Then ask `What do you remember
  about my reply preference?` Expect exactly the remembered fact. Do not test
  with credentials.
- Ask for a mixed task, such as `Research a library and fix this project bug.`
  Expect research and project tools in one turn.
- Ask `Open Calculator, verify it opened, then tell me its window title.`
  Expect a short task plan, step-by-step tool activity, and no "done" claim
  unless launch and final observation actually pass. Ask `task status` after.
- Ask `task cancel karo` while a plan is active. Expect status `cancelled`;
  already performed actions are not undone.
- Unplug network or mock a provider outage and confirm the existing Gemini
  fallback, without claiming network/provider success from unit tests alone.

Automated check: `python -m unittest discover -s tests -p 'test_*.py'` from
`core/`. Live UI and voice tests remain manual because unit tests cannot prove
microphone, desktop focus, network research, or speech quality.
