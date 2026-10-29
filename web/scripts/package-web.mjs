import { cp, mkdir, readdir, rm } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const web = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const target = resolve(web, '../src/stylelora/api/web_dist');
// Only replace the exact generated package resource directory, never user data.
if (!target.endsWith('/api/web_dist') && !target.endsWith('\\api\\web_dist'))
  throw new Error('Invalid build target');
await mkdir(target, { recursive: true });
for (const entry of await readdir(target)) await rm(resolve(target, entry), { recursive: true, force: true });
await cp(resolve(web, 'dist'), target, { recursive: true });
console.log('Packaged frontend in stylelora.api/web_dist');
