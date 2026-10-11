// Loaded once by the agent host; this helper saves an observation, never generates.
function nativeImageURLSha256(text) {
  // Native image data URLs are ASCII. Avoid a host crypto/library dependency.
  const constants = [], initial = [];
  for (let candidate = 2; constants.length < 64; candidate++) {
    let prime = true;
    for (let divisor = 2; divisor * divisor <= candidate; divisor++)
      if (candidate % divisor === 0) { prime = false; break; }
    if (prime) {
      if (initial.length < 8) initial.push((Math.sqrt(candidate) % 1 * 4294967296) | 0);
      constants.push((Math.cbrt(candidate) % 1 * 4294967296) | 0);
    }
  }
  const hash = initial, words = new Array(64);
  const rotate = (word, bits) => (word >>> bits) | (word << (32 - bits));
  const paddedLength = Math.ceil((text.length + 9) / 64) * 64;
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index++) {
      let word = 0;
      for (let byte = 0; byte < 4; byte++) {
        const pos = offset + index * 4 + byte;
        let value = pos < text.length ? text.charCodeAt(pos) : pos === text.length ? 128 : 0;
        if (pos >= paddedLength - 8) {
          const bitLength = text.length * 8;
          value = Math.floor(bitLength / 2 ** ((paddedLength - pos - 1) * 8)) & 255;
        }
        word = (word << 8) | value;
      }
      words[index] = word;
    }
    for (let index = 16; index < 64; index++) {
      const a = words[index - 15], b = words[index - 2];
      words[index] = ((rotate(a, 7) ^ rotate(a, 18) ^ (a >>> 3)) + words[index - 16]
        + (rotate(b, 17) ^ rotate(b, 19) ^ (b >>> 10)) + words[index - 7]) | 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index++) {
      const next = (h + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25))
        + ((e & f) ^ (~e & g)) + constants[index] + words[index]) | 0;
      const head = (next + (rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22))
        + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      h = g; g = f; f = e; e = (d + next) | 0; d = c; c = b; b = a;
      a = head;
    }
    [a, b, c, d, e, f, g, h].forEach((word, index) => hash[index] = (hash[index] + word) | 0);
  }
  return hash.map(word => (word >>> 0).toString(16).padStart(8, '0')).join('');
}

async function saveNativeHostReceipt(tools, observedReceipt, receiptPath) {
  const url = observedReceipt?.result?.image_url;
  const match = typeof url === 'string' && /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})$/.exec(url);
  const hint = observedReceipt?.result?.output_hint;
  const pathMatch = typeof hint === 'string' && / as (\/[^\n]+\/\.codex\/generated_images\/[^\n]+\.(?:png|jpe?g|webp)) by default\./.exec(hint);
  if (!match || !pathMatch) throw new Error('Native return has no usable local image; retain original observation for collection');
  if (typeof receiptPath !== 'string' || !receiptPath.startsWith('/')) throw new Error('An absolute receipt path is required');
  const snapshot = JSON.parse(JSON.stringify(observedReceipt));
  snapshot.result.image_url = null;
  const metadata = JSON.stringify(snapshot);
  if (metadata.length > 65536) throw new Error('Unexpected large native metadata; retain original observation');
  const code = `await (async () => {
    const fs = await import('node:fs/promises');
    const crypto = await import('node:crypto');
    const snapshot = JSON.parse(${JSON.stringify(metadata)});
    const url = ${JSON.stringify('data:' + match[1] + ';base64,')} + (await fs.readFile(${JSON.stringify(pathMatch[1])})).toString('base64');
    if (url.length !== ${url.length} || crypto.createHash('sha256').update(url).digest('hex') !== ${JSON.stringify(nativeImageURLSha256(url))})
      throw new Error('Cached image differs from actual native return');
    snapshot.result.image_url = url;
    const raw = JSON.stringify(snapshot), receiptPath = ${JSON.stringify(receiptPath)};
    const temporary = receiptPath + '.tmp-' + crypto.randomUUID();
    try {
      await fs.writeFile(temporary, raw, {flag: 'wx', mode: 0o600});
      try { await fs.link(temporary, receiptPath); }
      catch (error) { if (error.code !== 'EEXIST') throw error;
        if (await fs.readFile(receiptPath, 'utf8') !== raw) throw new Error('Original host receipt collision'); }
    } finally { await fs.rm(temporary, {force: true}); }
    nodeRepl.write({path: receiptPath, bytes: Buffer.byteLength(raw), sha256: crypto.createHash('sha256').update(raw).digest('hex')});
  })();`;
  const saved = await tools.mcp__node_repl__js({code, title: 'Save actual native image receipt', timeout_ms: 30000});
  if (saved.isError) throw new Error('Receipt saving failed; retain original observation and collect it without regeneration');
  return saved;
}
if (typeof module !== 'undefined') module.exports = {saveNativeHostReceipt, nativeImageURLSha256};
