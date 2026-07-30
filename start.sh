#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "┌─────────────────────────────────────────┐"
echo "│  TripOps AI · 文旅产品智能策划 Agent    │"
echo "└─────────────────────────────────────────┘"
echo ""

uv run python cli.py
