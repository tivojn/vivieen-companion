# Vivieen Relay — internet reach without touching EnConvo

The OpenClaw pattern: the iPhone and the MacBook both sit behind NAT, so
neither can call the other. Both **dial out** to this dumb mailbox
instead. It stores and forwards opaque envelopes in two directions per
channel and knows nothing else — no EnConvo, no keys, nothing worth
stealing. If the proof of concept earns its keep, this folder *is* the
spec to hand the EnConvo team; nothing on the EnConvo side changes now
or then.

```
iPhone ──► POST to_mac ──┐            ┌── long-poll to_mac ──► Mac agent
                         │   relay    │                          │ replays against
iPhone ◄── poll to_client┘  (Vercel)  └── POST to_client ◄─────┘ 127.0.0.1:8777
```

## Deploy (free tier)

```bash
npm i -g vercel
cd relay
vercel --prod          # sign in when prompted; note the deployment URL
```

Durable storage (recommended — in-memory only survives warm instances):
Vercel dashboard → Storage → Upstash Redis (free) → connect to the
project. It injects `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`
and the relay uses them automatically.

## Turn the Mac side on

```bash
echo "https://YOUR-DEPLOYMENT.vercel.app" > ~/Library/Application\ Support/Vivieen/relay-url
```

Restart Vivieen. The engine's relay agent (server/relay_agent.py) starts
only when that file exists and long-polls for envelopes, replaying them
against the same local API the phone speaks on the LAN (EnConvo lane,
avatars, health — an allow-list, not a proxy).

## Roll back

```bash
rm ~/Library/Application\ Support/Vivieen/relay-url
```

Restart. The agent never starts; nothing else changed.

## Security (proof-of-concept honest)

- Channel id = `sha256(pairing-token)[:16]`; proof = `sha256("viv-relay:"+token)`.
  The relay never sees the token. First claimant pins the proof
  (trust-on-first-use); a wrong key gets 403 forever after.
- The Mac agent only replays an allow-list of paths, with the engine's
  own token attached locally — the relay cannot mint requests the LAN
  phone couldn't already make.
- Boxes expire after 15 minutes in Redis.
- Live talk (websocket audio) does not cross the relay — Vercel
  functions have no websockets. Text, agent turns, step narration, and
  media cards all do.

## Prove it end to end without deploying

```bash
node relay/local.js   # same handler on http://127.0.0.1:8790
```

Point relay-url at that, restart Vivieen, and drive it with any client
that knows only the relay URL + pairing token. Verified 2026-08-03:
"Received — this came through the relay." in 13s, Mavis in full agent
mode, steps streaming.
