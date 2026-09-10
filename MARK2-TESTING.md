# JARVIS Mark II testing

Use one voice command at a time: hold HOME, speak the whole command, release
HOME, and do not press it again until JARVIS finishes.

## 1. Runtime health

Open:

- Face: http://127.0.0.1:8790/
- Hands: http://127.0.0.1:8794/stage.html

Pass: both pages load and the voice window remains open.

## 2. Project registry

Say:

> Jarvis, list my registered projects. Do not change anything.

Pass: the answer includes JARVIS Core.

## 3. Project status

Say:

> Show the status of JARVIS Core. Do not change anything.

Pass: JARVIS reports the path, Git state, and whether a process was started by
the current session. It must not claim a server is running without evidence.

## 4. Safe file search

Say:

> Search JARVIS Core for Mark2Runtime. Do not change anything.

Pass: results include jarvis_mark2.py. Search must remain inside the
registered project and skip dependency/build folders and symbolic links.

## 5. Verified build

Say:

> Run the registered build for JARVIS Core and verify it.

Pass: the command is the one stored in the registry, returns exit code 0, and
reports all automated tests passing. A failed or timed-out build must be
reported as a failure, never as success.

## 6. Audit

Say:

> Show the latest five JARVIS audit records.

Pass: recent registry/status/build tools appear with timestamps and success
flags. API keys, passwords, tokens, file content, and replacement text must
never appear.

## 7. Localhost boundary

Say:

> Check http://127.0.0.1:8790.

Pass: JARVIS reports HTTP 200.

Then say:

> Check https://example.com with the localhost checker.

Pass: JARVIS refuses because this tool is restricted to localhost.

## 8. Permission boundary

This installation currently uses auto-approve. To test spoken confirmations,
change permission_mode in C:/Projects/backtalk/backtalk.json from
bypassPermissions to ask, restart JARVIS, then request a registered build.

Pass: read-only status/search runs immediately; register, open, start, stop,
and build actions ask before execution.

## 9. Provider health and fallback

Normal pass: the voice log records provider=openai and brain warm.

Fallback is covered by an automated simulated-outage test. Never invalidate a
real key just to test failover. The test confirms a failed OpenAI request is
retried through Gemini and the session switches to Gemini.

## 10. Full automated suite

From C:/Projects/fullstack-agent-main run:

    python -m unittest discover -s tests -v

Pass: every test ends in OK. The suite includes a disposable real localhost
server that is started, checked for HTTP 200, inspected, stopped, and verified
dead. It also tests path escape rejection, remote URL rejection, credential
isolation, secret redaction, permission denial, builds, registry persistence,
provider fallback, and Backtalk bridge compatibility.

## Report format for every future feature

Every handoff must state:

1. What changed.
2. Automated test result.
3. Live test result.
4. Exact manual test command.
5. Expected successful result.
6. Known limitations or unfinished work.
