# Serving RefBase over HTTPS on the NCBJ network

The application itself needs no changes for HTTPS. All its redirects are relative
paths, it has no cookies or sessions, its only subresource is same-origin
`/static/…`, and nothing in the code reads the request scheme. TLS is entirely a
matter of what sits in front of the uvicorn process.

## The one hard prerequisite: a DNS name

A certificate is issued for a **hostname**, not for an IP address. If people reach
the library as `http://192.168.x.x:8000`, that has to become
`https://refbase.ncbj.gov.pl` (or whatever name IT assigns) before any of this
works. Most internal CAs will not issue certificates with an IP SAN at all, and
the browsers that do accept them treat them as a special case.

So step one is asking IT for a DNS A record pointing at the server.

## What to ask NCBJ IT for

Because the host is not reachable from the internet, Let's Encrypt's default
HTTP-01 challenge cannot validate it. The clean path is a certificate from the
institute's own CA, which domain-joined machines already trust — no browser
warnings for anyone.

Generate a key and a signing request on the server:

```bash
sudo mkdir -p /etc/ssl/refbase && cd /etc/ssl/refbase
sudo openssl req -new -newkey rsa:2048 -nodes \
  -keyout privkey.pem -out refbase.csr \
  -subj "/CN=refbase.ncbj.gov.pl/O=National Centre for Nuclear Research" \
  -addext "subjectAltName=DNS:refbase.ncbj.gov.pl"
sudo chmod 600 privkey.pem
```

Send `refbase.csr` to IT and ask for:

- a **server certificate** for `refbase.ncbj.gov.pl` (add every alias people will
  type as further `DNS:` entries in the SAN — a certificate that omits the name in
  the address bar produces a warning no matter how valid it is),
- the **CA chain** (root + any intermediates).

Concatenate what comes back, leaf first, into `/etc/ssl/refbase/fullchain.pem`.

### If IT cannot issue one

- **DNS-01 challenge.** If NCBJ controls the DNS zone and you can add TXT records,
  Let's Encrypt will issue a publicly-trusted certificate for a host that is not
  publicly reachable. Caddy supports this with a DNS-provider plugin. This is the
  best fallback — a real certificate, trusted everywhere, renewing automatically.
- **Caddy's internal CA.** Replace the `tls` line in the Caddyfile with
  `tls internal`. Caddy then runs its own CA. Every client must install Caddy's
  root certificate, or see a warning on every visit — workable for a handful of
  people, painful for forty.
- **Self-signed.** Same drawback as above with more manual work. Fine for a
  smoke test, not for the group.

## Deploying

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo cp deploy/refbase.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now refbase
sudo systemctl reload caddy
```

Edit the hostname in both files first. `deploy/refbase.service` assumes the app
lives in `/srv/refbase` with a virtualenv at `/srv/refbase/.venv` and runs as a
`refbase` user; adjust if your layout differs.

## What changes for the running app

Nothing in the code — but two things about how it is started:

- **`--host 127.0.0.1`** instead of `0.0.0.0`. With Caddy in front, uvicorn must
  not be reachable from the LAN, otherwise anyone can bypass TLS by connecting to
  port 8000 directly.
- **`--proxy-headers --forwarded-allow-ips=127.0.0.1`** so `X-Forwarded-For` is
  honoured and logs show real client addresses. Scoped to the proxy's address
  because these headers can be forged by whoever is allowed to set them.

`scripts/run.sh` is untouched: it is the development launcher and still binds
`0.0.0.0` for LAN testing over plain HTTP.

## A side effect worth knowing

`navigator.clipboard` only works in a secure context. Over plain HTTP the "Copy
BibTeX" buttons fall back to a hidden `<textarea>` and `document.execCommand`.
Under HTTPS the native clipboard API takes over, which is more reliable. The
fallback stays in `app/static/export.js` regardless — it costs nothing and keeps
the dev server working.
