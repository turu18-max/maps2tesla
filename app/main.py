"""maps-to-tesla web app: paste a Google Maps link, send the route.

Pages: / (send page), /setup (setup wizard: config form, public
key, domain registration, Tesla login, vehicle picker; also the
place to re-login when the refresh token dies).
"""
import asyncio
import json
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .gmaps import parse_route
from .snap import snap_stops, resolve_stops
from .sender import (DATA_DIR, exchange_code, list_vehicles, login_url,
                     prewarm_wake, public_key_pem, register_domain,
                     save_config, send_gps, send_gps_destination,
                     send_waypoints, send_waypoints_raw, set_vin,
                     setup_status, token_scopes)

app = FastAPI(title="maps-to-tesla")


@app.exception_handler(Exception)
async def _unhandled_to_json(request: Request, exc: Exception):
    """Never answer plain text. An unhandled error used to surface
    as a text/plain 'Internal Server Error', which the iPhone
    Shortcut cannot convert to a dictionary — it died at the
    Get Dictionary Value step before its retry branch could run
    (2026-11-23). JSON keeps every client-side failure path alive;
    the traceback still lands in the container log."""
    import logging
    logging.getLogger("uvicorn.error").exception(
        "unhandled error in %s", request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"サーバー内部エラー: {type(exc).__name__}: {exc}"})

PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>maps-to-tesla</title>
<style>
body { font-family: sans-serif; max-width: 640px; margin: 1em auto; padding: 0 1em; }
input { width: 100%; padding: .7em; font-size: 16px; box-sizing: border-box; }
button { padding: .8em 1em; font-size: 16px; margin: .3em 0; }
#out { white-space: pre-wrap; }
.small { color: #555; font-size: 13px; }
</style></head>
<body>
<h1>Google Maps → Tesla</h1>
<p>Google Maps で指定した経由地を、そのまま車へ送信します。</p>
<p>Google Maps の共有リンクを貼ってください。</p>
<div style="display: flex; gap: .4em;">
<input id="url" type="url" placeholder="https://maps.app.goo.gl/..." style="flex: 1; width: auto;">
<button id="clearUrl" title="リンクを消す" style="padding: .7em .9em; margin: 0;">×</button>
</div>
<p>
<button id="sendStops">経由地のまま送る</button>
</p>
<pre id="out"></pre>
<p class="small">トークン: <span id="tokState"></span> <a href="#" id="tokEdit">設定</a> ／ <a href="/setup">セットアップ／再ログイン</a></p>
<p class="small">build: auto-deploy-20</p>
<h2 style="font-size: 18px; margin-top: 1.2em;">最近の実行時間
<button id="reloadTimings" style="font-size: 13px; padding: .3em .8em;">更新</button></h2>
<div id="timings"></div>
<div id="timingLegend" style="margin-top: .6em; font-size: 13px; line-height: 2;"></div>
<script>
const out = document.getElementById('out');
function token() { return localStorage.getItem('mtt_token') || ''; }
function refreshTok() {
  document.getElementById('tokState').textContent = token() ? '設定済み' : '未設定';
}
document.getElementById('tokEdit').onclick = (e) => {
  e.preventDefault();
  const t = prompt('APIトークンを入力してください（初回のみ）', token());
  if (t !== null) { localStorage.setItem('mtt_token', t.trim()); refreshTok(); }
};
refreshTok();
async function call(path, body) {
  const r = await fetch(path, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Api-Token': token()},
    body: JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}
function linkUrl() { return document.getElementById('url').value.trim(); }
const urlInput = document.getElementById('url');
const savedUrl = localStorage.getItem('mtt_url');
if (savedUrl) urlInput.value = savedUrl;
urlInput.addEventListener('input', () => {
  localStorage.setItem('mtt_url', urlInput.value);
  pendingConfirm = false;
  document.getElementById('sendStops').textContent = '経由地のまま送る';
});
document.getElementById('clearUrl').onclick = () => {
  urlInput.value = '';
  localStorage.removeItem('mtt_url');
  urlInput.focus();
};
let pendingConfirm = false;
document.getElementById('sendStops').onclick = async () => {
  const btn = document.getElementById('sendStops');
  if (!pendingConfirm) {
    try {
      const rv = await call('/api/resolve', {url: linkUrl()});
      if (rv.unresolved && rv.unresolved.length) {
        out.textContent = '名前から場所を特定できなかった地点があります: ' +
          rv.unresolved.join(', ');
        loadTimings();
        return;
      }
      if ((rv.resolved && rv.resolved.length) ||
          (rv.dropped && rv.dropped.length)) {
        let msg = '';
        if (rv.resolved && rv.resolved.length) {
          msg += '名前から解決しました:\\n' +
            rv.resolved.map((r) => r.query + ' → ' + r.name).join('\\n') + '\\n';
        }
        if (rv.dropped && rv.dropped.length) {
          msg += rv.dropped.map((d) =>
            '外します: 座標の' + (d.role === 'destination' ? '目的地' : '経由地') +
            (d.nearest ? '（最寄りの' + d.nearest + 'まで' + d.distance_m + 'm）'
                       : '（近くに場所がありません）')).join('\\n') + '\\n';
        }
        out.textContent = msg + 'もう一度押すと送信します';
        pendingConfirm = true;
        btn.textContent = 'この内容で送信する';
        return;
      }
    } catch (e) { out.textContent = 'エラー: ' + e.message; loadTimings(); return; }
  }
  pendingConfirm = false;
  btn.textContent = '経由地のまま送る';
  out.textContent = '送信中...';
  try {
    const d = await call('/api/send', {url: linkUrl()});
    out.textContent = '送信しました\\n' + (d.route || '');
  } catch (e) { out.textContent = 'エラー: ' + e.message; }
  loadTimings();
};
const PHASES = [
  ['parse', 'リンク展開', '#4e79a7'],
  ['resolve', '名前解決', '#edc949'],
  ['send', '車へ送信', '#9c755f']
];
const KIND_LABEL = {'send': '経由地送信', 'resolve': '名前解決'};
function recSegs(r) {
  const segs = {};
  segs.parse = r.parse || 0;
  segs.resolve = r.resolve || 0;
  segs.send = r.send || 0;
  return segs;
}
async function loadTimings() {
  const div = document.getElementById('timings');
  try {
    const resp = await fetch('/api/timings',
      {headers: {'X-Api-Token': token()}});
    const data = await resp.json();
    const recs = (data.records || []).slice(0, 6);
    if (!recs.length) {
      div.innerHTML = '<p class="small">まだ記録がありません</p>';
      return;
    }
    const maxTotal = Math.max.apply(null, recs.map((r) => r.total || 0.1));
    div.innerHTML = '';
    recs.forEach((r) => {
      const segs = recSegs(r);
      const label = document.createElement('div');
      label.style.fontSize = '13px';
      label.style.margin = '10px 0 3px';
      const when = new Date(r.at * 1000).toLocaleTimeString('ja-JP');
      let extra = '';
      label.textContent = when + ' ' + (KIND_LABEL[r.kind] || r.kind) +
        ' 合計 ' + (r.total || 0).toFixed(2) + '秒 ' + extra;
      div.appendChild(label);
      const bar = document.createElement('div');
      bar.style.display = 'flex';
      bar.style.height = '20px';
      bar.style.border = '1px solid #888';
      bar.style.borderRadius = '4px';
      bar.style.overflow = 'hidden';
      bar.style.width = Math.max(4, (r.total / maxTotal) * 100) + '%';
      PHASES.forEach((p) => {
        const v = segs[p[0]] || 0;
        if (v <= 0) return;
        const seg = document.createElement('div');
        seg.style.width = (v / (r.total || 0.1)) * 100 + '%';
        seg.style.background = p[2];
        seg.title = p[1] + ' ' + v.toFixed(2) + '秒';
        bar.appendChild(seg);
      });
      div.appendChild(bar);
      const detail = document.createElement('div');
      detail.className = 'small';
      const parts = [];
      PHASES.forEach((p) => {
        const v = segs[p[0]] || 0;
        if (v > 0) parts.push(p[1] + ' ' + v.toFixed(2));
      });
      detail.textContent = parts.join(' / ');
      div.appendChild(detail);
    });
    const legend = document.getElementById('timingLegend');
    legend.innerHTML = '';
    PHASES.forEach((p) => {
      const chip = document.createElement('span');
      chip.style.display = 'inline-block';
      chip.style.width = '12px';
      chip.style.height = '12px';
      chip.style.borderRadius = '2px';
      chip.style.margin = '0 4px 0 12px';
      chip.style.background = p[2];
      legend.appendChild(chip);
      legend.appendChild(document.createTextNode(p[1]));
    });
  } catch (e) {
    div.innerHTML = '<p class="small">実行時間を取得できませんでした</p>';
  }
}
document.getElementById('reloadTimings').onclick = loadTimings;
loadTimings();
</script></body></html>"""


class LinkIn(BaseModel):
    url: str


def _api_token():
    path = os.path.join(DATA_DIR, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("api_token")
    return None


def _check_token(x_api_token: str | None):
    expected = _api_token()
    if expected and x_api_token != expected:
        raise HTTPException(401, "invalid api token")


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/timings")
async def timings(x_api_token: str | None = Header(default=None)):
    """Recent per-request timing records (newest first), rendered
    by the page as stacked bars so the bottleneck is visible on the
    NAS itself, without reading container logs."""
    _check_token(x_api_token)
    return {"records": list(reversed(_timing_records))}


@app.post("/api/parse")
async def parse(body: LinkIn):
    route = parse_route(body.url)
    return {
        "stops": route["stops"],
        "sendable_count": len(route["sendable"]),
        "missing": [s["value"] for s in route["missing"]],
    }


class CodeIn(BaseModel):
    code: str


SETUP_PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>セットアップ — maps-to-tesla</title>
<style>
body { font-family: sans-serif; max-width: 680px; margin: 1em auto; padding: 0 1em; }
button { padding: .7em 1em; font-size: 15px; margin: .3em 0; }
input, select, textarea { width: 100%; box-sizing: border-box; padding: .5em; margin: .2em 0 .6em; font-size: 15px; }
textarea { height: 9em; font-family: monospace; font-size: 12px; }
.step { border: 1px solid #ccc; border-radius: 8px; padding: .8em 1em; margin: 1em 0; }
.small { color: #666; font-size: 13px; }
.ok { color: #1a7f37; font-weight: bold; }
.ng { color: #b35c00; font-weight: bold; }
pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; }
label { font-size: 14px; font-weight: bold; }
</style></head><body>
<h1 style="font-size: 22px;">maps-to-tesla セットアップ</h1>
<p class="small">上から順に進めてください。各ステップの詳しい手順は
リポジトリの docs/SETUP.md を見てください。<br>
状態: <span id="sumState">読み込み中…</span></p>

<div class="step">
<h2 style="font-size:17px;">1. Tesla の登録情報 <span id="st1"></span></h2>
<label>Client ID</label>
<input id="f_client_id" autocomplete="off">
<label>Client Secret</label>
<input id="f_client_secret" type="password" autocomplete="off">
<p class="small" id="secretNote"></p>
<label>ドメイン（GitHub Pages: ユーザー名.github.io）</label>
<input id="f_domain" placeholder="yourname.github.io" autocomplete="off">
<label>リージョン</label>
<select id="f_region"><option value="na">na（日本はこちら）</option>
<option value="eu">eu</option><option value="cn">cn</option></select>
<button id="saveCfg">保存する</button>
<div id="tokenBox" style="display:none">
<p class="small">APIトークン（iPhoneショートカットの X-Api-Token ヘッダーに
設定する値です。このブラウザには自動で保存されました）:</p>
<input id="apiTokenOut" readonly>
</div>
<pre id="out1"></pre>
</div>

<div class="step">
<h2 style="font-size:17px;">2. 公開鍵 <span id="st2"></span></h2>
<p class="small">表示された公開鍵を、GitHub Pages の
/.well-known/appspecific/com.tesla.3p.public-key.pem
というファイルに置いてください。</p>
<button id="keyBtn">公開鍵を表示する</button>
<textarea id="keyOut" readonly style="display:none"></textarea>
<pre id="out2"></pre>
</div>

<div class="step">
<h2 style="font-size:17px;">3. ドメイン登録</h2>
<p class="small">公開鍵が上のURLで開けるようになってから押してください。
Tesla にドメインを登録します。</p>
<button id="regBtn">ドメインを登録する</button>
<pre id="out3"></pre>
</div>

<div class="step">
<h2 style="font-size:17px;">4. Tesla ログイン <span id="st4"></span></h2>
<p class="small">Tesla アカウントでログインして、このアプリに車両操作を
許可します。保存したログイン情報（リフレッシュトークン）が失効して
送信時に <b>login_required</b> と出たときの「再ログイン」も同じボタンです。</p>
<button id="loginBtn">Teslaでログインする</button>
<pre id="out4"></pre>
</div>

<div class="step">
<h2 style="font-size:17px;">5. 車の選択 <span id="st5"></span></h2>
<p class="small">ログイン後に押すと車両の一覧が出ます。自分の車を選んでください。</p>
<button id="vehBtn">車両を取得する</button>
<div id="vehList"></div>
<pre id="out5"></pre>
</div>

<p><a href="/">← 送信ページに戻る</a></p>
<script>
function token() { return localStorage.getItem('mtt_token') || ''; }
function H() { return {'X-Api-Token': token(), 'Content-Type': 'application/json'}; }
function mark(id, ok) { const e = document.getElementById(id);
  e.textContent = ok ? '✓ 済み' : '未'; e.className = ok ? 'ok' : 'ng'; }
async function loadStatus() {
  try {
    const r = await fetch('/setup/status', {headers: H()});
    const s = await r.json();
    if (!r.ok) { document.getElementById('sumState').textContent =
      '取得エラー: ' + (s.detail || r.status); return; }
    document.getElementById('sumState').textContent =
      (s.config_exists ? '設定あり' : '未設定') +
      (s.domain ? ' / ' + s.domain : '') +
      (s.vin_set ? ' / 車両：設定済み' : '');
    mark('st1', s.client_id_set && s.client_secret_set && !!s.domain);
    mark('st2', s.private_key_exists);
    mark('st4', s.tokens_exist);
    mark('st5', s.vin_set);
    if (s.domain) document.getElementById('f_domain').value = s.domain;
    document.getElementById('f_region').value = s.region || 'na';
    document.getElementById('secretNote').textContent = s.client_secret_set
      ? 'Client Secret は設定済みです（変更する場合だけ入力してください）' : '';
    if (s.client_id_set)
      document.getElementById('f_client_id').placeholder =
        '設定済み（変更する場合だけ入力）';
  } catch (e) { document.getElementById('sumState').textContent =
    '取得エラー: ' + e; }
}
document.getElementById('saveCfg').onclick = async () => {
  const out = document.getElementById('out1');
  out.textContent = '保存しています…';
  const body = {
    client_id: document.getElementById('f_client_id').value.trim(),
    client_secret: document.getElementById('f_client_secret').value.trim(),
    domain: document.getElementById('f_domain').value.trim(),
    region: document.getElementById('f_region').value };
  try {
    const r = await fetch('/setup/config',
      {method: 'POST', headers: H(), body: JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) { out.textContent = 'エラー: ' + (d.detail || r.status); return; }
    if (d.api_token) { localStorage.setItem('mtt_token', d.api_token);
      document.getElementById('apiTokenOut').value = d.api_token;
      document.getElementById('tokenBox').style.display = 'block'; }
    out.textContent = '保存しました。';
    loadStatus();
  } catch (e) { out.textContent = 'エラー: ' + e; }
};
document.getElementById('keyBtn').onclick = async () => {
  const out = document.getElementById('out2');
  out.textContent = '取得しています…';
  try {
    const r = await fetch('/setup/public-key', {headers: H()});
    const t = await r.text();
    if (!r.ok) { out.textContent = 'エラー: ' + t; return; }
    const ta = document.getElementById('keyOut');
    ta.value = t; ta.style.display = 'block'; out.textContent = '';
    loadStatus();
  } catch (e) { out.textContent = 'エラー: ' + e; }
};
document.getElementById('regBtn').onclick = async () => {
  const out = document.getElementById('out3');
  out.textContent = '登録しています…';
  try {
    const r = await fetch('/setup/register',
      {method: 'POST', headers: H()});
    const d = await r.json();
    out.textContent = r.ok ? '登録しました: ' + JSON.stringify(d)
      : 'エラー: ' + (d.detail || JSON.stringify(d));
  } catch (e) { out.textContent = 'エラー: ' + e; }
};
document.getElementById('loginBtn').onclick = async () => {
  const out = document.getElementById('out4');
  out.textContent = 'ログインURLを取得しています…';
  try {
    const r = await fetch('/setup/login-url', {headers: H()});
    const d = await r.json();
    if (!r.ok) { out.textContent = 'エラー: ' + (d.detail || r.status); return; }
    location.href = d.login_url;
  } catch (e) { out.textContent = 'エラー: ' + e; }
};
document.getElementById('vehBtn').onclick = async () => {
  const out = document.getElementById('out5');
  out.textContent = '取得しています…';
  const list = document.getElementById('vehList'); list.innerHTML = '';
  try {
    const r = await fetch('/setup/vehicles', {headers: H()});
    const d = await r.json();
    if (!r.ok) { out.textContent = 'エラー: ' + (d.detail || r.status); return; }
    out.textContent = '';
    d.vehicles.forEach(v => {
      const b = document.createElement('button');
      b.textContent = (v.display_name || '車両') + '（…' +
        (v.vin || '').slice(-6) + '）';
      b.onclick = async () => {
        const rr = await fetch('/setup/vin',
          {method: 'POST', headers: H(), body: JSON.stringify({vin: v.vin})});
        out.textContent = rr.ok ? 'この車を送信先に設定しました。'
          : 'エラー: ' + rr.status;
        loadStatus();
      };
      list.appendChild(b);
    });
    if (!d.vehicles.length)
      out.textContent = '車両が見つかりませんでした（ログイン状態を確認してください）';
  } catch (e) { out.textContent = 'エラー: ' + e; }
};
loadStatus();
</script>
</body></html>"""


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return SETUP_PAGE


@app.get("/setup/login-url")
async def setup_login_url(x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return {"login_url": await login_url()}


@app.post("/setup/exchange")
async def setup_exchange(body: CodeIn,
                         x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return await exchange_code(body.code)


@app.get("/setup/public-key")
async def setup_public_key(x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(await public_key_pem())


@app.get("/setup/callback", response_class=HTMLResponse)
async def setup_callback(code: str):
    """Landing spot for the GitHub Pages callback page: receives the
    OAuth code by browser navigation (no manual copy/paste, which can
    corrupt long codes in some terminals) and exchanges it."""
    result = await exchange_code(code)
    if result.get("refresh_token_saved"):
        return "<html><body><h1>認可が完了しました</h1>" \
               "<p>この画面を閉じて大丈夫です。</p></body></html>"
    return f"<html><body><h1>認可に失敗しました</h1><pre>{result}</pre></body></html>"


@app.post("/setup/register")
async def setup_register(x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return await register_domain()


@app.get("/setup/scopes")
async def setup_scopes(x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return await token_scopes()


class ConfigIn(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    domain: str = ""
    region: str = ""


class VinIn(BaseModel):
    vin: str


@app.get("/setup/status")
async def setup_status_ep(x_api_token: str | None = Header(default=None)):
    """Wizard checklist state. Before any config exists there is
    no api_token yet, so _check_token passes — first-run setup on
    the LAN is unauthenticated by design; once the wizard saves a
    config (which generates the token) every setup endpoint locks
    to it."""
    _check_token(x_api_token)
    return setup_status()


@app.post("/setup/config")
async def setup_config_ep(body: ConfigIn,
                          x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return save_config({
        "client_id": body.client_id,
        "client_secret": body.client_secret,
        "domain": body.domain,
        "region": body.region,
    })


@app.get("/setup/vehicles")
async def setup_vehicles(x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return {"vehicles": await list_vehicles()}


@app.post("/setup/vin")
async def setup_vin_ep(body: VinIn,
                       x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    return set_vin(body.vin)


# Name-based links (hand-built or from an AI travel plan) carry stop
# names with no Place ID. Resolve them by name search, cached briefly
# by URL so the page's resolve-then-send flow resolves only once.
_resolve_cache = {}


# A coordinate stop is only snapped to a place this close. Beyond
# it the "nearest place" is a guess (a private house has no Google
# place entry at all: a home coordinate's nearest registered place
# was a museum shop 166 m away, 2026-11-23). Farther coordinate
# stops are dropped from the send instead of being substituted,
# and the drop is stated in the result (chosen behavior, 2026-11-23).
COORD_SNAP_MAX_M = 100.0


def _drop_notice(dropped):
    """Human-readable statement of dropped coordinate stops, for
    the send result / Shortcut notification."""
    parts = []
    for d in dropped:
        role = "目的地" if d["role"] == "destination" else "経由地"
        if d["nearest"]:
            parts.append(
                f"座標の{role}（最寄りの{d['nearest']}まで"
                f"{d['distance_m']}m）")
        else:
            parts.append(f"座標の{role}（近くに場所がありません）")
    return " ※外しました: " + "、".join(parts)


def _apply_resolution(url, route):
    """Fill place_id/resolved_name on route['missing'] stops where a
    confident name match exists. Returns the list of resolutions.
    Stops that stay unresolved remain in route['missing'].
    Coordinate stops whose nearest place is farther than
    COORD_SNAP_MAX_M are dropped instead: removed from the route
    and listed in route['dropped']."""
    # Key the cache by the EXPANDED URL: the same link arrives in
    # different spellings (the page keeps a percent-encoded paste,
    # the Shortcut sends the raw share URL), and keying by the raw
    # string made the Shortcut pay the full ~13 s resolution again
    # right after the page preview had just computed it.
    cache_key = route.get("expanded") or url
    hit = _resolve_cache.get(cache_key)
    if hit and _time.monotonic() - hit[0] < 300:
        mapping, dropped = hit[1], hit[2]
    else:
        # Two kinds of ID-less stops: place NAMES (resolve by name
        # search) and raw COORDINATES, which some AI assistants emit
        # instead of names (snap to the nearest real place, the same
        # machinery route sending uses).
        import re as _re
        coord_re = _re.compile(
            r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
        name_stops, coord_stops = [], []
        for s in route["missing"]:
            m = coord_re.match(s["value"] or "")
            if m:
                coord_stops.append(
                    (s, (float(m.group(1)), float(m.group(2)))))
            else:
                name_stops.append(s)
        mapping = {}
        if name_stops:
            results = resolve_stops([s["name"] for s in name_stops])
            for s, r in zip(name_stops, results):
                if r:
                    mapping[s["name"]] = r
        dropped = []
        if coord_stops:
            # Coordinate stops are exact destinations, not corridor
            # pins: snap them with facility preference, so a big
            # facility's own point wins over a shop inside it
            # (a farm snapping to its in-farm restaurant, 2026-11-23).
            # Only a snap within COORD_SNAP_MAX_M is accepted;
            # anything farther is a guess and the stop is dropped.
            snapped = snap_stops([p for _, p in coord_stops])
            # A None here is usually a transient search miss — the
            # same pin returned its place on a retry in testing
            # (observed in testing, 2026-11-23) — so retry the misses once.
            # A FAR place is a confident answer and is not retried:
            # fishing for a nearer one would resurrect wrong places.
            miss = [i for i, r in enumerate(snapped) if r is None]
            if miss:
                again = snap_stops([coord_stops[i][1] for i in miss])
                for i, r in zip(miss, again):
                    snapped[i] = r
            for (s, _pin), r in zip(coord_stops, snapped):
                if r and r["distance_m"] <= COORD_SNAP_MAX_M:
                    mapping[s["name"]] = r
                else:
                    dropped.append({
                        "query": s["name"], "role": s["role"],
                        "nearest": r["name"] if r else None,
                        "distance_m": (round(r["distance_m"])
                                       if r else None)})
        _resolve_cache.clear()
        _resolve_cache[cache_key] = (_time.monotonic(), mapping, dropped)
    resolved = []
    for s in route["missing"]:
        r = mapping.get(s["name"])
        if r:
            s["place_id"] = r["place_id"]
            s["resolved_name"] = r["name"]
            resolved.append({"query": s["name"], "name": r["name"],
                             "place_id": r["place_id"]})
    if dropped:
        # Remove exactly the dropped stop objects (matched via the
        # missing list, so a same-coordinate origin is never hit).
        gone_names = {d["query"] for d in dropped}
        gone_ids = {id(s) for s in route["missing"]
                    if s["name"] in gone_names}
        route["stops"] = [s for s in route["stops"]
                          if id(s) not in gone_ids]
        route["missing"] = [s for s in route["missing"]
                            if id(s) not in gone_ids]
    route["dropped"] = dropped
    route["missing"] = [s for s in route["missing"]
                        if not s.get("place_id")]
    targets = [s for s in route["stops"]
               if s["role"] in ("waypoint", "destination")]
    route["sendable"] = [s for s in targets if s.get("place_id")]
    return resolved


@app.post("/api/resolve")
async def resolve(body: LinkIn,
                  x_api_token: str | None = Header(default=None)):
    """Parse + name-resolution preview WITHOUT sending, so the page
    can show which places the names resolved to before the send."""
    _check_token(x_api_token)
    _t0 = _time.monotonic()
    # The page's send almost always follows this preview within
    # seconds, so start the car waking here too (best effort).
    _wake_task = asyncio.create_task(prewarm_wake())
    route = parse_route(body.url)
    _t_parse = _time.monotonic() - _t0
    resolved = []
    _t_resolve = 0.0
    if route["missing"]:
        _t_r0 = _time.monotonic()
        resolved = await asyncio.to_thread(
            _apply_resolution, body.url, route)
        _t_resolve = _time.monotonic() - _t_r0
    _log_timing("resolve", total=_s(_time.monotonic() - _t0),
                parse=_s(_t_parse), resolve=_s(_t_resolve),
                resolved=len(resolved),
                unresolved=len(route["missing"]))
    _record("resolve", total=_time.monotonic() - _t0, parse=_t_parse,
            resolve=_t_resolve, resolved=len(resolved),
            unresolved=len(route["missing"]))
    return {"resolved": resolved,
            "dropped": route.get("dropped", []),
            "unresolved": [s["name"] for s in route["missing"]]}


@app.post("/api/send")
async def send(body: LinkIn, x_api_token: str | None = Header(default=None)):
    _check_token(x_api_token)
    _t0 = _time.monotonic()
    # Start waking the car NOW, in parallel with parsing and name
    # resolution. The Shortcut calls this endpoint directly (no
    # /api/resolve first), and a sleeping car is the main cause of
    # first-attempt failures: the wake poll alone can take most of
    # the client's timeout, so the request dies before the waypoints
    # are sent — while the wake itself succeeds, which is why the
    # second attempt then works instantly (2026-11-23).
    _wake_task = asyncio.create_task(prewarm_wake())
    route = parse_route(body.url)
    _t_parse = _time.monotonic() - _t0
    resolved = []
    _t_resolve = 0.0
    if route["missing"]:
        _t_r0 = _time.monotonic()
        resolved = await asyncio.to_thread(
            _apply_resolution, body.url, route)
        _t_resolve = _time.monotonic() - _t_r0
    if route["missing"]:
        raise HTTPException(
            422, f"Place ID を取得できない地点があります: "
                 f"{[s['value'] for s in route['missing']]}")
    if not route["sendable"]:
        raise HTTPException(422, "送信できる地点がありません")
    ids = [s["place_id"] for s in route["sendable"]]
    _t_send0 = _time.monotonic()
    try:
        result = await send_waypoints(ids)
    except Exception as e:
        # A failed car send must stay JSON (a plain-text 500 kills
        # the Shortcut's dictionary step before its retry can run)
        # and must leave a timing record, so the failure is visible
        # on the page instead of looking like "nothing happened".
        _t_send = _time.monotonic() - _t_send0
        _log_timing("send FAILED", total=_s(_time.monotonic() - _t0),
                    parse=_s(_t_parse), resolve=_s(_t_resolve),
                    send=_s(_t_send), stops=len(ids), error=str(e))
        _record("send", total=_time.monotonic() - _t0, parse=_t_parse,
                resolve=_t_resolve, send=_t_send, stops=len(ids))
        raise HTTPException(503, f"車への送信に失敗しました: {e}")
    _t_send = _time.monotonic() - _t_send0
    _log_timing("send", total=_s(_time.monotonic() - _t0),
                parse=_s(_t_parse), resolve=_s(_t_resolve),
                send=_s(_t_send), stops=len(ids))
    _record("send", total=_time.monotonic() - _t0, parse=_t_parse,
            resolve=_t_resolve, send=_t_send, stops=len(ids))
    names = [s.get("resolved_name") or s["name"]
             for s in route["sendable"]]
    route_str = " → ".join(names)
    dropped = route.get("dropped", [])
    if dropped:
        route_str += _drop_notice(dropped)
    return {"sent": [s["value"] for s in route["sendable"]],
            "names": names,
            "route": route_str,
            "resolved": resolved,
            "dropped": dropped,
            "result": result}


import time as _time

def _s(x):
    return f"{x:.2f}s"


def _log_timing(kind, **fields):
    """One greppable timing line per request, e.g.
    [timing] send total=7.31s parse=0.42s resolve=1.20s send=5.69s ...
    Read with: docker logs maps-to-tesla 2>&1 | grep timing"""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[timing] {kind} {parts}", flush=True)


# Recent timing records (raw seconds), kept in memory for the
# page's timing chart — the NAS logs are not reachable from outside
# the container, so the page renders its own history instead.
import collections as _collections

_timing_records = _collections.deque(maxlen=30)


def _record(kind, **raw):
    rec = {"kind": kind, "at": _time.time()}
    rec.update(raw)
    _timing_records.append(rec)


class GpsIn(BaseModel):
    lat: float
    lon: float
    order: int = 0


@app.post("/api/gps")
async def gps(body: GpsIn, x_api_token: str | None = Header(default=None)):
    """Probe endpoint: send a single coordinate pin to the car."""
    _check_token(x_api_token)
    return await send_gps(body.lat, body.lon, body.order)


class WaypointsRawIn(BaseModel):
    waypoints: str


@app.post("/api/waypoints-raw")
async def waypoints_raw(body: WaypointsRawIn,
                        x_api_token: str | None = Header(default=None)):
    """Probe endpoint: send a raw waypoints string to the car."""
    _check_token(x_api_token)
    return await send_waypoints_raw(body.waypoints)


class GpsDestIn(BaseModel):
    lat: float
    lon: float
    name: str = "ピン"
    order: int = 0


@app.post("/api/gps-dest")
async def gps_dest(body: GpsDestIn,
                   x_api_token: str | None = Header(default=None)):
    """Probe endpoint: send a named coordinate destination to the car."""
    _check_token(x_api_token)
    return await send_gps_destination(body.lat, body.lon, body.name,
                                      body.order)
