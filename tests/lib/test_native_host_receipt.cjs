const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const {saveNativeHostReceipt, nativeImageURLSha256} = require('../../scripts/native_host_receipt.js');
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
    return {content: [{type: 'text', text: JSON.stringify(output)}]};
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
