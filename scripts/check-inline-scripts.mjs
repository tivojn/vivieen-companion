import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const root = path.resolve(import.meta.dirname, '..');
const pages = ['web/index.html', 'web/settings.html'];
let checked = 0;

for (const relative of pages) {
  const html = fs.readFileSync(path.join(root, relative), 'utf8');
  const scripts = html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/gi);
  for (const match of scripts) {
    if (/\bsrc\s*=/.test(match[1])) continue;
    new Function(match[2]);
    checked += 1;
  }
}

if (!checked) {
  console.error('No inline scripts found to validate.');
  process.exit(1);
}
console.log(`Validated ${checked} browser script block${checked === 1 ? '' : 's'}.`);
