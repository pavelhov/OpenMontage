// Loaded once by the agent host. Native invocation is an explicit injected callback.
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

function observeNativeHostTiming(observer, stage, details = {}) {
  if (typeof observer !== 'function') return;
  // Diagnostics never change original ownership, persistence or completion.
  try { observer({stage, at_ms: Date.now(), ...details}); } catch (_) {}
}

async function saveNativeHostReceipt(tools, observedReceipt, receiptPath, observeTiming) {
  if (!tools || typeof tools.mcp__node_repl__js !== 'function')
    throw new Error('A callable Node REPL bridge is required to save the native host receipt');
  observeNativeHostTiming(observeTiming, 'receipt_prepare_start');
  const url = observedReceipt?.tool_name === 'image_gen.imagegen' ? observedReceipt?.result?.image_url : undefined;
  const match = typeof url === 'string' && /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})$/.exec(url);
  const hint = observedReceipt?.result?.output_hint;
  const pathMatch = typeof hint === 'string' && / as (\/[^\n]+\/\.codex\/generated_images\/[^\n]+\.(?:png|jpe?g|webp)) by default\./.exec(hint);
  if (url !== undefined && (!match || !pathMatch)) throw new Error('Native return has no usable local image; retain original observation for collection');
  if (typeof receiptPath !== 'string' || !receiptPath.startsWith('/')) throw new Error('An absolute receipt path is required');
  const snapshot = JSON.parse(JSON.stringify(observedReceipt));
  if (match) snapshot.result.image_url = null;
  const metadata = JSON.stringify(snapshot);
  if (metadata.length > 65536) throw new Error('Unexpected large native metadata; retain original observation');
  const imageRestore = match ? `
    const url = ${JSON.stringify('data:' + match[1] + ';base64,')} + (await fs.readFile(${JSON.stringify(pathMatch[1])})).toString('base64');
    if (clocks) clocks.image_read_end = Date.now();
    if (url.length !== ${url.length} || crypto.createHash('sha256').update(url).digest('hex') !== ${JSON.stringify(nativeImageURLSha256(url))})
      throw new Error('Cached image differs from actual native return');
    if (clocks) clocks.image_hash_end = Date.now();
    snapshot.result.image_url = url;` : '';
  const code = `await (async () => {
    const clocks = ${typeof observeTiming === 'function'} ? {start: Date.now()} : null;
    const fs = await import('node:fs/promises');
    const crypto = await import('node:crypto');
    const snapshot = JSON.parse(${JSON.stringify(metadata)});
    ${imageRestore}
    const raw = JSON.stringify(snapshot), receiptPath = ${JSON.stringify(receiptPath)};
    const temporary = receiptPath + '.tmp-' + crypto.randomUUID();
    try {
      if (clocks) clocks.write_start = Date.now();
      await fs.writeFile(temporary, raw, {flag: 'wx', mode: 0o600});
      if (clocks) clocks.write_end = Date.now();
      const file = await fs.open(temporary, 'r');
      try { await file.sync(); } finally { await file.close(); }
      try { await fs.link(temporary, receiptPath); }
      catch (error) { if (error.code !== 'EEXIST') throw error;
        if (await fs.readFile(receiptPath, 'utf8') !== raw) throw new Error('Original host receipt collision'); }
    } finally { await fs.rm(temporary, {force: true}); }
    const parent = await fs.open(receiptPath.slice(0, receiptPath.lastIndexOf('/')) || '/', 'r');
    try { await parent.sync(); } finally { await parent.close(); }
    if (clocks) clocks.sync_end = Date.now();
    nodeRepl.write(JSON.stringify({path: receiptPath, bytes: Buffer.byteLength(raw), sha256: crypto.createHash('sha256').update(raw).digest('hex'), ...(clocks ? {clocks} : {})}));
  })();`;
  observeNativeHostTiming(observeTiming, 'receipt_prepare_end');
  observeNativeHostTiming(observeTiming, 'receipt_rpc_start');
  const saved = await tools.mcp__node_repl__js({code, title: 'Save actual native host receipt', timeout_ms: 30000});
  observeNativeHostTiming(observeTiming, 'receipt_rpc_end');
  if (typeof observeTiming === 'function') {
    for (const block of saved?.content || []) {
      if (block.type !== 'text') continue;
      try {
        const clocks = JSON.parse(block.text)?.clocks;
        if (clocks) observeNativeHostTiming(observeTiming, 'node_receipt', {clocks});
      } catch (_) {}
    }
  }
  if (saved.isError) throw new Error('Receipt saving failed; retain original observation and collect it without regeneration');
  return saved;
}

function nativeHostReceiptBinding(saved) {
  for (const block of saved?.content || []) {
    if (block.type !== 'text') continue;
    let value;
    try { value = JSON.parse(block.text); } catch (_) { continue; }
    if (typeof value?.path === 'string' && value.path.startsWith('/')
        && Number.isSafeInteger(value.bytes) && value.bytes > 0
        && /^[a-f0-9]{64}$/.test(value.sha256))
      return {path: value.path, sha256: value.sha256};
  }
  throw new Error('Receipt metadata unavailable; recover saved original without another native invocation');
}

async function claimNativeHostInvocation(tools, receiptPath, action, invocation) {
  // This local ownership guard is not a tool result or engine authority. The
  // canonical original is already submitted; an interrupted owner must collect
  // it rather than calling a saved envelope again, even after host-store loss.
  const code = `await (async () => {
    const fs = await import('node:fs/promises');
    const receipt = ${JSON.stringify(receiptPath)}, marker = receipt + '.invoked';
    try { await fs.access(receipt); throw new Error('Original receipt already exists; collect it without another invocation'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const file = await fs.open(marker, 'wx', 0o600);
    try { await file.writeFile(${JSON.stringify(JSON.stringify({action_id: action.action_id, subject: action.subject, ...invocation}))}); await file.sync(); }
    finally { await file.close(); }
    const parent = await fs.open(receipt.slice(0, receipt.lastIndexOf('/')) || '/', 'r');
    try { await parent.sync(); } finally { await parent.close(); }
    nodeRepl.write('{"claimed":true}');
  })();`;
  const claimed = await tools.mcp__node_repl__js({code, title: 'Claim once-only native host invocation', timeout_ms: 30000});
  if (claimed.isError || !claimed.content?.some(block => block.type === 'text' && block.text === '{"claimed":true}'))
    throw new Error('Native invocation ownership unknown; collect original without replaying its envelope');
}

// One shared shape check for every completion consumer. Only a legitimate
// canonical engine step counts as progress; empty objects, raw exec results,
// arrays and malformed states never advance the producer.
const CANONICAL_STEP_STATUSES = ['mechanical', 'host_action', 'ready_to_compose', 'complete', 'blocked', 'pending', 'dispatched'];
const CANONICAL_ACTION_KINDS = ['restore_derived_views', 'prepare_board_image', 'begin_native_image',
  'promote_imported_board', 'receive_native_image', 'prepare_motion', 'bind_upstream_frame',
  'collect_grok_original', 'collect_openart_original', 'begin_openart_original',
  'receive_openart_original', 'poll_openart_original', 'download_openart_original',
  'compose_cut', 'complete', 'blocked'];
// Studio's finished composition state is the other legitimate return form:
// {status: complete|partial|incomplete, first_cut:{path,sha256}, clips:[...],
// delivery:{status, closeout_complete}, recoverable_originals:[...]} with no
// action; closeout may legitimately be pending or failed. It is accepted as-is; no action id is invented for it.
const TERMINAL_COMPOSITION_STATUSES = ['complete', 'partial', 'incomplete'];
function canonicalProductionStep(value) {
  const isObject = item => item !== null && typeof item === 'object' && !Array.isArray(item);
  const action = isObject(value) ? value.action : undefined;
  const cut = isObject(value) ? value.first_cut : undefined, delivery = isObject(value) ? value.delivery : undefined;
  if (isObject(value) && !('action' in value) && TERMINAL_COMPOSITION_STATUSES.includes(value.status)
      && isObject(cut) && typeof cut.path === 'string' && cut.path
      && typeof cut.sha256 === 'string' && /^[0-9a-f]{64}$/.test(cut.sha256)
      && Array.isArray(value.clips) && isObject(delivery) && typeof delivery.status === 'string' && delivery.status
      && typeof delivery.closeout_complete === 'boolean' && Array.isArray(value.recoverable_originals))
    return value;
  if (!isObject(value) || !CANONICAL_STEP_STATUSES.includes(value.status) || !isObject(action)
      || !CANONICAL_ACTION_KINDS.includes(action.kind) || typeof action.action_id !== 'string' || !action.action_id
      || ('host_envelope' in value && !isObject(value.host_envelope)))
    throw new Error('Completion is not a canonical production step; the original stays consumed and unresolved');
  return value;
}

// Keep only the execution fields the host actually exposed.
function completionDiagnostics(result) {
  const kept = {};
  if (result && typeof result === 'object')
    for (const field of ['exit_code', 'output', 'stderr', 'session_id'])
      if (result[field] !== undefined) kept[field] = result[field];
  return kept;
}

async function consumeNativeHostAction(tools, step, options) {
  // One emitted envelope only. A completion may itself emit the next envelope;
  // return that exact step to the producer rather than calling advance again.
  // receiptPath is fixed per original across retries/restarts: changing it changes
  // ownership identity. Lost observations require canonical original collection.
  // OpenArt completion callbacks select the actual structuredContent payload for
  // the engine; the full raw connector result stays in the saved host receipt.
  if (!tools || typeof tools.mcp__node_repl__js !== 'function')
    throw new Error('A callable Node REPL bridge is required before native invocation');
  const {nativeTool, receiptPath, getObservation, retainObservation, completeAction} = options;
  const action = step?.action, envelope = step?.host_envelope;
  const toolName = envelope?.tool_name || envelope?.tool;
  const image = action?.kind === 'receive_native_image';
  if (!envelope || !(image || action?.kind === 'receive_openart_original')
      || typeof nativeTool?.invoke !== 'function' || nativeTool.tool_name !== toolName
      || typeof getObservation !== 'function' || typeof retainObservation !== 'function'
      || typeof completeAction !== 'function')
    throw new Error('An exact emitted generation envelope and explicit host callbacks are required');
  if (image && (toolName !== 'image_gen.imagegen' || envelope.call_id !== action.subject))
    throw new Error('Native image envelope differs from current receive action');
  if (typeof receiptPath !== 'string' || !receiptPath.startsWith('/'))
    throw new Error('An absolute receipt path is required before native invocation');
  const key = 'native-host:' + action.subject;
  const timing = typeof options.observeTiming === 'function'
    ? event => options.observeTiming({...event, subject: action.subject}) : undefined;
  let observed = await getObservation(key);
  if (observed?.invocation_pending)
    throw new Error('Native invocation already attempted; collect original without replaying its envelope');
  if (!observed) {
    const argumentsSnapshot = JSON.parse(JSON.stringify(envelope.arguments));
    // Protect the exact arguments handed to the actual host callback.
    const freeze = value => {
      if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
      return value;
    };
    const actualArguments = freeze(JSON.parse(JSON.stringify(argumentsSnapshot)));
    // Fence in this host session before invoking; if either retention or the
    // durable exclusive claim fails, no native callback is entered.
    await retainObservation(key, {invocation_pending: true, tool_name: nativeTool.tool_name, arguments: argumentsSnapshot});
    await claimNativeHostInvocation(tools, receiptPath, action,
      {tool_name: nativeTool.tool_name, arguments: argumentsSnapshot});
    let result, raised, didRaise = false;
    try { result = await nativeTool.invoke(actualArguments); }
    catch (error) { raised = error; didRaise = true; }
    observeNativeHostTiming(timing, 'native_return', {raised: didRaise});
    observed = {tool_name: nativeTool.tool_name, arguments: argumentsSnapshot};
    if (didRaise) {
      observed.call_id = action.subject;
      observed.exception = {message: String(raised?.message ?? raised), type: String(raised?.name || typeof raised)};
    } else observed.result = result;
    // Retain the actual return before any saving/completion RPC can be interrupted.
    observeNativeHostTiming(timing, 'retention_start');
    await retainObservation(key, observed);
    observeNativeHostTiming(timing, 'retention_end');
  }
  if (observed.tool_name !== toolName
      || JSON.stringify(observed.arguments) !== JSON.stringify(envelope.arguments))
    throw new Error('Retained original invocation differs from emitted envelope');
  const receipt = nativeHostReceiptBinding(await saveNativeHostReceipt(tools, observed, receiptPath, timing));
  const complete = async outcome => {
    observeNativeHostTiming(timing, 'completion_start');
    const next = await completeAction(action, outcome);
    observeNativeHostTiming(timing, 'completion_end');
    canonicalProductionStep(next);
    // Engine metadata counts its completion mechanics, not the actual host job.
    // Preserve the exact next action/envelope while keeping job exposure unknown.
    return {...next, provider_calls: null};
  };
  if (observed.exception) {
    if (image) return complete({host_exception: receipt});
    // OpenArt's engine keeps the begun original uncertain. It has no exception
    // completion outcome; retain evidence and stop for original collection.
    return {status: 'host_exception', action, host_exception: receipt, provider_calls: null};
  }
  return complete(image ? {host_receipt: receipt} : {result: observed.result});
}

// Drain the one original completion session, then recover its already-written
// return file. Stdout is deliberately ignored: it may be empty or truncated even
// though complete_action durably emitted the exact next step before printing.
async function consumeSavedNativeCompletion(tools, {completion, sessionId, completionPath}) {
  if (!tools || typeof tools.mcp__node_repl__js !== 'function'
      || typeof completionPath !== 'string' || !completionPath.startsWith('/'))
    throw new Error('Saved completion recovery requires Node REPL access and an absolute path');
  const initial = completionDiagnostics(completion);
  let final = initial, currentSession = sessionId || completion?.session_id;
  const unresolved = (detail, fields) => Object.assign(new Error('Original completion unresolved; recover the saved completion at '
    + completionPath + ' without rerunning completion or its provider: ' + detail),
    {completion_diagnostics: {initial, final}, ...fields});
  while (currentSession) {
    // A running owned session stays pending; it is never classified lost.
    if (typeof tools.write_stdin !== 'function')
      throw unresolved('original completion session is active but write_stdin is unavailable', {completion_state: 'running'});
    const completed = await tools.write_stdin({session_id: currentSession,
      yield_time_ms: 30000, max_output_tokens: 6000});
    final = completionDiagnostics(completed);
    currentSession = completed?.session_id;
  }
  // Only an exposed integer exit code proves the command ended; otherwise unknown.
  const ended = Number.isInteger(final.exit_code) ? 'terminated' : 'unknown';
  const code = `await (async () => {
    const fs = await import('node:fs/promises');
    let text;
    try { text = await fs.readFile(${JSON.stringify(completionPath)}, 'utf8'); }
    catch (error) { nodeRepl.write(JSON.stringify({ok:false, missing:error?.code === 'ENOENT', error:String(error?.message || error)})); return; }
    try { nodeRepl.write(JSON.stringify({ok:true, value:JSON.parse(text)})); }
    catch (error) { nodeRepl.write(JSON.stringify({ok:false, missing:false, error:String(error?.message || error)})); }
  })();`;
  let envelope;
  try {
    const saved = await tools.mcp__node_repl__js({code, title: 'Recover saved native completion', timeout_ms: 30000});
    if (saved?.isError) throw new Error('Node REPL returned an error');
    envelope = JSON.parse(saved?.content?.find(block => block.type === 'text')?.text);
  } catch (error) {
    throw unresolved(String(error?.message || error), {completion_state: ended});
  }
  if (!envelope?.ok)
    throw unresolved(envelope?.error || 'saved completion unavailable',
      {completion_state: ended, saved_step: envelope?.missing ? 'missing' : 'invalid'});
  try { canonicalProductionStep(envelope.value); }
  catch (error) { throw unresolved(error.message, {completion_state: ended, saved_step: 'invalid'}); }
  // The saved step stays exact; the exec evidence travels beside it.
  return {...envelope.value, completion_diagnostics: {initial, final}};
}
if (typeof module !== 'undefined') module.exports = {
  saveNativeHostReceipt, nativeImageURLSha256, nativeHostReceiptBinding, consumeNativeHostAction,
  consumeSavedNativeCompletion, canonicalProductionStep,
};
