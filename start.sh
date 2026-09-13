#!/bin/bash
# Generate the Discord help pages, then serve the site with live reload.
# The help pages are generated from data/discord.sql and are not committed,
# so this step is needed before Zola can see them.
set -e
python3 scripts/build_help_pages.py
zola serve -i 0.0.0.0
