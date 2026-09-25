#!/usr/bin/env python3
"""Run magicpin's judge_simulator.py without editing it: configuration comes from environment variables.

    BOT_URL=https://<project>.vercel.app SIM_PROVIDER=groq SIM_API_KEY=... SIM_MODEL=<small model> \
    SIM_SCENARIO=auto_reply_hell python -X utf8 scripts/run_simulator.py

SIM_SCENARIO: warmup | phase2_short | auto_reply_hell | intent_transition | hostile | all | full_evaluation
Use a DIFFERENT model/provider for SIM_* than the bot's gpt-oss models: the simulator's judging calls share the quota otherwise.
(-X utf8 matters on Windows: the simulator reads the dataset JSON with the default codec, which garbles the rupee sign.)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import judge_simulator as js  # noqa: E402

js.BOT_URL = os.environ.get("BOT_URL", "http://localhost:8080")
js.LLM_PROVIDER = os.environ.get("SIM_PROVIDER", js.LLM_PROVIDER)
js.LLM_API_KEY = os.environ.get("SIM_API_KEY", js.LLM_API_KEY)
js.LLM_MODEL = os.environ.get("SIM_MODEL", js.LLM_MODEL)
js.TEST_SCENARIO = os.environ.get("SIM_SCENARIO", "all")
js.main()
