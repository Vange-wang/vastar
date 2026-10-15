// libuv <=1.52.1 creates spawn pipes outside AppContainer's LOCAL namespace.
// Pre-open container-scoped pipes and inherit fds; no token/Job/capability changes.
// Upstream diagnosis and fix: https://github.com/libuv/libuv/pull/5181
const net = require('net');
const fs = require('fs');
const crypto = require('crypto');
const childProcess = require('child_process');

async function pair() {
  const server = net.createServer();
  const name = '\\\\?\\pipe\\LOCAL\\vastar-' + crypto.randomUUID();
  const accepted = new Promise((resolve, reject) => {
    server.once('connection', resolve); server.once('error', reject);
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject); server.listen(name, resolve);
  });
  const fd = fs.openSync(name, 'r+');
  const socket = await accepted;
  return { server, fd, socket };
}

async function install(engine) {
  const pipes = [];
  try {
    for (let i = 0; i < 4; i++) pipes.push(await pair());
  } catch (error) {
    for (const p of pipes) { p.socket.destroy(); p.server.close(); fs.closeSync(p.fd); }
    throw error;
  }
  const original = childProcess.spawn;
  let used = false;
  childProcess.spawn = (executable, args, options) => {
    if (used || executable !== engine) throw new Error('unexpected_child_process');
    used = true;
    const child = original(executable, args, { ...options, stdio: ['ignore', ...pipes.map(p => p.fd)] });
    child.stdio = [null, ...pipes.map(p => p.socket)];
    child.stdout = pipes[0].socket;
    child.stderr = pipes[1].socket;
    for (const p of pipes) fs.closeSync(p.fd);
    child.once('exit', () => { for (const p of pipes) { p.socket.destroy(); p.server.close(); } });
    return child;
  };
}

module.exports = { install };
