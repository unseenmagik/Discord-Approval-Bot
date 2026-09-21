# Discord Approval Bot

A gatekeeper bot that checks who referred new members before giving them access to your server.

## How it works

1. A pinned panel in your welcome channel has a **Verify** button.
2. A member presses it and a form asks who referred them. They enter **one** of:
   - **The referrer's Discord username or user ID** (an `@mention` also works). The referrer must be in the server and hold one of the `verified_role_ids`.
   - **The referrer's name**, if they don't know the username. It must match a name in `referrers.json`. Capitals and extra spaces are ignored.
3. If it matches, the bot gives them `approved_role_id` and posts an approval notice to the admin channel, tagging the approval contacts.
4. If it doesn't match, the bot tells them why and how many attempts they have left. After `max_attempts` failures (default 3), they are locked, and the admin channel gets an alert tagging the lockout contacts. The alert has **Approve** and **Reset attempts** buttons.

Anyone who already holds a verified role (or the approved role) is told they're already verified. All replies are private to the member. Every result is logged with a timestamp in `data/approvals.db` (SQLite).

## Admin commands

Only users in `admin_user_ids` or with a role in `admin_role_ids` can run these commands.

| Command | What it does |
| --- | --- |
| `/verify-panel [channel]` | Posts and pins the verification panel (defaults to `welcome_channel_id`). |
| `/approvals list [status] [user] [limit]` | Recent results with timestamps. `status` is All, Approved, Declined or Locked. |
| `/approvals user <user>` | One member's current status and history. |
| `/approvals approve <member>` | Approves a member manually. |
| `/approvals reset <member>` | Clears a member's failed attempts and unlocks them. |
| `/approvals export` | Downloads the full log as CSV. |
| `/referrers list` | Shows all accepted names. |
| `/referrers add <name>` / `/referrers remove <name>` | Edits the names list. |
| `/referrers export` | Downloads `referrers.json`. |
| `/referrers import <file> [Replace\|Merge]` | Uploads a JSON file of names. |

Names can be used by any number of members. You can also edit `referrers.json` directly on the server. The bot picks up the change on the next lookup, and if the file is invalid it keeps using the previous list and logs a warning. Format:

```json
{ "names": ["Jane Smith", "John Doe"] }
```

To hide the admin commands from regular members, go to **Server Settings → Integrations → (this bot)** and restrict them to your admin roles. The bot checks admin access itself either way.

## Discord setup

1. In the [Developer Portal](https://discord.com/developers/applications), create an application and add a bot. Copy the token.
2. Under **Bot → Privileged Gateway Intents**, turn on **Server Members Intent**. The bot can't look up referrers without it.
3. Invite the bot with the `bot` and `applications.commands` scopes and these permissions:
   View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages (to pin the panel), and Manage Roles.
4. In **Server Settings → Roles**, drag the bot's role **above** the approved role, or it can't assign it.
5. Make sure the bot can post in the admin channel. To tag roles in notices, either make those roles mentionable or give the bot **Mention @everyone, @here and All Roles**. User tags always work.

## Install on Ubuntu

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo useradd --system --create-home --shell /usr/sbin/nologin approvalbot
sudo git clone <your-repo-url> /opt/discord-approval-bot   # or copy the files there
sudo chown -R approvalbot:approvalbot /opt/discord-approval-bot
cd /opt/discord-approval-bot

sudo -u approvalbot python3 -m venv .venv
sudo -u approvalbot .venv/bin/pip install -r requirements.txt
sudo -u approvalbot cp config.ini.example config.ini
sudo -u approvalbot nano config.ini          # fill in token and IDs
sudo chmod 600 config.ini

sudo -u approvalbot .venv/bin/python bot.py  # test run, Ctrl+C to stop
```

`referrers.json` and `data/approvals.db` are created on first run. On startup the bot posts and pins the panel in `welcome_channel_id` unless one is already there (turn this off with `auto_post = false` under `[panel]`). If it can't, the log says why, e.g. which channel permission is missing. `/verify-panel` posts one manually.

### Option A: systemd

```bash
sudo cp deploy/approval-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now approval-bot
journalctl -u approval-bot -f                # logs
```

### Option B: pm2

```bash
cd /opt/discord-approval-bot
pm2 start ecosystem.config.js
pm2 save && pm2 startup                      # restart on reboot
pm2 logs approval-bot
```

pm2 runs the bot as whichever user started it, and that user needs write access to the folder. Use systemd or pm2, not both.

## Backups

Back up `config.ini`, `referrers.json` and `data/`. The database uses WAL mode, so copy `data/approvals.db*` together, or run `sqlite3 data/approvals.db ".backup backup.db"`.

## Configuration

See `config.ini.example`. Every setting is commented. To use a config file somewhere else, set `APPROVAL_BOT_CONFIG=/path/to/config.ini`.
