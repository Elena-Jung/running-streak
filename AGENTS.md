# AGENTS.md

**Scope:** If a user hands you this repository and asks you to deploy / self-host it on **their** server, follow this file. This is a Discord "running streak" bot. It is host-agnostic and configured entirely through a `.env` file. Your goal: a running `docker compose` stack on the user's host, joined to their Discord server, with **all secrets entered by the human** and **nothing secret committed**.

The behavioral source of truth is `DESIGN.md` (see §3 "확정 결정사항 / immutable decisions"). The human-facing walkthrough is `docs/SELF_HOSTING.md` (Korean). The design rationale is `RUNNING_STREAK_BOT_SPEC.md`.

> Note: the repo and the bot's user-facing strings are Korean. This file is in English for portability; keep that in mind when reading the rest of the repo.

---

## Hard guardrails (non-negotiable)

1. **Never accept or request the `DISCORD_TOKEN` or `POSTGRES_PASSWORD` in chat.** These transit the AI provider — treat them as poison. Scaffold `.env` with placeholders and have the human paste secrets themselves, in their own terminal on their own host.
2. **Never commit or push `.env` or any secret.** `.env` is already gitignored (only `.env.example` is tracked). Before any commit, activate the repo's secret-guard hook: `git config core.hooksPath .githooks`. Do not use `git commit --no-verify` unless the user explicitly confirms an intentional exception.
3. **Discord-side setup is human-only.** Creating the application, enabling the **MESSAGE CONTENT** privileged intent, and inviting the bot via OAuth2 cannot be done by you. Instruct the human and wait for confirmation.
4. **Do not weaken or "improve" the immutable decisions in `DESIGN.md` §3** (no scheduler/background jobs; OCR is supplementary and local-only; streak date = upload time in the configured timezone with the 04:00 day boundary; grace ≤3 days keeps / ≥4 resets; query commands are read-only). Changing the timezone/locale (below) is allowed; changing the rules is not — if the user wants a rule changed, confirm explicitly first.
5. **Confirm before any destructive action** — `docker compose down -v` (deletes the `pgdata` volume = all streak data), removing pre-existing containers/volumes/images, or touching any Postgres already on the host. The bot's own DB publishes **no host port**, so it will not collide with an existing database.
6. **Do not write literal guarded patterns** into any file you create or edit (private LAN IP addresses, SSH password-authentication config lines, private-key blocks, or real/realistic Discord-token-shaped strings). The pre-commit hook scans added lines and will block the commit. Describe such topics generically. The `.env.example` placeholders are safe to reference verbatim.

---

## What you may receive vs. must never receive

| Value | In chat? | How to obtain it |
|---|---|---|
| `DISCORD_GUILD_ID` | ✅ OK (not secret) | from the user, or guide them to copy it via Developer Mode |
| `TARGET_CHANNEL_ID` | ✅ OK (not secret) | same |
| Timezone / `DAY_RESET_HOUR` / language preference | ✅ OK | from the user |
| `POSTGRES_USER`, `POSTGRES_DB` (non-secret names) | ✅ OK | from the user, or keep defaults |
| **`DISCORD_TOKEN`** | ❌ NEVER | human pastes it into `.env` in their terminal |
| **`POSTGRES_PASSWORD`** | ❌ NEVER | human generates + pastes; you may suggest they run `openssl rand -base64 24` |

---

## Configuration surface (env-driven)

Required (the bot will not start without these): `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `TARGET_CHANNEL_ID`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.
Optional with defaults: `POSTGRES_HOST=db`, `POSTGRES_PORT=5432`, `OCR_ENABLED=true`, `TZ=Asia/Seoul`.
`docker-compose.yml` runs two services: `db` (postgres:16-alpine, **no published host port**, named volume `pgdata`) and `bot` (built from `./bot`). Slash commands are synced guild-scoped to `DISCORD_GUILD_ID`.

---

## Ordered steps

1. **Prereq check.** `docker compose version`, `git --version`. Confirm the host has outbound HTTPS to `discord.com`. The base image is multi-arch (ARM/x86), so no arch action is needed.
2. **Clone** the repo into the user's chosen directory.
3. **Activate the secret guard:** `git config core.hooksPath .githooks`.
4. **Scaffold `.env`:** `cp .env.example .env`. Fill ONLY the non-secret values (`DISCORD_GUILD_ID`, `TARGET_CHANNEL_ID`, optionally `POSTGRES_USER`/`POSTGRES_DB`, `TZ`). Leave `DISCORD_TOKEN` and `POSTGRES_PASSWORD` as placeholders, then **stop and ask the human to fill them in their own terminal.**
5. **Human checkpoint — Discord.** Tell the human to: create the application; enable **MESSAGE CONTENT INTENT** (without it, uploads are silently ignored — the #1 failure); invite the bot with scopes `bot` + `applications.commands` and permissions View Channels / Send Messages / Read Message History / Add Reactions (optional Manage Messages); then paste the token into `.env`. Wait for confirmation. (Details in `docs/SELF_HOSTING.md` §2.)
6. **Locale / timezone check.** Ask the user's timezone and whether they want a non-Korean UI.
   - Non-Korea timezone: edit `bot/app/events.py` (the `KST = ZoneInfo("Asia/Seoul")` line and the `DAY_RESET_HOUR = 4` line), `.env` `TZ`, and `bot/Dockerfile` `ENV TZ`.
   - Non-Korean UI: translate the Korean strings in `bot/app/commands.py` (`HELP_TEXT`, all command `name=`/`description=`, responses) and `bot/app/events.py` (completion message). This is a sizeable effort — confirm scope with the user.
   - Any code edit requires a rebuild (source is COPYed into the image, not volume-mounted). Do **not** change the DESIGN §3 rules.
7. **Build & run:** `docker compose up -d --build`.
8. **Verify:** `docker compose logs bot` shows login + slash-command sync; `docker compose ps` shows `db` with an empty PORTS column; optionally `docker compose run --rm bot python -m pytest -q`.
9. **Report to the user:** what is running; the Discord verification checklist (run the register command → upload a photo to the target channel → expect a ✅ reaction then the completion message); how to back up (`pg_dump`, not configured by default); how to update (`git pull` + rebuild); and a reminder that `.env` is intentionally uncommitted.
10. **If you committed any change** (e.g. locale edits): work on a branch off `master`, confirm the diff contains no secrets, let the pre-commit hook run, and push **only when the user asks**.

---

## Common failure modes to watch for

- MESSAGE CONTENT intent off → bot connects and syncs commands but never reacts to uploads.
- Wrong `TARGET_CHANNEL_ID`, or bot lacks channel permissions → total silence, no error.
- Timezone not changed for a non-Korea deployment → streaks roll over at Korea 04:00.
- Edited code but forgot to rebuild → old image keeps running.
- iPhone HEIC uploads aren't OCR'd (no pillow-heif); the streak still counts, only the OCR extras are empty — not a bug.

---

## References

- `docs/SELF_HOSTING.md` — full human walkthrough (Korean).
- `DESIGN.md` — single source of truth; read §3 before changing anything behavioral.
- `RUNNING_STREAK_BOT_SPEC.md` — rationale (WHY) behind each decision.
