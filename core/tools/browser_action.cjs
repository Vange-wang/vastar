const { version } = require('./browser_capture.cjs');
const { resolve, secrets } = require('./browser_targets.cjs');
const values = new Map();

async function verify(page, command) {
  if (new URL(page.url()).origin !== command.origin) return { verified: false, kind: 'origin_changed' };
  if (page.url() !== command.target.document_url) return { verified: false, kind: 'document_changed' };
  if (command.action === 'browser_fill') {
    const value = values.get(command.operation_id);
    if (value === undefined) return { verified: false, kind: 'input_state_unavailable' };
    const target = await resolve(page, command.target);
    return { verified: await target.inputValue() === value, kind: 'field_value' };
  }
  const element = page.getByTestId(command.verify.testid);
  const verified = await element.count() === 1 && await element.isVisible()
    && (await element.innerText()).trim() === command.verify.text;
  return { verified, kind: 'visible_business_state' };
}

async function check(page, command) {
  if (new URL(page.url()).origin !== command.origin) throw new Error('origin_changed');
  if (await version(page) !== command.page_revision) throw new Error('stale_page_revision');
  return resolve(page, command.target);
}

async function action(page, command, emit, approval) {
  await check(page, command);
  if (command.action === 'browser_click' && (await verify(page, command)).verified) throw new Error('business_state_preexisting');
  emit({ type: 'commit_request', id: command.id });
  const granted = await approval();
  if (!granted.approved) throw new Error('authorization_revoked');
  const match = await check(page, command);
  // Resolve a fresh handle just for this action, so Playwright cannot retarget a replacement node.
  const handle = await match.elementHandle();
  if (!handle) throw new Error('target_changed');
  try {
    if (command.action === 'browser_fill') {
      if (values.size >= 64) throw new Error('action_limit');
      values.set(command.operation_id, command.value);
      secrets.add(command.value);
      await handle.fill(command.value, { timeout: 1500 });
    } else await handle.click({ timeout: 1500, noWaitAfter: true });
  } finally { await handle.dispose(); }
  for (let i = 0; i < 40; i++) {
    const proof = await verify(page, command);
    if (proof.verified) { emit({ type: 'action_proof', id: command.id, proof }); return proof; }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return { verified: false, kind: 'business_state_unconfirmed' };
}

module.exports = { action, verify };
