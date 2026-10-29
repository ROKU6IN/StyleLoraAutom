import { readdir, readFile } from 'node:fs/promises';
import { resolve, relative } from 'node:path';
const root = resolve('src');
const failures = [];
async function walk(path) {
  for (const item of await readdir(path, { withFileTypes: true })) {
    const full = resolve(path, item.name);
    if (item.isDirectory()) {
      await walk(full);
      continue;
    }
    if (!/\.(tsx?|css)$/.test(item.name) || item.name === 'schema.d.ts') continue;
    const name = relative(root, full).replaceAll('\\', '/'),
      text = await readFile(full, 'utf8');
    if (!name.startsWith('tokens/') && !name.startsWith('styles/tokens/')) {
      if (/#[0-9a-fA-F]{3,8}\b/.test(text)) failures.push(`${name}: hard-coded color`);
      if (/\b\d+(?:\.\d+)?px\b/.test(text)) failures.push(`${name}: hard-coded pixel geometry`);
    }
    if (/<svg\b/.test(text)) failures.push(`${name}: inline SVG`);
    if (name.startsWith('ui/') && /from\s+['"][^'"]*(?:features|api)\//.test(text))
      failures.push(`${name}: UI imports business modules`);
    if (/(?:localStorage|sessionStorage)\.(?:setItem|\w+\s*=)/.test(text))
      failures.push(`${name}: direct persisted storage`);
  }
}
await walk(root);
if (failures.length) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else console.log('Design boundaries: tokens, SVG files, UI imports and storage passed.');
