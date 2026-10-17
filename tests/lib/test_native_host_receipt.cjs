const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const repl = require('node:repl');
const {PassThrough} = require('node:stream');
const {saveNativeHostReceipt, nativeImageURLSha256, consumeNativeHostAction, consumeSavedNativeCompletion, canonicalProductionStep} = require('../../scripts/native_host_receipt.js');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type), data]);
  let crc = 0xffffffff;
  for (const byte of body) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  const size = Buffer.alloc(4), check = Buffer.alloc(4);
  size.writeUInt32BE(data.length); check.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
  return Buffer.concat([size, body, check]);
}
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'native-host-receipt-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const imagePath = path.join(root, '.codex/generated_images/child/exec-test.png');
  await fs.mkdir(path.dirname(imagePath), {recursive: true});
  // A valid multi-megabyte PNG, not a huge JSON-shaped mock or an external fixture.
  const header = Buffer.alloc(13); header.writeUInt32BE(1024, 0); header.writeUInt32BE(1024, 4);
  header[8] = 8; header[9] = 2;
  const pixels = crypto.randomBytes(1024 * (1024 * 3 + 1));
  for (let row = 0; row < 1024; row++) pixels[row * (1024 * 3 + 1)] = 0;
  const image = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), chunk('IHDR', header),
    chunk('IDAT', zlib.deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
  await fs.writeFile(imagePath, image);
  const observation = {tool_name: 'image_gen.imagegen', arguments: {prompt: 'Exact café prompt', transparent_background: false},
    result: {image_url: 'data:image/png;base64,' + image.toString('base64'),
      output_hint: `Generated images are saved to ${path.dirname(imagePath)} as ${imagePath} by default.\nLeave the original in place.`}, tool_call_id: 'actual-host-call'};
  observation.extra = JSON.parse('{"__proto__":{"retained":"JSON field"}}');
  const receiptPath = path.join(root, 'receipt.json');
  const calls = [];
  const tools = {mcp__node_repl__js: async request => {
    calls.push(request);
    let output;
    await new AsyncFunction('nodeRepl', request.code)({write: item => output = item});
    return {content: [{type: 'text', text: typeof output === 'string' ? output : JSON.stringify(output)}]};
  }};
  return {root, imagePath, image, observation, receiptPath, tools, calls};
}

const doneStep = {status: 'host_action', action: {kind: 'prepare_motion', subject: 'same', action_id: 'd'.repeat(64)}};
test('ASCII fingerprint matches standard SHA-256 across block and native-result boundaries', () => {
  for (const size of [0, 3, 55, 56, 63, 64, 65, 1024, 3336502]) {
    const text = 'data:image/png;base64,' + 'Ab0+/='.repeat(Math.ceil(size / 6)).slice(0, size);
    assert.equal(nativeImageURLSha256(text), crypto.createHash('sha256').update(text).digest('hex'));
  }
});
test('multi-MB observation is persisted losslessly through one tiny RPC and remains recoverable', async t => {
  const f = await fixture(t);
  const saved = await saveNativeHostReceipt(f.tools, f.observation, f.receiptPath);
  assert.ok(f.observation.result.image_url.length > 4_000_000);
  assert.equal(f.calls.length, 1); assert.ok(f.calls[0].code.length < 5000);
  const raw = await fs.readFile(f.receiptPath);
  const binding = JSON.parse(saved.content[0].text);
  assert.deepEqual(JSON.parse(raw), f.observation);
  assert.equal(binding.sha256, crypto.createHash('sha256').update(raw).digest('hex'));
  assert.equal(binding.bytes, raw.length);
  assert.deepEqual(Buffer.from(JSON.parse(raw).result.image_url.split(',')[1], 'base64'), f.image);
  assert.ok(JSON.stringify(saved).length < 500);
});
test('optional bridge clocks separate host work and Node persistence without changing the original', async t => {
  const f = await fixture(t), events = [];
  let clock = 1000;
  t.mock.method(Date, 'now', () => ++clock);
  const options = hostOptions(f, async () => f.observation.result, async () => doneStep);
  options.observeTiming = event => events.push(event);
  assert.deepEqual(await consumeNativeHostAction(f.tools, imageStep(f), options),
    {...doneStep, provider_calls: null});
  assert.deepEqual(events.map(event => event.stage), ['native_call', 'native_return', 'retention_start', 'retention_end', 'native_classified',
    'receipt_prepare_start', 'receipt_prepare_end', 'receipt_rpc_start', 'receipt_rpc_end',
    'node_receipt', 'completion_start', 'completion_end']);
  assert.ok(events.every(event => event.subject === 'original-call' && Number.isSafeInteger(event.at_ms)));
  const node = events.find(event => event.stage === 'node_receipt').clocks;
  assert.deepEqual(Object.keys(node), ['start', 'image_read_end', 'image_hash_end', 'write_start', 'write_end', 'sync_end']);
  assert.ok(Object.values(node).every(Number.isSafeInteger));
  assert.deepEqual(Object.values(node), Object.values(node).slice().sort((a, b) => a - b));
  const original = JSON.parse(await fs.readFile(f.receiptPath));
  assert.equal(original.result.image_url, f.observation.result.image_url);
  assert.ok(!('timing' in original));
  assert.equal(f.calls.length, 2);
  assert.ok(f.calls.every(request => request.code.length < 5000));
});
test('diagnostic observer failure does not interrupt original retention or completion', async t => {
  const f = await fixture(t);
  const options = hostOptions(f, async () => f.observation.result, async () => doneStep);
  options.observeTiming = () => { throw new Error('diagnostic sink unavailable'); };
  assert.deepEqual(await consumeNativeHostAction(f.tools, imageStep(f), options),
    {...doneStep, provider_calls: null});
  assert.equal(JSON.parse(await fs.readFile(f.receiptPath)).result.image_url, f.observation.result.image_url);
});
test('receipt diagnostics preserve exact bytes and missing RPC replies leave unmatched clocks', async t => {
  const f = await fixture(t), events = [];
  const original = await saveNativeHostReceipt(f.tools, f.observation, f.receiptPath);
  assert.deepEqual(Object.keys(JSON.parse(original.content[0].text)), ['path', 'bytes', 'sha256']);
  const before = await fs.readFile(f.receiptPath);
  await saveNativeHostReceipt(f.tools, f.observation, f.receiptPath, event => events.push(event));
  assert.deepEqual(await fs.readFile(f.receiptPath), before);
  assert.ok(events.some(event => event.stage === 'node_receipt'));
  events.length = 0;
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => { await rpc(request); throw new Error('reply lost'); };
  await assert.rejects(saveNativeHostReceipt(f.tools, f.observation, f.receiptPath,
    event => events.push(event)), /reply lost/);
  assert.deepEqual(events.map(event => event.stage),
    ['receipt_prepare_start', 'receipt_prepare_end', 'receipt_rpc_start']);
  assert.deepEqual(await fs.readFile(f.receiptPath), before);
});
test('lost save response resumes the same original without another host call or receipt overwrite', async t => {
  const f = await fixture(t), rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => { await rpc(request); throw new Error('lost response'); };
  await assert.rejects(saveNativeHostReceipt(f.tools, f.observation, f.receiptPath), /lost response/);
  const original = await fs.readFile(f.receiptPath);
  f.tools.mcp__node_repl__js = rpc;
  await saveNativeHostReceipt(f.tools, f.observation, f.receiptPath);
  assert.deepEqual(await fs.readFile(f.receiptPath), original);
});
test('changed cached bytes and a colliding original fail closed', async t => {
  const f = await fixture(t);
  await fs.appendFile(f.imagePath, 'different');
  await assert.rejects(saveNativeHostReceipt(f.tools, f.observation, f.receiptPath), /differs/);
  await assert.rejects(fs.readFile(f.receiptPath), {code: 'ENOENT'});
  await fs.writeFile(f.imagePath, f.image);
  await fs.writeFile(f.receiptPath, 'retained original');
  await assert.rejects(saveNativeHostReceipt(f.tools, f.observation, f.receiptPath), /collision/);
  assert.equal(await fs.readFile(f.receiptPath, 'utf8'), 'retained original');
  assert.deepEqual((await fs.readdir(f.root)).filter(name => name.includes('.tmp-')), []);
});
test('missing local output and host transport errors retain an unresolved observation', async t => {
  const f = await fixture(t);
  await fs.rm(f.imagePath);
  await assert.rejects(saveNativeHostReceipt(f.tools, f.observation, f.receiptPath), {code: 'ENOENT'});
  await assert.rejects(saveNativeHostReceipt(f.tools, {...f.observation, result: {...f.observation.result, output_hint: 'No actual path'}}, f.receiptPath), /no usable local image/);
  await assert.rejects(saveNativeHostReceipt({mcp__node_repl__js: async () => ({isError: true})}, f.observation, f.receiptPath), /Receipt saving failed/);
});

function hostOptions(f, invoke, completeAction) {
  const originals = new Map();
  return {
    nativeTool: {tool_name: 'image_gen.imagegen', invoke}, receiptPath: f.receiptPath,
    getObservation: key => originals.get(key),
    retainObservation: (key, value) => originals.set(key, value), completeAction,
  };
}
function imageStep(f) {
  return {status: 'host_action', action: {kind: 'receive_native_image', subject: 'original-call', action_id: 'unchanged'},
    host_envelope: {tool_name: 'image_gen.imagegen', call_id: 'original-call', arguments: f.observation.arguments}};
}
test('invalid Node bridge fails before ownership effects and corrected bridge can invoke once', async t => {
  const f = await fixture(t), step = imageStep(f);
  let nativeCalls = 0, observationReads = 0, retentions = 0, completions = 0;
  const options = hostOptions(f, async () => { nativeCalls++; return f.observation.result; }, async () => {
    completions++; return doneStep;
  });
  const getObservation = options.getObservation, retainObservation = options.retainObservation;
  options.getObservation = (...args) => { observationReads++; return getObservation(...args); };
  options.retainObservation = (...args) => { retentions++; return retainObservation(...args); };
  for (const invalid of [undefined, null, {}, {mcp__node_repl__js: null}, {mcp__node_repl__js: {}}]) {
    await assert.rejects(consumeNativeHostAction(invalid, step, options), /Node REPL bridge/);
    assert.equal(observationReads, 0); assert.equal(retentions, 0);
    assert.equal(nativeCalls, 0); assert.equal(completions, 0);
    await assert.rejects(fs.access(f.receiptPath + '.invoked'), {code: 'ENOENT'});
  }
  const timingEvents = [];
  await assert.rejects(saveNativeHostReceipt({}, f.observation, f.receiptPath,
    event => timingEvents.push(event)), /Node REPL bridge/);
  assert.deepEqual(timingEvents, []); assert.equal(f.calls.length, 0);
  await consumeNativeHostAction(f.tools, step, options);
  assert.equal(nativeCalls, 1); assert.equal(completions, 1);
  const restarted = hostOptions(f, options.nativeTool.invoke, options.completeAction);
  await assert.rejects(consumeNativeHostAction(f.tools, step, restarted), /already exists/);
  assert.equal(nativeCalls, 1); assert.equal(completions, 1);
});
test('actual image callback, durable lossless original and completion share one operation', async t => {
  const f = await fixture(t), step = imageStep(f);
  let nativeCalls = 0, completionCalls = 0;
  const next = {status: 'host_action', action: {kind: 'receive_native_image', subject: 'next-original', action_id: 'n'.repeat(64)},
    host_envelope: {call_id: 'next-original', tool_name: 'image_gen.imagegen', arguments: {prompt: 'next'}}};
  const options = hostOptions(f, async args => {
    nativeCalls++;
    assert.deepEqual(args, f.observation.arguments);
    assert.ok(Object.isFrozen(args));
    return {...f.observation.result, additive_host_field: {kept: ['all', 'fields']}};
  }, async (action, outcome) => {
    completionCalls++;
    assert.strictEqual(action, step.action);
    assert.deepEqual(Object.keys(outcome), ['host_receipt']);
    const raw = await fs.readFile(outcome.host_receipt.path);
    assert.equal(outcome.host_receipt.sha256, crypto.createHash('sha256').update(raw).digest('hex'));
    const retained = JSON.parse(raw);
    assert.deepEqual(retained.result.additive_host_field, {kept: ['all', 'fields']});
    assert.equal(retained.result.image_url, f.observation.result.image_url);
    return next;
  });
  const carried = await consumeNativeHostAction(f.tools, step, options);
  assert.strictEqual(carried.action, next.action); assert.strictEqual(carried.host_envelope, next.host_envelope);
  assert.equal(carried.provider_calls, null);
  assert.equal(nativeCalls, 1); assert.equal(completionCalls, 1);
  assert.equal(f.calls.length, 2); assert.ok(f.calls.every(request => request.code.length < 5000));
  await assert.rejects(consumeNativeHostAction(f.tools, {...next, host_envelope: undefined}, options), /exact emitted/);
  assert.equal(nativeCalls, 1);  // restart's receive-only action cannot resubmit
});
test('saving and completion interruptions recover retained original without paid resubmission', async t => {
  const f = await fixture(t), step = imageStep(f);
  let nativeCalls = 0, completionCalls = 0;
  const options = hostOptions(f, async () => { nativeCalls++; return f.observation.result; }, async () => {
    completionCalls++;
    if (completionCalls === 1) throw new Error('interrupted completion');
    return doneStep;
  });
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => {
    const result = await rpc(request);
    if (request.title === 'Save actual native host receipt') throw new Error('interrupted save response');
    return result;
  };
  await assert.rejects(consumeNativeHostAction(f.tools, step, options), /interrupted save/);
  const original = await fs.readFile(f.receiptPath);
  f.tools.mcp__node_repl__js = rpc;
  await assert.rejects(consumeNativeHostAction(f.tools, step, options), /interrupted completion/);
  assert.deepEqual(await consumeNativeHostAction(f.tools, step, options), {...doneStep, provider_calls: null});
  assert.deepEqual(await fs.readFile(f.receiptPath), original);
  assert.equal(nativeCalls, 1); assert.equal(completionCalls, 2);
});
test('raised image exception is durable, exact and never triggers another invocation', async t => {
  const f = await fixture(t), step = imageStep(f);
  let calls = 0;
  const options = hostOptions(f, async () => { calls++; throw new TypeError('Actual transport café failure'); }, async (action, outcome) => {
    assert.strictEqual(action, step.action);
    const retained = JSON.parse(await fs.readFile(outcome.host_exception.path));
    assert.deepEqual(retained, {tool_name: 'image_gen.imagegen', arguments: f.observation.arguments,
      call_id: 'original-call', exception: {message: 'Actual transport café failure', type: 'TypeError'}});
    return {status: 'host_action', action};
  });
  await consumeNativeHostAction(f.tools, step, options);
  await consumeNativeHostAction(f.tools, step, options);
  assert.equal(calls, 1);
});
test('OpenArt return is durably retained before completing, and polling stays a separate boundary', async t => {
  const f = await fixture(t);
  const step = {action: {kind: 'receive_openart_original', subject: 'motion-original'},
    host_envelope: {tool: 'mcp__codex_apps__openart_openart_generate_video', arguments: {prompt: 'actual', model: 'native-model'}}};
  const payload = {historyId: 'actual-job', status: 'PENDING', future_field: ['retained', 1]};
  const result = {content: [{type: 'text', text: JSON.stringify(payload)},
    {type: 'text', text: 'Generation submitted; display-only instructions.'}],
    structuredContent: payload, isError: false};
  let calls = 0;
  const options = hostOptions(f, async args => { calls++; assert.deepEqual(args, step.host_envelope.arguments); return result; }, async (action, outcome) => {
    assert.strictEqual(action, step.action); assert.strictEqual(outcome.result, result);
    assert.deepEqual(JSON.parse(await fs.readFile(f.receiptPath)).result, result);
    return {action: {kind: 'poll_openart_original', subject: 'motion-original', action_id: 'p'.repeat(64)}, status: 'host_action'};
  });
  options.nativeTool.tool_name = step.host_envelope.tool;
  const pending = await consumeNativeHostAction(f.tools, step, options);
  await assert.rejects(consumeNativeHostAction(f.tools, pending, options), /exact emitted/);
  assert.equal(calls, 1);
});
test('raised OpenArt exception saves evidence and leaves original uncertain without fabricated result', async t => {
  const f = await fixture(t);
  const step = {action: {kind: 'receive_openart_original', subject: 'motion-original'},
    host_envelope: {tool: 'mcp__codex_apps__openart_openart_generate_video', arguments: {prompt: 'actual'}}};
  let calls = 0;
  const options = hostOptions(f, async () => { calls++; throw new Error('Original transport exception'); }, () => assert.fail('no supported exception completion'));
  options.nativeTool.tool_name = step.host_envelope.tool;
  const stopped = await consumeNativeHostAction(f.tools, step, options);
  assert.equal(stopped.status, 'host_exception'); assert.equal(stopped.provider_calls, null);
  assert.strictEqual(stopped.action, step.action);
  const original = JSON.parse(await fs.readFile(stopped.host_exception.path));
  assert.equal(original.exception.message, 'Original transport exception');
  assert.ok(!('result' in original)); assert.equal(calls, 1);
});
test('helper is executable in fresh functions.exec style without Node imports or CommonJS', async t => {
  const f = await fixture(t);
  const source = await fs.readFile(path.join(__dirname, '../../scripts/native_host_receipt.js'), 'utf8');
  const helper = new Function(source + '; return consumeNativeHostAction;')();
  const options = hostOptions(f, async () => f.observation.result, async () => doneStep);
  assert.deepEqual(await helper(f.tools, imageStep(f), options), {...doneStep, provider_calls: null});
});
test('failed result retention and host-store loss never replay the emitted invocation', async t => {
  const f = await fixture(t), step = imageStep(f);
  let calls = 0;
  const options = hostOptions(f, async () => { calls++; return f.observation.result; }, () => assert.fail('result was not retained'));
  const retain = options.retainObservation;
  options.retainObservation = (key, value) => {
    if ('result' in value) throw new Error('Actual host store failed after return');
    retain(key, value);
  };
  await assert.rejects(consumeNativeHostAction(f.tools, step, options), /host store failed/);
  await assert.rejects(consumeNativeHostAction(f.tools, step, options), /already attempted/);
  const restarted = hostOptions(f, options.nativeTool.invoke, options.completeAction);
  await assert.rejects(consumeNativeHostAction(f.tools, step, restarted), /EEXIST/);
  assert.equal(calls, 1);
  const marker = JSON.parse(await fs.readFile(f.receiptPath + '.invoked'));
  assert.equal(marker.subject, step.action.subject);
  assert.deepEqual(marker.arguments, step.host_envelope.arguments);
  assert.ok(!('result' in marker));
});
test('pre-invocation store failure calls no host tool', async t => {
  const f = await fixture(t);
  let calls = 0;
  const options = hostOptions(f, async () => { calls++; return f.observation.result; }, () => assert.fail());
  options.retainObservation = () => { throw new Error('Host store unavailable'); };
  await assert.rejects(consumeNativeHostAction(f.tools, imageStep(f), options), /store unavailable/);
  assert.equal(calls, 0); assert.equal(f.calls.length, 0);
});
// Successful recovery returns the exact saved step plus exec evidence beside it.
function savedStep(recovered) { const {completion_diagnostics, ...step} = recovered; return step; }
test('saved completion is canonical with empty, truncated or chunked stdout and drains only its original session', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  const exactStep = {status: 'host_action', action: {kind: 'receive_native_image', subject: 'next', action_id: 'e'.repeat(64)},
    host_envelope: {tool_name: 'image_gen.imagegen', call_id: 'next', arguments: {prompt: 'retained'}}};
  await fs.writeFile(completionPath, JSON.stringify(exactStep));
  let drains = 0, completionRuns = 1, providerRuns = 1;
  f.tools.write_stdin = async request => {
    drains++;
    assert.equal(request.session_id, 'original-session');
    if (drains === 1) return {session_id: 'original-session', output: '{"status":"host_'};
    return {exit_code: 0, output: 'action"'};
  };
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => {
    if (request.title !== 'Recover saved native completion') return rpc(request);
    const match = request.code.match(/readFile\(("(?:[^"\\]|\\.)*")/);
    assert.ok(match, 'recovery reads the durable completion path');
    try {
      const value = JSON.parse(await fs.readFile(JSON.parse(match[1]), 'utf8'));
      return {content: [{type: 'text', text: JSON.stringify({ok: true, value})}]};
    } catch (error) {
      return {content: [{type: 'text', text: JSON.stringify({ok: false, error: error.message})}]};
    }
  };
  for (const output of ['', '{"status":"host_action"', '{"status":"host_action","action":']) {
    const completion = {exit_code: 0, output};
    if (!drains) completion.session_id = 'original-session';
    const recovered = await consumeSavedNativeCompletion(f.tools, {completion, completionPath});
    assert.deepEqual(savedStep(recovered), exactStep);
    assert.deepEqual(recovered.completion_diagnostics.initial, completion);
    assert.deepEqual(recovered.completion_diagnostics.final,
      completion.session_id ? {exit_code: 0, output: 'action"'} : completion);
    assert.deepEqual(recovered.host_envelope, exactStep.host_envelope);
  }
  assert.equal(drains, 2);
  assert.equal(completionRuns, 1); assert.equal(providerRuns, 1);
});
test('saved completion recovery without a session reads the exact saved step after empty stdout', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  const exactStep = doneStep;
  await fs.writeFile(completionPath, JSON.stringify(exactStep));
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => {
    if (request.title !== 'Recover saved native completion') return rpc(request);
    const match = request.code.match(/readFile\(("(?:[^"\\]|\\.)*")/);
    return {content: [{type: 'text', text: JSON.stringify({ok: true, value: JSON.parse(await fs.readFile(JSON.parse(match[1]), 'utf8'))})}]};
  };
  const recovered = await consumeSavedNativeCompletion(f.tools, {completionPath});
  assert.deepEqual(savedStep(recovered), exactStep);
  assert.deepEqual(recovered.completion_diagnostics, {initial: {}, final: {}});
  assert.deepEqual(JSON.parse(await fs.readFile(completionPath)), exactStep);
  assert.equal(f.tools.write_stdin, undefined);
});
test('emitted recovery code runs repeatedly in one persistent Node REPL context', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  const exactStep = doneStep;
  await fs.writeFile(completionPath, JSON.stringify(exactStep));
  let captured;
  const server = repl.start({input: new PassThrough(), output: new PassThrough(), terminal: false, prompt: ''});
  t.after(() => server.close());
  server.context.nodeRepl = {write: value => { captured = value; }};
  f.tools.mcp__node_repl__js = async request => {
    if (request.title !== 'Recover saved native completion') throw new Error('unexpected RPC');
    await new Promise((resolve, reject) => server.eval(request.code, server.context, 'completion-recovery', error => error ? reject(error) : resolve()));
    return {content: [{type: 'text', text: captured}]};
  };
  assert.deepEqual(savedStep(await consumeSavedNativeCompletion(f.tools, {completionPath})), exactStep);
  assert.deepEqual(savedStep(await consumeSavedNativeCompletion(f.tools, {completionPath})), exactStep);
});
test('missing or corrupt saved completion remains explicitly unresolved without resubmission', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  let completionRuns = 1, providerRuns = 1;
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => {
    if (request.title !== 'Recover saved native completion') return rpc(request);
    const match = request.code.match(/readFile\(("(?:[^"\\]|\\.)*")/);
    try {
      const value = JSON.parse(await fs.readFile(JSON.parse(match[1]), 'utf8'));
      return {content: [{type: 'text', text: JSON.stringify({ok: true, value})}]};
    } catch (error) {
      return {content: [{type: 'text', text: JSON.stringify({ok: false, error: error.message})}]};
    }
  };
  for (const contents of [null, '{truncated']) {
    if (contents === null) await fs.rm(completionPath, {force: true});
    else await fs.writeFile(completionPath, contents);
    await assert.rejects(consumeSavedNativeCompletion(f.tools, {completionPath}), /Original completion unresolved.*without rerunning completion or its provider/);
  }
  assert.equal(completionRuns, 1); assert.equal(providerRuns, 1);
});
test('existing saved original prevents stale-envelope invocation after process restart', async t => {
  const f = await fixture(t), step = imageStep(f);
  await saveNativeHostReceipt(f.tools, f.observation, f.receiptPath);
  let calls = 0;
  const options = hostOptions(f, async () => { calls++; return f.observation.result; }, () => assert.fail());
  await assert.rejects(consumeNativeHostAction(f.tools, step, options), /already exists/);
  assert.equal(calls, 0);
  assert.deepEqual(JSON.parse(await fs.readFile(f.receiptPath)), f.observation);
});

// U2: one shared shape check for both completion consumers.
const canonicalNext = {status: 'host_action', steps: 1, provider_calls: 0,
  action: {kind: 'receive_native_image', subject: 'next', action_id: 'a'.repeat(64)},
  host_envelope: {tool_name: 'image_gen.imagegen', call_id: 'next', arguments: {prompt: 'retained'}}};
const sharedCompletionSteps = require('../fixtures/native_completion_steps.json');
const nonSteps = [{}, null, [], 'host_action', {status: 'host_action'}, {status: 'bogus', action: canonicalNext.action},
  {status: 'host_action', action: {kind: 'invented', action_id: 'x'}}, {status: 'host_action', action: {kind: 'complete'}},
  {exit_code: 0, output: ''}, {session_id: 'still-running'}, {...canonicalNext, host_envelope: 'text'},
  ...sharedCompletionSteps.filter(item => !item.valid).map(item => item.step)];
function savedReader(f) {
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => {
    if (request.title !== 'Recover saved native completion') return rpc(request);
    const match = request.code.match(/readFile\(("(?:[^"\\]|\\.)*")/);
    try {
      return {content: [{type: 'text', text: JSON.stringify({ok: true, value: JSON.parse(await fs.readFile(JSON.parse(match[1]), 'utf8'))})}]};
    } catch (error) {
      return {content: [{type: 'text', text: JSON.stringify({ok: false, missing: error.code === 'ENOENT', error: error.message})}]};
    }
  };
}
test('U2 shared shape check rejects empty, raw exec and malformed completion as progress', async t => {

  assert.equal(typeof canonicalProductionStep, 'function');
  for (const value of nonSteps) assert.throws(() => canonicalProductionStep(value), /canonical production step/, JSON.stringify(value));
  assert.strictEqual(canonicalProductionStep(canonicalNext), canonicalNext);
  for (const {step, valid} of sharedCompletionSteps)
    if (valid) assert.strictEqual(canonicalProductionStep(step), step);
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  savedReader(f);
  for (const value of nonSteps) {
    let completions = 0;
    const g = await fixture(t);
    const options = hostOptions(g, async () => g.observation.result, async () => { completions++; return value; });
    await assert.rejects(consumeNativeHostAction(g.tools, imageStep(g), options), /canonical production step/);
    assert.equal(completions, 1);
    await fs.writeFile(completionPath, JSON.stringify(value));
    await assert.rejects(consumeSavedNativeCompletion(f.tools, {completion: {exit_code: 0, output: ''}, completionPath}),
      error => /Original completion unresolved/.test(error.message) && error.saved_step === 'invalid');
  }
});
test('U2 nonzero exit or missing saved output keeps actual initial/final diagnostics without rerun', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  savedReader(f);
  f.tools.write_stdin = async () => ({exit_code: 137, output: 'killed partial'});
  const error = await consumeSavedNativeCompletion(f.tools,
    {completion: {session_id: 'owned', output: '{"status":'}, completionPath}).catch(e => e);
  assert.match(error.message, /Original completion unresolved/);
  assert.equal(error.saved_step, 'missing'); assert.equal(error.completion_state, 'terminated');
  assert.deepEqual(error.completion_diagnostics, {initial: {session_id: 'owned', output: '{"status":'},
    final: {exit_code: 137, output: 'killed partial'}});
  const plain = await consumeSavedNativeCompletion(f.tools, {completion: {exit_code: 1, stderr: 'boom'}, completionPath}).catch(e => e);
  assert.deepEqual(plain.completion_diagnostics, {initial: {exit_code: 1, stderr: 'boom'}, final: {exit_code: 1, stderr: 'boom'}});
  await fs.writeFile(completionPath, JSON.stringify(canonicalNext));
  f.tools.write_stdin = async () => ({exit_code: 0, output: '{"trunc'});
  const recovered = await consumeSavedNativeCompletion(f.tools, {completion: {session_id: 'owned'}, completionPath});
  assert.deepEqual(savedStep(recovered), canonicalNext);
  assert.deepEqual(recovered.completion_diagnostics, {initial: {session_id: 'owned'}, final: {exit_code: 0, output: '{"trunc'}});
  // Absence of a session alone is not termination evidence.
  await fs.rm(completionPath);
  const unknown = await consumeSavedNativeCompletion(f.tools, {completion: {output: '{"st'}, completionPath}).catch(e => e);
  assert.equal(unknown.completion_state, 'unknown'); assert.equal(unknown.saved_step, 'missing');
  assert.deepEqual(unknown.completion_diagnostics, {initial: {output: '{"st'}, final: {output: '{"st'}});
});
test('U2 a still-running owned completion stays pending and is consumed once it finishes', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  savedReader(f);
  let drains = 0;
  f.tools.write_stdin = async request => {
    drains++; assert.equal(request.session_id, 'owned');
    if (drains < 3) return {session_id: 'owned', output: ''};
    await fs.writeFile(completionPath, JSON.stringify(canonicalNext));
    return {exit_code: 0, output: ''};
  };
  assert.deepEqual(savedStep(await consumeSavedNativeCompletion(f.tools, {completion: {session_id: 'owned', output: ''}, completionPath})), canonicalNext);
  assert.equal(drains, 3);
  delete f.tools.write_stdin;
  await fs.rm(completionPath);
  const running = await consumeSavedNativeCompletion(f.tools, {completion: {session_id: 'owned2'}, completionPath}).catch(e => e);
  assert.equal(running.completion_state, 'running');
  assert.equal(running.saved_step, undefined);
});
test('U2 Studio terminal composition states are progress without an invented action', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  savedReader(f);
  for (const status of ['complete', 'partial', 'incomplete']) {
    // Shape of Studio _finish_state: deliver_standalone/handoff fields + closeout delivery.
    const finished = {status, currency: {status: 'current', reasons: []}, annotation_currency: {status: 'current'},
      first_cut: {path: 'renders/first-cut-v001.mp4', sha256: 'c'.repeat(64)}, clips: [{clip_id: 'c1', review: 'unknown'}],
      export_profile: 'tiktok', operation_id: 'deliver-op', not_certification: true, posted: false, provider_calls: 0,
      delivery: {status: status === 'incomplete' ? 'closeout_failed' : 'closeout_pending', closeout_complete: false},
      recoverable_originals: status === 'complete' ? [] : [{kind: 'collect_grok_original'}]};
    assert.strictEqual(canonicalProductionStep(finished), finished);
    const g = await fixture(t);
    const direct = await consumeNativeHostAction(g.tools, imageStep(g), hostOptions(g, async () => g.observation.result, async () => finished));
    assert.deepEqual(direct, {...finished, provider_calls: null}); assert.equal(direct.action, undefined);
    await fs.writeFile(completionPath, JSON.stringify(finished));
    const saved = await consumeSavedNativeCompletion(f.tools, {completion: {exit_code: 0, output: ''}, completionPath});
    assert.deepEqual(savedStep(saved), finished);
    const {first_cut, ...noCut} = finished;
    for (const broken of [{status}, {status, delivery: {}, recoverable_originals: []}, noCut,
      {...finished, first_cut: {path: 'cut.mp4', sha256: 'short'}}, {...finished, clips: undefined},
      {...finished, delivery: {status: 'delivered'}}, {...finished, delivery: null},
      {...finished, recoverable_originals: {}}, {...finished, action: undefined}])
      assert.throws(() => canonicalProductionStep(broken), /canonical production step/);
  }
});

for (const disposition of ['returned_result', 'error_result', 'exception']) {
  test(`optional host boundary observations classify ${disposition} and same-original recovery`, async t => {
    const f = await fixture(t), events = [];
    const step = {action: {kind: 'receive_openart_original', subject: 'motion-original'},
      host_envelope: {tool: 'mcp__codex_apps__openart_openart_generate_video', arguments: {prompt: 'actual'}}};
    const result = disposition === 'error_result' ? {isError: true, content: [{type: 'text', text: 'Transport closed'}]}
      : {structuredContent: {historyId: 'actual-job', status: 'PENDING'}, isError: false};
    let calls = 0;
    const options = hostOptions(f, async () => {
      calls++;
      if (disposition === 'exception') throw new Error('actual transport failure');
      return result;
    }, async () => doneStep);
    options.nativeTool.tool_name = step.host_envelope.tool;
    options.observeTiming = event => events.push(event);
    await consumeNativeHostAction(f.tools, step, options);
    assert.equal(events.filter(event => event.stage === 'native_call').length, 1);
    assert.equal(events.filter(event => event.stage === 'native_return').length, 1);
    assert.equal(events.find(event => event.stage === 'native_classified').disposition, disposition);
    assert.ok(events.findIndex(event => event.stage === 'retention_end') <
      events.findIndex(event => event.stage === 'native_classified'));
    const original = await fs.readFile(f.receiptPath);
    events.length = 0;
    await consumeNativeHostAction(f.tools, step, options);
    assert.equal(calls, 1);
    assert.equal(events[0].stage, 'same_original_recovery');
    assert.equal(events[1].stage, 'native_classified');
    assert.equal(events[1].disposition, disposition);
    assert.ok(!events.some(event => ['native_call', 'native_return'].includes(event.stage)));
    assert.deepEqual(await fs.readFile(f.receiptPath), original);
  });
}
