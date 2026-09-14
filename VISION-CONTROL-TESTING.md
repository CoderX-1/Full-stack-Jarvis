# JARVIS Vision-Control Engine — Phase 1 test gate

Do not begin UI State Graph work until every automated check passes and Ayaan
explicitly says the manual test passed.

## Automated tests

Run from the source repository:

```powershell
python -B -m unittest discover -s tests -v
python -B -m unittest -v test_windows_control.py
$env:PYTHONPATH = (Get-Location).Path
& "C:\Projects\JARVIS\components\backtalk\.venv\Scripts\python.exe" -B vision_smoke_test.py
```

The isolated smoke test must verify local OCR, word coordinates, a unique
visual target, a real click, a changed screenshot, changed OCR text, and clean
closure of its own temporary window. Run it while the Windows desktop is
unlocked and interactive; Windows intentionally blocks foreground activation
and screenshots on a locked or disconnected desktop.

## Manual voice test

1. Open Windows Calculator and keep it visible.
2. Hold HOME and say: `Jarvis, look at the Calculator window and tell me what text you can see. Do not click anything.`
3. Pass: JARVIS names text actually visible in Calculator and says it used local vision/OCR.
4. Hold HOME and say: `Jarvis, visually find the button labeled Standard in Calculator. Tell me how many matches you found, but do not click.`
5. Pass: JARVIS reports a precise match or honestly says the label is not visible; it does not click.
6. Hold HOME and say: `Jarvis, visually click the Calculator button labeled 7 and verify the display changed.`
7. Pass: Calculator shows `7`, JARVIS reports before/after visual verification, and no other window is clicked.
8. Say: `Jarvis, visually click text called definitely-not-on-screen.`
9. Pass: nothing is clicked and JARVIS reports no match.
10. Open any sign-in page with a password field, but do not enter a password. Say: `Jarvis, observe this window.`
11. Pass: JARVIS reports password-field redaction; it never reads, repeats, saves, or audits password contents.

Reply exactly `manual test passed` only after all steps pass. Otherwise report
the failed step and observed behavior; remain on Vision-Control Engine Phase 1.
