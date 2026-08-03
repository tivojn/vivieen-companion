// Vivieen Relay - a dumb rendezvous mailbox, the OpenClaw pattern.
//
// Neither the phone nor the Mac can accept an inbound connection, so both
// DIAL OUT to this box. It stores and forwards opaque JSON envelopes in
// two directions per channel (to_mac, to_client) and knows nothing else:
// no EnConvo, no keys, no message contents worth reading. Proof-of-concept
// quality on purpose - if it earns its keep, this whole file is the spec
// to hand the EnConvo team.
//
// Storage: Upstash Redis via REST when UPSTASH_REDIS_REST_URL/TOKEN are
// set (the Vercel marketplace free tier), else an in-memory Map - fine
// for local harness runs, NOT durable on serverless.
//
// Auth: trust-on-first-use. The channel id is sha256(pairing-token)[:16];
// the auth proof is sha256("viv-relay:" + pairing-token). The relay never
// sees the token itself; the first claimant of a channel pins the proof
// and everyone after must match it.

const memory = new Map();

async function redis(...command) {
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return null;
  const r = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  return (await r.json()).result;
}

const hasRedis = () =>
  Boolean(process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN);

async function boxPush(key, value) {
  if (hasRedis()) {
    await redis("RPUSH", key, JSON.stringify(value));
    await redis("EXPIRE", key, "900");
    return;
  }
  const box = memory.get(key) || [];
  box.push(value);
  if (box.length > 500) box.splice(0, box.length - 500);
  memory.set(key, box);
}

async function boxRead(key, after) {
  if (hasRedis()) {
    const rows = (await redis("LRANGE", key, String(after), "-1")) || [];
    return rows.map((row) => JSON.parse(row));
  }
  return (memory.get(key) || []).slice(after);
}

async function boxLen(key) {
  if (hasRedis()) return Number((await redis("LLEN", key)) || 0);
  return (memory.get(key) || []).length;
}

async function pinGet(channel) {
  if (hasRedis()) return await redis("GET", `pin:${channel}`);
  return memory.get(`pin:${channel}`) || null;
}

async function pinSet(channel, proof) {
  if (hasRedis()) {
    await redis("SET", `pin:${channel}`, proof, "EX", "2592000");
    return;
  }
  memory.set(`pin:${channel}`, proof);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export default async function handler(request, response) {
  const q = request.query || {};
  const channel = String(q.channel || "");
  const proof = String(request.headers["x-viv-proof"] || "");
  const dir = q.dir === "to_mac" ? "to_mac" : "to_client";
  if (!/^[0-9a-f]{16}$/.test(channel) || !/^[0-9a-f]{64}$/.test(proof)) {
    return response.status(400).json({ error: "bad channel or proof" });
  }
  const pinned = await pinGet(channel);
  if (!pinned) await pinSet(channel, proof);
  else if (pinned !== proof) {
    return response.status(403).json({ error: "channel claimed by another key" });
  }
  const key = `box:${channel}:${dir}`;

  if (request.method === "POST") {
    const body = typeof request.body === "object" && request.body ? request.body : {};
    for (const item of Array.isArray(body.items) ? body.items : [body]) {
      await boxPush(key, item);
    }
    return response.status(200).json({ ok: true, len: await boxLen(key) });
  }

  // GET: long-poll for anything after ?after=N, up to ?wait seconds.
  const after = Math.max(0, Number(q.after) || 0);
  const wait = Math.min(25, Math.max(0, Number(q.wait) || 0));
  const deadline = Date.now() + wait * 1000;
  for (;;) {
    const items = await boxRead(key, after);
    if (items.length || Date.now() >= deadline) {
      return response.status(200).json({ items, next: after + items.length });
    }
    await sleep(700);
  }
}
