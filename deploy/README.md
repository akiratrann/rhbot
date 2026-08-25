# Running rhbot on a VPS

A laptop cannot do this. Close the lid and macOS suspends every process — no
ticks, no exits, no kill switch, positions frozen open with nothing watching
them. A $5/month always-on server is the only honest way to run unattended.

## 1. Create the server

You do this part — I can't open accounts or buy hosting.

Any provider works. **Ubuntu 24.04, 1GB RAM** is more than enough; the bot is
idle almost all the time. DigitalOcean, Hetzner, Vultr and Linode are all ~$4–6
a month.

Add your SSH key during creation. Note the IP.

## 2. Deploy

From this directory on your laptop:

```bash
rsync -az --exclude .venv --exclude state --exclude logs --exclude .git \
      ./ root@YOUR_SERVER:/opt/rhbot/
rsync -az --chmod=600 .env root@YOUR_SERVER:/opt/rhbot/.env
ssh root@YOUR_SERVER 'bash /opt/rhbot/deploy/setup.sh'
```

That installs Python, creates an unprivileged `rhbot` user, sets up systemd,
enables the firewall, and **starts the paper bot**. It also installs the live
service but leaves it stopped.

After the first deploy, pushing changes is one command:

```bash
./deploy/sync.sh root@YOUR_SERVER
```

## 3. Check on it

```bash
./deploy/status.sh root@YOUR_SERVER
```

Service state, next relearn run, kill-switch state, both dashboards' equity and
P&L, and a 24h error count.

To see a dashboard in your browser, tunnel it — the ports are bound to
localhost on purpose and must never be exposed:

```bash
ssh -N -L 5001:127.0.0.1:5001 -L 5003:127.0.0.1:5003 root@YOUR_SERVER
```

Then open <http://127.0.0.1:5001> (paper) or <http://127.0.0.1:5003> (live).

## 4. Going live

```bash
./deploy/go-live.sh root@YOUR_SERVER
```

Runs pre-flight on the server first and refuses to start if anything fails,
then asks you to type `LIVE` to confirm. Nothing starts without that.

Stop it:

```bash
ssh root@YOUR_SERVER 'systemctl stop rhbot-live.service'
```

Emergency halt — leaves the service up but blocks every order on the next tick:

```bash
ssh root@YOUR_SERVER 'touch /opt/rhbot/state/STOP && chown rhbot /opt/rhbot/state/STOP'
```

## What runs automatically

| Unit | What it does |
|---|---|
| `rhbot-paper.service` | Paper bot. `Restart=always`, survives crashes and reboots. |
| `rhbot-live.service`  | Live bot. Same, but only after you start it once. |
| `rhbot-relearn.timer` | Sundays 22:00 ET: walk-forward re-selection, applied to the **paper** config only. |

The relearn deliberately does not touch `config.live.yaml`. Re-pointing real
money at newly selected symbols without a human looking first is not something
worth automating — a weekly screen over 64 candidates will always surface a few
winners by chance.

## Keys

`.env` carries credentials that can trade real money. On the server it is mode
`600`, owned by `rhbot`. Do not commit it, and do not widen those permissions.

If the server is ever compromised, revoke the keys from the Alpaca dashboard
first — that invalidates them instantly and is faster than anything else you
could do.

## State lives on the server

`sync.sh` never copies `state/`. The server's book is the real one; overwriting
it with a laptop copy reintroduces exactly the position drift that reconcile.py
exists to fix. If the two ever disagree:

```bash
ssh root@YOUR_SERVER 'systemctl stop rhbot-live.service'
ssh root@YOUR_SERVER 'cd /opt/rhbot && sudo -u rhbot .venv/bin/python reconcile.py --config config.live.yaml --apply'
ssh root@YOUR_SERVER 'systemctl start rhbot-live.service'
```
