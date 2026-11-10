// Execute the actual app/modules with deferred responses and a minimal DOM.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';

const [root, scenario] = process.argv.slice(2);
class Element {
  constructor(id = '') {
    this.id = id;
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.listeners = {};
    this.textContent = '';
    this.value = '';
    this.hidden = false;
    const classes = new Set();
    this.classList = {
      add: (...values) => values.forEach(value => classes.add(value)),
      remove: (...values) => values.forEach(value => classes.delete(value)),
      contains: value => classes.has(value),
      toggle: (value, force) => force ? classes.add(value) : classes.delete(value),
    };
  }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html || ''; }
  setAttribute(key, value) { this.attributes.set(key, value); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  removeAttribute(key) { this.attributes.delete(key); }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  querySelector() { return new Element(); }
  appendChild(child) { child.parentNode = this; this.children.push(child); }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(x => x !== this);
    this.parentNode = null;
  }
}

const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
};
const views = ['view-upload', 'view-jobs', 'view-report'].map(el);
el('view-upload').classList.add('active');
el('report-done-content').innerHTML = 'PROTOTYPE CONTENT ONLY';
const timers = new Map();
const deadlines = new Map();
const storage = new Map();
let timerId = 0;
let fetchImpl = () => Promise.resolve(response({jobs: []}));
const context = vm.createContext({
  console,
  document: {
    getElementById: el,
    querySelector: selector => el(selector),
    querySelectorAll: selector => selector === '.view' ? views : [],
    createElement: () => new Element(),
  },
  window: {
    location: {origin: 'http://fixture.test'},
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setTimeout: (callback, delay) => { const id = ++timerId; deadlines.set(id, {callback, delay}); return id; },
    clearTimeout: id => deadlines.delete(id),
  },
  URL: {createObjectURL: () => 'blob:fixture', revokeObjectURL() {}},
  FormData: class { append() {} },
  AbortController,
  fetch: (...args) => fetchImpl(...args),
  setTimeout: () => 0,
  setInterval: callback => { const id = ++timerId; timers.set(id, callback); return id; },
  clearInterval: id => timers.delete(id),
});

function response(body, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => body};
}
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}
function raw(id, status = 'running', extra = {}) {
  return {id, status, name: id, workspace_id: 'local', ...extra};
}
const modules = new Map();
async function moduleAt(filename) {
  if (modules.has(filename)) return modules.get(filename);
  let source = await fs.readFile(filename, 'utf8');
  if (filename === path.join(root, 'frontend/app.js')) {
    assert.match(source, /\}\)\(\);\s*$/);
    source = source.replace(/\}\)\(\);\s*$/, `
      globalThis.hooks = {apiJson, mapJob, loadJobs, refreshJob, startPolling, stopPolling,
        openReport, renderReportShell, mergeJob, uploadState, getJobs: () => jobs,
        loadReportAudience, resetReportFrameState, reportAudienceState, goTab};
    })();`);
  }
  const module = new vm.SourceTextModule(source, {context, identifier: filename});
  modules.set(filename, module);
  await module.link((specifier, parent) => moduleAt(path.resolve(path.dirname(parent.identifier), specifier)));
  return module;
}
const app = await moduleAt(path.join(root, 'frontend/app.js'));
await app.evaluate();
const flush = () => new Promise(resolve => setImmediate(resolve));
await flush();
const h = context.hooks;
const text = node => node.textContent + node.innerHTML + node.children.map(text).join('');

function reportContext(job, audience = 'internal') {
  h.resetReportFrameState();
  const shell = new Element(), empty = new Element(), loading = new Element();
  const previous = audience ? new Element() : null;
  if (previous) shell.appendChild(previous);
  shell.querySelector = selector => selector === '.report-frame-empty' ? empty
    : selector === '.report-frame-loading' ? loading : previous;
  Object.assign(h.reportAudienceState, {jobId: job.id, shell, activeFrame: previous, activeAudience: audience});
  return {shell, empty, loading, previous};
}
const headResponse = (status = 200, type = 'text/html') => ({ok: status === 200, headers: {get: () => type}});

const cases = {
  async api_errors() {
    fetchImpl = () => Promise.resolve({ok: true, status: 200, json: async () => { throw new Error('truncated'); }});
    await assert.rejects(h.apiJson('/api/jobs'));
    for (const body of [null, [], 'wrong', 12]) {
      fetchImpl = () => Promise.resolve(response(body));
      await assert.rejects(h.apiJson('/api/jobs'));
    }
    fetchImpl = () => Promise.resolve(response({error: 'request rejected'}, 503));
    await assert.rejects(h.apiJson('/api/jobs'), /request rejected/);
    fetchImpl = () => Promise.resolve(response({jobs: []}));
    assert.equal((await h.apiJson('/api/jobs')).jobs.length, 0);
  },
  async entity_contracts() {
    for (const body of [{}, {id: '', status: 'queued'}, {id: 42, status: 'queued'}, {id: 'a', status: 'unknown'}]) {
      assert.throws(() => h.mapJob(body));
    }
    for (const status of ['queued', 'running', 'completed', 'degraded', 'failed']) {
      assert.equal(h.mapJob(raw('valid', status)).id, 'valid');
    }
    h.mergeJob(h.mapJob(raw('keep')));
    fetchImpl = () => Promise.resolve(response({}));
    await h.loadJobs();
    assert.equal(h.getJobs()[0].id, 'keep', 'invalid list cleared previous jobs');
    fetchImpl = () => Promise.resolve(response(raw('wrong-id', 'completed')));
    await h.refreshJob('keep', true);
    assert.equal(h.getJobs().length, 1, 'mismatched detail inserted a different job');
  },
  async poll_selection() {
    h.mergeJob(h.mapJob(raw('a')));
    h.mergeJob(h.mapJob(raw('b')));
    h.startPolling('a');
    const old = deferred();
    fetchImpl = () => old.promise;
    const pending = h.refreshJob('a', true);
    h.startPolling('b');
    old.resolve(response(raw('a', 'completed')));
    await pending;
    assert.equal(timers.size, 1, 'old job stopped current job polling');
    h.stopPolling();
    assert.equal(timers.size, 0);
  },
  async poll_order() {
    const first = deferred(), second = deferred();
    let calls = 0;
    fetchImpl = () => (++calls === 1 ? first.promise : second.promise);
    const old = h.refreshJob('same', true);
    const current = h.refreshJob('same', true);
    second.resolve(response(raw('same', 'completed')));
    await current;
    first.resolve(response(raw('same', 'running')));
    await old;
    assert.equal(h.getJobs()[0].status, 'done', 'old response regressed terminal state');
  },
  async poll_slow() {
    h.mergeJob(h.mapJob(raw('slow', 'running')));
    h.startPolling('slow');
    const requests = [];
    fetchImpl = () => {
      const request = deferred();
      requests.push(request);
      return request.promise;
    };
    const tick = () => Array.from(timers.values()).forEach(callback => callback());
    for (let i = 0; i < 6; i++) {
      tick();
      if (i > 0) {
        requests[i - 1].resolve(response(raw('slow', 'running', {progress: i * 10})));
        await flush();
      }
    }
    assert.equal(h.getJobs()[0].progress, 50, 'slow successful polling made no progress');
    tick();
    requests[5].resolve(response(raw('slow', 'completed')));
    await flush();
    assert.equal(h.getJobs()[0].status, 'done', 'a later in-flight request hid completion');
    assert.equal(timers.size, 0, 'slow completion did not stop polling');
    requests[6].resolve(response(raw('slow', 'running', {progress: 60})));
    await flush();
    assert.equal(h.getJobs()[0].status, 'done', 'a late active snapshot reopened a completed job');
    assert.equal(timers.size, 0);
    fetchImpl = () => Promise.resolve(response(raw('slow', 'failed')));
    await h.refreshJob('slow', true);
    assert.equal(h.getJobs()[0].status, 'failed', 'terminal correction was incorrectly blocked');
  },
  async list_order() {
    const late = deferred();
    fetchImpl = () => late.promise;
    const pending = h.loadJobs();
    h.mergeJob(h.mapJob(raw('new-upload', 'queued')));
    late.resolve(response({jobs: []}));
    await pending;
    assert.equal(h.getJobs()[0].id, 'new-upload', 'late list removed accepted job');
    const first = deferred(), second = deferred();
    let calls = 0;
    fetchImpl = () => (++calls === 1 ? first.promise : second.promise);
    const older = h.loadJobs(), newer = h.loadJobs();
    second.resolve(response({jobs: [raw('newer')]}));
    await newer;
    first.resolve(response({jobs: [raw('older')]}));
    await older;
    assert.equal(h.getJobs()[0].id, 'newer');
  },
  async list_partial_update() {
    h.mergeJob(h.mapJob(raw('a', 'running')));
    const detail = deferred(), list = deferred();
    let calls = 0;
    fetchImpl = () => ++calls === 1 ? detail.promise : list.promise;
    const olderDetail = h.refreshJob('a', true);
    const newerList = h.loadJobs();
    detail.resolve(response(raw('a', 'running', {progress: 63})));
    await olderDetail;
    list.resolve(response({jobs: [raw('a', 'running', {progress: 12}), raw('b', 'completed')]}));
    await newerList;
    assert.equal(h.getJobs().length, 2, 'a detail update discarded unrelated list jobs');
    assert.equal(h.getJobs().find(job => job.id === 'b').status, 'done');
    assert.equal(h.getJobs().find(job => job.id === 'a').progress, 63, 'list overwrote a local detail update');
  },
  async detail_after_list() {
    h.mergeJob(h.mapJob(raw('cross', 'running')));
    const detail = deferred(), list = deferred();
    fetchImpl = url => url === '/api/jobs' ? list.promise : detail.promise;
    const older = h.refreshJob('cross', true), newer = h.loadJobs();
    list.resolve(response({jobs: [raw('cross', 'completed')]}));
    await newer;
    detail.resolve(response(raw('cross', 'failed')));
    await older;
    assert.equal(h.getJobs()[0].status, 'done', 'old detail replaced a newer list result');
    fetchImpl = () => Promise.resolve(response(raw('cross', 'failed')));
    await h.refreshJob('cross', true);
    assert.equal(h.getJobs()[0].status, 'failed', 'fresh terminal correction was blocked');
  },
  async removed_job_detail() {
    h.mergeJob(h.mapJob(raw('removed', 'running')));
    const detail = deferred(), list = deferred();
    fetchImpl = url => url === '/api/jobs' ? list.promise : detail.promise;
    const older = h.refreshJob('removed', true), newer = h.loadJobs();
    list.resolve(response({jobs: []}));
    await newer;
    detail.resolve(response(raw('removed', 'running')));
    await older;
    assert.equal(h.getJobs().length, 0, 'old detail resurrected a removed job');
  },
  async list_terminal_state() {
    h.mergeJob(h.mapJob(raw('terminal', 'completed')));
    fetchImpl = () => Promise.resolve(response({jobs: [raw('terminal', 'running')]}));
    await h.loadJobs();
    assert.equal(h.getJobs()[0].status, 'done', 'list reopened a known terminal job');
    fetchImpl = () => Promise.resolve(response({jobs: [raw('terminal', 'failed')]}));
    await h.loadJobs();
    assert.equal(h.getJobs()[0].status, 'failed', 'list could not correct a terminal result');
  },
  async report_timeout() {
    const job = h.mapJob(raw('report-timeout', 'completed', {bd_report_url: '/bd', creator_report_url: '/creator'}));
    const view = reportContext(job);
    const pending = deferred();
    let signal, calls = 0;
    fetchImpl = (_url, options) => { calls++; signal = options.signal; return pending.promise; };
    h.loadReportAudience(job, 'creator');
    const frame = view.shell.children.at(-1);
    const deadline = [...deadlines.entries()].find(([, item]) => item.delay === 30000);
    assert.ok(deadline, 'report load has no bounded failure transition');
    deadlines.delete(deadline[0]);
    deadline[1].callback();
    assert.equal(signal.aborted, true);
    assert.equal(h.reportAudienceState.activeFrame, view.previous);
    assert.equal(view.shell.getAttribute('aria-busy'), 'false');
    assert.equal(view.loading.hidden, true);
    pending.resolve(headResponse());
    await flush();
    frame.onload();
    assert.equal(h.reportAudienceState.activeFrame, view.previous, 'late response defeated timeout');
    assert.equal(calls, 1, 'timeout automatically retried the request');
  },
  async report_cancel_and_success() {
    const job = h.mapJob(raw('report-old', 'completed', {bd_report_url: '/old', creator_report_url: '/old-creator'}));
    const view = reportContext(job, null), pending = deferred();
    let signal;
    fetchImpl = (_url, options) => { signal = options.signal; return pending.promise; };
    h.loadReportAudience(job, 'creator');
    const abandoned = view.shell.children.at(-1);
    const next = h.mapJob(raw('report-next', 'completed', {bd_report_url: '/next', creator_report_url: '/next-creator'}));
    const current = reportContext(next, null);
    assert.equal(signal.aborted, true, 'switching jobs did not cancel report loading');
    fetchImpl = () => Promise.resolve(headResponse());
    h.loadReportAudience(next, 'creator');
    await flush();
    const frame = current.shell.children.at(-1);
    frame.contentDocument = {contentType: 'text/html'};
    frame.onload();
    assert.equal(h.reportAudienceState.activeFrame, frame);
    assert.equal(h.reportAudienceState.activeAudience, 'creator');
    pending.resolve(headResponse());
    await flush();
    abandoned.onload();
    abandoned.onerror();
    frame.onload();
    assert.equal(h.reportAudienceState.activeFrame, frame, 'stale or duplicate callback changed the active frame');
    assert.equal(frame.parentNode, current.shell);
    assert.equal([...deadlines.values()].some(item => item.delay === 30000), false);
  },
  async report_leave_loading() {
    for (const destination of ['running', 'failed', 'jobs', 'upload']) {
      const job = h.mapJob(raw('loading-' + destination, 'completed', {bd_report_url: '/bd', creator_report_url: '/creator'}));
      const view = reportContext(job, null), pending = deferred();
      let signal;
      fetchImpl = (_url, options) => {
        if (options?.method === 'HEAD') { signal = options.signal; return pending.promise; }
        return Promise.resolve(response({jobs: []}));
      };
      h.loadReportAudience(job, 'creator');
      const frame = view.shell.children.at(-1);
      if (destination === 'jobs' || destination === 'upload') h.goTab(destination);
      else {
        const next = h.mapJob(raw('next-' + destination, destination));
        h.mergeJob(next);
        fetchImpl = (_url, options) => options?.method === 'HEAD' ? pending.promise : Promise.resolve(response(raw(next.id, destination)));
        h.openReport(next.id);
      }
      assert.equal(signal.aborted, true, 'leaving a report did not cancel its pending request');
      assert.equal(h.reportAudienceState.jobId, null);
      const message = el('toast-text').textContent;
      pending.resolve(headResponse());
      await flush();
      frame.onerror();
      assert.equal(el('toast-text').textContent, message, 'old report emitted a failure in another view');
      h.stopPolling();
    }
  },
  async report_rejection() {
    const job = h.mapJob(raw('report-errors', 'completed', {bd_report_url: '/bd', creator_report_url: '/creator'}));
    for (const failure of ['head', 'mime', 'frame']) {
      const view = reportContext(job);
      fetchImpl = () => Promise.resolve(headResponse(failure === 'head' ? 404 : 200, failure === 'mime' ? 'application/pdf' : 'text/html'));
      h.loadReportAudience(job, 'creator');
      const frame = view.shell.children.at(-1);
      await flush();
      if (failure === 'frame') frame.onerror();
      assert.equal(h.reportAudienceState.activeFrame, view.previous);
      assert.equal(h.reportAudienceState.selectedAudience, 'internal');
      assert.equal(view.shell.getAttribute('aria-busy'), 'false');
    }
  },
  async missing_report() {
    for (const status of ['completed', 'degraded']) {
      h.renderReportShell(h.mapJob(raw('real-job', status)));
      const content = text(el('report-done-content'));
      assert.ok(!content.includes('PROTOTYPE CONTENT ONLY'), 'real task rendered prototype data');
      assert.ok(!content.includes('差距等级'), 'real task rendered fabricated grades');
      assert.ok(content.includes('报告'), 'missing report has no visible status');
    }
    h.renderReportShell(h.mapJob(raw('with-report', 'completed', {bd_report_url: '/real-report.html'})));
    assert.equal(el('report-done-content').getAttribute('data-real-job'), 'with-report');
  },
  async upload_protocol() {
    h.uploadState.files.benchmark = {name: 'benchmark.mp4'};
    h.uploadState.files.creator = {name: 'creator.mp4'};
    el('btn-start').disabled = false;
    fetchImpl = () => Promise.resolve({ok: true, status: 200, json: async () => { throw new Error('truncated'); }});
    el('btn-start').listeners.click();
    await flush();
    assert.ok(el('toast-text').textContent.includes('提交未确认'));
    assert.equal(el('btn-start').disabled, false);
    assert.equal(h.getJobs().length, 0, 'malformed acknowledgment created a task');
    assert.ok(h.uploadState.files.creator, 'failed upload reset selected inputs');
    fetchImpl = url => Promise.resolve(url === '/api/jobs'
      ? response(raw('accepted', 'queued'), 202) : response(raw('accepted')));
    el('btn-start').listeners.click();
    await flush();
    assert.equal(h.getJobs()[0].id, 'accepted');
    assert.equal(h.uploadState.files.creator, null);
  },
  async upload_singleflight() {
    const creator = {name: 'creator.mp4', type: 'video/mp4', size: 2};
    h.uploadState.files.benchmark = {name: 'benchmark.mp4'};
    h.uploadState.files.creator = creator;
    el('btn-start').disabled = false;
    const pending = deferred();
    let calls = 0;
    fetchImpl = () => { calls += 1; return pending.promise; };
    el('btn-start').listeners.click();
    await flush();
    el('input-creator').listeners.change({target: {files: [{...creator, name: 'replacement.mp4'}]}});
    el('btn-start').listeners.click();
    await flush();
    assert.equal(calls, 1, 'changing a file enabled duplicate submission');
    assert.equal(h.uploadState.files.creator, creator, 'pending input was replaced');
    pending.resolve(response({error: 'rejected'}, 503));
    await flush();
    assert.equal(el('btn-start').disabled, false);
  },
};
assert.ok(cases[scenario], 'unknown scenario');
await cases[scenario]();
console.log(JSON.stringify({scenario, passed: true}));
