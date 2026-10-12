const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const repl = require('node:repl');
const {PassThrough} = require('node:stream');
const {saveNativeHostReceipt, nativeImageURLSha256, consumeNativeHostAction, consumeSavedNativeCompletion} = require('../../scripts/native_host_receipt.js');
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
test('actual image callback, durable lossless original and completion share one operation', async t => {
  const f = await fixture(t), step = imageStep(f);
  let nativeCalls = 0, completionCalls = 0;
  const next = {status: 'host_action', action: {kind: 'receive_native_image', subject: 'next-original'},
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
    return {status: 'host_action'};
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
  assert.deepEqual(await consumeNativeHostAction(f.tools, step, options), {status: 'host_action', provider_calls: null});
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
  const result = {content: [{type: 'text', text: JSON.stringify({historyId: 'actual-job', status: 'pending'})}],
    structuredContent: {future_field: ['retained', 1]}};
  let calls = 0;
  const options = hostOptions(f, async args => { calls++; assert.deepEqual(args, step.host_envelope.arguments); return result; }, async (action, outcome) => {
    assert.strictEqual(action, step.action); assert.strictEqual(outcome.result, result);
    assert.deepEqual(JSON.parse(await fs.readFile(f.receiptPath)).result, result);
    return {action: {kind: 'poll_openart_original', subject: 'motion-original'}, status: 'host_action'};
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
  const options = hostOptions(f, async () => f.observation.result, async () => ({status: 'host_action'}));
  assert.deepEqual(await helper(f.tools, imageStep(f), options), {status: 'host_action', provider_calls: null});
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
test('saved completion is canonical with empty, truncated or chunked stdout and drains only its original session', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  const exactStep = {status: 'host_action', action: {kind: 'receive_native_image', subject: 'next'},
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
    assert.deepEqual(recovered, exactStep);
    assert.deepEqual(recovered.host_envelope, exactStep.host_envelope);
  }
  assert.equal(drains, 2);
  assert.equal(completionRuns, 1); assert.equal(providerRuns, 1);
});
test('saved completion recovery without a session reads the exact saved step after empty stdout', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  const exactStep = {status: 'host_action', action: {kind: 'prepare_motion', subject: 'same'}};
  await fs.writeFile(completionPath, JSON.stringify(exactStep));
  const rpc = f.tools.mcp__node_repl__js;
  f.tools.mcp__node_repl__js = async request => {
    if (request.title !== 'Recover saved native completion') return rpc(request);
    const match = request.code.match(/readFile\(("(?:[^"\\]|\\.)*")/);
    return {content: [{type: 'text', text: JSON.stringify({ok: true, value: JSON.parse(await fs.readFile(JSON.parse(match[1]), 'utf8'))})}]};
  };
  assert.deepEqual(await consumeSavedNativeCompletion(f.tools, {completionPath}), exactStep);
  assert.equal(f.tools.write_stdin, undefined);
});
test('emitted recovery code runs repeatedly in one persistent Node REPL context', async t => {
  const f = await fixture(t), completionPath = path.join(f.root, 'completed-step.json');
  const exactStep = {status: 'host_action', action: {kind: 'prepare_motion', subject: 'same'}};
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
  assert.deepEqual(await consumeSavedNativeCompletion(f.tools, {completionPath}), exactStep);
  assert.deepEqual(await consumeSavedNativeCompletion(f.tools, {completionPath}), exactStep);
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
