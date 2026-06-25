# Run tests (PYTHONPYCACHEPREFIX avoids stale OneDrive-locked .pyc files)
test:
	PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m pytest tests/

test-q:
	PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m pytest tests/ -q

.PHONY: test test-q
