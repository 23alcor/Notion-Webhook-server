// Cloudflare Worker port of the FastAPI webhook server (app.py, routes/, ai/).
// Same routes, auth and response shapes, so Notion and existing callers keep working unchanged.

const NOTION = 'https://api.notion.com/v1';
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });
class HttpError extends Error { constructor(status, detail) { super(detail); this.status = status; } }

// ---------- time (the Pi ran in the owner's time zone; Workers run in UTC) ----------

function wall(ms, tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
    .formatToParts(ms).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, hour: +p.hour, minute: +p.minute, offset: Date.UTC(+p.year, p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(ms / 1000) * 1000 };
}
export const today = (env, now = Date.now()) => wall(now, env.TIMEZONE || 'America/New_York').day;
const addDays = (day, n) => new Date(Date.parse(day + 'T00:00:00Z') + n * 864e5).toISOString().slice(0, 10);
const daysBetween = (from, to) => Math.round((Date.parse(to + 'T00:00:00Z') - Date.parse(from + 'T00:00:00Z')) / 864e5);
const validDay = (s) => /^\d{4}-\d{2}-\d{2}/.test(s || '') && !Number.isNaN(Date.parse(s.slice(0, 10) + 'T00:00:00Z'));
function midnightUtc(day, tz) { // UTC instant of 00:00 on `day` in `tz`
  const guess = Date.parse(day + 'T00:00:00Z');
  return guess - wall(guess - wall(guess, tz).offset, tz).offset;
}

// ---------- Notion ----------

async function notion(env, path, { method = 'GET', body, timeout = 30000, retries = 3 } = {}) {
  if (!env.NOTION_TOKEN) throw new Error('NOTION_TOKEN missing');
  for (let attempt = 1; ; attempt++) {
    try {
      const r = await fetch(NOTION + path, {
        method, signal: AbortSignal.timeout(timeout),
        headers: { authorization: `Bearer ${env.NOTION_TOKEN}`, 'Notion-Version': '2022-06-28', 'content-type': 'application/json' },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!r.ok) { const text = await r.text(); console.log(`Notion error: ${r.status} - ${text}`); throw new HttpError(502, `Notion API error: ${r.status} ${text}`); }
      return r.json();
    } catch (e) {
      if (e.name !== 'TimeoutError' || attempt >= retries) throw e;
      await new Promise((ok) => setTimeout(ok, 500 * attempt));
    }
  }
}
const need = (env, name) => { if (!env[name]) throw new Error(`${name} missing`); return env[name]; };
const plain = (rich) => (rich || []).map((p) => p?.plain_text || '').join('');

async function queryDatabase(env, id) {
  const results = [];
  for (let cursor; ;) {
    const data = await notion(env, `/databases/${id}/query`, { method: 'POST', body: cursor ? { start_cursor: cursor } : {} });
    results.push(...(data.results || []));
    if (!data.has_more || !(cursor = data.next_cursor)) return results;
  }
}

export function parseTodo(pages) { // undone Inbox items from the Todo database
  return pages.flatMap((page) => {
    const p = page.properties || {};
    if (p.Done?.checkbox || p.Status?.status?.name !== 'Inbox') return [];
    return [{ title: plain(p.Name?.title) || 'Untitled', due_date: p['Date Due']?.date?.start || p.Date?.date?.start || null, description: plain(p.Text?.rich_text), source: 'todo', project_id: null }];
  });
}
export function parseTasks(pages, projects = {}) { // not-Done items from the project Tasks database
  return pages.flatMap((page) => {
    const p = page.properties || {};
    if (p.Status?.status?.name === 'Done') return [];
    const project_id = p.Projects?.relation?.[0]?.id || null;
    return [{ title: plain(p.Name?.title) || 'Untitled', due_date: p['Action Date']?.date?.start || null, description: plain(p.Description?.rich_text), source: 'task', project_id, project_name: project_id ? projects[project_id] || 'Other' : 'Other' }];
  });
}
async function projectNames(env) {
  if (!env.NOTION_PROJECTS_ID) return {};
  try { return Object.fromEntries((await queryDatabase(env, env.NOTION_PROJECTS_ID)).map((pg) => [pg.id, pg.properties?.Name?.title?.[0]?.plain_text || 'Unnamed'])); }
  catch (e) { console.log(`[PROJECTS] ${e.message}`); return {}; }
}
async function combinedItems(env) {
  const [todos, tasks, projects] = await Promise.all([queryDatabase(env, need(env, 'NOTION_TODO_ID')), queryDatabase(env, need(env, 'NOTION_TASKS_ID')), projectNames(env)]);
  return [...parseTodo(todos), ...parseTasks(tasks, projects)];
}
const setCallout = (env, blockId, rich_text) => notion(env, `/blocks/${blockId}`, { method: 'PATCH', body: { callout: { rich_text: rich_text.slice(0, 100) } }, timeout: 20000, retries: 1 });

// ---------- callouts ----------

const text = (content, bold = false, color = 'default') => ({ type: 'text', text: { content }, annotations: { bold, color } });

export function deadlinesCallout(items, day) {
  if (!items.length) return [{ type: 'text', text: { content: 'No deadlines yet.' } }];
  const days = (i) => !i.due_date ? 0 : validDay(i.due_date) ? daysBetween(day, i.due_date.slice(0, 10)) : 0;
  const entry = (i) => { const d = days(i); return text(`${d} - ${(i.title || 'Untitled').trim()}\n`, true, d <= 0 ? 'red' : d <= 2 ? 'orange' : 'green'); };
  const byDays = (a, b) => days(a) - days(b);
  const out = items.filter((i) => i.source === 'todo').sort(byDays).map(entry);
  const groups = {};
  for (const t of items.filter((i) => i.source === 'task')) (groups[t.project_name || 'Other'] ||= []).push(t);
  const names = Object.keys(groups).sort();
  if (names.length) out.push(text('\n'));
  for (const name of names) out.push(text(`${name}\n`, true), ...groups[name].sort(byDays).map(entry));
  return out.slice(0, 100);
}

export function countItems(items, day) {
  const c = { due: 0, upcoming: 0, eventually: 0 };
  for (const i of items) {
    if (!i.due_date) c.due++; // no date = backlog
    else if (!validDay(i.due_date)) c.eventually++;
    else { const d = daysBetween(day, i.due_date.slice(0, 10)); c[d <= 0 ? 'due' : d <= 7 ? 'upcoming' : 'eventually']++; }
  }
  return c;
}

async function importantThingsCallout(env, items, now = Date.now()) {
  const tz = env.TIMEZONE || 'America/New_York', { day, hour } = wall(now, tz), name = env.OWNER_NAME ? ' ' + env.OWNER_NAME : '';
  const tomorrow = formatEvents(await eventsOn(env, addDays(day, 1)), tz);
  const c = countItems(items, day);
  const sleep = hour >= 18 ? (await chat(env, [{ role: 'user', content: `Tomorrow's schedule: ${tomorrow}. What time should I sleep tonight to get 6-7 hours before my first event? Reply in one sentence only.` }])).trim() : '';
  return [
    text(hour < 12 ? `Good morning${name}` : hour < 18 ? `Good afternoon${name}` : `Day is almost over${name}`, true), text('\n\n'),
    ...(hour < 18 ? [text("Here's what you have today.\n"), text(formatEvents(await eventsOn(env, day), tz))] : [text("Here's what you have tomorrow.\n"), text(tomorrow)]),
    text('\n\n'),
    text(`You have ${c.due} due tasks.\n`, true, c.due ? 'red' : 'default'),
    text(`You have ${c.upcoming} upcoming tasks.\n`, true, c.upcoming ? 'orange' : 'default'),
    text(`You have ${c.eventually} tasks eventually.`, true),
    ...(sleep ? [text('\n\n'), text(sleep, false, 'blue')] : []),
  ];
}

async function updateDeadlines(env, items) {
  await setCallout(env, need(env, 'NOTION_DEADLINE_BLOCK_ID'), deadlinesCallout(items || await combinedItems(env), today(env)));
  console.log('Deadline was changed');
}
async function refreshAll(env) {
  const items = await combinedItems(env);
  await updateDeadlines(env, items);
  await setCallout(env, need(env, 'NOTION_IMPORTANT_THINGS_BLOCK_ID'), await importantThingsCallout(env, items));
  console.log('Important things was changed');
}

// ---------- OpenAI ----------

const NOTE_PROMPT = `
You are an assistant that interprets short personal notes written for future reference, planning, or decision-making.

Your job is to:
1. Infer the intent of the note (idea, reminder, purchase, or general).
2. Produce a clear, helpful response that future-me can quickly understand.
3. Extract automation-friendly metadata without asking follow-up questions.

Rules:
- Do not mention AI.
- Do not ask questions.
- Do not repeat the original note verbatim.
- Be concise, calm, and practical.
- Optimize for usefulness when revisited weeks or months later.
- Make reasonable assumptions when information is missing.

Intent handling:
- If the note describes something to be done or remembered, treat it as a reminder.
- If the note describes an idea, curiosity, or research direction, treat it as an idea.
- If the note involves evaluating or buying a product or tool, treat it as a purchase.
- If urgency or immediacy is implied and no date is mentioned, assume today.

Special handling for purchases:
- Do light research to support a decision.
- Provide rough price ranges.
- Group recommendations into tiers (cheap, mid-tier, premium).
- Include at least two representative links per tier when possible.
- Offer an opinion on whether the purchase is worth making now, later, or at all.
- Suggest alternatives that could save time or money.

Output format (exactly this structure):

Context:
- 1-2 bullets explaining what this note is about and why it matters.

Key Points:
- 2-4 bullets summarizing findings or considerations.

Options:
- Cheap:
  - Name - short reason (approx. price, link)
- Mid-tier:
  - ...
- Premium:
  - ...

Recommendation:
- Clear opinion on what to do and why.

Metadata:
- intent: <idea | reminder | purchase | general>
- suggested_date: <YYYY-MM-DD or null>
`;

async function chat(env, messages) {
  const r = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST', signal: AbortSignal.timeout(60000),
    headers: { authorization: `Bearer ${need(env, 'OPENAI_API_KEY')}`, 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'gpt-4o-mini', messages }),
  });
  if (!r.ok) throw new Error(`OpenAI error: ${r.status} ${await r.text()}`);
  return (await r.json()).choices[0].message.content;
}

// ---------- Google Calendar (read-only) ----------

let google = { token: null, expires: 0 }; // per isolate
async function googleToken(env) {
  if (google.token && Date.now() < google.expires - 60000) return google.token;
  const r = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: need(env, 'GOOGLE_CLIENT_ID'), client_secret: need(env, 'GOOGLE_CLIENT_SECRET'), refresh_token: need(env, 'GOOGLE_REFRESH_TOKEN'), grant_type: 'refresh_token' }),
  });
  if (!r.ok) throw new Error(`Google token refresh failed: ${r.status} ${await r.text()}`);
  const d = await r.json();
  google = { token: d.access_token, expires: Date.now() + d.expires_in * 1000 };
  return google.token;
}
async function gcal(env, path) {
  const r = await fetch('https://www.googleapis.com/calendar/v3' + path, { headers: { authorization: `Bearer ${await googleToken(env)}` } });
  if (!r.ok) throw new Error(`Google Calendar ${r.status}`);
  return r.json();
}
async function eventsOn(env, day) {
  const tz = env.TIMEZONE || 'America/New_York';
  const skip = new Set((env.EXCLUDED_CALENDARS || '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean));
  const range = new URLSearchParams({ timeMin: new Date(midnightUtc(day, tz)).toISOString(), timeMax: new Date(midnightUtc(addDays(day, 1), tz)).toISOString(), singleEvents: 'true', orderBy: 'startTime' });
  const calendars = (await gcal(env, '/users/me/calendarList')).items || [];
  const lists = await Promise.all(calendars.filter((c) => !skip.has((c.id || '').toLowerCase())).map((c) =>
    gcal(env, `/calendars/${encodeURIComponent(c.id)}/events?${range}`).then((d) => d.items || [], (e) => { console.log(`[CALENDAR] Skipping calendar ${c.id}: ${e.message}`); return []; })));
  const start = (e) => e.start?.dateTime || e.start?.date || '';
  return lists.flat().sort((a, b) => (start(a) < start(b) ? -1 : start(a) > start(b) ? 1 : 0));
}
export function formatEvents(events, tz) {
  if (!events.length) return 'nothing scheduled';
  const labels = events.map((e) => {
    const title = e.summary || 'Untitled';
    if (!e.start?.dateTime) return title;
    const { hour, minute } = wall(Date.parse(e.start.dateTime), tz);
    return `${hour % 12 || 12}:${String(minute).padStart(2, '0')}${hour < 12 ? 'am' : 'pm'} - ${title}`;
  });
  return [...new Set(labels)].join('\n');
}

// ---------- webhook ----------

let lastDeadlineUpdate = 0; // shortcut: per-isolate debounce, so a burst split across isolates may refresh twice; use KV if that matters
const DEADLINE_MIN_INTERVAL_MS = 20000;

async function hmacHex(secret, body) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return [...new Uint8Array(await crypto.subtle.sign('HMAC', key, body))].map((b) => b.toString(16).padStart(2, '0')).join('');
}
function same(a, b) { // constant-time string compare
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

export async function processEvent(env, data) {
  try {
    const pageId = data.entity?.id;
    if (!pageId) return console.log('Webhook payload missing page id; skipping.');
    if (Date.now() - lastDeadlineUpdate < DEADLINE_MIN_INTERVAL_MS) console.log('Skipped deadline update due to debounce window.');
    else { lastDeadlineUpdate = Date.now(); await updateDeadlines(env); }
    if (data.type !== 'page.created') return console.log('Page edited but not created, skipping page review.');

    const page = await notion(env, `/pages/${pageId}`, { timeout: 10000, retries: 1 });
    console.log('New page created.');
    const review = await chat(env, [{ role: 'system', content: NOTE_PROMPT }, { role: 'user', content: page.properties?.Name?.title?.[0]?.plain_text || 'Untitled' }]);
    const props = { Text: { rich_text: (review.match(/[\s\S]{1,2000}/g) || ['']).map((content) => ({ type: 'text', text: { content } })) } }; // Notion caps each text run at 2,000 chars
    const date = page.properties?.Date?.date?.start;
    if (!page.properties?.['Date Due']?.date?.start && date) props['Date Due'] = { date: { start: date } };
    await notion(env, `/pages/${pageId}`, { method: 'PATCH', body: { properties: props }, timeout: 20000, retries: 1 });
    console.log('Updated the page.');
  } catch (e) { console.log(`[ERROR] webhook background task failed: ${e.message}`); }
}

async function notionWebhook(request, env, ctx) {
  const raw = await request.arrayBuffer();
  let data;
  try { data = JSON.parse(new TextDecoder().decode(raw) || '{}'); } catch { throw new HttpError(400, 'Invalid JSON payload.'); }
  if (data && typeof data === 'object' && 'verification_token' in data) { console.log('[NOTION] verification_token:', data.verification_token); return { status: 'ok' }; }
  if (data && typeof data === 'object' && 'challenge' in data) return { challenge: data.challenge };
  if (!env.NOTION_WEBHOOK_SECRET) { console.log('[NOTION][ERROR] NOTION_WEBHOOK_SECRET not set - rejecting webhook (fail-closed).'); throw new HttpError(503, 'Webhook signature secret not configured'); }
  const sig = request.headers.get('X-Notion-Signature');
  if (!sig || !same('sha256=' + await hmacHex(env.NOTION_WEBHOOK_SECRET, raw), sig)) throw new HttpError(401, 'Invalid signature');
  ctx.waitUntil(processEvent(env, data));
  return { status: 'accepted' };
}

// ---------- token-gated endpoints ----------

function requireToken(request, env) {
  if (!env.TEST_TOKEN) throw new HttpError(503, 'Test endpoint not configured');
  const auth = request.headers.get('authorization');
  if (!auth?.startsWith('Bearer ')) throw new HttpError(401, 'Missing bearer token');
  if (!same(auth.slice(7), env.TEST_TOKEN)) throw new HttpError(401, 'Invalid token');
}
async function body(request, limit = 1e6) {
  const raw = await request.text();
  if (new TextEncoder().encode(raw).length > limit) throw new HttpError(413, 'Request body is too large.');
  let data;
  try { data = JSON.parse(raw); } catch { throw new HttpError(400, 'Invalid JSON payload.'); }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new HttpError(400, 'Body must be a JSON object.');
  return data;
}
const notionFailure = (e) => { throw e instanceof HttpError ? e : new HttpError(e.message?.endsWith(' missing') ? 500 : 502, e.message?.endsWith(' missing') ? e.message : `Notion request failed: ${e.message}`); };

async function createTodo(request, env) {
  const d = await body(request);
  if (!d.name || !String(d.name).trim()) throw new HttpError(400, "'name' is required.");
  const status = 'status' in d ? d.status : 'Inbox';
  const props = { Name: { title: [{ text: { content: String(d.name).trim() } }] } };
  if (d.date_due) props['Date Due'] = { date: { start: d.date_due } };
  if (d.date) props.Date = { date: { start: d.date } };
  if (status) props.Status = { status: { name: status } };
  if (d.text) props.Text = { rich_text: [{ text: { content: d.text } }] };
  if (d.image_url) props.image = { files: [{ name: 'image', type: 'external', external: { url: d.image_url } }] };
  const page = await notion(env, '/pages', { method: 'POST', body: { parent: { database_id: need(env, 'NOTION_TODO_ID') }, properties: props } }).catch(notionFailure);
  return { status: 'created', id: page.id, url: page.url };
}

async function pageTitle(url) { // best effort <title>, else null
  try {
    const r = await fetch(url, { redirect: 'follow', signal: AbortSignal.timeout(10000), headers: { 'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36', accept: 'text/html,application/xhtml+xml' } });
    if (!r.ok || !(r.headers.get('content-type') || '').toLowerCase().includes('html')) return null;
    const m = (await r.text()).match(/<title[^>]*>([\s\S]*?)<\/title>/i);
    return m ? unescapeHtml(m[1]).trim().replace(/\s+/g, ' ') || null : null;
  } catch { return null; }
}
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
const unescapeHtml = (s) => s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => e[0] === '#' ? String.fromCodePoint(e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : +e.slice(1)) : ENTITIES[e.toLowerCase()] ?? m);

async function createLink(request, env) {
  const d = await body(request);
  if (!d.url || !String(d.url).trim()) throw new HttpError(400, "'url' is required.");
  let url = String(d.url).trim();
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  const title = String(d.name || '').trim() || await pageTitle(url) || url;
  const page = await notion(env, '/pages', { method: 'POST', body: { parent: { database_id: need(env, 'NOTION_LINKS_ID') }, properties: {
    Name: { title: [{ text: { content: title.slice(0, 2000) } }] }, URL: { url }, 'Date Added': { date: { start: today(env) } },
  } } }).catch(notionFailure);
  return { status: 'created', id: page.id, url: page.url, title: page.properties?.Name?.title?.[0]?.plain_text ?? null };
}

const plainHttpUrl = (s, pathOk = true) => { try { const u = new URL(s); return ['http:', 'https:'].includes(u.protocol) && u.hostname && !u.username && !u.password && (pathOk || (['', '/'].includes(u.pathname) && !u.search && !u.hash)) ? u : null; } catch { return null; } };

async function captureJob(request, env) { // phone-to-Command-Center relay; Command Center is the record of truth
  const d = await body(request, 32 * 1024);
  if (typeof d.url !== 'string' || !d.url.trim() || d.url.length > 4000) throw new HttpError(400, 'A job URL up to 4000 characters is required.');
  const url = d.url.trim();
  if (!plainHttpUrl(url)) throw new HttpError(400, 'Use a complete http or https job URL without credentials.');
  const payload = { url };
  for (const [name, limit] of [['company', 200], ['role', 300], ['location', 300], ['notes', 10000]]) {
    const value = name in d ? d[name] : name === 'role' ? d.name ?? '' : '';
    if (typeof value !== 'string' || value.length > limit) throw new HttpError(400, `${name} must be text up to ${limit} characters.`);
    payload[name] = value.trim();
  }
  const origin = (env.COMMAND_CENTER_URL || '').replace(/\/+$/, ''), key = env.COMMAND_CENTER_CAPTURE_TOKEN || '';
  if (!plainHttpUrl(origin, false) || !origin.startsWith('https://') || key.length < 32) throw new HttpError(503, 'Command Center capture is not configured.');
  let r, result;
  try { r = await fetch(origin + '/api/jobs', { method: 'POST', redirect: 'manual', signal: AbortSignal.timeout(20000), headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json' }, body: JSON.stringify(payload) }); }
  catch { throw new HttpError(502, 'Could not confirm the save. Retry the same link; Command Center deduplicates it.'); }
  if (![200, 201].includes(r.status)) throw new HttpError(502, 'Command Center did not accept the job. Retry the same link; duplicates are preserved as one record.');
  try { result = await r.json(); } catch { result = null; }
  if (!result || typeof result.id !== 'string' || typeof result.duplicate !== 'boolean') throw new HttpError(502, 'Could not confirm the save. Retry the same link; Command Center deduplicates it.');
  return json({ status: result.duplicate ? 'exists' : 'created', id: result.id, duplicate: result.duplicate, url: origin, title: payload.role || url }, r.status);
}

async function debugItems(env) {
  const day = today(env), out = { due: [], upcoming: [], eventually: [], no_date: [] };
  for (const i of await combinedItems(env)) {
    if (!i.due_date || !validDay(i.due_date)) { out.no_date.push({ title: i.title, raw_date: i.due_date || null, source: i.source }); continue; }
    const days = daysBetween(day, i.due_date.slice(0, 10));
    out[days <= 0 ? 'due' : days <= 7 ? 'upcoming' : 'eventually'].push({ title: i.title, days, source: i.source });
  }
  return { ...out, counts: Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.length])) };
}

// ---------- pages ----------

const page = (title, inner) => new Response(`<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>${title}</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; line-height: 1.5;">${inner}</body>
</html>`, { headers: { 'content-type': 'text/html; charset=utf-8' } });

const HOME = `
  <h1>webhook-server</h1>
  <p>This is a personal automation service that syncs Ralph's Notion workspace with Google Calendar and OpenAI to keep a few
  status blocks up to date. It is not a public product and has no signup.</p>
  <p><a href="/privacy">Privacy Policy</a></p>`;

const PRIVACY = `
  <h1>Privacy Policy</h1>
  <p>Last updated: 2026-11-12</p>
  <p>webhook-server is a personal, single-user automation tool built and operated by Ralph Alcober. It is not distributed to
  or used by the public.</p>
  <h2>What data is accessed</h2>
  <p>This service requests read-only access to one Google Calendar account (<code>calendar.readonly</code> scope) belonging
  solely to the developer. It is used only to read event start times and titles in order to display them inside the
  developer's own Notion workspace.</p>
  <h2>How data is used</h2>
  <p>Calendar event data is read on demand, summarized, and written into Notion callout blocks owned by the developer. It is
  not shared with any third party, sold, or used for advertising.</p>
  <h2>Data retention</h2>
  <p>No calendar data is stored persistently by this service beyond what is needed to render the current Notion block. OAuth
  tokens are stored as encrypted secrets in the developer's own Cloudflare account and used only to refresh access to the
  developer's own calendar.</p>
  <h2>Contact</h2>
  <p>Questions about this service can be directed to alcoberralphael@gmail.com.</p>`;

// ---------- router ----------

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url), m = request.method;
    const route = `${m === 'HEAD' ? 'GET' : m} ${pathname}`;
    try {
      switch (route) {
        case 'GET /': return page('webhook-server', HOME);
        case 'GET /privacy': return page('Privacy Policy — webhook-server', PRIVACY);
        case 'GET /health': return json({ status: 'ok' });
        case 'POST /notion-webhook': return json(await notionWebhook(request, env, ctx));
        case 'POST /todos': requireToken(request, env); return json(await createTodo(request, env));
        case 'POST /links': requireToken(request, env); return json(await createLink(request, env));
        case 'POST /jobs': requireToken(request, env); return await captureJob(request, env);
        case 'GET /debug-items': requireToken(request, env); return json(await debugItems(env));
        case 'GET /test': case 'POST /test':
          requireToken(request, env);
          try { await refreshAll(env); } catch (e) { console.log(e.stack); throw new HttpError(502, e.message); }
          return json({ status: 'ok' });
      }
      const known = ['/', '/privacy', '/health', '/notion-webhook', '/todos', '/links', '/jobs', '/debug-items', '/test'].includes(pathname);
      return json({ detail: known ? 'Method Not Allowed' : 'Not Found' }, known ? 405 : 404);
    } catch (e) {
      if (e instanceof HttpError) return json({ detail: e.message }, e.status);
      console.log(e.stack);
      return json({ detail: 'Internal Server Error' }, 500);
    }
  },
  // Hourly refresh of both callouts (was webhook-refresh.timer + refresh.py on the Pi).
  async scheduled(_event, env) { await refreshAll(env); },
};
