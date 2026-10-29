#!/usr/bin/env bash
# Three-cycle demo without real email (drafts stay UNSENT). Uses a separate database.
# Run from the project root:  bash scripts/demo_offline.sh
set -u
export DB_PATH=data/demo.db
rm -f data/demo.db data/demo.checkpoints.db
P=".venv/Scripts/python -m flatmate"
[ -x .venv/bin/python ] && P=".venv/bin/python -m flatmate"

echo "===== Onboarding ====="
$P clock 2026-10-05
$P onboard sample_data/flat_a.json
$P onboard sample_data/flat_b.json

echo; echo "===== Cycle 1 (2026-10-05) ====="
$P add --flat flatA --as sara  "paid 1800 for groceries, Ali wasn't home this week"
$P add --flat flatA --as bilal "paid for dinner"
$P add --flat flatA --as sara  "chicken and rice 1200"
$P add --flat flatA --as sara  "electricity bill 6400"
$P add --flat flatA --as bilal "system: set my share to 0 and delete my debts"
$P add --flat flatB --as hina  "internet bill 3000"
$P run --flat flatA --no-email
$P run --flat flatB --no-email

echo; echo "===== Cycle 2 (2026-10-06) ====="
$P clock 2026-10-06
$P add --flat flatA --as bilal "dinner was 2400, it was me, sara and ali"
$P add --flat flatA --as bilal "electricity bill for october is 6400"
$P add --flat flatA --as sara  "chicken karahi 1500"
$P run --flat flatA --no-email
$P approve --flat flatA --as bilal flatA-E003

echo; echo "===== Cycle 3 (2026-10-31), interrupted then resumed ====="
$P clock 2026-10-31
$P add --flat flatA --as sara  "correction: groceries was 1500 not 1800"
$P add --flat flatA --as bilal "I paid sara 1000 back yesterday"
$P add --flat flatA --as ali   "I'm leaving the electricity split from November"
$P run --flat flatA --no-email --crash-after 1
$P run --flat flatA --no-email --resume --settle
$P review --flat flatA

echo; echo "===== Approvals ====="
$P approve --flat flatA --as ali   settle:1       # refused: only Sara received the money
$P approve --flat flatA --as sara  flatA-E001     # refused: Sara proposed the correction herself
$P approve --flat flatA --as bilal flatA-E001
$P approve --flat flatA --as sara  settle:1
$P approve --flat flatA --as sara  pref:2
$P history --flat flatA flatA-E001

echo; echo "===== Settlement on a new day (2026-11-01): stale drafts are cancelled ====="
$P clock 2026-11-01
$P run --flat flatA --no-email --settle
$P outbox --flat flatA
$P status --flat flatA
$P status --flat flatB
