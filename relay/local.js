// Local harness: the same relay function behind plain node http, so the
// whole internet-reach loop can be proven on one machine before anything
// touches Vercel.  node relay/local.js  ->  http://127.0.0.1:8790
import http from "node:http";
import handler from "./api/relay.js";

http
  .createServer((request, response) => {
    const url = new URL(request.url, "http://x");
    request.query = Object.fromEntries(url.searchParams);
    let raw = "";
    request.on("data", (chunk) => (raw += chunk));
    request.on("end", () => {
      try { request.body = raw ? JSON.parse(raw) : {}; } catch { request.body = {}; }
      response.status = (code) => ((response.statusCode = code), response);
      response.json = (value) => {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify(value));
      };
      handler(request, response).catch((error) => {
        response.statusCode = 500;
        response.end(JSON.stringify({ error: String(error) }));
      });
    });
  })
  .listen(8790, () => console.log("vivieen relay (local) on :8790"));
