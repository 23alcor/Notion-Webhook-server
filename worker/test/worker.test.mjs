import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import test from 'node:test';
import worker, { countItems, deadlinesCallout, formatEvents, parseTasks, parseTodo, processEvent, today } from '../src/index.js';

const env = {
  NOTION_TOKEN: 'n', NOTION_WEBHOOK_SECRET: 'whsec', TEST_TOKEN: 'test-token', OPENAI_API_KEY: 'o', TIMEZONE: 'America/New_York',
  GOOGLE_CLIENT_ID: 'c', GOOGLE_CLIENT_SECRET: 's', GOOGLE_REFRESH_TOKEN: 'r', OWNER_NAME: 'Alex', EXCLUDED_CALENDARS: 'skip@example.com',
  NOTION_TODO_ID: 'todo', NOTION_TASKS_ID: 'tasks', NOTION_PROJECTS_ID: 'projects', NOTION_LINKS_ID: 'links',
  NOTION_DEADLINE_BLOCK_ID: 'deadline-block', NOTION_IMPORTANT_THINGS_BLOCK_ID: 'important-block',
  COMMAND_CENTER_URL: 'https://cc.example.com', COMMAND_CENTER_CAPTURE_TOKEN: 'k'.repeat(32),
};
const title = (t) => ({ title: [{ plain_text: t }] });
const DB = {
  todo: [
    { id: 't1', properties: { Name: title('Pay rent'), Status: { status: { name: 'Inbox' } }, Done: { checkbox: false }, 'Date Due': { date: { start: '2026-11-12' } } } },
    { id: 't2', properties: { Name: title('Old'), Status: { status: { name: 'Inbox' } }, Done: { checkbox: true } } },
    { id: 't3', properties: { Name: title('Someday'), Status: { status: { name: 'Later' } } } },
  ],
  tasks: [
    { id: 'k1', properties: { Name: title('Write report'), Status: { status: { name: 'In progress' } }, 'Action Date': { date: { start: '2026-11-14' } }, Projects: { relation: [{ id: 'p1' }] } } },
    { id: 'k2', properties: { Name: title('Finished'), Status: { status: { name: 'Done' } } } },
  ],
  projects: [{ id: 'p1', properties: { Name: title('Capstone') } }],
};

function mockFetch(t, { now = Date.parse('2026-11-12T15:00:00Z') } = {}) {
  const calls = [];
  t.mock.timers.enable({ apis: ['Date'], now });
  t.mock.method(globalThis, 'fetch', async (url, opts = {}) => {
    const u = new URL(url), body = opts.body && typeof opts.body === 'string' && opts.body[0] === '{' ? JSON.parse(opts.body) : opts.body;
    calls.push({ url: u, method: opts.method || 'GET', body, headers: opts.headers });
    const ok = (d, status = 200) => new Response(JSON.stringify(d), { status, headers: { 'content-type': 'application/json' } });
    const db = u.pathname.match(/^\/v1\/databases\/(\w+)\/query$/);
    if (db) return ok({ results: DB[db[1]], has_more: false });
    if (u.pathname === '/v1/pages/new-page' && opts.method === 'GET') return ok({ id: 'new-page', properties: { Name: title('Buy a monitor'), Date: { date: { start: '2026-11-12' } } } });
    if (u.pathname === '/v1/pages' && opts.method === 'POST') return ok({ id: 'created', url: 'https://notion.so/created', properties: { Name: title(body.properties.Name.title[0].text.content) } });
    if (u.hostname === 'api.notion.com') return ok({});
    if (u.hostname === 'api.openai.com') return ok({ choices: [{ message: { content: body.messages.length === 2 ? 'Summary '.repeat(400) : 'Sleep by 10pm.' } }] });
    if (u.hostname === 'oauth2.googleapis.com') return ok({ access_token: 'at', expires_in: 3600 });
    if (u.pathname.endsWith('/calendarList')) return ok({ items: [{ id: 'me@example.com' }, { id: 'skip@example.com' }] });
    if (u.pathname.includes('/events')) return ok({ items: [{ summary: 'Lab', start: { dateTime: '2026-11-12T14:30:00-05:00' } }, { summary: 'Holiday', start: { date: '2026-11-12' } }] });
    if (u.hostname === 'cc.example.com') return ok({ id: 'job1', duplicate: false }, 201);
    if (u.hostname === 'example.org') return new Response('<html><title> Cool &amp; Useful\n Page </title></html>', { headers: { 'content-type': 'text/html' } });
    throw new Error('unexpected fetch ' + url);
  });
  return calls;
}
const req = (path, { method = 'GET', body, token, headers = {} } = {}) => new Request('https://api.test' + path, {
  method, headers: { ...(token ? { authorization: 'Bearer ' + token } : {}), ...headers }, ...(body !== undefined ? { body: typeof body === 'string' ? body : JSON.stringify(body) } : {}),
});
const call = async (r) => { const waits = []; const res = await worker.fetch(r, env, { waitUntil: (p) => waits.push(p) }); await Promise.all(waits); return res; };

test('pages, health, 404 and 405', async () => {
  assert.equal((await (await call(req('/health'))).json()).status, 'ok');
  assert.match(await (await call(req('/privacy'))).text(), /calendar\.readonly/);
  assert.equal((await call(req('/nope'))).status, 404);
  assert.equal((await call(req('/todos'))).status, 405);
});

test('webhook: handshake, signature check, then background processing', async (t) => {
  const calls = mockFetch(t);
  assert.deepEqual(await (await call(req('/notion-webhook', { method: 'POST', body: { verification_token: 'v' } }))).json(), { status: 'ok' });
  assert.deepEqual(await (await call(req('/notion-webhook', { method: 'POST', body: { challenge: 'c' } }))).json(), { challenge: 'c' });
  assert.equal((await call(req('/notion-webhook', { method: 'POST', body: '{nope' }))).status, 400);

  const raw = JSON.stringify({ type: 'page.created', entity: { id: 'new-page' } });
  assert.equal((await call(req('/notion-webhook', { method: 'POST', body: raw, headers: { 'X-Notion-Signature': 'sha256=bad' } }))).status, 401);
  const sig = 'sha256=' + createHmac('sha256', 'whsec').update(raw).digest('hex');
  const res = await call(req('/notion-webhook', { method: 'POST', body: raw, headers: { 'X-Notion-Signature': sig } }));
  assert.deepEqual(await res.json(), { status: 'accepted' });

  const deadline = calls.find((c) => c.url.pathname === '/v1/blocks/deadline-block');
  assert.ok(deadline, 'deadline callout refreshed');
  const patch = calls.find((c) => c.url.pathname === '/v1/pages/new-page' && c.method === 'PATCH');
  assert.equal(patch.body.properties.Text.rich_text.length, 2, 'long summary split into 2,000-char runs');
  assert.deepEqual(patch.body.properties['Date Due'], { date: { start: '2026-11-12' } }, 'Date copied to Date Due');

  // An edit right after: debounced, and no AI review.
  const before = calls.length;
  await processEvent(env, { type: 'page.properties_updated', entity: { id: 'new-page' } });
  assert.equal(calls.length, before);
});

test('parsers and deadline callout', () => {
  const items = [...parseTodo(DB.todo), ...parseTasks(DB.tasks, { p1: 'Capstone' })];
  assert.deepEqual(items.map((i) => i.title), ['Pay rent', 'Write report']);
  const callout = deadlinesCallout(items, '2026-11-12');
  assert.deepEqual(callout.map((r) => r.text.content), ['0 - Pay rent\n', '\n', 'Capstone\n', '2 - Write report\n']);
  assert.deepEqual(callout.map((r) => r.annotations?.color), ['red', 'default', 'default', 'orange']);
  assert.equal(deadlinesCallout([], '2026-11-12')[0].text.content, 'No deadlines yet.');
  assert.deepEqual(countItems([{ due_date: null }, { due_date: '2026-11-15' }, { due_date: '2026-12-30' }, { due_date: 'bad' }], '2026-11-12'), { due: 1, upcoming: 1, eventually: 2 });
});

test('time zone: today and event times use the owner time zone, not UTC', (t) => {
  assert.equal(today(env, Date.parse('2026-11-13T03:00:00Z')), '2026-11-12', '10 PM in New York is still the 12th');
  assert.equal(formatEvents([{ summary: 'Lab', start: { dateTime: '2026-11-12T19:30:00Z' } }, { summary: 'Lab', start: { dateTime: '2026-11-12T19:30:00Z' } }], 'America/New_York'), '2:30pm - Lab');
  assert.equal(formatEvents([], 'America/New_York'), 'nothing scheduled');
});

test('/test refreshes both callouts, skipping excluded calendars', async (t) => {
  const calls = mockFetch(t, { now: Date.parse('2026-11-12T23:30:00Z') }); // 6:30 PM in New York: tomorrow + sleep line
  assert.equal((await call(req('/test', { method: 'POST' }))).status, 401);
  const res = await call(req('/test', { method: 'POST', token: 'test-token' }));
  assert.equal(res.status, 200);
  const important = calls.find((c) => c.url.pathname === '/v1/blocks/important-block').body.callout.rich_text.map((r) => r.text.content).join('');
  assert.match(important, /^Day is almost over Alex\n\nHere's what you have tomorrow\.\nHoliday\n2:30pm - Lab/);
  assert.match(important, /You have 1 due tasks\.\nYou have 1 upcoming tasks\.\nYou have 0 tasks eventually\.\n\nSleep by 10pm\./);
  const events = calls.filter((c) => c.url.pathname.includes('/events'));
  assert.equal(events.length, 1, 'excluded calendar skipped');
  assert.equal(events[0].url.searchParams.get('timeMin'), '2026-11-13T05:00:00.000Z', 'tomorrow starts at local midnight');
});

test('/todos, /links and /jobs', async (t) => {
  const calls = mockFetch(t);
  assert.equal((await call(req('/todos', { method: 'POST', body: {}, token: 'test-token' }))).status, 400);
  const todo = await (await call(req('/todos', { method: 'POST', body: { name: ' Call mom ', date_due: '2026-11-20' }, token: 'test-token' }))).json();
  assert.deepEqual(todo, { status: 'created', id: 'created', url: 'https://notion.so/created' });
  assert.equal(calls.at(-1).body.properties.Status.status.name, 'Inbox');

  const link = await (await call(req('/links', { method: 'POST', body: { url: 'example.org/x' }, token: 'test-token' }))).json();
  assert.equal(link.title, 'Cool & Useful Page');
  assert.equal(calls.at(-1).body.properties.URL.url, 'https://example.org/x');

  assert.equal((await call(req('/jobs', { method: 'POST', body: { url: 'ftp://x' }, token: 'test-token' }))).status, 400);
  assert.equal((await call(req('/jobs', { method: 'POST', body: { url: 'https://x.com', notes: 'n'.repeat(40000) }, token: 'test-token' }))).status, 413);
  const job = await call(req('/jobs', { method: 'POST', body: { url: 'https://jobs.example.com/1', name: 'GPU Engineer' }, token: 'test-token' }));
  assert.equal(job.status, 201);
  assert.deepEqual(await job.json(), { status: 'created', id: 'job1', duplicate: false, url: 'https://cc.example.com', title: 'GPU Engineer' });
  assert.equal(calls.at(-1).headers.authorization, 'Bearer ' + 'k'.repeat(32));
});
