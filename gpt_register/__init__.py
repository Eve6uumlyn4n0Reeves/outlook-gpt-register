"""Lightweight GPT registration job framework (rewritten).

Provides a minimal job lifecycle compatible with existing endpoints:
- create_run / execute_run / get_status / cancel / pause / resume
- writes jsonl + sqlite under run-logs

This is a scaffold: plug real automation into execute_run later.
"""

