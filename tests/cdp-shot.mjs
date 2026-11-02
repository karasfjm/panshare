/* 极简 CDP 截图 + 交互脚本（用 Node 内置 WebSocket，无需依赖） */
import fs from 'node:fs';

const PORT = process.env.CDP_PORT || '9333';
const URL_ = process.argv[2];
const OUT = process.argv[3];
const SCRIPT = process.argv[4] || '';

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  let page = list.find((t) => t.type === 'page');
  if (!page) {
    page = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json();
  }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const events = [];
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) p.rej(new Error(JSON.stringify(msg.error)));
      else p.res(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Network.enable');
  // 关掉磁盘缓存：改完前端要立刻看到最新的 JS
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await send('Emulation.setDeviceMetricsOverride',
    { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });

  if (URL_ && URL_ !== '-') {
    await send('Page.navigate', { url: URL_ });
    await new Promise((r) => setTimeout(r, 2500));
  }

  if (SCRIPT) {
    const res = await send('Runtime.evaluate', {
      expression: SCRIPT, awaitPromise: true, returnByValue: true,
    });
    if (res.exceptionDetails) {
      console.log('SCRIPT ERROR:', JSON.stringify(res.exceptionDetails).slice(0, 800));
    } else {
      console.log('SCRIPT RESULT:', JSON.stringify(res.result?.value));
    }
    await new Promise((r) => setTimeout(r, 1200));
  }

  // OUT 传 '-' 表示只跑脚本、不截图（否则会生成一个名为 "-" 的垃圾文件）
  if (OUT && OUT !== '-') {
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    console.log('saved:', OUT);
  }

  // 控制台错误
  const errs = events.filter((e) => e.method === 'Log.entryAdded' && e.params.entry.level === 'error');
  if (errs.length) {
    console.log('CONSOLE ERRORS:');
    errs.slice(0, 10).forEach((e) => console.log('  ', e.params.entry.text.slice(0, 300)));
  }
  const excs = events.filter((e) => e.method === 'Runtime.exceptionThrown');
  if (excs.length) {
    console.log('PAGE EXCEPTIONS:');
    excs.slice(0, 10).forEach((e) => console.log('  ', (e.params.exceptionDetails?.exception?.description || '').slice(0, 300)));
  }
  ws.close();
}
main().catch((e) => { console.error('ERR', e.message); process.exit(1); });
