# Contributing

Thanks for helping! Keep it simple:

1. Fork, branch, make your change.
2. `python3 -m unittest discover -s tests -v` must pass (needs `tmux` and `ttyd`; tests use a private tmux socket and never touch your sessions).
3. Security-sensitive changes (anything in `server/auth.py`, `server/termproxy.py`, request routing in `server/app.py`) need a test in `tests/test_security.py`.
4. Open a PR describing what changed and why.

Report vulnerabilities privately via GitHub Security Advisories, not public issues.
