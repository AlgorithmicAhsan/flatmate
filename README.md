# FlatMate — Shared-Flat Expense Agent

A single-agent system that keeps a shared flat's expense ledger. Members log expenses as notes (CLI) or emails. Each cycle, the agent reads new messages, decides what to do with each one (record, ask, link a bill, propose a correction, and so on), checks every decision against fixed rules, writes to a SQLite ledger, and emails questions, approval notices and reminders.

## What is in this repository

| Submission item | Where |
|---|---|
| Source code | `flatmate/` |
| Dependency versions | `requirements.txt` and the table below |
| Setup instructions | This README |
| Configuration example without secrets | `.env.example` |
| Memory schema and initialization | `flatmate/schema.sql`; applied automatically by `db.connect()` on the first command |
| Anonymized sample inputs | `sample_data/` (fictional flats; real addresses come from `.env`, never the files) |
| Scenario results | `evaluation/results.md`, `evaluation/results.json` |
| Logs | `evaluation/raw/` (command transcripts), `exports/*_action_log.jsonl` (agent action log) |
| Generated outputs | `exports/*.xlsx` (Excel review view of the representative run) |
| Technical report | `REPORT.md` |
| Approved proposal | `PROPOSAL.md` |
| Tests and scripts | `tests/`, `scripts/demo_offline.sh`, `scripts/run_evaluation.py` |

## Setup

Requires Python 3.11.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # then fill in GROQ_API_KEY, EMAIL_ADDRESS, EMAIL_APP_PASSWORD
```

- **Groq key:** console.groq.com → API Keys. The model is set by `GROQ_MODEL` (default `openai/gpt-oss-120b`).
- **Email:** a Gmail account you own, with 2-Step Verification on and an app password (myaccount.google.com/apppasswords).
- Member addresses in `sample_data/*.json` that start with `+` become plus-addresses of your inbox, e.g. `+sara` → `you+sara@gmail.com`. Plus-addresses can receive email but cannot send it (Gmail rewrites the sender), so they are used for members who only receive.
- `$BILAL_EMAIL` means "the address in that `.env` setting". Set it to a second account you own so Bilal can send real emails that the agent reads over IMAP. If it is empty, Bilal falls back to `you+bilal@gmail.com`.

| Dependency | Version |
|---|---|
| langgraph | 1.2.17 |
| langgraph-checkpoint-sqlite | 3.1.2 |
| langchain-groq | 1.1.3 |
| langchain-core | 1.7.0 |
| python-dotenv | 1.2.4 |
| openpyxl | 3.1.5 |
| LLM | Groq `openai/gpt-oss-120b`, temperature 0 |

## Quick start

```bash
python -m flatmate clock 2026-10-05                     # demo clock (business date)
python -m flatmate onboard sample_data/flat_a.json
python -m flatmate add --flat flatA --as sara "paid 1800 for groceries, Ali wasn't home"
python -m flatmate run --flat flatA                     # one agent cycle (reads + sends email)
python -m flatmate status --flat flatA                  # balances, settlement, what is waiting
python -m flatmate review --flat flatA                  # pending items with their evidence
python -m flatmate approve --flat flatA --as bilal flatA-E001
```

Full offline demo (three cycles, crash and resume, approvals; no real email): `bash scripts/demo_offline.sh`

Tests for the deterministic core: `python -m unittest discover tests`

## Commands

| Command | Purpose |
|---|---|
| `clock [DATE]` | Show or set the demo clock |
| `onboard FILE` | Create a flat, its members and starting preferences |
| `add --flat F --as M "text"` | Save a member's note; handled on the next run |
| `run --flat F` | Run one cycle. Options: `--settle`, `--no-email`, `--simulate-email-failure`, `--crash-after N`, `--resume` |
| `status --flat F` | Balances, suggested settlement, counts of pending items |
| `review --flat F` | Pending expenses, corrections, preference changes, payment claims, open questions |
| `approve` / `reject --flat F --as M ITEM` | ITEM is an expense id, `pref:<id>` or `settle:<id>` |
| `history --flat F EXPENSE` | Every version of one expense |
| `outbox --flat F` | Queued, sent, failed and cancelled emails |
| `log --flat F [-v]` | Action log: tool calls, memory reads/writes, LLM decisions, errors |
| `export --flat F` | Excel review view and the action log as JSON lines, in `exports/` |

Set `MEMORY_ENABLED=false` in `.env` for the memory on/off comparison.

## How it works

```
start_cycle -> fetch_inputs -> next_message --(a message)--> decide <--> lookup
                                   ^                           |
                                   |                       (Decision)
                                   +----------- act <----------+
                              next_message --(none left)--> settle -> notify -> send -> finish
```

| Step | What happens | Code |
|---|---|---|
| start_cycle | Opens a cycle and loads memory: open questions, preferences, last episode | `agent.py` |
| fetch_inputs | Reads new emails from member addresses over IMAP, then queues unhandled notes | `email_tools.read_new_emails` |
| decide | The LLM reads one message, may call read-only lookup tools, then submits a `Decision` | `agent.py`, `agent_tools.py` |
| lookup | Runs the lookup tools: `search_expenses`, `get_preferences`, `get_open_questions`, `get_balances`, `get_recent_episodes` | `agent_tools.py` |
| act | Plain code validates the Decision and writes to the ledger. Rule violations are refused and logged | `agent.apply_decision`, `ledger.py` |
| settle | Optional: settlement plan and reminders within limits | `agent.py`, `balances.py` |
| notify | Approval emails, once per item; withdraws out-of-date drafts | `agent.py` |
| send | Sends the outbox over SMTP; failures stay queued for retry | `email_tools.send_outbox` |
| finish | Saves an episode summary used by later cycles | `agent.py` |

LangGraph saves a checkpoint after every step (`data/<db>.checkpoints.db`), so an interrupted cycle continues with `run --resume`.

### What the LLM decides, and what code decides

- **LLM:** what a message means and which action to take, which lookups to make, what to ask.
- **Code:** all money maths (integer paisa), splits, preference application, approval rules, permissions, duplicate safety net, reminder limits, idempotency. The LLM cannot write to the ledger directly and has no tool that can approve, delete or change members.

### Files

| File | Contents |
|---|---|
| `flatmate/schema.sql` | All tables, commented |
| `flatmate/db.py` | Connection, demo clock, action log |
| `flatmate/ledger.py` | Expenses, versions, preferences, payments, questions, permission rules |
| `flatmate/balances.py` | Splits, balances, settlement plan |
| `flatmate/agent.py` | LangGraph cycle and the Decision schema |
| `flatmate/agent_tools.py` | Read-only lookup tools for the LLM |
| `flatmate/email_tools.py` | IMAP read, outbox, SMTP send |
| `flatmate/export.py` | Excel review view |
| `flatmate/cli.py` | Command-line interface |

## Memory

SQLite (`data/flatmate.db`) is the authoritative store. The Excel export is written fresh and never read back.

| Kind | Tables |
|---|---|
| Working state | `working_state`, `cycles` (status `running` until finished), LangGraph checkpoints |
| Long-term memory | `expenses` + `expense_shares` (versioned), `preferences`, `settlements`, `questions`, `cycles.summary` |
| Source knowledge | `messages` (raw notes and emails), `expense_evidence` |
| Reliability | `outbox` (unique `msg_key`), `action_log`, unique `idem_key` on every agent write |

- **Corrections** add a new version. The old one stays approved until someone other than the proposer approves the change, then becomes `superseded`.
- **Isolation:** every query filters by `flat_id`. The LLM's tools are built for one flat. Incoming email is assigned to a flat by the sender's address, never by its text.
- **Retries:** the same message never creates two records, the same email is never queued twice, and a sent email is never sent again.

## Safety rules

- Expenses are auto-approved only if the logger is the payer and the amount is under `AUTO_RECORD_LIMIT` (Rs 5,000). Otherwise they wait for approval.
- Approvals and payment confirmations are accepted only through the CLI, never from email text.
- Reminders: at most `REMINDER_MAX_PER_DEBT` per debt, at least `REMINDER_MIN_DAYS` apart.
- Message and email text is treated as untrusted data.
- Email is sent only to member addresses stored at onboarding.
