/**
 * LIVE two-user concurrent verification test against the NEW AgentProof
 * contract (steward requirement: "each user verifies and displays only
 * their own record").
 *
 * Wallet A submits Agent A and Wallet B submits Agent B as concurrent
 * in-flight requests. B is verified FIRST (reversed finalization), then
 * A. Then:
 *   - each wallet identifies ITS record id via its OWN user-scoped
 *     listing get_my_verifications (never the global count)
 *   - each wallet reads its OWN record via get_my_verification(exact id)
 *   - each wallet reading the OTHER'S id gets not_authorized, no leaks
 *
 * Robust to slow Studionet finalization (12-min waits, status log).
 * Keys are kept in /tmp for this run only; the record ids are
 * cross-checked against the receipts when available.
 * Run: node tests/frontend/live_two_user_concurrent.mjs
 */
import { createRequire } from 'node:module';
import { writeFileSync, readFileSync, existsSync } from 'node:fs';
const __require = createRequire(import.meta.url);
globalThis.window = globalThis;
__require('/home/ubuntu/agentproof/frontend/genlayer-sdk.bundle.js');
const SDK = window.GenLayerSDK;

const ADDR_FILE = '/home/ubuntu/agentproof/frontend/contract-address.js';
const KEYFILE = '/tmp/agentproof_live_keys.json';

let failures = 0;
function assert(cond, label, detail){
  const ok = Boolean(cond);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
}

function txReturnValue(receipt){
  try {
    const lead = Array.isArray(receipt?.consensus_data?.leader_receipt)
      ? receipt.consensus_data.leader_receipt[0]
      : receipt?.consensus_data?.leader_receipt;
    const payload = lead?.result?.payload;
    if (payload && typeof payload.readable === 'string'){
      const n = Number(payload.readable);
      if (Number.isFinite(n)) return n;
    }
  } catch {}
  return null;
}

const STATUS_NAMES = {0:'UNINITIALIZED',1:'PENDING',2:'PROPOSING',3:'COMMITTING',4:'REVEALING',
  5:'ACCEPTED',6:'UNDETERMINED',7:'FINALIZED',8:'CANCELED',9:'APPEAL_REVEALING',
  10:'APPEAL_COMMITTING',11:'READY_TO_FINALIZE',12:'VALIDATORS_TIMEOUT',13:'LEADER_TIMEOUT'};

function txStatusName(tx){
  let s = tx && tx.status;
  if (s === undefined || s === null) return '';
  s = String(s);
  if (/^\d+$/.test(s)) s = STATUS_NAMES[Number(s)] || s;
  return s;
}

async function waitForFinal(client, hash, label, minutes = 12){
  const deadline = Date.now() + minutes * 60 * 1000;
  let last = '';
  while (Date.now() < deadline){
    try {
      const tx = await client.getTransaction({ hash });
      if (tx){
        last = txStatusName(tx);
        if (last === 'FINALIZED') return tx;
        if (['UNDETERMINED', 'CANCELED', 'LEADER_TIMEOUT', 'VALIDATORS_TIMEOUT'].includes(last)){
          throw new Error(`${label} terminal status ${last}`);
        }
      }
    } catch (e){ if (/terminal status/.test(String(e.message))) throw e; }
    await new Promise(r => setTimeout(r, 6000));
  }
  throw new Error(`${label} not FINALIZED in ${minutes}m (last=${last})`);
}

async function main(){
  const src = readFileSync(ADDR_FILE, 'utf8');
  const m = src.match(/"0x[0-9a-fA-F]{40}"/);
  if (!m) throw new Error('no contract address in contract-address.js');
  const CONTRACT = m[0].slice(1, -1);
  console.log('contract:', CONTRACT);

  // Reuse persisted keys when a prior run created records but died
  // mid-scenario (avoids orphaning PENDING records; the previous run's
  // records #6/#7 are completed by THIS run when keys persist).
  let walletA, walletB;
  if (existsSync(KEYFILE)){
    ({ walletA, walletB } = JSON.parse(readFileSync(KEYFILE, 'utf8')));
    walletA = SDK.createAccount(walletA);
    walletB = SDK.createAccount(walletB);
    console.log('reusing persisted wallets:', walletA.address, walletB.address);
  } else {
    walletA = SDK.createAccount(SDK.generatePrivateKey());
    walletB = SDK.createAccount(SDK.generatePrivateKey());
    // viem LocalAccount keeps the raw private key in `.source`
    const pkOf = a => String(a.source).startsWith('0x')
      ? String(a.source) : '0x' + String(a.source);
    writeFileSync(KEYFILE, JSON.stringify({
      walletA: pkOf(walletA), walletB: pkOf(walletB),
    }));
  }
  const clientA = SDK.createClient({ chain: SDK.studionet, account: walletA });
  const clientB = SDK.createClient({ chain: SDK.studionet, account: walletB });
  const client = SDK.createClient({ chain: SDK.studionet });

  // 1) identify each wallet's records via the user-scoped listing
  //    (NEVER the global count). If empty -> submit new concurrent pair.
  let listA = JSON.parse(await clientA.readContract({
    address: CONTRACT, functionName: 'get_my_verifications', args: [50, 0] }));
  let listB = JSON.parse(await clientB.readContract({
    address: CONTRACT, functionName: 'get_my_verifications', args: [50, 0] }));

  if (listA.total === 0 || listB.total === 0){
    for (const [clientX, addr] of [[clientA, walletA.address], [clientB, walletB.address]]){
      try { await clientX.request({ method: 'sim_fundAccount', params: [addr, 5000000000000000000] }); }
      catch (e){ console.log('fund note:', String(e).slice(0, 80)); }
    }
    console.log('submitting concurrent requests…');
    const hashAP = clientA.writeContract({
      address: CONTRACT, functionName: 'request_verification', account: walletA,
      args: ['Concurrent Agent A', 'https://docs.tavily.com/documentation/api-reference/endpoint/search.md', '', JSON.stringify([{"id":"API_ACCESS"}]), ''],
    });
    const hashBP = clientB.writeContract({
      address: CONTRACT, functionName: 'request_verification', account: walletB,
      args: ['Concurrent Agent B', 'https://example.com/', '', JSON.stringify([{"id":"MCP_SUPPORT"}]), ''],
    });
    const [hashA, hashB] = await Promise.all([hashAP, hashBP]);
    console.log('A request tx:', String(hashA).slice(0, 20) + '…', ' B request tx:', String(hashB).slice(0, 20) + '…');
    // waits run in parallel; each wallet's OWN receipt gives its OWN id
    const [txA, txB] = await Promise.all([
      waitForFinal(clientA, hashA, 'A request'),
      waitForFinal(clientB, hashB, 'B request'),
    ]);
    const vidA = txReturnValue(txA);
    const vidB = txReturnValue(txB);
    assert(Number.isInteger(vidA), 'A receives exact integer id from its own receipt', `vidA=${vidA}`);
    assert(Number.isInteger(vidB), 'B receives exact integer id from its own receipt', `vidB=${vidB}`);
    assert(vidA !== vidB, 'ids are distinct', `${vidA} vs ${vidB}`);
    listA = JSON.parse(await clientA.readContract({ address: CONTRACT, functionName: 'get_my_verifications', args: [50, 0] }));
    listB = JSON.parse(await clientB.readContract({ address: CONTRACT, functionName: 'get_my_verifications', args: [50, 0] }));
    assert(listA.verifications.some(v => String(v.verification_id) === String(vidA)),
      'A\'s user-scoped listing contains its exact id');
    assert(listB.verifications.some(v => String(v.verification_id) === String(vidB)),
      'B\'s user-scoped listing contains its exact id');
  }

  // 2) pick each wallet's record (agent name distinguishes A vs B)
  const recAmeta = listA.verifications.find(v => v.agent_name === 'Concurrent Agent A');
  const recBmeta = listB.verifications.find(v => v.agent_name === 'Concurrent Agent B');
  if (!recAmeta || !recBmeta) throw new Error('could not find both concurrent records');
  const vidA = Number(recAmeta.verification_id);
  const vidB = Number(recBmeta.verification_id);
  console.log(`A id=${vidA} B id=${vidB}`);

  // 3) REVERSED finalization: B verifies FIRST, then A
  if (recBmeta.state !== 'SEALED'){
    const vB = await clientB.writeContract({
      address: CONTRACT, functionName: 'verify_agent', account: walletB, args: [vidB] });
    await waitForFinal(clientB, vB, 'B verify');
    console.log('B sealed first (reversed order)');
  }
  if (recAmeta.state !== 'SEALED'){
    const vA = await clientA.writeContract({
      address: CONTRACT, functionName: 'verify_agent', account: walletA, args: [vidA] });
    await waitForFinal(clientA, vA, 'A verify');
    console.log('A sealed second');
  }

  // 4) each wallet reads its OWN sealed record via the user-scoped read
  const mineA = JSON.parse(await clientA.readContract({ address: CONTRACT, functionName: 'get_my_verification', args: [vidA] }));
  const mineB = JSON.parse(await clientB.readContract({ address: CONTRACT, functionName: 'get_my_verification', args: [vidB] }));
  assert(!mineA.error && mineA.state === 'SEALED' && String(mineA.verification_id) === String(vidA),
    'A reads its OWN sealed record by exact id');
  assert(!mineB.error && mineB.state === 'SEALED' && String(mineB.verification_id) === String(vidB),
    'B reads its OWN sealed record by exact id');
  assert(mineA.agent_name === 'Concurrent Agent A', "A's record is Agent A", mineA.agent_name);
  assert(mineB.agent_name === 'Concurrent Agent B', "B's record is Agent B", mineB.agent_name);

  // 5) CROSS-USER reads denied, no data leak
  const deniedA = JSON.parse(await clientA.readContract({ address: CONTRACT, functionName: 'get_my_verification', args: [vidB] }));
  const deniedB = JSON.parse(await clientB.readContract({ address: CONTRACT, functionName: 'get_my_verification', args: [vidA] }));
  assert(deniedA.error === 'not_authorized', 'A cannot read B\'s record', JSON.stringify(deniedA));
  assert(deniedB.error === 'not_authorized', 'B cannot read A\'s record', JSON.stringify(deniedB));
  assert(!('agent_url' in deniedA) && !('evidence_sources' in deniedA) && !('capability_details' in deniedA),
    'denial response leaks no record data');

  // 6) per-user listing isolation (post-seal)
  const listA2 = JSON.parse(await clientA.readContract({ address: CONTRACT, functionName: 'get_my_verifications', args: [50, 0] }));
  const listB2 = JSON.parse(await clientB.readContract({ address: CONTRACT, functionName: 'get_my_verifications', args: [50, 0] }));
  assert(listA2.verifications.every(v => String(v.verification_id) === String(vidA)),
    'A\'s listing contains ONLY A\'s record', JSON.stringify(listA2.verifications.map(v=>v.verification_id)));
  assert(listB2.verifications.every(v => String(v.verification_id) === String(vidB)),
    'B\'s listing contains ONLY B\'s record', JSON.stringify(listB2.verifications.map(v=>v.verification_id)));

  console.log(failures === 0 ? '\nALL_OK: live concurrent two-user isolation verified' : `\nFAILED: ${failures}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch(e => { console.error('ERROR', e); process.exit(1); });
