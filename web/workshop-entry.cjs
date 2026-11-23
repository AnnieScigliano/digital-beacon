const LAB_PATH = '/taller-oliva/living-lab';

function rejectLab(req, res, status) {
  const message = status === 503
    ? 'No pudimos verificar el acceso a Rostro.'
    : 'Entrá con la cuenta autorizada para abrir Rostro.';
  const body = Buffer.from(JSON.stringify({ error: message }));
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'private, no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  res.end(req.method === 'HEAD' ? undefined : body);
}

async function createServer(root = __dirname, {
  privateLabAuth,
  labOrigin = process.env.PAIR_LAB_ORIGIN,
  labHumanRef = process.env.PAIR_LAB_HUMAN_REF,
} = {}) {
  const { createServer: base } = require('./workshop-entry.cjs');
  const { createPairHandler } = await import('./pair-mode/home-handler.mjs');
  let labAuth = privateLabAuth;
  if (!labAuth && labOrigin && labHumanRef) {
    const { createHomeAuth } = await import('./home-auth.mjs');
    labAuth = createHomeAuth({ origin: labOrigin, humanRef: labHumanRef });
  }

  const server = base(root);
  const previous = server.listeners('request');
  const pair = createPairHandler(root);
  server.removeAllListeners('request');

  function dispatch(req, res) {
    if (!pair(req, res)) {
      for (const listener of previous) listener.call(server, req, res);
    }
  }

  server.on('request', async (req, res) => {
    let pathname;
    try {
      pathname = new URL(req.url, 'http://localhost').pathname;
    } catch {
      return dispatch(req, res);
    }
    if (pathname !== LAB_PATH && !pathname.startsWith(LAB_PATH + '/')) {
      return dispatch(req, res);
    }
    if (!labAuth) return rejectLab(req, res, 503);
    try {
      await labAuth.authenticate(req);
    } catch (error) {
      const status = [401, 403, 503].includes(error?.status) ? error.status : 503;
      return rejectLab(req, res, status);
    }
    dispatch(req, res);
  });
  return server;
}

module.exports = { createServer };
if (require.main === module) {
  createServer().then(server => {
    server.listen(Number(process.env.PORT), process.env.HOST, () => {
      process.stdout.write('Workshop with Pair Mode listening\n');
    });
  }).catch(error => {
    process.stderr.write(error.message + '\n');
    process.exitCode = 1;
  });
}
