# Updating a CloudCore peer (2026-10-02)

Brings a peer such as Llwyn-y-Groes up to the hub:
- **F-201:** security fixes, plus the retired `dev-token`.
- **F-203:** the isolated lab network, so the llm-chat coordinator's proof VMs can run there.

Run these **on the peer**, in order. Your repo path may differ from `~/IdeaProjects/cloudcore`.

## 1. Pull the code

```bash
cd ~/IdeaProjects/cloudcore
git pull
```

## 2. Create the peer's own API tokens

After this pull, the API **refuses to start** without a token or with `dev-token`, so do this before restarting. These are the peer's own random tokens, not the hub's.

```bash
( umask 077; mkdir -p ~/.config/cloudcore; [ -s ~/.config/cloudcore/api.env ] || python3 -c "
import secrets
print('# CloudCore API tokens (F-201). Keep private: mode 0600, never commit.')
print('CLOUDCORE_API_TOKEN=cc_' + secrets.token_urlsafe(32))
print('CLOUDCORE_EXAMPLES_TOKEN=ccx_' + secrets.token_urlsafe(32))
print('CLOUDCORE_LABVM_TOKEN=ccl_' + secrets.token_urlsafe(32))" > ~/.config/cloudcore/api.env )
chmod 600 ~/.config/cloudcore/api.env
```

## 3. Point the API service at the token file, and restart

```bash
sed -i 's|^Environment=CLOUDCORE_API_TOKEN=.*|EnvironmentFile=%h/.config/cloudcore/api.env|' \
    ~/.config/systemd/user/cloudcore-api.service
grep -n "EnvironmentFile\|CLOUDCORE_API_TOKEN" ~/.config/systemd/user/cloudcore-api.service
systemctl --user daemon-reload
systemctl --user restart cloudcore-api.service
systemctl --user is-active cloudcore-api.service
```

If the `grep` shows no `EnvironmentFile=` line, add `EnvironmentFile=%h/.config/cloudcore/api.env` under `[Service]` by hand, then reload and restart as above.

## 3b. Make sure an API restart can't kill the host's VMs

An installed unit is a copy, so `git pull` doesn't update it. Units installed before 17 Sep 2026 lack `KillMode=process`, and without it every restart of `cloudcore-api` kills every VM it started (F-207). This doesn't restart anything:

```bash
grep -q '^KillMode=process' ~/.config/systemd/user/cloudcore-api.service || \
    sed -i '/^\[Service\]/a KillMode=process' ~/.config/systemd/user/cloudcore-api.service
systemctl --user daemon-reload
systemctl --user show cloudcore-api -p KillMode     # expect KillMode=process
```

## 4. Check the API locally

```bash
set -a; . ~/.config/cloudcore/api.env; set +a
curl -s -o /dev/null -w "new token: %{http_code}\n" -H "Authorization: Bearer $CLOUDCORE_API_TOKEN" http://127.0.0.1:8080/v1/instances
curl -s -o /dev/null -w "dev-token: %{http_code}\n" -H "Authorization: Bearer dev-token" http://127.0.0.1:8080/v1/instances
```

Expect `200` for the new token, then `401` for `dev-token`. If the dashboard is open, refresh it.

## 5. Create the isolated lab network

```bash
sudo api/setup-lab-network.sh --controllers 192.168.100.0/24,192.168.101.0/24
```

## 5b. Accept the lab-VM token every host's guests carry (two-host S5)

A guest asks its own host's lab-VM broker, using the token from whichever host built it, so every host's broker shares one token. Run this **on the hub**; it never prints the token:

```bash
grep '^CLOUDCORE_LABVM_TOKEN=' ~/.config/cloudcore/api.env \
  | ssh <you>@<peer LAN address> 'bash ~/IdeaProjects/cloudcore/api/import-labvm-token.sh'
```

Then on the peer: `systemctl --user restart cloudcore-api` (safe once step 3b is done).

## 6. Tell Claude

The hub-side checks are then:
- the peer reports `lab_network: true`;
- F-201 is closed on the peer's 8082 (no token gets 401, the master token gets 403);
- peering still works in both directions;
- a proof target and prober boot on the peer's lab network, with the same isolation tests as on the hub (F-203).

The hub talks to the peer with per-peer tokens, so none of this touches the link itself.
