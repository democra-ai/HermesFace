/**
 * DNS fix preload script for HF Spaces (Node.js side).
 *
 * Patches Node.js dns.lookup to:
 *   1. Check pre-resolved domains from /tmp/dns-resolved.json (populated by dns-resolve.py)
 *   2. Fall back to DNS-over-HTTPS (Cloudflare) for any other unresolvable domain
 *
 * Loaded via: NODE_OPTIONS="--require /path/to/dns-fix.cjs"
 *
 * Hermes Agent is primarily Python, but Node processes (playwright, whatsapp-bridge,
 * web dashboard build) benefit from this preload when launched from bash.
 */
"use strict";

const dns = require("dns");
const https = require("https");
const fs = require("fs");

let preResolved = {};
try {
  const raw = fs.readFileSync("/tmp/dns-resolved.json", "utf8");
  preResolved = JSON.parse(raw);
  const count = Object.keys(preResolved).length;
  if (count > 0) {
    console.log(`[dns-fix] Loaded ${count} pre-resolved domains`);
  }
} catch {
  // File not found or parse error — proceed without pre-resolved cache
}

const runtimeCache = new Map();

function dohResolve(hostname, callback) {
  const cached = runtimeCache.get(hostname);
  if (cached && cached.expiry > Date.now()) {
    return callback(null, cached.ip);
  }

  const url = `https://1.1.1.1/dns-query?name=${encodeURIComponent(hostname)}&type=A`;
  const req = https.get(
    url,
    { headers: { Accept: "application/dns-json" }, timeout: 15000 },
    (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        try {
          const data = JSON.parse(body);
          const aRecords = (data.Answer || []).filter((a) => a.type === 1);
          if (aRecords.length === 0) {
            return callback(new Error(`DoH: no A record for ${hostname}`));
          }
          const ip = aRecords[0].data;
          const ttl = Math.max((aRecords[0].TTL || 300) * 1000, 60000);
          runtimeCache.set(hostname, { ip, expiry: Date.now() + ttl });
          callback(null, ip);
        } catch (e) {
          callback(new Error(`DoH parse error: ${e.message}`));
        }
      });
    }
  );
  req.on("error", (e) => callback(new Error(`DoH request failed: ${e.message}`)));
  req.on("timeout", () => {
    req.destroy();
    callback(new Error("DoH request timed out"));
  });
}

const origLookup = dns.lookup;

dns.lookup = function patchedLookup(hostname, options, callback) {
  if (typeof options === "function") {
    callback = options;
    options = {};
  }
  if (typeof options === "number") {
    options = { family: options };
  }
  options = options || {};

  if (
    !hostname ||
    hostname === "localhost" ||
    hostname === "0.0.0.0" ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    /^\d+\.\d+\.\d+\.\d+$/.test(hostname) ||
    /^::/.test(hostname)
  ) {
    return origLookup.call(dns, hostname, options, callback);
  }

  if (preResolved[hostname]) {
    const ip = preResolved[hostname];
    if (options.all) {
      return process.nextTick(() => callback(null, [{ address: ip, family: 4 }]));
    }
    return process.nextTick(() => callback(null, ip, 4));
  }

  origLookup.call(dns, hostname, options, (err, address, family) => {
    if (!err && address) {
      return callback(null, address, family);
    }
    if (err && (err.code === "ENOTFOUND" || err.code === "EAI_AGAIN")) {
      dohResolve(hostname, (dohErr, ip) => {
        if (dohErr || !ip) {
          return callback(err);
        }
        if (options.all) {
          return callback(null, [{ address: ip, family: 4 }]);
        }
        callback(null, ip, 4);
      });
    } else {
      callback(err, address, family);
    }
  });
};
